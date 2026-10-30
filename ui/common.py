"""Helpers shared by the app's sections: wording, pickers, people, email, saved-schedule list."""

from __future__ import annotations

import hashlib
import json
import re
import time as time_mod
from datetime import date

import pandas as pd
import streamlit as st

from scheduler import clean, mail, month_range, simulate
from scheduler import google as gsvc
from scheduler.config import DAYS
from scheduler.data import ATTRS
from scheduler.storage import MAIN_ID
from ui.state import S


def next_month(today: date) -> tuple[date, date]:
    """The first and last day of the month after `today`."""
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return month_range(y, m)


def plural(n: int, word: str, many: str | None = None) -> str:
    """'1 team', '3 teams' (or a given plural: plural(2, 'match', 'matches'))."""
    return f"{n} {word if n == 1 else (many or word + 's')}"


def blank_template(start: date, end: date) -> bytes:
    """Template workbook with example rows on each sheet."""
    ex = simulate(2, 1, 1, seed=1, start=start, end=end)
    for attr in ATTRS.values():
        setattr(ex, attr, getattr(ex, attr).head(2))
    ex.locals = ex.locals.assign(dates=f"{start:%m/%d/%Y}-{end:%m/%d/%Y}")  # every program has its dates
    return ex.to_excel_bytes()


def secret(name: str):
    """A value from Streamlit's secrets, or None when there's no secrets file (running locally)."""
    try:
        return st.secrets.get(name)
    except Exception:  # noqa: BLE001  (no secrets file)
        return None


@st.cache_data(ttl=60, show_spinner=False)
def saved_list(url: str | None, ver: int) -> list[dict]:
    """The saved-schedule menu. Cached so clicking around doesn't query the
    database every time; refreshed whenever a schedule is created, renamed
    or deleted (ver), and at least once a minute."""
    store = S.store
    return store.list()


def saved_changed():
    """Refresh the cached list of saved schedules on the next run."""
    st.session_state.saved_ver = st.session_state.get("saved_ver", 0) + 1


def payload_hash(payload: dict) -> str:
    """A fingerprint of a payload, to skip saving when nothing changed."""
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def bound(widget, label: str, key: str, **kw):
    """A widget whose value lives in session_state[key] even on runs where the
    widget isn't drawn (Streamlit throws away a widget's own state then, which
    lost the schedule dates when an admin viewed the app as a coach)."""
    wk = f"w_{key}"
    if key in st.session_state:
        st.session_state[wk] = st.session_state[key]

    def _copy():
        st.session_state[key] = st.session_state[wk]
    return widget(label, key=wk, on_change=_copy, **kw)


def inputs_fingerprint(inp) -> str:
    """A quick fingerprint of every input table, to tell whether anything changed
    (much faster than writing the tables out as text)."""
    tables = [(sheet, list(df.columns), df.values.tolist()) for sheet, attr in ATTRS.items()
              if isinstance(df := getattr(inp, attr, None), pd.DataFrame)]
    return hashlib.sha1(repr(tables).encode()).hexdigest()


def cleaned_inputs(inp, start, end):
    """clean(inp, start, end), worked out once per change instead of on every click.
    Raises ValueError like clean() does."""
    key = (inputs_fingerprint(inp), str(start), str(end))
    hit = st.session_state.get("_clean_cache")
    if hit and hit[0] == key:
        if isinstance(hit[1], ValueError):
            raise hit[1]
        return hit[1]
    try:
        out = clean(inp, start, end)
    except ValueError as e:
        st.session_state._clean_cache = (key, e)
        raise
    st.session_state._clean_cache = (key, out)
    return out


def pending_requests_count() -> int:
    """How many change requests are waiting, re-checked at most every 20 seconds
    (or right away on the Requests tab) instead of on every click."""
    c = st.session_state.get("_n_pending")
    if c and time_mod.time() - c[0] < 20 and st.session_state.get("nav") != "Requests":
        return c[1]
    n = len(S.store.requests("pending"))
    st.session_state._n_pending = (time_mod.time(), n)
    return n


def coach_names() -> list[str]:
    """Coach names in the open schedule (for linking logins and "View as")."""
    i = st.session_state.get("inp")
    if i is None:
        return []
    try:
        return cleaned_inputs(i, st.session_state.get("range_start"), st.session_state.get("range_end")).coaches
    except Exception:  # noqa: BLE001
        return []


def sched_admin_emails(exclude: str = "") -> list[str]:
    """Every scheduling admin's email (minus `exclude`), for request notifications."""
    store = S.store
    return [u["email"] for u in store.users() if u.get("sched_admin") and u["email"] != (exclude or "").lower()]


def decided(req: dict, status: str, note: str = "") -> str:
    """Marks a request approved / declined and emails the coach. Returns the flash message."""
    store, APP_LINK, VIEWER = S.store, S.APP_LINK, S.VIEWER
    by = VIEWER.person.label if VIEWER.person else "A scheduling admin"
    st.session_state.pop("_n_pending", None)  # the waiting count changes
    if not store.decide_request(req["id"], status, VIEWER.person.email if VIEWER.person else "", note):
        return "Someone else already decided that request."
    store.log(VIEWER.log_name, "Request " + ("approved" if status == "approved" else "declined"),
              f"{req['who']}: {req['summary']}" + (f" ({note})" if note else ""), MAIN_ID)
    word = "Approved" if status == "approved" else "Declined"
    err = send_mail([req["email"]], mail.decision(req, status, by, note, APP_LINK)) if req.get("email") else None
    return f"{word}: {req['summary']}." + (f" (Email to {req['who']} not sent: {err}.)" if err else
                                           f" {req['who']} was emailed." if req.get("email") else "")


def mine(team_text) -> bool:
    """For a coach: is this (a team, or 'A, B' / 'A; B') one of theirs? Admins see everything."""
    COACH_VIEW, MY_TEAMS = S.COACH_VIEW, S.MY_TEAMS
    if not COACH_VIEW:
        return True
    return bool({x.strip() for x in re.split(r"[;,]", str(team_text or ""))} & MY_TEAMS)


def template_locals(a: pd.DataFrame, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Locals that are part of the usual week: running on most of that weekday's dates between
    start and end (default: the dates being worked on)."""
    start, end, closures = start or S.start, end or S.end, S.closures
    loc = a[a["kind"] == "Local"]
    if loc.empty:
        return loc
    per_day = {wd: len({d for d in pd.date_range(start, end).date if DAYS[d.weekday()] == wd and d not in closures})
               for wd in DAYS}
    counts = loc.groupby(["name", "day", "slot", "court"])["date"].nunique().reset_index(name="n")
    keep = counts[counts.apply(lambda r: r["n"] * 2 >= per_day[r["day"]], axis=1)]
    return loc.drop_duplicates(subset=["day", "slot", "court", "name"]).merge(
        keep[["name", "day", "slot", "court"]], on=["name", "day", "slot", "court"])


def team_order() -> list[str]:
    """Team names, girls then boys, each in the pecking order (top team first)."""
    cleaned = S.cleaned
    from scheduler.config import pecking_key, team_is_boys
    if not cleaned:
        return []
    return [t.name for t in sorted(cleaned.teams, key=lambda t: (team_is_boys(t.club, t.name),
                                                                 pecking_key(t.age, t.level, False)))]


def team_sections() -> dict[str, str]:
    """Each team's side ('Girls' / 'Boys'), for headings in the team-dates spreadsheet."""
    cleaned = S.cleaned
    from scheduler.config import team_is_boys
    return {t.name: "Boys" if team_is_boys(t.club, t.name) else "Girls" for t in (cleaned.teams if cleaned else [])}


def team_coaches() -> dict[str, list[str]]:
    """Each team's coaches, head coach(es) first, for the printed team dates."""
    cleaned = S.cleaned
    return {t.name: t.staff for t in (cleaned.teams if cleaned else [])}


def people_options(a: pd.DataFrame) -> list[str]:
    """Clubs, teams and coaches for the 'highlight' / 'only show' pickers."""
    cleaned = S.cleaned
    clubs = sorted({c for c in a["club"] if isinstance(c, str) and c})
    teams_ = sorted(set(a.loc[a["kind"] == "Practice", "name"]))
    coaches = cleaned.coaches if cleaned else []
    return clubs + teams_ + coaches


# ------------------------------------------------- Google connection --
def g_conn() -> dict | None:
    """The connected Google account (re-read at most every 30 s)."""
    store = S.store
    c = st.session_state.get("_gconn")
    if c and time_mod.time() - c[0] < 30:
        return c[1]
    conn = gsvc.connection(store)
    st.session_state._gconn = (time_mod.time(), conn)
    return conn


def g_session():
    """An authorized Google session for email and calendars, or None if Google isn't connected."""
    SECRETS, store, GCFG = S.SECRETS, S.store, S.GCFG
    try:
        return gsvc.session(store, GCFG, SECRETS) if GCFG else None
    except Exception:  # noqa: BLE001  (token no longer valid: shown in Access)
        return None


def send_mail(to: list[str], msg: tuple[str, str, str], reply_to: str | None = None) -> str | None:
    """Sends one email as the connected account. Returns an error message, or None if sent."""
    conn = g_conn()
    if not gsvc.can_email(conn):
        return "email isn't connected yet (Access → Google connection)"
    sess = g_session()
    if sess is None:
        return "the Google connection needs to be redone (Access → Google connection)"
    subject, text, html_ = msg
    try:
        gsvc.send(sess, conn["email"], to, subject, text, html_, reply_to=reply_to)
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)


def tournament_note(team: str, d: date) -> str | None:
    """Why `team` shouldn't practice on day d because of a travel tournament it goes to: 'at SoCal Cup
    that day', 'the day after SoCal Cup'... None if nothing."""
    from datetime import timedelta
    cleaned = getattr(S, "cleaned", None)
    for tn in (cleaned.tournaments if cleaned is not None else []):
        if tn.kind != "travel" or team not in tn.teams:
            continue
        if tn.start <= d <= tn.end:
            return f"at {tn.name} that day"
        if d == tn.start - timedelta(days=1):
            return f"the day before {tn.name}"
        if d == tn.end + timedelta(days=1):
            return f"the day after {tn.name}"
    return None


def is_app_admin() -> bool:
    """The signed-in app admin, looking as themselves (not 'View as' someone else)."""
    v = getattr(S, "VIEWER", None)
    return bool(v is not None and v.person is not None and v.person.app_admin and v.acting_as is None)


def off_preference(a: pd.DataFrame) -> pd.DataFrame:
    """The bookings with an 'off_pref' column: for each team practice that isn't at its coaches'
    preferred days and times, why (a no-date or 'rather not' of the team or its coaches; or a day
    or time its wishlist doesn't name). '' for the rest. Weekend wishlist rows only count on
    weekends, weekday ones on weekdays."""
    from scheduler.config import DAYS
    cleaned = getattr(S, "cleaned", None)
    if cleaned is None or not len(a):
        return a.assign(off_pref="")
    teams = {t.name: t for t in cleaned.teams}
    rules: dict[str, list] = {}
    for r in cleaned.rules:
        rules.setdefault(r.who, []).append(r)
    out = []
    for r in a.to_dict("records"):
        t = teams.get(r.get("name"))
        if r.get("kind") != "Practice" or t is None:
            out.append("")
            continue
        d, s_ = r["date"], r["slot"]
        why = []
        for who in [t.name] + t.staff:
            for rl in rules.get(who, []):
                if not rl.manual and rl.matches(d, s_):  # (hand edits' removals aren't preferences)
                    kind = "no-date" if rl.kind == "no" else "rather not"
                    whose = "team" if who == t.name else who
                    why.append(f"{whose}'s {kind} ({rl.describe()}" + (f": {rl.note}" if rl.note else "") + ")")
        weekend = d.weekday() >= 5
        wishes = [w for w in cleaned.wishes.get(t.name, [])
                  if not w.days or (any(x in ("Sat", "Sun") for x in w.days) == weekend)]
        if wishes:
            hit = [w.choice for w in wishes if w.matches(d, s_)]
            if not hit:
                on_day = [w for w in wishes if not w.days or DAYS[d.weekday()] in w.days]
                why.append("wishlist has another time that day" if on_day else "not a wishlist day")
            elif min(hit) > 1:
                why.append("2nd-choice wishlist time")
        out.append("; ".join(dict.fromkeys(why)))
    return a.assign(off_pref=out)
