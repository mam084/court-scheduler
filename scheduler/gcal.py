"""Google Calendar sync: pushes a published schedule to the club's calendars.

Calendars (the Calendars sheet, or the Publish tab):
  - one per team: that team's practices and tournaments
  - one per club side (e.g. SDVBC, Seaside): every practice and tournament
  - one per locals program: its sessions (and a local tournament it runs)

How a sync works
  1. desired_events(): every event the published schedule should have.
  2. plan(): reads what's on each calendar now (only events this app made,
     marked with a private tag) and works out what to create, update and
     delete. Reading changes nothing, so this is the preview.
  3. apply(): makes those changes.

Events anyone made by hand never carry the tag, so a sync never touches them.
Test mode sends everything to one test calendar instead, with the calendar
it would have gone to in front of each title.

Google access is a service account (a robot Google account): share each
calendar with its address ("Make changes to events") and put its key in
the GOOGLE_SERVICE_ACCOUNT secret. See docs/SETUP_CALENDAR.md.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from .config import SLOT_TIMES, SLOTS, court_name, is_boys_club

TZ = "America/Los_Angeles"
TAG = "court_scheduler"
KINDS = ["Team", "Club", "Program"]


@dataclass
class Event:
    """One event for a Google Calendar, with a stable key so a later sync updates it instead of adding a copy."""
    calendar_id: str
    target: str          # "Team: 16 Black", "Club: SDVBC", "Program: G HS L"
    key: str             # stable identity: target + what + when
    summary: str
    description: str
    start: date | datetime
    end: date | datetime
    all_day: bool = False
    location: str = ""

    @property
    def event_id(self) -> str:
        # Google event ids: lowercase a-v and 0-9. md5 hex fits.
        return "cs" + hashlib.md5(f"{self.calendar_id}|{self.key}".encode()).hexdigest()

    @property
    def fingerprint(self) -> str:
        return hashlib.md5("|".join(map(str, (self.summary, self.description, self.location, self.start,
                                              self.end, self.all_day))).encode()).hexdigest()[:16]

    def body(self) -> dict:
        def when(v):
            return {"date": v.isoformat()} if self.all_day else {"dateTime": v.isoformat(), "timeZone": TZ}
        return {
            "id": self.event_id, "summary": self.summary, "description": self.description,
            "location": self.location, "start": when(self.start), "end": when(self.end),
            "status": "confirmed",
            "extendedProperties": {"private": {TAG: "1", "cs_key": self.key, "cs_hash": self.fingerprint}},
        }


# ------------------------------------------------------------ mapping --

def links_from(df: pd.DataFrame | None) -> dict[tuple[str, str], str]:
    """Calendars sheet -> {(kind, name): calendar id}. Blank ids are skipped."""
    out = {}
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return out
    for _, r in df.fillna("").iterrows():
        kind, name, cid = str(r.get("kind", "")).strip().title(), str(r.get("name", "")).strip(), \
            str(r.get("calendar_id", "")).strip()
        if kind in KINDS and name and cid:
            out[kind, name] = cid
    return out


def link_rows(teams, clubs, programs, current: pd.DataFrame | None) -> pd.DataFrame:
    """One row per team, club and program (plus any linked before), with
    the calendar id filled in where it's known."""
    known = links_from(current)
    rows = [("Club", c) for c in clubs] + [("Team", t) for t in teams] + [("Program", p) for p in programs]
    rows += [k for k in known if k not in rows]
    return pd.DataFrame([{"kind": k, "name": n, "calendar_id": known.get((k, n), "")} for k, n in rows],
                        columns=["kind", "name", "calendar_id"])


# ------------------------------------------------------- what to send --

def _dt(d: date, hm: tuple[int, int]) -> datetime:
    return datetime(d.year, d.month, d.day, hm[0], hm[1])


def _courts(courts) -> str:
    nums = sorted({int(c) for c in courts if pd.notna(c)})
    if len(nums) == 1:
        return court_name(nums[0])
    from .views import courts_label
    return "Courts " + courts_label(nums)


def desired_events(practices: pd.DataFrame, local_rows: pd.DataFrame, tournaments: list, teams: list,
                   links: dict, start: date, end: date, test_calendar: str = "", location: str = "",
                   programs_side: dict | None = None) -> tuple[list[Event], list[str]]:
    """Every event the schedule should put on the linked calendars, and a
    note for each team / club / program that has events but no calendar."""
    out: list[Event] = []
    missing: set[str] = set()
    club_of = {t.name: t.club for t in teams}
    programs_side = programs_side or {}

    def put(kind, name, key, summary, desc, s, e, all_day=False):
        cid = links.get((kind, name))
        if not cid:
            missing.add(f"{kind}: {name}")
            return
        target = f"{kind}: {name}"
        if test_calendar:
            cid, summary = test_calendar, f"[{target}] {summary}"
        out.append(Event(cid, target, f"{target}|{key}", summary, desc, s, e, all_day, location))

    # Practices: the team's calendar and its club's calendar.
    if practices is not None and len(practices):
        for r in practices.itertuples():
            if not (start <= r.date <= end) or r.slot not in SLOT_TIMES:
                continue
            (s0, s1) = SLOT_TIMES[r.slot]
            ct = court_name(int(r.court)) if pd.notna(r.court) else "court TBA"
            coaches = ", ".join(c.strip() for c in str(getattr(r, "coach", "") or "").split(";") if c.strip())
            desc = f"{SLOTS[r.slot]} · {ct}" + (f"\nCoaches: {coaches}" if coaches else "")
            paired = getattr(r, "paired", "")
            if isinstance(paired, str) and paired:
                desc += f"\nShared court: {paired}"
            key = f"practice|{r.date}|{r.slot}|{r.name}"
            put("Team", r.name, key, f"{r.name} practice · {ct}", desc, _dt(r.date, s0), _dt(r.date, s1))
            club = getattr(r, "club", "") or club_of.get(r.name, "")
            if club:
                put("Club", club, key, f"{r.name} practice · {ct}", desc, _dt(r.date, s0), _dt(r.date, s1))

    # Locals programs: one event per session, listing its courts.
    if local_rows is not None and len(local_rows):
        lr = local_rows[(local_rows["date"] >= start) & (local_rows["date"] <= end)]
        for (d, slot, name), g in lr.groupby(["date", "slot", "name"]):
            if slot not in SLOT_TIMES:
                continue
            (s0, s1) = SLOT_TIMES[slot]
            ct = _courts(g["court"])
            coaches = ", ".join(c.strip() for c in str(g["coach"].iloc[0] or "").split(";") if c.strip())
            put("Program", name, f"program|{d}|{slot}|{name}", f"{name} · {ct}",
                f"{SLOTS[slot]} · {ct}" + (f"\nCoaches: {coaches}" if coaches else ""), _dt(d, s0), _dt(d, s1))

    # Tournaments: all-day events.
    clubs = sorted({t.club for t in teams if t.club})
    for tn in tournaments or []:
        if tn.end < start or tn.start > end:
            continue
        s, e = tn.start, tn.end + timedelta(days=1)   # all-day end is exclusive
        key = f"tournament|{tn.start}|{tn.name}"
        if tn.kind == "travel":
            title = f"{tn.name} (travel tournament)"
            desc = "Teams: " + ", ".join(tn.teams) + "\nNo practice for these teams on tournament days."
            for team in tn.teams:
                put("Team", team, key, title, desc, s, e, True)
            for club in sorted({club_of.get(n, "") for n in tn.teams} - {""}):
                put("Club", club, key, title, desc, s, e, True)
        else:
            title = f"{tn.name} (local tournament, until 7 PM)"
            desc = f"Hosted here until 7 PM on {_courts(tn.courts)}." + (
                f"\nWorking it: {', '.join(tn.coaches)}" if tn.coaches else "")
            side = programs_side.get(getattr(tn, "program", ""), "")
            for club in clubs:
                if not side or (side == "Boys") == is_boys_club(club):
                    put("Club", club, key, title, desc, s, e, True)
            if getattr(tn, "program", ""):
                put("Program", tn.program, key, title, desc, s, e, True)
    return out, sorted(missing)


# ------------------------------------------------------- Google access --

def service_account_info(secrets) -> dict | None:
    """The service account key from secrets: a [GOOGLE_SERVICE_ACCOUNT]
    table (the key file's fields) or the key file's JSON as a string."""
    try:
        raw = secrets.get("GOOGLE_SERVICE_ACCOUNT")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return {k: raw[k] for k in raw}


class CalendarClient:
    """Minimal Google Calendar API client (REST, as the service account)."""
    BASE = "https://www.googleapis.com/calendar/v3"

    def __init__(self, info: dict, session=None):
        if session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/calendar"])
            session = AuthorizedSession(creds)
        self.s = session
        self.robot = info.get("client_email", "")

    def _url(self, cid: str, *parts: str) -> str:
        return "/".join([self.BASE, "calendars", urllib.parse.quote(cid, safe="")] + list(parts))

    def _call(self, method: str, url: str, **kw):
        for attempt in range(5):
            r = self.s.request(method, url, timeout=30, **kw)
            if r.status_code in (429, 500, 503) or (r.status_code == 403 and "ateLimit" in r.text):
                time.sleep(1.5 * (attempt + 1))   # Google asks to slow down: back off and retry
                continue
            return r
        return r

    def calendar_name(self, cid: str) -> str:
        r = self._call("GET", self._url(cid))
        if r.status_code == 404:
            raise LookupError("not found, or not shared with the app yet")
        if r.status_code == 403:
            raise PermissionError("shared, but without permission to make changes")
        r.raise_for_status()
        return r.json().get("summary", cid)

    def app_events(self, cid: str, start: date, end: date) -> list[dict]:
        """Events this app made on a calendar, overlapping start..end."""
        out, token = [], None
        from zoneinfo import ZoneInfo
        z = ZoneInfo(TZ)
        params = {"privateExtendedProperty": f"{TAG}=1", "singleEvents": "true", "maxResults": 2500,
                  "timeMin": datetime(start.year, start.month, start.day, tzinfo=z).isoformat(),
                  "timeMax": (datetime(end.year, end.month, end.day, tzinfo=z) + timedelta(days=1)).isoformat()}
        while True:
            if token:
                params["pageToken"] = token
            r = self._call("GET", self._url(cid, "events"), params=params)
            if r.status_code in (403, 404):
                raise PermissionError(f"can't read calendar {cid} (is it shared with {self.robot}?)")
            r.raise_for_status()
            data = r.json()
            out += [e for e in data.get("items", []) if e.get("status") != "cancelled"]
            token = data.get("nextPageToken")
            if not token:
                return out

    def create(self, cid: str, body: dict):
        r = self._call("POST", self._url(cid, "events"), json=body)
        if r.status_code == 409:   # the id was used before (an event since deleted): bring it back
            r = self._call("PUT", self._url(cid, "events", body["id"]), json=body)
        r.raise_for_status()

    def update(self, cid: str, event_id: str, body: dict):
        r = self._call("PUT", self._url(cid, "events", event_id), json=body)
        r.raise_for_status()

    def delete(self, cid: str, event_id: str):
        r = self._call("DELETE", self._url(cid, "events", event_id))
        if r.status_code not in (200, 204, 404, 410):
            r.raise_for_status()


# ----------------------------------------------------------- the sync --

@dataclass
class Plan:
    """What a sync would do: events to create, update and delete, and how many are already right."""
    create: list = field(default_factory=list)    # [Event]
    update: list = field(default_factory=list)    # [Event]
    delete: list = field(default_factory=list)    # [(calendar_id, event id, summary, start)]
    unchanged: int = 0
    errors: list = field(default_factory=list)    # [str]: calendars that couldn't be read

    @property
    def n_changes(self) -> int:
        return len(self.create) + len(self.update) + len(self.delete)

    def table(self, names: dict | None = None) -> pd.DataFrame:
        names = names or {}
        rows = []
        for what, evs in (("Add", self.create), ("Update", self.update)):
            for e in evs:
                rows.append({"change": what, "calendar": names.get(e.calendar_id, e.target),
                             "when": _when(e.start, e.all_day), "event": e.summary})
        for cid, _id, summary, when in self.delete:
            rows.append({"change": "Remove", "calendar": names.get(cid, cid), "when": when, "event": summary})
        return pd.DataFrame(rows, columns=["change", "calendar", "when", "event"])


def _when(v, all_day=False) -> str:
    return f"{v:%a %b %-d}" if all_day or not isinstance(v, datetime) else f"{v:%a %b %-d, %-I:%M %p}"


def plan(events: list[Event], client: CalendarClient, start: date, end: date,
         calendars: list[str] | None = None, leave: tuple[str, ...] = ()) -> Plan:
    """What a sync would change. Only reads the calendars.
    calendars: every calendar the sync covers (so a calendar whose events
    all went away gets them removed), defaulting to those with events.
    leave: kinds of the app's own events this sync leaves alone, e.g. ("program", "tournament")
    when those aren't being sent (they're never removed then)."""
    p = Plan()
    by_cal = defaultdict(list)
    for e in events:
        by_cal[e.calendar_id].append(e)
    for cid in sorted(set(by_cal) | set(calendars or [])):
        try:
            have = {ev["id"]: ev for ev in client.app_events(cid, start, end)}
        except Exception as ex:  # noqa: BLE001
            p.errors.append(f"{cid}: {ex}")
            continue
        want = {e.event_id: e for e in by_cal[cid]}
        for eid, e in want.items():
            if eid not in have:
                p.create.append(e)
            elif have[eid].get("extendedProperties", {}).get("private", {}).get("cs_hash") != e.fingerprint:
                p.update.append(e)
            else:
                p.unchanged += 1
        for eid, ev in have.items():
            key = ev.get("extendedProperties", {}).get("private", {}).get("cs_key", "")
            kind = key.split("|")[1] if key.count("|") >= 1 else ""  # 'Team: 16 Black|practice|...'
            if eid not in want and not (leave and kind in leave):
                st_ = ev.get("start", {})
                p.delete.append((cid, eid, ev.get("summary", ""),
                                 st_.get("date") or str(st_.get("dateTime", ""))[:16].replace("T", " ")))
    return p


def apply(p: Plan, client: CalendarClient, progress=None) -> tuple[int, list[str]]:
    """Makes the planned changes. Returns (changes made, errors)."""
    done, errors = 0, []
    total = max(1, p.n_changes)
    steps = [("create", e) for e in p.create] + [("update", e) for e in p.update] + [("delete", d) for d in p.delete]
    for i, (what, item) in enumerate(steps):
        try:
            if what == "create":
                client.create(item.calendar_id, item.body())
            elif what == "update":
                client.update(item.calendar_id, item.event_id, item.body())
            else:
                client.delete(item[0], item[1])
            done += 1
        except Exception as ex:  # noqa: BLE001
            label = item.summary if what != "delete" else item[2]
            errors.append(f"{what} {label}: {ex}")
        if progress:
            progress((i + 1) / total)
    return done, errors
