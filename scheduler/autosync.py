"""Keeping the club's Google Calendars up to date without anyone pressing Sync.

Turned on in Publish → Google Calendar → Automatic updates (off until an admin
turns it on). Then:
  - every evening around 5 PM Pacific, a GitHub Actions job
    (.github/workflows/calendar-sync.yml) runs `python -m scheduler.autosync`,
    which brings every calendar in line with the schedule;
  - when a scheduling admin approves a coach's request on published dates, the
    app updates that team's calendar and its club calendar right away.

What it sends: the schedule as it is now (hand edits and approved requests
included) on PUBLISHED dates only. Dates that haven't been published are never
touched. Like a manual sync, it only ever changes events this app made.
Team and club calendars get practices; programs and tournaments go too only
when "Include programs and tournaments" is on for automatic updates.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import clock, gcal
from . import google as gsvc
from .changes import practices_of
from .config import team_is_boys
from .data import ScheduleInput, clean
from .storage import MAIN_ID, Store, all_periods, read_period

SETTING = "gcal_autosync"        # {"on": bool, "extras": bool}
LAST = "gcal_autosync_last"      # the last run: when, what, how it went
EVENING_HOUR = 17                # the daily run: 5 PM Pacific


# ---------------------------------------------------------- settings --
def settings(store: Store) -> dict:
    """{'on': bool, 'extras': bool}: is automatic syncing on, and does it send programs and tournaments."""
    try:
        raw = json.loads(store.get_setting(SETTING) or "{}")
    except ValueError:
        raw = {}
    return {"on": bool(raw.get("on")), "extras": bool(raw.get("extras"))}


def save_settings(store: Store, on: bool, extras: bool) -> None:
    store.set_setting(SETTING, json.dumps({"on": bool(on), "extras": bool(extras)}))


def last_run(store: Store) -> dict | None:
    try:
        return json.loads(store.get_setting(LAST) or "null")
    except ValueError:
        return None


# ------------------------------------------------------------- client --
def client_for(store: Store, secrets) -> gcal.CalendarClient | None:
    """The connected club Google account (Access → Google connection), else a service account key."""
    cfg = gsvc.client_config(secrets)
    conn = gsvc.connection(store)
    if cfg and gsvc.can_calendar(conn):
        sess = gsvc.session(store, cfg, secrets)
        if sess is not None:
            return gcal.CalendarClient({"client_email": conn["email"]}, session=sess)
    sa = gcal.service_account_info(secrets)
    return gcal.CalendarClient(sa) if sa else None


# --------------------------------------------------------------- sync --
@dataclass
class Outcome:
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: list = field(default_factory=list)
    ranges: list = field(default_factory=list)    # published date ranges covered
    skipped: str = ""                              # why nothing ran, if it didn't

    @property
    def changes(self) -> int:
        return self.added + self.updated + self.removed

    def summary(self) -> str:
        if self.skipped:
            return self.skipped
        s = (f"{self.added} added, {self.updated} updated, {self.removed} removed"
             if self.changes else "already up to date")
        return s + (f", {len(self.errors)} failed" if self.errors else "")


def _published_ranges(payload: dict):
    """(start, end, practices, program sessions, cleaned inputs) for each published run of dates."""
    inp = ScheduleInput.from_records(payload.get("inputs", {}))
    for _key, ent in sorted(all_periods(payload).items()):
        if not ent.get("published"):
            continue
        s_, e_, _settings, result, published = read_period(ent, inp)
        inp.start, inp.end = s_, e_
        if result is not None and result.ok and len(result.assignments):
            a = result.assignments
            practices = a[a["kind"] == "Practice"]
            local_rows = a[a["kind"] == "Local"]
        else:  # published, but no build saved: what was published
            practices = practices_of([v for v in published if v.get("practices")][-1]) \
                if any(v.get("practices") for v in published) else None
            local_rows = None
        try:
            cleaned = clean(inp, s_, e_)
        except ValueError:
            cleaned = None
        yield s_, e_, practices, local_rows, cleaned


def sync(store: Store, secrets, teams: set[str] | None = None, who: str = "Automatic update",
         force: bool = False) -> Outcome:
    """Bring the linked calendars in line with the schedule on published dates.
    teams: only the calendars these teams' events go on (their team calendars and their club
    calendars), e.g. after approving a request; None for every linked calendar.
    force: run even when automatic updates are off (the "Sync now" button)."""
    out = Outcome()
    opts = settings(store)
    if not (opts["on"] or force):
        out.skipped = "automatic updates are off"
        return out
    rec = store.get(MAIN_ID)
    if rec is None:
        out.skipped = "no schedule yet"
        return out
    client = client_for(store, secrets)
    if client is None:
        out.skipped = "Google Calendar isn't connected (Access → Google connection)"
        _remember(store, who, out, teams)
        return out
    payload = rec["payload"]
    inp = ScheduleInput.from_records(payload.get("inputs", {}))
    links = gcal.links_from(inp.calendars)
    if not opts["extras"]:
        links = {k: v for k, v in links.items() if k[0] != "Program"}
    address = _secret(secrets, "CLUB_ADDRESS") or ""
    for s_, e_, practices, local_rows, cleaned in _published_ranges(payload):
        if cleaned is None:
            out.errors.append(f"{s_:%b %-d}–{e_:%b %-d}: the schedule's inputs have a problem; skipped")
            continue
        side = {lp.name: lp.side for lp in cleaned.locals}
        evs, _missing = gcal.desired_events(
            practices, local_rows if opts["extras"] else None, cleaned.tournaments if opts["extras"] else [],
            cleaned.teams, links, s_, e_, "", address, side)
        cals = set(links.values())
        if teams is not None:  # just these teams' calendars (and their whole club calendars)
            mine = {e.calendar_id for e in evs if e.target in {f"Team: {t}" for t in teams}}
            mine |= {links.get(("Team", t)) for t in teams} - {None}
            boys = {team_is_boys(t_.club, t_.name) for t_ in cleaned.teams if t_.name in teams}
            mine |= {links.get(("Club", gcal.club_calendar(links, b))) for b in boys} - {None}
            cals &= mine
            evs = [e for e in evs if e.calendar_id in cals]
        if not cals:
            continue
        p = gcal.plan(evs, client, s_, e_, sorted(cals), leave=() if opts["extras"] else ("program", "tournament"))
        out.errors += p.errors
        _n, errs = gcal.apply(p, client)
        out.errors += errs
        out.added += len(p.create)
        out.updated += len(p.update)
        out.removed += len(p.delete)
        out.ranges.append(f"{s_:%b %-d}–{e_:%b %-d}")
    if not out.ranges and not out.errors:
        out.skipped = "nothing published to sync"
    _remember(store, who, out, teams)
    return out


def _remember(store: Store, who: str, out: Outcome, teams) -> None:
    what = ("Google Calendar (" + ", ".join(sorted(teams)) + ")") if teams else "Google Calendar"
    if out.changes or out.errors or not teams:
        store.log(who, what, out.summary() + (f": {'; '.join(out.errors[:3])}" if out.errors else ""), MAIN_ID)
    store.set_setting(LAST, json.dumps({"at": datetime.now(UTC).isoformat(), "who": who, "teams": sorted(teams or []),
                                        "summary": out.summary(), "errors": out.errors[:10]}))


def _secret(secrets, name):
    try:
        return secrets.get(name)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------- the evening job (GitHub) --
def secrets_from_env() -> dict:
    """The app's secrets, for a run outside Streamlit, from environment variables (GitHub secrets)."""
    auth = {k: os.environ[v] for k, v in (("client_id", "GOOGLE_CLIENT_ID"), ("client_secret", "GOOGLE_CLIENT_SECRET"),
                                          ("cookie_secret", "COOKIE_SECRET"), ("redirect_uri", "AUTH_REDIRECT_URI"))
            if os.environ.get(v)}
    out: dict = {"auth": auth} if auth else {}
    for k in ("TOKEN_KEY", "CLUB_ADDRESS", "ADMIN_EMAILS"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT"):
        out["GOOGLE_SERVICE_ACCOUNT"] = os.environ["GOOGLE_SERVICE_ACCOUNT"]
    return out


def evening_due(store: Store, now: datetime | None = None) -> bool:
    """The scheduled job fires at 4 and 5 PM Pacific (GitHub runs on UTC, which shifts with daylight
    saving); it syncs once a day, at or after 5 PM."""
    now = now or clock.now()
    if now.hour < EVENING_HOUR:
        return False
    last = last_run(store)
    if last and last.get("who") == "Evening update":
        when = datetime.fromisoformat(last["at"]).astimezone(clock.now().tzinfo).date()
        if when == now.date():
            return False
    return True


def main(argv: list[str]) -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:  # not set up yet: say so in the run's log, without failing (and emailing) every evening
        print("::warning::DATABASE_URL isn't set, so nothing ran. See docs/SETUP_AUTOSYNC.md.")
        return 0
    store = Store(url)
    manual = "--now" in argv
    if not manual and not evening_due(store):
        print("Not due (before 5 PM Pacific, or already ran today).")
        return 0
    try:
        out = sync(store, secrets_from_env(), who="Evening update" if not manual else "Manual run (GitHub)")
    except Exception as e:  # noqa: BLE001
        from .problems import record
        record(store, "Evening calendar update", e, who="Evening update", secrets=secrets_from_env())
        traceback.print_exc()
        return 1
    print(out.summary())
    for err in out.errors:
        print("  ", err)
    if out.errors:
        from .problems import record
        record(store, "Evening calendar update", "; ".join(out.errors[:5]), who="Evening update",
               secrets=secrets_from_env())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

