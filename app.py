"""Court Scheduler – Streamlit app.

Run locally:  streamlit run app.py
"""

from __future__ import annotations

import hashlib
import io
from html import escape as html_escape
import json
import os
import re
import sys
import time as time_mod
from datetime import date

import pandas as pd
import streamlit as st

# Always load the current scheduler code. After a push, Streamlit Cloud can
# rerun app.py while older scheduler modules are still in memory, which shows
# up as an ImportError until the app is rebooted.
for _mod in [m for m in list(sys.modules) if m == "scheduler" or m.startswith("scheduler.")]:
    del sys.modules[_mod]

from scheduler import ScheduleInput, clean, month_range, simulate, solve
from scheduler.config import (
    ALL_COURTS, COURTS, DAYS, MAX_PRACTICES_PER_WEEK, PRACTICE_COURT, PRACTICES_PER_WEEK, PROGRAM_KEYS,
    SLOT_KEYS, SLOTS, WEEKDAY_KEYS, WEEKDAYS, WEEKEND_KEYS, court_name, court_short,
)
from scheduler.data import (
    ATTRS, COLUMNS, blank, empty, keep_programs, merge_import, parse_courts, parse_date_ranges, parse_days, parse_names, parse_rank,
    parse_list, parse_times, program_coach_conflicts, program_side, span_text, to_date, tournament_conflicts,
)
from scheduler import privates as pv
from scheduler import access
from scheduler import gcal
from scheduler import pecking as pk
from scheduler.export import calendar_excel, calendar_ics, team_dates_excel, to_excel, to_ics
from scheduler.paper import paper_weeks_html
from scheduler.solver import week_start
from scheduler.changes import add_version, burden, diff, pattern_of, practices_of, published_at, snapshot
from scheduler.storage import (
    MAIN_ID, Store, all_periods, current_period, database_url, ensure_main, make_payload, period_key, read_payload,
    read_period, read_published,
)
from scheduler.views import (
    DARK_CSS, calendar_print_html, club_stripes, court_grid_html, legend_html, month_calendar_html, open_slot_map, practice_counts_html,
    range_label, short_list_text, week_dates, weeks_print_html,
)

st.set_page_config(page_title="Court Scheduler", layout="wide")
try:
    DARK = st.context.theme.type == "dark"
except Exception:  # noqa: BLE001
    DARK = False
if DARK:  # the court grid, calendar and counts follow the app's theme (⋮ → Settings)
    st.markdown(DARK_CSS, unsafe_allow_html=True)


def next_month(today: date) -> tuple[date, date]:
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
    return ex.to_excel_bytes()


def secret(name: str):
    try:
        return st.secrets.get(name)
    except Exception:  # noqa: BLE001  (no secrets file)
        return None


# ------------------------------------------------------------ password --
def _all_secrets():
    try:
        st.secrets.get("_")
        return st.secrets
    except Exception:  # noqa: BLE001  (no secrets file)
        return {}


SECRETS = _all_secrets()
AUTH_ON = access.auth_configured(SECRETS)   # Google sign-in set up in secrets
APP_PASSWORD = secret("APP_PASSWORD")
if APP_PASSWORD and not AUTH_ON and not st.session_state.get("authed"):
    st.title("Court Scheduler")
    pw = st.text_input("Club password", type="password")
    if pw and pw == str(APP_PASSWORD):
        st.session_state.authed = True
        st.rerun()
    elif pw:
        st.error("That password isn't right.")
    st.stop()


# ------------------------------------------------------- saved schedules --
@st.cache_resource
def get_store(url: str | None) -> Store:
    return Store(url)


try:
    store = get_store(database_url())
except Exception as e:  # noqa: BLE001  (bad DATABASE_URL: explain instead of crashing)
    url = database_url() or ""
    st.error("**Couldn't connect to the database in `DATABASE_URL`.**")
    if re.search(r"@db\.[a-z0-9]+\.supabase\.co", url):
        st.markdown(
            "This is Supabase's **Direct connection** string, which Streamlit Cloud can't reach. "
            "In Supabase, click **Connect** → **Connection string** → change *Method* to "
            "**Session pooler**, and copy that one instead. It looks like "
            "`postgresql://postgres.<project>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`.")
    else:
        st.markdown("Check the address and password in the app's **Settings → Secrets**. "
                    "For Supabase, use the **Session pooler** connection string.")
    st.caption(f"Details: {type(e).__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}")
    st.stop()
PROJECT_KEYS = ["project_id", "project_name", "inp", "result", "result_period", "inp_sig", "last_saved",
                "range_start", "range_end", "time_limit", "makeup", "keep_prev", "published", "base_version", "_save_sig",
                "pending_prefs", "periods", "periods_dirty", "periods_deleted"]

# --------------------------------------------------------------- sign-in --
# With Google sign-in set up (see docs/SETUP_LOGINS.md), only club addresses
# get in, and each person's role decides what they can change. Without it
# (or locally), the app works as before: anyone who opens it can do
# everything. COURT_DEV_USER=someone@sdvbc.org tries a role locally.
if AUTH_ON:
    if not st.user.is_logged_in:
        st.title("Court Scheduler")
        st.markdown("Sign in with your club Google account to see the schedule.")
        st.button("Sign in with Google", type="primary", on_click=st.login)
        st.stop()
    _email, _name = str(st.user.get("email") or ""), str(st.user.get("name") or "")
    if not access.email_allowed(_email, SECRETS):
        st.title("Court Scheduler")
        st.error(f"{_email} isn't a club account. Sign in with your @{access.allowed_domains(SECRETS)[0]} address.")
        st.button("Sign out", on_click=st.logout)
        st.stop()
elif os.environ.get("COURT_DEV_USER"):
    _email, _name = os.environ["COURT_DEV_USER"], ""
else:
    _email = _name = None


def _me() -> access.Person | None:
    """The signed-in person, re-read from the database at most every 30 s."""
    if _email is None:
        return None
    cached = st.session_state.get("_me")
    if cached and cached[0] == _email and time_mod.time() - cached[1] < 30:
        return cached[2]
    p = access.person_for(store, _email, _name, SECRETS)
    st.session_state._me = (_email, time_mod.time(), p)
    return p


ME = _me()


@st.cache_data(ttl=60, show_spinner=False)
def _saved_list(url: str | None, ver: int) -> list[dict]:
    """The saved-schedule menu. Cached so clicking around doesn't query the
    database every time; refreshed whenever a schedule is created, renamed
    or deleted (ver), and at least once a minute."""
    return store.list()


def saved_changed():
    st.session_state.saved_ver = st.session_state.get("saved_ver", 0) + 1


def payload_hash(payload: dict) -> str:
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _load_into_session(rec: dict, key: str | None = None):
    """Opens the club's schedule at a period (key; default: the one stored on top)."""
    payload = rec["payload"]
    inp_ = ScheduleInput.from_records(payload.get("inputs", {}))
    periods_ = all_periods(payload)
    top = period_key(date.fromisoformat(payload["start"]), date.fromisoformat(payload["end"]))
    key = key if key in periods_ else top
    s_, e_, settings, result_, published_ = read_period(periods_.pop(key), inp_)
    inp_.start, inp_.end = s_, e_
    st.session_state.update(
        project_id=MAIN_ID, project_name=rec["name"], inp=inp_, result=result_,
        result_period=(result_.start, result_.end) if result_ else None,
        inp_sig=("project", MAIN_ID, str(rec["updated_at"])),
        range_start=s_, range_end=e_,
        time_limit=int(settings.get("time_limit", 60)), makeup=bool(settings.get("makeup", True)),
        keep_prev=bool(settings.get("keep_prev", True)), published=published_,
        base_version=settings.get("base_version"), pending_prefs=list(settings.get("pending_prefs") or []),
        periods=periods_, periods_dirty=set(), periods_deleted=set(),
        last_saved=payload_hash(make_payload(inp_, s_, e_, settings, result_, published_, periods=periods_)),
    )
    st.session_state.pop("_save_sig", None)


def open_project(pid: str = MAIN_ID, key: str | None = None) -> bool:
    """(Re)opens the club's schedule, at the period being viewed unless `key` says otherwise."""
    rec = ensure_main(store, next_month(date.today()))
    if key is None and st.session_state.get("range_start"):
        key = period_key(st.session_state.range_start, st.session_state.range_end)
    _load_into_session(rec, key)
    return True


def _period_entry() -> dict:
    """The period being viewed, in the stored shape."""
    from scheduler.storage import result_to_dict
    return {"start": st.session_state.range_start.isoformat(), "end": st.session_state.range_end.isoformat(),
            "settings": {"time_limit": st.session_state.time_limit, "makeup": st.session_state.makeup,
                         "keep_prev": st.session_state.get("keep_prev", True),
                         "base_version": st.session_state.get("base_version"),
                         "pending_prefs": list(st.session_state.get("pending_prefs") or [])},
            "result": result_to_dict(st.session_state.get("result")),
            "published": list(st.session_state.get("published") or [])}


def switch_period(key: str, new: tuple[date, date] | None = None):
    """Shows another period (or starts new dates), keeping this one."""
    cur = period_key(st.session_state.range_start, st.session_state.range_end)
    periods_ = st.session_state.periods
    periods_[cur] = _period_entry()
    if CAN:
        st.session_state.periods_dirty.add(cur)
    if new is not None:
        s_, e_ = new
        settings = {"time_limit": st.session_state.time_limit, "makeup": st.session_state.makeup}
        result_, published_ = None, []
    else:
        s_, e_, settings, result_, published_ = read_period(periods_[key], st.session_state.inp)
    periods_.pop(period_key(s_, e_), None)
    st.session_state.update(
        result=result_, result_period=(result_.start, result_.end) if result_ else None,
        range_start=s_, range_end=e_,
        time_limit=int(settings.get("time_limit", 60)), makeup=bool(settings.get("makeup", True)),
        keep_prev=bool(settings.get("keep_prev", True)), published=published_,
        base_version=settings.get("base_version"), pending_prefs=list(settings.get("pending_prefs") or []))
    for k in ("w_range_start", "w_range_end"):
        st.session_state.pop(k, None)


def period_label(entry_or_dates) -> str:
    if isinstance(entry_or_dates, dict):
        s_, e_ = date.fromisoformat(entry_or_dates["start"]), date.fromisoformat(entry_or_dates["end"])
        pub = bool(entry_or_dates.get("published"))
    else:
        (s_, e_), pub = entry_or_dates, bool(st.session_state.get("published"))
    return range_label(s_, e_) + ("" if pub else " · not published")


def periods_for_save(rec_payload: dict | None) -> dict:
    """The other periods to save: what's in the database (another admin may
    be working on other dates), with this session's changes on top."""
    out = all_periods(rec_payload) if rec_payload else {}
    for k in st.session_state.get("periods_deleted", set()):
        out.pop(k, None)
    for k in st.session_state.get("periods_dirty", set()):
        if k in st.session_state.periods:
            out[k] = st.session_state.periods[k]
    for k in st.session_state.periods:  # periods this session knows that the database lost track of
        out.setdefault(k, st.session_state.periods[k])
    out.pop(period_key(st.session_state.range_start, st.session_state.range_end), None)
    return out


if st.session_state.get("project_id") != MAIN_ID:
    open_project()

# ---------------------------------------------------------------- sidebar --
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


def _coach_names() -> list[str]:
    """Coach names in the open schedule (for linking logins and "View as")."""
    i = st.session_state.get("inp")
    if i is None:
        return []
    try:
        return clean(i, st.session_state.get("range_start"), st.session_state.get("range_end")).coaches
    except Exception:  # noqa: BLE001
        return []


with st.sidebar:
    st.title("Court Scheduler")
    acting = None
    if ME is not None:
        st.caption(f"Signed in as **{ME.label}** · {', '.join(ME.roles)}"
                   + ("" if ME.coach or ME.sched_admin else " · not linked to a coach name yet"))
        if ME.app_admin:
            _opts = ["Myself", "Scheduling admin", "Coach (not linked)"] + [f"Coach: {c}" for c in _coach_names()]
            _va = st.selectbox("View as", _opts, key="view_as",
                               help="See the app exactly as someone with that role does, to test it. "
                                    "Only you see this.")
            if _va == "Scheduling admin":
                acting = access.Person(ME.email, ME.name, ME.coach, False, True)
            elif _va == "Coach (not linked)":
                acting = access.Person(ME.email, ME.name, "", False, False)
            elif _va.startswith("Coach: "):
                acting = access.Person(ME.email, _va[7:], _va[7:], False, False)
        if AUTH_ON:
            st.button("Sign out", on_click=st.logout, key="signout")
    VIEWER = access.Viewer(ME, acting)
    st.caption("🌙 Dark mode: **⋮** (top right) → **Settings** → theme.")
    CAN = VIEWER.can_schedule
    saved = _saved_list(database_url(), st.session_state.get("saved_ver", 0))
    names = {p["id"]: p["name"] for p in saved}
    if not CAN and not st.session_state.get("_coach_default"):
        # A coach opens on the dates covering today (else the latest published ones).
        st.session_state._coach_default = True
        _cur = period_key(st.session_state.range_start, st.session_state.range_end)
        _all = dict(st.session_state.periods, **{_cur: _period_entry()})
        _today = date.today()
        _pick = next((k for k, v in _all.items() if v.get("published")
                      and date.fromisoformat(v["start"]) <= _today <= date.fromisoformat(v["end"])), None)
        _pick = _pick or max((k for k, v in _all.items() if v.get("published")), default=None)
        if _pick and _pick != _cur:
            switch_period(_pick)

    # ------------------------------------------------------------- dates --
    current = MAIN_ID
    cur_key = period_key(st.session_state.range_start, st.session_state.range_end)
    p_opts = sorted(list(st.session_state.periods) + [cur_key])
    p_lab = {k: period_label(v) for k, v in st.session_state.periods.items()}
    p_lab[cur_key] = period_label((st.session_state.range_start, st.session_state.range_end))
    pick = st.selectbox("Dates", p_opts, index=p_opts.index(cur_key), format_func=p_lab.get,
                        key=f"period_{cur_key}_{len(p_opts)}",
                        help="The schedule is built a run of dates at a time. Pick which dates to see.")
    if pick != cur_key:
        switch_period(pick)
        st.rerun()

    run = False
    if not CAN:
        st.caption("You can change your teams' preferences in **Edit practices**; other changes go through the "
                   "scheduling admins.")
    else:
        with st.expander("Schedule new dates"):
            _ns = st.session_state.range_end + pd.Timedelta(days=1)
            n_start = st.date_input("From", value=_ns, format="MM/DD/YYYY", key="new_from")
            n_end = st.date_input("To", value=n_start + pd.Timedelta(days=13), format="MM/DD/YYYY", key="new_to")
            st.caption("Starts a new set of dates with the club's teams, coaches, programs and tournaments. "
                       "The dates you've already scheduled stay in the **Dates** menu.")
            if st.button("Start these dates", width="stretch"):
                nk = period_key(n_start, n_end)
                if n_end < n_start:
                    st.error("The end date is before the start date.")
                elif nk == cur_key or nk in st.session_state.periods:
                    st.error("Those dates are already in the Dates menu.")
                else:
                    switch_period(nk, (n_start, n_end))
                    st.session_state.resolve = True
                    st.session_state.flash = f"Started {range_label(n_start, n_end)}."
                    st.rerun()
        _no_teams = st.session_state.inp.teams.dropna(how="all").empty
        with st.expander("Import coaches' spreadsheet", expanded=_no_teams):
            st.caption("Adds any new teams and coaches (the ones already here stay as they are) and brings in "
                       "everyone's no-dates and wishlists from the file, replacing their old ones. Programs, "
                       "tournaments and open days stay.")
            rep = st.file_uploader("Club spreadsheet (.xlsx)", type=["xlsx"], key="replace_main")
            use_file = st.checkbox("Also replace programs and tournaments", key="replace_kept_main",
                                   help="Off: the Programs and Tournaments tabs stay as they are. On: use the "
                                        "spreadsheet's, where it has any (an empty or missing sheet never "
                                        "wipes them).")
            if rep is not None and st.button("Import", width="stretch"):
                try:
                    new_inp = ScheduleInput.from_excel(rep)
                    notes = merge_import(new_inp, st.session_state.inp, st.session_state.range_start,
                                         use_file_programs=use_file)
                    st.session_state.inp = new_inp
                    st.session_state.inp_sig = ("project", current, rep.name, rep.size)
                    st.session_state.result = None
                    st.session_state.pending_prefs = []   # those coaches' changes are in the file now
                    st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
                    st.session_state.resolve = True
                    st.session_state.flash = (f"Imported {rep.name}" + (f": {'; '.join(notes)}" if notes else "")
                                              + ".")
                    if ME is not None:
                        store.log(VIEWER.log_name, "Import", rep.name + (f" ({'; '.join(notes)})" if notes else ""),
                                  MAIN_ID)
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(f"Couldn't read that file: {e}")
            st.download_button("Download blank template", lambda: blank_template(st.session_state.range_start,
                                                                                 st.session_state.range_end),
                               "court_scheduler_template.xlsx",
                               help="Sheets: Teams, Availability, Wishlist, Locals, Weekends, Closures, "
                                    "Tournaments. Example rows show the format.")
        with st.expander("Solver settings", expanded=False):
            bound(st.slider, "Time limit (seconds)", "time_limit", min_value=5, max_value=120)
            bound(st.checkbox, "Make up practices lost to closures", "makeup",
                  help="If on, a team whose usual day is a closure gets a makeup that week "
                       "(on another weekday or an open weekend).")
            bound(st.checkbox, "Keep practices where they are when rebuilding", "keep_prev",
                  help="Rebuilding moves only the practices that have to move, starting from the last "
                       "published schedule (or the version you restored, or the current one if nothing's "
                       "published). Turn off to start fresh.")
        run = st.button("Auto-schedule", type="primary", width="stretch")
        with st.expander("Change or remove these dates"):
            bound(st.date_input, "From", "range_start", format="MM/DD/YYYY")
            bound(st.date_input, "To", "range_end", format="MM/DD/YYYY")
            if p_opts != [cur_key]:
                sure = st.checkbox(f"Yes, remove {range_label(st.session_state.range_start, st.session_state.range_end)}"
                                   " for everyone", key=f"del_{cur_key}")
                if st.button("Remove these dates", disabled=not sure, width="stretch"):
                    gone = cur_key
                    other = next(k for k in p_opts if k != cur_key)
                    switch_period(other)
                    st.session_state.periods.pop(gone, None)
                    st.session_state.periods_dirty.discard(gone)
                    st.session_state.periods_deleted.add(gone)
                    st.session_state._force_save = True
                    st.session_state.flash = "Removed those dates."
                    st.rerun()
        old = [p for p in saved if p["id"] != MAIN_ID]
        if old:
            with st.expander("Old saved schedules"):
                st.caption("From before the club had one schedule. Bring one's dates in (its schedule and "
                           "published versions; the club's teams and programs stay as they are here).")
                o_pick = st.selectbox("Schedule", [p["id"] for p in old], format_func=names.get, key="old_pick")
                if st.button("Add its dates", width="stretch"):
                    rec_o = store.get(o_pick)
                    if rec_o is not None:
                        ent = current_period(rec_o["payload"])
                        k = period_key(date.fromisoformat(ent["start"]), date.fromisoformat(ent["end"]))
                        if k == cur_key or k in st.session_state.periods:
                            st.error("Those dates are already in the Dates menu.")
                        else:
                            st.session_state.periods[k] = ent
                            st.session_state.periods_dirty.add(k)
                            st.session_state._force_save = True
                            st.session_state.flash = f"Added {period_label(ent)} to the Dates menu."
                            st.rerun()
        if not store.permanent:
            st.caption("⚠️ Saved on the app server only: it survives refreshes but can be cleared when the app "
                       "restarts. Connect a database (see README) to keep schedules permanently.")

if "flash_warn" in st.session_state:
    st.warning(st.session_state.pop("flash_warn"))

if not CAN:  # an admin saved a newer version: show it
    _rec = next((p for p in saved if p["id"] == MAIN_ID), None)
    _sig = st.session_state.get("inp_sig")
    if _rec and _sig and _sig[0] == "project" and len(_sig) == 3 and _sig[2] != str(_rec["updated_at"]):
        open_project()

inp: ScheduleInput = st.session_state.inp
for _sheet, _attr in (("Tournaments", "tournaments"), ("Coaches", "coaches"), ("Overrides", "overrides"),
                      ("Privates", "privates"), ("Calendars", "calendars"), ("Resolved", "resolved")):
    if not isinstance(getattr(inp, _attr, None), pd.DataFrame):  # schedules saved before these sheets existed
        setattr(inp, _attr, empty(_sheet))
for _sheet, _attr in (("Locals", "locals"), ("Teams", "teams"), ("Tournaments", "tournaments"),
                      ("Privates", "privates")):
    _df = getattr(inp, _attr)
    for _c in COLUMNS[_sheet]:  # columns added since a schedule was saved; text, so edits can write into them
        if _c not in _df.columns:
            _df[_c] = ""
        elif _df[_c].dtype != object:
            _df[_c] = _df[_c].astype(object)
st.session_state.setdefault("keep_prev", True)
st.session_state.setdefault("show_moved", False)
st.session_state.setdefault("published", [])
start, end = st.session_state.range_start, st.session_state.range_end
time_limit, makeup = st.session_state.time_limit, st.session_state.makeup
keep_prev = st.session_state.keep_prev
published = st.session_state.published
if end < start:
    st.error("The schedule's end date is before its start date.")
    st.stop()
inp.start, inp.end = start, end

# Auto-schedule button, or adding/removing a program or tournament, rebuilds.
if run or st.session_state.pop("resolve", False):
    with st.spinner("Building the schedule…"):
        try:
            prev = prev_pat = restore = None
            restore_id = st.session_state.pop("restore_version", None)
            restore_ver = next((v for v in published if v["id"] == restore_id), None)
            # After a restore, rebuilds lean on the restored version (not the
            # newest one) until something new is published.
            base = (restore_ver or next((v for v in published if v["id"] == st.session_state.get("base_version")),
                                        None) or (published[-1] if published else None))
            if restore_ver is not None:
                restore = practices_of(restore_ver)
                prev, prev_pat = restore, pattern_of(restore_ver)
            elif keep_prev:
                if base is not None:
                    prev, prev_pat = practices_of(base), pattern_of(base)
                elif st.session_state.get("result") is not None and st.session_state.result.ok:
                    prev, prev_pat = st.session_state.result.assignments, st.session_state.result.pattern
            st.session_state.result = solve(inp, start, end, time_limit_s=time_limit, makeup_closures=makeup,
                                            previous=prev, previous_pattern=prev_pat, restore=restore)
            if restore_ver is not None:
                st.session_state.base_version = restore_ver["id"]
            st.session_state.pop("pending_rebuild", None)
            st.session_state.pending_prefs = []
            st.session_state.result_period = (start, end)
            sf = st.session_state.result.shortfalls if st.session_state.result.ok else None
            if sf is not None and len(sf):
                st.toast(f"{plural(sf['team'].nunique(), 'team')} short of practices in {plural(sf['week_of'].nunique(), 'week')}. "
                         "See the warning at the top.", icon="⚠️")
            elif sf is not None:
                st.toast("Every team got all its practices.")
        except ValueError as e:
            backup = st.session_state.pop("overrides_backup", None)
            if backup is not None:  # a manual change that can't work: undo it, keep the schedule
                inp.overrides = backup
                st.session_state.pop("flash", None)  # its "done" message no longer applies
                st.error(f"That change wasn't applied. {e}")
            else:
                st.session_state.result = None
                st.error(str(e))
        else:
            st.session_state.pop("overrides_backup", None)

result = st.session_state.get("result")
if result is not None and st.session_state.get("result_period") != (start, end):
    result = None  # dates changed since the last run

# ------------------------------------------------------------------ header --
st.header(f"{st.session_state.project_name} · {range_label(start, end)}")

try:
    cleaned = clean(inp, start, end)
    closures = cleaned.closures
except ValueError as e:
    st.error(str(e))
    cleaned, closures = None, {}
privs, priv_problems = pv.rows(inp.privates, start.year)
# A coach sees only the warnings about their own teams and themselves.
COACH_VIEW = not CAN
MY_COACH = VIEWER.coach_name
MY_TEAMS = set(VIEWER.my_teams(cleaned.teams)) if cleaned else set()


def mine(team_text) -> bool:
    """For a coach: is this (a team, or 'A, B' / 'A; B') one of theirs? Admins see everything."""
    if not COACH_VIEW:
        return True
    return bool({x.strip() for x in re.split(r"[;,]", str(team_text or ""))} & MY_TEAMS)


t_conflicts = tournament_conflicts(cleaned.teams, cleaned.tournaments) if cleaned else []
if COACH_VIEW:
    t_conflicts = [c for c in t_conflicts if (MY_COACH and c["coach"] == MY_COACH)
                   or set(c["teams"]) & MY_TEAMS or set(c.get("teams2", [])) & MY_TEAMS]
p_conflicts = program_coach_conflicts(cleaned.locals, start, end, closures) if cleaned else []
if COACH_VIEW:
    p_conflicts = [c for c in p_conflicts if MY_COACH and c["coach"] == MY_COACH]


def p_conflict_text(c: dict) -> str:
    ds = c["dates"]
    when = ", ".join(f"{d:%-m/%-d}" for d in ds[:4]) + (f" (+{len(ds) - 4} more)" if len(ds) > 4 else "")
    (pa, pb), (sa, sb) = c["programs"], c["slots"]
    return f"**{c['coach']}** is on **{pa}** ({sa}) and **{pb}** ({sb}) at the same time: {when}"


# ---------------------------------------------------- coverage flags --
# A coach who can't be in two places at once (tournaments, programs). Each
# can be marked resolved once coverage is found, and stays resolved while
# the clash is the same (a changed date or team makes a new flag).
def _span(a, b) -> str:
    return f"{a:%a %b %-d}" + ("" if a == b else f"–{b:%a %b %-d}")


def coverage_flags() -> list[dict]:
    out = []
    for c in t_conflicts:
        if c["end"] < date.today() and not (c["start"] <= end and c["end"] >= start):
            continue  # over and done, and not in the dates being looked at
        if c["kind"] == "two_travel":
            text = (f"**{c['coach']}** has two travel tournaments: **{c['travel']}** "
                    f"({', '.join(c['teams'])}) and **{c['travel2']}** ({', '.join(c['teams2'])})")
            tag = "Two travel tournaments"
        else:
            text = (f"**{c['coach']}** is working **{c['local']}** (local) and coaching "
                    f"{', '.join(c['teams'])} at **{c['travel']}**")
            tag = "Local + travel"
        out.append({"id": c["id"], "when": _span(c["start"], c["end"]), "sort": c["start"], "text": text,
                    "tag": tag, "area": "tournament"})
    for c in p_conflicts:
        (pa, pb), (sa, sb) = c["programs"], c["slots"]
        ds = c["dates"]
        when = (_span(ds[0], ds[0]) if len(ds) == 1 else f"{ds[0]:%b %-d} + {len(ds) - 1} more")
        out.append({"id": f"pp|{c['coach']}|{pa}|{pb}|{sa}|{sb}", "when": when, "sort": ds[0],
                    "text": f"**{c['coach']}** is on **{pa}** ({sa}) and **{pb}** ({sb}) at the same time",
                    "tag": "Two programs", "area": "program"})
    return sorted(out, key=lambda f: (f["sort"], f["text"]))


FLAGS = coverage_flags()
_res = inp.resolved.dropna(how="all") if len(inp.resolved) else inp.resolved
RESOLVED = {str(r["flag"]): r for _, r in _res.iterrows()} if len(_res) else {}
OPEN_FLAGS = [f for f in FLAGS if f["id"] not in RESOLVED]


def resolve_flag(fid: str, note: str):
    row = {"flag": fid, "note": note.strip(), "who": VIEWER.log_name if ME is not None else "",
           "at": f"{date.today():%m/%d/%Y}"}
    inp.resolved = pd.concat([inp.resolved[inp.resolved["flag"] != fid], pd.DataFrame([row])], ignore_index=True)
    if ME is not None:
        store.log(VIEWER.log_name, "Flag resolved", f"{fid}" + (f" · {note.strip()}" if note.strip() else ""),
                  MAIN_ID)


def reopen_flag(fid: str):
    inp.resolved = inp.resolved[inp.resolved["flag"] != fid].reset_index(drop=True)


def flags_panel(area: str | None = None, key: str = "flags", expanded: bool = False):
    """A collapsible list of coverage flags, one line each, with a Resolve
    button per flag (admins) and the resolved ones tucked underneath."""
    flags = [f for f in FLAGS if area is None or f["area"] == area]
    if not flags:
        return
    open_ = [f for f in flags if f["id"] not in RESOLVED]
    done = [f for f in flags if f["id"] in RESOLVED]
    label = (f"🚩 {plural(len(open_), 'coverage flag')} to sort out" if open_ else "✅ Coverage flags all sorted out") \
        + (f" · {len(done)} resolved" if done else "")
    with st.expander(label, expanded=expanded and bool(open_), key=f"{key}_box"):
        if open_:
            st.caption("A coach who can't be in two places at once. Once you've found coverage, mark it resolved.")
        for f in open_:
            c1, c2 = st.columns([5, 1], vertical_alignment="center")
            c1.markdown(f"🔴 **{f['when']}** · {f['text']}  \n<small>{f['tag']}</small>", unsafe_allow_html=True)
            if CAN:
                with c2.popover("Resolve", width="stretch"):
                    note = st.text_input("How it's covered (optional)", key=f"{key}_note_{f['id']}",
                                         placeholder="e.g. Cliff subs for the local")
                    if st.button("Mark resolved", key=f"{key}_ok_{f['id']}", type="primary"):
                        resolve_flag(f["id"], note)
                        st.rerun()
        if done and st.toggle(f"Show resolved ({len(done)})", key=f"{key}_showdone"):
            for f in done:
                r = RESOLVED[f["id"]]
                extra = " · ".join(str(v) for v in (r.get("note"), r.get("who"), r.get("at")) if not blank(v))
                c1, c2 = st.columns([5, 1], vertical_alignment="center")
                c1.markdown(f"✅ ~~{f['when']}~~ · {f['text']}" + (f"  \n<small>{html_escape(extra)}</small>"
                                                                   if extra else ""), unsafe_allow_html=True)
                if CAN and c2.button("Reopen", key=f"{key}_re_{f['id']}", width="stretch"):
                    reopen_flag(f["id"])
                    st.rerun()


# Everything that needs a look goes in one collapsible box: (st.error | st.warning, markdown).
alerts: list[tuple] = []

if CAN and inp.teams.dropna(how="all").empty:
    st.info("**Getting these dates ready:** 1) set up the locals in **Programs**, tournaments in **Tournaments** and "
            "any weekends or school-break days in **Open days**; 2) **Import coaches' spreadsheet** in the sidebar "
            "for the teams, no-dates and wishlists; 3) **Auto-schedule**.")
elif result is None:
    if st.session_state.get("nav") not in ("Court view", "Calendar"):  # those show their own note
        st.info("Press **Auto-schedule** in the sidebar to build the schedule for these dates. "
                "You can review and edit the inputs in the **Inputs** tab first." if CAN else
                "These dates haven't been scheduled yet.")
elif not result.ok:
    st.error(f"No schedule found ({result.status}). Check for more teams than open courts, "
             "or a coach whose no-dates block every slot.")
    result = None
else:
    m = result.metrics
    c = st.columns(6)
    c[0].metric("Practices", m["practices_scheduled"])
    c[1].metric("Missed", m["missed_practices"], help="Practices short of each team's weekly target")
    c[2].metric("Weekend practices", m["weekend_practices"])
    c[3].metric("Wishlist 1st choice", f"{m['wishlist_pct']}%" if m["wishlist_pct"] is not None else "–",
                help="Weekday practices matching the team's 1st-choice wishlist")
    c[4].metric("Coach double-ups", m["double_ups"], help="A coach's teams back-to-back on the same day")
    c[5].metric("On usual schedule", f"{m['on_pattern_pct']}%" if m.get("on_pattern_pct") is not None else "–",
                help="Weekday practices on the team's usual day and time (break days in 2-hour blocks don't count)")
    short = result.shortfalls
    if len(short) and COACH_VIEW:
        short = short[short["team"].map(mine)]
    if len(short):
        n_teams, n_missed = short["team"].nunique(), int(short["short"].sum())
        lines = [f"- **Week of {ws:%b %-d}:** {short_list_text(g)}" for ws, g in short.groupby("week_of")]
        alerts.append((st.error, f"**{plural(n_teams, 'team')} {'is' if n_teams == 1 else 'are'} short "
                                 f"{plural(n_missed, 'practice')}:**\n" + "\n".join(lines)
                       + ("" if COACH_VIEW else "\n\nWhy each one happened is in the **Review** tab, and the full "
                                                 "week-by-week counts are there too.")))
    my_broken = [b for b in result.broken if mine(b["team"])]
    if my_broken:
        lines = [f"- **{b['team']}** · {b['date']:%a %b %-d} {b['slot_label']}: {b['who']}" for b in my_broken]
        alerts.append((st.warning, f"**{plural(len(my_broken), 'practice')} had to go on a no-date** so the team "
                                   "still gets 2 practices that week:\n" + "\n".join(lines)))
    iss = result.issues if result.issues is not None else pd.DataFrame(columns=["issue"])
    if COACH_VIEW and len(iss):
        iss = iss[iss["team"].map(mine)]
    satsun = iss[iss["issue"].astype(str).str.startswith("Saturday then Sunday")] if len(iss) else iss
    if len(satsun):
        lines = [f"- **{r.team}** · {r.issue.split(': ', 1)[-1]}" for r in satsun.itertuples()]
        alerts.append((st.warning, f"**{plural(len(satsun), 'team')} practice{'s' if len(satsun) == 1 else ''} "
                                   "Saturday then Sunday**, the only way to avoid a missed practice or a head "
                                   "coach's no-date:\n" + "\n".join(lines)))
    pair = iss[iss["issue"].astype(str).str.startswith("Paired:")] if len(iss) else iss
    if len(pair):
        pair = pair.assign(_o=pair["issue"].map(lambda x: x.split(": ", 1)[-1])).sort_values(["week_of", "_o"])
        lines = [f"- **{r.issue.split(': ', 1)[-1]}** · {r.detail.split(';')[0]}: {r.team}" for r in pair.itertuples()]
        alerts.append((st.warning, f"**{plural(len(pair), 'timeslot')} had to pair teams (3 teams on 2 courts)**, "
                                   "the only way to keep those practices without breaking a no-date:\n"
                       + "\n".join(lines)))
    asst = iss[iss["issue"].astype(str).str.startswith("Assistant no-date broken")] if len(iss) else iss
    if len(asst):
        alerts.append((st.warning, f"**{plural(len(asst), 'practice')} without an assistant who marked a "
                                   "no-date:**\n" + "\n".join(f"- **{r.team}** · {r.issue.split(': ', 1)[-1]}: "
                                                               f"{r.detail}" for r in asst.itertuples())))
    if result.warnings and CAN:
        alerts.append((st.warning, f"**{plural(len(result.warnings), 'note')} about the input:**\n"
                       + "\n".join(f"- {w}" for w in result.warnings)))
    if m.get("status") == "FEASIBLE" and CAN:
        st.caption(f"The solver hit its {time_limit}-second limit, so this is a good schedule but maybe not "
                   "the best one. Raise the time limit in **Solver settings** for a better one.")
    if CAN and st.session_state.get("pending_prefs"):
        _pp = st.session_state.pending_prefs
        st.info(f"**Coaches changed preferences since the last build:** {'; '.join(_pp[-6:])}"
                + (f" (+{len(_pp) - 6} more)" if len(_pp) > 6 else "")
                + ". Press **Auto-schedule** to fit them in.")
    if CAN and st.session_state.get("pending_rebuild"):
        st.info(f"**{st.session_state.pending_rebuild}** since the last build. Press **Auto-schedule** "
                "when you're done making changes.")

flags_panel(key="flags_top")

if alerts:
    n_red = sum(f is st.error for f, _ in alerts)
    with st.expander(f"{plural(len(alerts), 'warning')}" + (f" · {n_red} to fix" if n_red else ""),
                     expanded=True, key="warnings_box", icon="🚩" if n_red else "⚠️"):
        for show, text in alerts:
            show(text)

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

# Navigation. Only the chosen section is built on each click (tabs would
# build every section every time), and the choice is remembered, so saving a
# program or removing something keeps you where you were.
ALL_VIEWS = ["Court view", "Calendar", "Edit practices", "Programs", "Tournaments", "Open days", "Privates",
             "Coaches", "Pecking order", "Publish", "Review", "Inputs", "Access"]
(TAB_COURT, TAB_CAL, TAB_EDIT, TAB_PROG, TAB_TOURN, TAB_OPEN, TAB_PRIV, TAB_COACH, TAB_PECK, TAB_PUB, TAB_REVIEW,
 TAB_INPUTS, TAB_ACCESS) = ALL_VIEWS
ADMIN_ONLY = {TAB_PROG, TAB_TOURN, TAB_OPEN, TAB_PECK, TAB_PUB, TAB_REVIEW, TAB_INPUTS}
VIEWS = [v for v in ALL_VIEWS if (CAN or v not in ADMIN_ONLY)
         and (v != TAB_ACCESS or (ME is not None and VIEWER.can_manage_access and VIEWER.acting_as is None))]
if st.session_state.get("nav") not in VIEWS:
    st.session_state.nav = VIEWS[0]
VIEW = st.radio("Section", VIEWS, key="nav", horizontal=True, label_visibility="collapsed")


def template_locals(a: pd.DataFrame) -> pd.DataFrame:
    """Locals that are part of the usual week: running on most of that weekday's dates."""
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
    from scheduler.config import pecking_key, team_is_boys
    if not cleaned:
        return []
    return [t.name for t in sorted(cleaned.teams, key=lambda t: (team_is_boys(t.club, t.name),
                                                                 pecking_key(t.age, t.level, False)))]


def team_sections() -> dict[str, str]:
    from scheduler.config import team_is_boys
    return {t.name: "Boys" if team_is_boys(t.club, t.name) else "Girls" for t in (cleaned.teams if cleaned else [])}


def people_options(a: pd.DataFrame) -> list[str]:
    clubs = sorted({c for c in a["club"] if isinstance(c, str) and c})
    teams_ = sorted(set(a.loc[a["kind"] == "Practice", "name"]))
    coaches = cleaned.coaches if cleaned else []
    return clubs + teams_ + coaches


# ------------------------------------------- every date (court/calendar) --
# The court view and calendar can show every date scheduled so far, not just
# the dates being worked on: each other run of dates shows its latest build.
def season_result():
    from scheduler.solver import ScheduleResult
    from scheduler.storage import period_bookings
    cache = st.session_state.setdefault("_season_cache", {})
    parts, wk = [], set(result.weekend_dates if result is not None else [])
    s_all, e_all = start, end
    for k, ent in st.session_state.get("periods", {}).items():
        sig = (k, len(json.dumps(ent.get("result") or {}, default=str)))
        if sig not in cache:
            cache[sig] = period_bookings(ent)
        a_, wd_ = cache[sig]
        if not len(a_):
            continue
        a_ = a_[a_["date"].map(lambda d: not (start <= d <= end))]  # these dates' own build wins
        parts.append(a_)
        wk |= set(wd_)
        s_all, e_all = min(s_all, min(a_["date"], default=s_all)), max(e_all, max(a_["date"], default=e_all))
    if result is None or not parts:
        return result, start, end
    a_all = pd.concat([result.assignments] + parts, ignore_index=True)
    return ScheduleResult(status=result.status, assignments=a_all, pattern=result.pattern, issues=result.issues,
                          metrics=result.metrics, warnings=result.warnings, start=s_all, end=e_all,
                          weekend_dates=sorted(wk), tournaments=result.tournaments,
                          practice_counts=result.practice_counts, broken=result.broken), s_all, e_all


def programs_preview():
    """Before these dates are auto-scheduled: the programs, tournaments,
    open days and closures on their own, so the court view and calendar
    show them right away. Rebuilt only when one of those changes."""
    from scheduler.data import ATTRS as _AT
    base = ScheduleInput(**{a: empty(sh) for sh, a in _AT.items()})
    for attr in ("locals", "weekends", "closures", "tournaments"):
        setattr(base, attr, getattr(inp, attr))
    sig = hashlib.sha1(json.dumps([base.to_records(), str(start), str(end)], sort_keys=True,
                                  default=str).encode()).hexdigest()
    hit = st.session_state.get("_preview")
    if hit and hit[0] == sig:
        return hit[1]
    try:
        r_ = solve(base, start, end, time_limit_s=5)
    except ValueError:
        r_ = None
    r_ = r_ if r_ is not None and r_.ok else None
    st.session_state._preview = (sig, r_)
    return r_


_view_saved = (result, start, end)
PREVIEW = False
if VIEW in (TAB_COURT, TAB_CAL) and result is None and cleaned is not None:
    result = programs_preview()
    PREVIEW = result is not None
    if PREVIEW:
        st.info("**Not auto-scheduled yet.** Showing the programs, tournaments and open days for these dates. "
                + ("Press **Auto-schedule** when the teams are in to add practices." if CAN else
                   "Practices show up once the scheduling admins build these dates."))
if VIEW in (TAB_COURT, TAB_CAL) and result is not None and st.session_state.get("periods"):
    if st.toggle("Show every date scheduled", key="all_dates",
                 help="Include the other dates in the Dates menu (each as last built), so you can look back or "
                      "ahead while working on these."):
        result, start, end = season_result()

# ---------------------------------------------------------- court view --
if VIEW == TAB_COURT:
    if result is None:
        st.caption("No schedule yet.")
    else:
        a = result.assignments
        stripes = club_stripes(a["club"])
        weeks = sorted({week_start(d) for d in a["date"]}
                       | {week_start(d.date()) for d in pd.date_range(start, end, freq="7D")} | {week_start(end)})
        options = ["Weekly template"] + [f"Week of {w:%b %-d}" for w in weeks]
        c1, c2 = st.columns([1, 2])
        pick = c1.selectbox("Show", options, index=1 if weeks else 0,  # open on the first week
                            help="The template is each team's usual weekday slot; weeks show the actual dates, "
                                 "including weekends, makeups and closures.")
        hl = set(c2.multiselect("Highlight a club, team or coach", people_options(a)))
        tcols = iter(st.columns(5 if MY_COACH else 4))
        if MY_COACH:
            if next(tcols).toggle("Highlight my practices", key="hl_mine",
                                  help="Your teams' practices and the programs you work."):
                hl |= {MY_COACH} | MY_TEAMS
        show_open = next(tcols).toggle("Show open court times", value=True, key="show_open",
                                       help="A dashed “Open” box marks every free practice time on each court.")
        show_moved = next(tcols).toggle("Outline moved practices", key="show_moved",
                                        help="Outline makeups and practices that aren't on the team's usual day "
                                             "or time.") if CAN else False
        show_priv = next(tcols).toggle("Show privates", value=True, key="show_priv",
                                       help="Private lessons booked in the Privates tab (weeks only).")
        show_lc = next(tcols).toggle("Show program coaches", key="show_lc",
                                     help="List the coaches working each locals program on its box.")
        week_privs = privs if show_priv else []
        if hl and week_privs:
            week_privs = [p for p in week_privs if p["coach"] in hl or p["team"] in hl]
        if not show_moved:
            a = a.assign(on_pattern=True)
        if published:
            _s0, _e0 = _view_saved[1], _view_saved[2]
            ch = diff(practices_of(published[-1]), a[a["date"].map(lambda d: _s0 <= d <= _e0)], cleaned, _s0, _e0)
            if len(ch):
                st.caption(f"{plural(len(ch), 'change')} since “{published[-1]['label']}” was published. "
                           "See **Publish** for the list.")

        st.markdown(legend_html(a.loc[a["kind"] == "Practice", "age"], stripes, show_moved), unsafe_allow_html=True)
        if pick == "Weekly template":
            tmpl = result.pattern.rename(columns={"team": "name"}).assign(kind="Practice", on_pattern=True)
            loc = template_locals(a)
            book = pd.concat([tmpl, loc[[c for c in list(tmpl.columns) + ["side"] if c in loc.columns]]],
                             ignore_index=True)
            st.markdown(court_grid_html(book, None, closures, hl or None, stripes,
                                        open_slots=open_slot_map(None, cleaned) if show_open else None,
                                        local_coaches=show_lc),
                        unsafe_allow_html=True)
            st.caption("Courts are ranked 1 → 7 → 6 → 5 → 3 → 4 → 2; older and 1s teams get the better courts.")
        else:
            ws = weeks[options.index(pick) - 1]
            dd = week_dates(ws, start, end, result.weekend_dates)
            wk_short = result.shortfalls[result.shortfalls["week_of"] == ws] if len(result.shortfalls) else []
            if len(wk_short) and COACH_VIEW:
                wk_short = wk_short[wk_short["team"].map(mine)]
            if len(wk_short):
                st.error(f"Short of practices this week: {short_list_text(wk_short)}", icon="⚠️")
            book = a[a["date"].map(week_start) == ws]
            st.markdown(court_grid_html(book, dd, closures, hl or None, stripes, result.tournaments,
                                        result.weekend_dates,
                                        open_slots=open_slot_map(dd, cleaned) if show_open else None,
                                        privates=[p for p in week_privs if week_start(p["date"]) == ws],
                                        local_coaches=show_lc),
                        unsafe_allow_html=True)

        d0, d1, d2 = st.columns(3)
        d0.download_button("Printable weeks · paper style", type="primary", width="stretch",
                           data=lambda: paper_weeks_html(result.assignments, start, end, closures, result.tournaments,
                                                         f"{st.session_state.get('project_name') or 'Court schedule'}",
                                                         privs, result.practice_counts, show_lc,
                                                         block_days=result.weekend_dates).encode(),
                           file_name=f"court_sheets_{start:%Y%m%d}-{end:%Y%m%d}.html", mime="text/html",
                           help="Like the sheets on the board: every timeslot on every court is a line, filled in "
                                "where something's booked and blank where it's open, so privates and changes can "
                                "be written in. Two landscape pages a week, with each day's practice count and the "
                                "week's total at the bottom. Open it in your browser, then print or save as PDF.")
        d1.download_button("Download schedule (Excel)", lambda: to_excel(result, closures),
                           f"schedule_{start:%Y%m%d}-{end:%Y%m%d}.xlsx", width="stretch")
        d2.download_button("Printable weeks · color",
                           lambda: weeks_print_html(a, start, end, closures, hl or None, result.tournaments,
                                                    result.weekend_dates,
                                                    f"{st.session_state.get('project_name') or 'Court schedule'}",
                                                    week_privs, show_moved,
                                                    (lambda dd: open_slot_map(dd, cleaned)) if show_open else None,
                                                    show_lc).encode(),
                           f"court_weeks_{start:%Y%m%d}-{end:%Y%m%d}.html", mime="text/html", width="stretch",
                           help="Every week's court view in one page; each week prints on two landscape pages (courts 1-4, then 5-7 and the practice court). "
                                "With **Show open court times** on, every open court time prints with a line to "
                                "write a name in. Open it in your browser, then print or save as PDF.")
        st.download_button("Every team's dates (Excel)", lambda: team_dates_excel(
                               result, closures, team_order(), st.session_state.get("project_name") or "", team_sections()),
                           f"team_dates_{start:%Y%m%d}-{end:%Y%m%d}.xlsx", width="stretch",
                           help="One row per team (girls, then boys, each in the pecking order) with every practice (time and court) and "
                                "tournament date; tournament days are circled. Prints landscape on one page wide.")

# ------------------------------------------------------------ calendar --
if VIEW == TAB_CAL:
    if result is None:
        st.caption("No schedule yet.")
    else:
        a = result.assignments
        c1, c2 = st.columns([2, 1])
        hl = set(c1.multiselect("Only show a club, team or coach", people_options(a), key="cal_filter"))
        if MY_COACH and c2.toggle("Show only my practices", key="cal_mine",
                                  help="Your teams' practices and the programs you work."):
            hl |= {MY_COACH} | MY_TEAMS
        show_locals = c2.toggle("Include locals programs", value=True)
        cal_lc = c2.toggle("Show program coaches", key="cal_lc", disabled=not show_locals,
                           help="List the coaches working each locals program.") and show_locals
        show_moved = c2.toggle("Outline moved practices", key="show_moved",
                               help="Outline makeups and practices that aren't on the team's usual day "
                                    "or time.") if CAN else False
        st.markdown(month_calendar_html(a if show_moved else a.assign(on_pattern=True), start, end, closures, hl or None, show_locals, result.weekend_dates,
                                        result.tournaments, cal_lc), unsafe_allow_html=True)

        who = ("_" + "_".join(sorted(hl))) if hl else ""
        fname = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"calendar{who}_{start:%Y%m%d}-{end:%Y%m%d}")
        title = f"{st.session_state.get('project_name') or 'Practice calendar'}"
        st.caption("Download what's shown above" + (f" ({', '.join(sorted(hl))})" if hl else " (everyone)") + ":")
        d1, d2, d3 = st.columns(3)
        d1.download_button("Google Calendar (.ics)",
                           lambda: calendar_ics(result, hl or None, show_locals, title), f"{fname}.ics",
                           mime="text/calendar", width="stretch",
                           help="In Google Calendar: Settings → Import & export → Import. Tip: import into a new "
                                "calendar (e.g. 'Club practices') so it's easy to remove and re-import after changes.")
        d2.download_button("Printable calendar",
                           lambda: calendar_print_html(a, start, end, closures, hl or None, show_locals,
                                                       result.weekend_dates, result.tournaments, title,
                                                       cal_lc).encode(),
                           f"{fname}.html", mime="text/html", width="stretch",
                           help="Opens in your browser; print it or save it as a PDF. One month per page.")
        d3.download_button("Calendar (Excel)", lambda: calendar_excel(result, closures, hl or None, show_locals),
                           f"{fname}.xlsx", width="stretch",
                           help="The club's calendar layout: a block per week, each day with time · what · courts.")
        st.download_button("Every team's dates (Excel)", lambda: team_dates_excel(
                               result, closures, team_order(), st.session_state.get("project_name") or "", team_sections()),
                           f"team_dates_{start:%Y%m%d}-{end:%Y%m%d}.xlsx", key="team_dates_cal",
                           help="One row per team with every practice and tournament date; tournament days are "
                                "circled.")


result, start, end = _view_saved  # back to the dates being worked on

# ------------------------------------------------------ edit practices --
TEAM_SLOT_CHOICES = WEEKDAY_KEYS + WEEKEND_KEYS


def team_slot_label(k: str) -> str:
    return SLOTS[k] + (" (weekend)" if k in WEEKEND_KEYS else "")


def without_pin(ov: pd.DataFrame, team: str, d: date, slot: str) -> pd.DataFrame:
    """The overrides minus any hand 'Add' that placed this team's practice at d/slot, so a hand-placed
    practice can be moved or removed again (instead of the old Add fighting the new change)."""
    if ov.empty:
        return ov
    ds = pd.to_datetime(ov["date"].astype(str), errors="coerce").dt.date
    hit = ((ov["team"] == team) & ov["action"].astype(str).str.strip().str.lower().isin(["add", "extra"])
           & (ds == d) & (ov["time"].astype(str).str.strip() == slot))
    return ov[~hit].reset_index(drop=True)


def apply_overrides(rows: list[dict], message: str, backup: pd.DataFrame | None = None,
                    replace_pin: tuple | None = None):
    """Add manual-change rows and rebuild; rolled back if the rebuild can't honor them.
    replace_pin=(team, date, slot) first drops the hand 'Add' that put that practice there."""
    st.session_state.overrides_backup = inp.overrides.copy() if backup is None else backup
    base = without_pin(inp.overrides, *replace_pin) if replace_pin else inp.overrides
    inp.overrides = pd.concat([base, pd.DataFrame(rows)], ignore_index=True)
    st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
    st.session_state.resolve = True
    st.session_state.flash = message
    st.rerun()


def _pref_key(r: dict, who_col: str) -> tuple:
    return tuple(str(r.get(c, "") if not blank(r.get(c, "")) else "").strip()
                 for c in (who_col, "type" if who_col == "who" else "choice", "days", "dates", "times", "note"))


def change_prefs(team: str, add_wish: dict | None, add_avoid: dict | None, drop_wish: list, drop_avoid: list,
                 message: str, what: str):
    """Saves a team's preference change. Scheduling admins edit the open
    schedule and rebuild. A coach's change is applied to the latest saved
    copy (so nobody else's edits are overwritten), saved right away, and
    listed for the admins to fit in at the next Auto-schedule."""
    def apply(i: ScheduleInput):
        if add_wish:
            i.wishlist = pd.concat([i.wishlist, pd.DataFrame([add_wish])], ignore_index=True)
        if add_avoid:
            i.availability = pd.concat([i.availability, pd.DataFrame([add_avoid])], ignore_index=True)
        for attr, col, keys in (("wishlist", "team", drop_wish), ("availability", "who", drop_avoid)):
            df = getattr(i, attr).fillna("")
            left = list(keys)
            keep = []
            for idx, r in df.iterrows():
                k = _pref_key(r.to_dict(), col)
                if k in left:
                    left.remove(k)
                else:
                    keep.append(idx)
            setattr(i, attr, getattr(i, attr).loc[keep].reset_index(drop=True))
    if ME is not None:
        store.log(VIEWER.log_name, "Preference", f"{team}: {what}", st.session_state.project_id)
    if CAN:
        apply(inp)
        st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
        st.session_state.resolve = True
        st.session_state.flash = message + " Schedule rebuilt."
        st.rerun()
    pid = st.session_state.project_id
    rec = store.get(pid)
    if rec is None:
        st.error("This schedule was deleted.")
        return
    i_, s_, e_, settings_, r_ = read_payload(rec["payload"])
    apply(i_)
    settings_["pending_prefs"] = list(settings_.get("pending_prefs") or []) + [
        f"{VIEWER.who.label if VIEWER.who else 'A coach'} · {team}: {what}"]
    store.save(pid, rec["name"], make_payload(i_, s_, e_, settings_, r_, read_published(rec["payload"]),
                                              periods=rec["payload"].get("periods")))
    saved_changed()
    open_project(pid)
    st.session_state.flash = message + " The scheduling admins will fit it in at the next rebuild."
    st.rerun()


if VIEW == TAB_EDIT:
    if result is None or cleaned is None:
        st.caption("Build the schedule first.")
    else:
        if CAN:
            st.markdown("Change practices by hand. The scheduler **keeps these changes every time it rebuilds**, "
                        "and fits everything else around them.")
            team_names = [t.name for t in cleaned.teams]
        else:
            st.markdown("Tell the scheduler what your teams would rather have. Preferences save right away and "
                        "are weighed against every other team's at the next rebuild. Your own no-dates and "
                        "times are on the **Coaches** tab. To move, add or remove a "
                        "practice, ask a scheduling admin (requests in the app are coming next).")
            team_names = VIEWER.my_teams(cleaned.teams)
            if not team_names:
                st.info("Your login isn't linked to a team yet. Ask an app admin to link it to your coach name."
                        if not VIEWER.coach_name else f"{VIEWER.coach_name} isn't on any team in this schedule.")
                st.stop()
        e_team = st.selectbox("Team", team_names, key="ed_team")
        e_per_week = next((t.per_week for t in cleaned.teams if t.name == e_team), PRACTICES_PER_WEEK)
        a = result.assignments
        tp = a[(a["kind"] == "Practice") & (a["name"] == e_team)].copy()
        if len(tp):
            tp["o"] = tp["slot"].map(SLOT_KEYS.index)
            tp = tp.sort_values(["date", "o"])
        usual_rows = result.pattern[result.pattern["team"] == e_team] if len(result.pattern) else result.pattern
        usual_txt = ", ".join(f"{r.day} {r.slot}" for r in usual_rows.itertuples()) or "none"
        _hc = "home_court" if "home_court" in usual_rows.columns else "court"  # schedules saved before home_court
        home = f" · home Court {usual_rows[_hc].iloc[0]}" if len(usual_rows) else ""
        st.caption(f"Usual days: **{usual_txt}**{home} · **{plural(e_per_week, 'practice')} a week**")

        c_left, c_right = st.columns([3, 2])
        with c_left:
            st.markdown("**Practices**")
            if len(tp):
                _flag = lambda col: (tp[col] if col in tp else pd.Series(False, index=tp.index)).map(  # noqa: E731
                    lambda v: "Yes" if v == True else "")  # noqa: E712
                st.dataframe(pd.DataFrame({
                    "Date": tp["date"].map(lambda d: f"{d:%a %b %-d}"), "Time": tp["slot_label"],
                    "Court": tp["court"], "Extra": _flag("extra"),
                    "Paired": (tp["paired"] if "paired" in tp else pd.Series("", index=tp.index)).fillna(""),
                }), hide_index=True, width="stretch", height=min(420, 38 + 35 * len(tp)))
            else:
                st.caption("No practices in this schedule.")

        with c_right:
            mode = st.radio("What do you want to do?", ["Suggest a preference", "Move a practice", "Remove a practice",
                                                        "Add a practice", "Set usual days", "Practices per week"]
                            if CAN else ["Suggest a preference"],
                            key="ed_mode",
                            help="A preference is weighed against everyone else's and kept when possible. "
                                 "The other options pin a practice exactly where you put it.")
            labels = [f"{r.date:%a %b %-d} · {r.slot_label} · Court {r.court}" for r in tp.itertuples()]
            if mode in ("Move a practice", "Remove a practice"):
                if not labels:
                    st.caption("No practices to change.")
                else:
                    lab_i = st.selectbox("Practice", labels, key=f"ed_pick_{e_team}")
                    old = tp.iloc[labels.index(lab_i) if lab_i in labels else 0]
                    if mode == "Move a practice":
                        pk = f"{e_team}_{old['date']}_{old['slot']}"  # fields reset to the chosen practice
                        nd = st.date_input("New date", value=old["date"], min_value=start, max_value=end,
                                           format="MM/DD/YYYY", key=f"ed_nd_{pk}")
                        ns = st.selectbox("New time", TEAM_SLOT_CHOICES, format_func=team_slot_label,
                                          index=TEAM_SLOT_CHOICES.index(old["slot"]) if old["slot"] in TEAM_SLOT_CHOICES else 0,
                                          key=f"ed_ns_{pk}")
                        nc = st.selectbox("Court", ["Any"] + COURTS, key=f"ed_nc_{pk}",
                                          format_func=lambda c: "Any open court" if c == "Any" else f"Court {c}")
                        same = nd == old["date"] and ns == old["slot"]
                        same_court = same and (nc == "Any" or str(nc) == str(old["court"]))
                        if st.button("Move it", type="primary", width="stretch", disabled=same_court,
                                     help="Pick a different date, time or court first." if same_court else None):
                            where = f" at {ns}" + ("" if nc == "Any" else f", Court {nc}")
                            apply_overrides([
                                {"team": e_team, "action": "Remove", "date": f"{old['date']:%m/%d/%Y}", "days": "",
                                 "time": old["slot"], "court": "", "note": f"moved to {nd:%a %-m/%-d} {ns}"},
                                {"team": e_team, "action": "Add", "date": f"{nd:%m/%d/%Y}", "days": "", "time": ns,
                                 "court": "" if nc == "Any" else str(nc), "note": f"moved from {old['date']:%a %-m/%-d}"},
                            ], f"Moved {e_team} from {old['date']:%a %b %-d} {old['slot']} to {nd:%a %b %-d}{where}.",
                                replace_pin=(e_team, old["date"], old["slot"]))
                    else:
                        how = st.radio("Then", ["Find a makeup that week if possible", "Cancel it (no makeup)"],
                                       key="ed_how")
                        if st.button("Remove it", type="primary", width="stretch"):
                            act = "Remove" if how.startswith("Find") else "Cancel"
                            apply_overrides([{"team": e_team, "action": act, "date": f"{old['date']:%m/%d/%Y}",
                                              "days": "", "time": old["slot"], "court": "", "note": "removed by hand"}],
                                            f"{'Removed' if act == 'Remove' else 'Cancelled'} {e_team}'s "
                                            f"{old['date']:%a %b %-d} practice.",
                                            replace_pin=(e_team, old["date"], old["slot"]))
            elif mode == "Suggest a preference":
                st.caption("Tell the scheduler what this team would rather have. It's weighed against every other "
                           "team's requests, so it's kept when it fits, not forced.")
                pk = f"pref_{e_team}"
                p_days = st.multiselect("Would rather practice on", WEEKDAYS, key=f"{pk}_pd",
                                        placeholder="Any day")
                p_times = st.multiselect("at", WEEKDAY_KEYS, format_func=lambda k: SLOTS[k], key=f"{pk}_pt",
                                         placeholder="Any time")
                p_rank = st.radio("How much", ["1st choice", "2nd choice"], horizontal=True, key=f"{pk}_pr")
                a_days = st.multiselect("Would rather not practice on", WEEKDAYS, key=f"{pk}_ad",
                                        placeholder="Any day")
                a_times = st.multiselect("at ", WEEKDAY_KEYS, format_func=lambda k: SLOTS[k], key=f"{pk}_at",
                                         placeholder="Any time")
                p_note = st.text_input("Note (optional)", key=f"{pk}_note", placeholder="e.g. coach works until 4")
                want = bool(p_days or p_times)
                avoid = bool(a_days or a_times)
                if st.button("Save preference", type="primary", width="stretch", disabled=not (want or avoid)):
                    bits, wrow, arow = [], None, None
                    if want:
                        wrow = {c: "" for c in COLUMNS["Wishlist"]}
                        wrow.update({"team": e_team, "choice": 1 if p_rank.startswith("1") else 2,
                                     "days": ", ".join(p_days), "times": ", ".join(p_times), "note": p_note})
                        bits.append(f"prefers {' / '.join(p_days) or 'any day'} {', '.join(p_times)}".strip())
                    if avoid:
                        arow = {c: "" for c in COLUMNS["Availability"]}
                        arow.update({"who": e_team, "type": "Not preferred", "days": ", ".join(a_days),
                                     "times": ", ".join(a_times), "note": p_note})
                        bits.append(f"would rather not have {' / '.join(a_days) or 'any day'} "
                                    f"{', '.join(a_times)}".strip())
                    change_prefs(e_team, wrow, arow, [], [], f"Saved: {e_team} " + " and ".join(bits) + ".",
                                 " and ".join(bits))

                # This team's current preferences, removable here.
                wl = inp.wishlist.fillna("")
                wl = wl[wl["team"] == e_team]
                av = inp.availability.fillna("")
                av = av[(av["who"] == e_team) & av["type"].astype(str).str.lower().str.startswith("not")]
                prefs = {("w", i): f"Prefers ({'1st' if str(r['choice']).startswith('1') else '2nd'}): "
                                   f"{r['days'] or 'any day'} {r['times'] or 'any time'}" for i, r in wl.iterrows()}
                prefs.update({("a", i): f"Rather not: {r['days'] or 'any day'} {r['times'] or 'any time'}"
                              for i, r in av.iterrows()})
                if prefs:
                    st.markdown("**Current preferences**")
                    for lab in prefs.values():
                        st.caption("• " + lab)
                    drop = st.multiselect("Remove", list(prefs), format_func=lambda k: prefs[k], key=f"{pk}_drop")
                    if st.button("Remove selected", disabled=not drop, key=f"{pk}_dropbtn"):
                        change_prefs(e_team, None, None,
                                     [_pref_key(wl.loc[i].to_dict(), "team") for k, i in drop if k == "w"],
                                     [_pref_key(av.loc[i].to_dict(), "who") for k, i in drop if k == "a"],
                                     f"Removed {plural(len(drop), 'preference')} for {e_team}.",
                                     "removed " + "; ".join(prefs[k] for k in drop))
            elif mode == "Add a practice":
                ad = st.date_input("Date", value=start, min_value=start, max_value=end, format="MM/DD/YYYY", key="ed_ad")
                as_ = st.selectbox("Time", TEAM_SLOT_CHOICES, format_func=team_slot_label, index=1, key="ed_as")
                ac = st.selectbox("Court", ["Any"] + COURTS, key="ed_ac",
                                  format_func=lambda c: "Any open court" if c == "Any" else f"Court {c}")
                how_add = st.radio("This practice is", [f"Extra (on top of the week's {e_per_week})",
                                                        "Instead of one of the week's practices"], key="ed_add_how",
                                   help="Extra: the team keeps its usual practices and gets this one too. "
                                        "Instead: the scheduler drops another practice that week to make room.")
                extra = how_add.startswith("Extra")
                if st.button("Add it", type="primary", width="stretch"):
                    apply_overrides([{"team": e_team, "action": "Extra" if extra else "Add", "date": f"{ad:%m/%d/%Y}",
                                      "days": "", "time": as_, "court": "" if ac == "Any" else str(ac),
                                      "note": "extra practice" if extra else "added by hand"}],
                                    f"Added {'an extra' if extra else 'a'} practice for {e_team} on {ad:%a %b %-d} at {as_}.")
            elif mode == "Practices per week":
                st.caption("For a team that wants more (or fewer) practices every week. The scheduler gives the "
                           "first two the usual priority; a 3rd or more is added wherever there's room without "
                           "breaking anyone's no-dates. For a single extra practice, use **Add a practice**.")
                n_pw = st.number_input("Practices a week", 1, MAX_PRACTICES_PER_WEEK, value=e_per_week,
                                       key=f"ed_pw_{e_team}")
                if st.button("Save", type="primary", width="stretch", disabled=n_pw == e_per_week):
                    t = inp.teams.copy()
                    t["practices"] = t["practices"].astype(object) if "practices" in t.columns else ""
                    t.loc[t["team"].astype(str).str.strip() == e_team, "practices"] = (
                        "" if n_pw == PRACTICES_PER_WEEK else str(int(n_pw)))
                    inp.teams = t
                    st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
                    st.session_state.resolve = True
                    st.session_state.flash = f"{e_team} now has {plural(int(n_pw), 'practice')} a week. Schedule rebuilt."
                    st.rerun()
            else:
                n_ud = min(e_per_week, len(WEEKDAYS))
                ud = st.multiselect(f"Days (pick {n_ud})", WEEKDAYS, max_selections=n_ud, key=f"ed_ud_{e_team}",
                                    default=[r.day for r in usual_rows.itertuples()][:n_ud])
                us = st.selectbox("Time", WEEKDAY_KEYS, format_func=lambda k: SLOTS[k], key=f"ed_us_{e_team}",
                                  index=WEEKDAY_KEYS.index(usual_rows["slot"].iloc[0])
                                  if len(usual_rows) and usual_rows["slot"].iloc[0] in WEEKDAY_KEYS else 1)
                uc = st.selectbox("Home court", ["Any"] + COURTS, key=f"ed_uc_{e_team}",
                                  format_func=lambda c: "Scheduler picks" if c == "Any" else f"Court {c}")
                if st.button("Set usual days", type="primary", width="stretch", disabled=not ud):
                    keep = inp.overrides[~((inp.overrides["team"] == e_team) &
                                           inp.overrides["action"].astype(str).str.lower().str.startswith("usual"))]
                    original = inp.overrides.copy()
                    inp.overrides = keep.reset_index(drop=True)
                    apply_overrides([{"team": e_team, "action": "Usual", "date": "", "days": ", ".join(ud),
                                      "time": us, "court": "" if uc == "Any" else str(uc), "note": "set by hand"}],
                                    f"{e_team} now practices {' & '.join(ud)} at {us}.", backup=original)

        if CAN:
            st.subheader("Manual changes")
        ov = inp.overrides.fillna("")
        if not CAN:
            pass
        elif ov.empty:
            st.caption("None yet.")
        else:
            show_all = st.toggle("Show all teams", value=False, key="ed_all")
            view = ov if show_all else ov[ov["team"] == e_team]
            if view.empty:
                st.caption(f"No manual changes for {e_team}.")
            else:
                lab = {i: f"{r['team']} · {r['action']} · " + " ".join(x for x in (r["date"], r["days"], r["time"],
                       f"Court {r['court']}" if str(r["court"]).strip() else "") if str(x).strip())
                       for i, r in view.iterrows()}
                st.dataframe(view.rename(columns=str.title), hide_index=True, width="stretch")
                undo = st.multiselect("Undo changes", list(lab), format_func=lambda i: lab[i], key="ed_undo")
                if st.button("Undo selected", disabled=not undo):
                    inp.overrides = inp.overrides.drop(index=undo).reset_index(drop=True)
                    st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
                    st.session_state.resolve = True
                    st.session_state.flash = f"Undid {plural(len(undo), 'change')}. Schedule rebuilt."
                    st.rerun()

# ------------------------------------------------------------ programs --
SLOT_CHOICES = PROGRAM_KEYS + WEEKDAY_KEYS + WEEKEND_KEYS


def slot_label(k: str) -> str:
    kind = "JumpStart" if k in PROGRAM_KEYS else "Weekday" if k in WEEKDAY_KEYS else "Weekend"
    return f"{kind} · {SLOTS[k]}"


SIDES = ["Girls", "Boys", "Other"]


def program_defaults(row: dict | None) -> dict:
    """Form values for a new program (row=None) or an existing Locals row."""
    if row is None:
        return {"name": "", "side": "Girls", "on_site": True, "days": [], "every_week": False,
                "p_start": start, "p_end": end, "times": [], "courts": [], "coaches_sel": [], "coaches_new": "",
                "tourns": []}
    yr = start.year
    try:
        ranges = parse_date_ranges(row.get("dates"), yr)
    except ValueError:
        ranges = []
    try:
        times = sorted(parse_times(row.get("time")) or [], key=SLOT_KEYS.index)
    except ValueError:
        times = []
    try:
        courts = parse_courts(row.get("courts"))
    except ValueError:
        courts = []
    names = parse_names(row.get("coaches"))
    known = set(cleaned.coaches) if cleaned else set()
    side = program_side(row.get("side"), str(row.get("program", ""))) or "Other"
    on = str(row.get("on_site", "")).strip().lower()
    return {
        "name": str(row.get("program", "")), "side": side,
        "on_site": on in ("", "yes", "y", "true", "1", "nan", "none"),
        "days": [d for d in parse_days(row.get("days")) if d in DAYS],
        "every_week": not ranges,
        "p_start": ranges[0][0] if ranges else start, "p_end": ranges[0][1] if ranges else end,
        "times": [t for t in times if t in SLOT_CHOICES], "courts": courts,
        "coaches_sel": [n for n in names if n in known],
        "coaches_new": ", ".join(n for n in names if n not in known),
        "tourns": program_tournaments(str(row.get("program", ""))),
    }


TOURN_COLS = ["date", "last_day", "name", "courts"]


def _is_program_tourn(df: pd.DataFrame, program: str) -> pd.Series:
    """Local tournaments that belong to a program (its tournament dates)."""
    if df.empty or not program:
        return pd.Series(False, index=df.index)
    return (df["type"].astype(str).str.strip().str.lower().str.startswith("loc")
            & (df["program"].fillna("").astype(str).str.strip().str.lower() == program.strip().lower()))


def program_tournaments(program: str) -> list[dict]:
    """A program's local tournaments, as rows for its form's tournament-dates table."""
    tdf = inp.tournaments.fillna("")
    out = []
    for _, r in tdf[_is_program_tourn(tdf, program)].iterrows():
        try:
            a = parse_date_ranges(r["start_date"], start.year)[0][0]
            b = a if blank(r["end_date"]) else parse_date_ranges(r["end_date"], start.year)[0][0]
        except (ValueError, IndexError):
            continue
        out.append({"date": a, "last_day": None if b == a else b, "name": str(r["name"]),
                    "courts": str(r["courts"]).strip()})
    return out


def tournament_rows(program: str, table: pd.DataFrame) -> tuple[list[str], list[dict]]:
    """Tournaments rows from a program form's tournament-dates table."""
    problems, rows = [], []
    for i, r in table.reset_index(drop=True).iterrows():
        d = r.get("date")
        if d is None or pd.isna(d):
            if not blank(r.get("name")) or not blank(r.get("courts")):
                problems.append(f"Tournament row {i + 1} needs a date.")
            continue
        d = pd.Timestamp(d).date()
        last = r.get("last_day")
        last = d if last is None or pd.isna(last) else pd.Timestamp(last).date()
        if last < d:
            problems.append(f"Tournament on {d:%b %-d}: the last day is before the first.")
            continue
        ct = "" if blank(r.get("courts")) else str(r["courts"]).strip()
        try:
            cl = parse_courts(ct) if ct else []
        except ValueError as e:
            problems.append(f"Tournament on {d:%b %-d}: {e}")
            continue
        rows.append({"name": (str(r["name"]).strip() if not blank(r.get("name")) else "") or f"{program} Tournament",
                     "type": "Local", "start_date": f"{d:%m/%d/%Y}", "end_date": f"{last:%m/%d/%Y}",
                     "club": "", "ages": "", "teams": "", "program": program,
                     "courts": ", ".join("P" if c not in COURTS else str(c) for c in sorted(cl)), "coaches": "",
                     "note": ""})
    return problems, rows


def program_form(key: str, dv: dict, submit_labels: list[str]):
    """Renders the program form; returns (clicked label or None, values)."""
    known_coaches = cleaned.coaches if cleaned else []
    with st.form(key, clear_on_submit=key == "add_program"):
        c1, c2, c3 = st.columns([2, 1, 1])
        name = c1.text_input("Program name", value=dv["name"], placeholder="e.g. G 14 League or JumpStart")
        side = c2.radio("Side", SIDES, index=SIDES.index(dv["side"]), horizontal=True,
                        help="Girls programs show in blue, boys in green.")
        on_site = c3.checkbox("Uses our courts", value=dv["on_site"],
                              help="Uncheck for programs somewhere else (e.g. a high school team). "
                                   "Those only block the coaches.")
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        days = c1.multiselect("Days", DAYS, default=dv["days"], placeholder="e.g. Tue, Thu")
        every = c2.checkbox("Every week", value=dv["every_week"], help="No start or end date.")
        p_start = c3.date_input("Start date", value=dv["p_start"], format="MM/DD/YYYY")
        p_end = c4.date_input("End date", value=dv["p_end"], format="MM/DD/YYYY")
        c1, c2 = st.columns(2)
        times = c1.multiselect("Time", SLOT_CHOICES, default=dv["times"], format_func=slot_label,
                               help="Pick every timeslot it uses (e.g. 3:45 and 5:30 for a 3:45–7:20 program). "
                                    "JumpStart times (3:30–4:30, 4:30–5:30) block 3:45 practices on those courts.")
        courts = c2.multiselect("Courts used", ALL_COURTS, default=[c for c in dv["courts"] if c in ALL_COURTS],
                                format_func=court_name,
                                help="Remove a court here if sign-ups are low. Leave empty if not known yet "
                                     "(it then blocks only the coaches). The practice court is for programs "
                                     "only; team practices never use it.")
        c1, c2 = st.columns(2)
        coaches_sel = c1.multiselect("Coaches working it", known_coaches,
                                     default=[c for c in dv["coaches_sel"] if c in known_coaches])
        coaches_new = c2.text_input("Other coaches", value=dv["coaches_new"], placeholder="Names separated by commas")
        st.markdown("**Local tournament dates**")
        st.caption("Tournaments this program hosts here. Each row is one tournament (add as many as you like, "
                   "several on the same day is fine). They take their courts and this program's coaches until "
                   "7 PM. Courts like `1-4` or `1, 2, P`; blank = courts 1–7.")
        tdf = pd.DataFrame(dv["tourns"], columns=TOURN_COLS)
        for c in ("date", "last_day"):
            tdf[c] = pd.to_datetime(tdf[c], errors="coerce")
        tdf["name"] = tdf["name"].astype(object).fillna("")
        tdf["courts"] = tdf["courts"].astype(object).fillna("")
        tourns = st.data_editor(
            tdf, num_rows="dynamic", hide_index=True, width="stretch", key=f"{key}_tourns",
            column_config={
                "date": st.column_config.DateColumn("Date", format="MM/DD/YYYY", required=True),
                "last_day": st.column_config.DateColumn("Last day (if more than one)", format="MM/DD/YYYY"),
                "name": st.column_config.TextColumn("Name (optional)", help="Blank: “<program> Tournament”"),
                "courts": st.column_config.TextColumn("Courts", help="Like 1-4 or 1, 2, P. Blank = courts 1-7.")})
        cols = st.columns(len(submit_labels) + 3)
        clicked = None
        for i, lab in enumerate(submit_labels):
            if cols[i].form_submit_button(lab, type="primary" if i == 0 else "secondary", width="stretch"):
                clicked = lab
    return clicked, dict(name=name, side=side, on_site=on_site, days=days, every=every, p_start=p_start,
                         p_end=p_end, times=times, courts=courts, coaches_sel=coaches_sel, coaches_new=coaches_new,
                         tourns=tourns)


def program_row(v: dict) -> tuple[list[str], dict]:
    problems = []
    if not v["name"].strip():
        problems.append("Give the program a name.")
    if not v["days"]:
        problems.append("Pick at least one day.")
    if not v["times"]:
        problems.append("Pick at least one timeslot.")
    if not v["every"] and v["p_end"] < v["p_start"]:
        problems.append("The end date is before the start date.")
    for d in v["days"]:
        ok = [t for t in v["times"] if (t in WEEKEND_KEYS) == (d in ("Sat", "Sun"))]
        if v["times"] and not ok:
            problems.append(f"{d} needs a {'weekend' if d in ('Sat', 'Sun') else 'weekday'} timeslot.")
    row = {
        "program": v["name"].strip(), "side": "" if v["side"] == "Other" else v["side"],
        "days": ", ".join(d for d in DAYS if d in v["days"]),
        "time": ", ".join(sorted(v["times"], key=SLOT_KEYS.index)),
        "courts": ", ".join("P" if c not in COURTS else str(c) for c in sorted(v["courts"])) or "TBD",
        "coaches": "; ".join(dict.fromkeys(v["coaches_sel"] + parse_names(v["coaches_new"]))),
        "on_site": "Yes" if v["on_site"] else "No",
        "dates": "" if v["every"] else f"{v['p_start']:%m/%d/%Y}-{v['p_end']:%m/%d/%Y}",
    }
    return problems, row


def try_locals(new_locals: pd.DataFrame, what: str, new_tourn: pd.DataFrame | None = None) -> bool:
    tourn = inp.tournaments if new_tourn is None else new_tourn
    trial = ScheduleInput(inp.teams, inp.availability, inp.wishlist, new_locals, inp.weekends, inp.closures,
                          tournaments=tourn, coaches=inp.coaches, overrides=inp.overrides)
    try:
        clean(trial, start, end)
    except ValueError as e:
        st.error(f"Couldn't {what}: {e}")
        return False
    inp.locals = new_locals.reset_index(drop=True)
    if new_tourn is not None:
        inp.tournaments = new_tourn.reset_index(drop=True)
    st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
    if st.session_state.get("prog_autobuild", True):
        st.session_state.resolve = True
    else:
        st.session_state.pending_rebuild = "Programs changed"
    return True


def replace_program_tournaments(old_name: str, new_rows: list[dict]) -> pd.DataFrame:
    """The Tournaments table with a program's local tournaments swapped for new_rows."""
    keep = inp.tournaments[~_is_program_tourn(inp.tournaments, old_name)] if old_name else inp.tournaments
    return pd.concat([keep, pd.DataFrame(new_rows, columns=COLUMNS["Tournaments"])], ignore_index=True)


def done_msg(what: str) -> str:
    return f"{what}. " + ("Schedule rebuilt." if st.session_state.get("prog_autobuild", True)
                          else "Press Auto-schedule when you're done to rebuild.")


if VIEW == TAB_PROG:
    st.markdown("Programs (locals, JumpStart, leagues…) are blocked off on their courts, and the practice "
                "schedule is rebuilt around them when you save.")
    flags_panel("program", key="flags_prog", expanded=True)
    st.toggle("Rebuild the schedule each time I save a program", value=True, key="prog_autobuild",
              help="Turn off to change several programs in a row, then press Auto-schedule once at the end.")
    ver = st.session_state.get("locals_ver", 0)

    # ---- edit an existing program
    if not inp.locals.empty:
        st.subheader("Edit a program")
        labels = [f"{r['program']} · {r['days']} · {r['time']}" + (f" · {r['dates']}" if str(r["dates"]).strip() else "")
                  for _, r in inp.locals.fillna("").iterrows()]
        labels = [lab if labels.count(lab) == 1 else f"{lab} (#{i + 1})" for i, lab in enumerate(labels)]
        pick_lab = st.selectbox("Program", labels, index=None,
                                placeholder="Choose a program to change (e.g. drop a court)", key=f"edit_pick_{ver}")
        pick = labels.index(pick_lab) if pick_lab in labels else None
        if pick is not None:
            row = inp.locals.fillna("").iloc[pick].to_dict()
            clicked, v = program_form(f"edit_program_{pick}_{ver}", program_defaults(row),
                                      ["Save changes", "Delete program"])
            old_name = str(row["program"]).strip()
            others = (inp.locals["program"].astype(str).str.strip() == old_name).sum() > 1  # other rows, same program
            if clicked == "Delete program":
                n_t = 0 if others else int(_is_program_tourn(inp.tournaments, old_name).sum())
                if try_locals(inp.locals.drop(inp.locals.index[pick]), "delete it",
                              replace_program_tournaments(old_name, []) if n_t else None):
                    st.session_state.flash = done_msg(f"Deleted {row['program']}"
                                                      + (f" and its {plural(n_t, 'tournament')}" if n_t else ""))
                    st.rerun()
            elif clicked:
                problems, new_row = program_row(v)
                t_problems, t_rows = tournament_rows(new_row["program"], v["tourns"])
                if problems or t_problems:
                    st.error(" ".join(problems + t_problems))
                else:
                    nl = inp.locals.copy()
                    for k, val in new_row.items():
                        nl.at[nl.index[pick], k] = val
                    if new_row["program"] != old_name and others:
                        # renaming one row of a program: keep its tournaments with the other rows
                        new_t = replace_program_tournaments("", t_rows) if t_rows else None
                    else:
                        new_t = replace_program_tournaments(old_name, t_rows)
                    if try_locals(nl, "save the changes", new_t):
                        st.session_state.flash = done_msg(f"Updated {new_row['program']}"
                                                          + (f" ({plural(len(t_rows), 'tournament date')})"
                                                             if t_rows else ""))
                        st.rerun()

    # ---- one session more or less
    if not inp.locals.empty and cleaned is not None:
        st.subheader("Add or cancel one session")
        st.caption("For a single week: cancel one session (e.g. a holiday or low sign-ups) or add one extra. "
                   "The rest of the program stays as it is.")
        prog_names = list(dict.fromkeys(str(n).strip() for n in inp.locals["program"] if not blank(n)))
        c1, c2 = st.columns([2, 1])
        one_prog = c1.selectbox("Program", prog_names, key=f"one_prog_{ver}")
        one_mode = c2.radio("Change", ["Cancel one session", "Add one session"], key=f"one_mode_{ver}",
                            horizontal=True)
        rows_of = inp.locals.index[inp.locals["program"].astype(str).str.strip() == one_prog].tolist()
        lps = [lp for lp in cleaned.locals if lp.name == one_prog]
        if one_mode == "Cancel one session":
            run_dates = sorted({d for d in pd.date_range(start, end).date for lp in lps if lp.runs_on(d)
                                and d not in closures})
            if not run_dates:
                st.caption(f"{one_prog} has no sessions left in these dates.")
            else:
                c_day = st.selectbox("Session", run_dates, format_func=lambda d: f"{d:%a %b %-d}", key=f"one_cancel_{ver}")
                if st.button("Cancel this session", type="primary", key=f"one_cancel_btn_{ver}"):
                    nl = inp.locals.copy()
                    nl["skip"] = nl["skip"].astype(object) if "skip" in nl.columns else ""
                    for i in rows_of:
                        if DAYS[c_day.weekday()] in parse_days(nl.at[i, "days"]):
                            old = "" if blank(nl.at[i, "skip"]) else str(nl.at[i, "skip"]).strip()
                            nl.at[i, "skip"] = "; ".join(x for x in (old, f"{c_day:%m/%d/%Y}") if x)
                    if try_locals(nl, "cancel that session"):
                        st.session_state.flash = done_msg(f"Cancelled {one_prog} on {c_day:%a %b %-d}")
                        st.rerun()
        else:
            dv = program_defaults(inp.locals.fillna("").loc[rows_of[0]].to_dict()) if rows_of else program_defaults(None)
            c1, c2, c3 = st.columns(3)
            a_day = c1.date_input("Date", value=start, min_value=start, max_value=end, format="MM/DD/YYYY",
                                  key=f"one_add_day_{ver}")
            wkend = a_day.weekday() >= 5
            t_opts = [t for t in SLOT_CHOICES if (t in WEEKEND_KEYS) == wkend]
            a_times = c2.multiselect("Time", t_opts, default=[t for t in dv["times"] if t in t_opts],
                                     format_func=slot_label, key=f"one_add_t_{ver}_{wkend}")
            a_courts = c3.multiselect("Courts", ALL_COURTS, default=[c for c in dv["courts"] if c in ALL_COURTS],
                                      format_func=court_name, key=f"one_add_c_{ver}")
            if st.button("Add this session", type="primary", disabled=not a_times, key=f"one_add_btn_{ver}"):
                base = inp.locals.fillna("").loc[rows_of[0]].to_dict() if rows_of else {}
                row = {c: base.get(c, "") for c in COLUMNS["Locals"]}
                row.update({"days": DAYS[a_day.weekday()], "time": ", ".join(sorted(a_times, key=SLOT_KEYS.index)),
                            "courts": ", ".join("P" if c not in COURTS else str(c) for c in sorted(a_courts)) or "TBD",
                            "dates": f"{a_day:%m/%d/%Y}", "skip": ""})
                if try_locals(pd.concat([inp.locals, pd.DataFrame([row])], ignore_index=True), "add that session"):
                    st.session_state.flash = done_msg(f"Added a {one_prog} session on {a_day:%a %b %-d}")
                    st.rerun()
        skipped = [(i, d) for i in rows_of if "skip" in inp.locals.columns and not blank(inp.locals.at[i, "skip"])
                   for d in str(inp.locals.at[i, "skip"]).split(";") if d.strip()]
        if skipped:
            labels_sk = {f"{i}|{d.strip()}": f"{d.strip()} ({inp.locals.at[i, 'days']})" for i, d in skipped}
            undo_sk = st.multiselect(f"Cancelled {one_prog} sessions · pick any to bring back", list(labels_sk),
                                     format_func=lambda k: labels_sk[k], key=f"one_undo_{ver}")
            if st.button("Bring back", disabled=not undo_sk, key=f"one_undo_btn_{ver}"):
                nl = inp.locals.copy()
                for k in undo_sk:
                    i, d = k.split("|", 1)
                    i = int(i)
                    nl.at[i, "skip"] = "; ".join(x.strip() for x in str(nl.at[i, "skip"]).split(";")
                                                 if x.strip() and x.strip() != d)
                if try_locals(nl, "bring those sessions back"):
                    st.session_state.flash = done_msg(f"Brought back {plural(len(undo_sk), 'session')} of {one_prog}")
                    st.rerun()

    # ---- add a program
    st.subheader("Add a program")
    clicked, v = program_form("add_program", program_defaults(None), ["Add program"])
    if clicked:
        problems, row = program_row(v)
        t_problems, t_rows = tournament_rows(row["program"], v["tourns"])
        if problems or t_problems:
            st.error(" ".join(problems + t_problems))
        elif try_locals(pd.concat([inp.locals, pd.DataFrame([row])], ignore_index=True), f"add {row['program']}",
                        replace_program_tournaments("", t_rows) if t_rows else None):
            when = "every week" if v["every"] else f"{v['p_start']:%b %-d}–{v['p_end']:%b %-d}"
            st.session_state.flash = done_msg(f"Added {row['program']} ({row['days']}, {when})"
                                              + (f" with {plural(len(t_rows), 'tournament date')}" if t_rows else ""))
            st.rerun()

    # ---- list
    st.subheader("Current programs")
    if inp.locals.empty:
        st.caption("No programs yet.")
    else:
        view = inp.locals.copy().fillna("")
        # Show the side the colors actually use (guessed from the name when blank).
        view["side"] = [program_side(s, n) or "Other" for s, n in zip(view["side"], view["program"])]
        st.dataframe(view.rename(columns={"program": "Program", "side": "Side", "days": "Days", "time": "Time",
                                          "courts": "Courts", "coaches": "Coaches", "on_site": "On site",
                                          "dates": "Dates", "skip": "Cancelled"}),
                     hide_index=True, width="stretch")

# --------------------------------------------------------- tournaments --
if VIEW == TAB_TOURN:
    st.markdown("**Local** tournaments are hosted here and take over their courts until 7 PM (7:20 practices still "
                "happen), along with the coaches working them. "
                "**Travel** tournaments don't use our courts, but the teams going can't practice those days, "
                "avoid the days right before and after, and get priority for their two practices that week.")
    team_list = cleaned.teams if cleaned else []
    clubs = sorted({t.club for t in team_list if t.club})
    ages_all = sorted({t.age for t in team_list})
    program_names = sorted({lp.name for lp in cleaned.locals}) if cleaned else []

    # Add a new tournament, or pick one to edit (the form fills in with it).
    _trows = inp.tournaments.fillna("")
    _tlab = [f"{r['name']} · {r['type']} · {r['start_date']}" for _, r in _trows.iterrows()]
    _tlab = [lab if _tlab.count(lab) == 1 else f"{lab} (#{i + 1})" for i, lab in enumerate(_tlab)]
    t_sel = st.selectbox("Tournament", ["__new__"] + list(range(len(_trows))),
                         format_func=lambda o: "➕ New tournament" if o == "__new__" else f"Edit: {_tlab[o]}",
                         key=f"t_sel_{len(_trows)}")
    editing = t_sel != "__new__"
    cur_t = _trows.iloc[t_sel] if editing else None
    sfx = f"_{t_sel}_{st.session_state.get('locals_ver', 0)}" if editing else ""

    def _tval(col, default=""):
        return str(cur_t[col]).strip() if editing and not blank(cur_t[col]) else default

    def _tdate(col, default):
        try:
            return to_date(_tval(col), start.year) if _tval(col) else default
        except (ValueError, TypeError):
            return default

    with st.container(border=True):
        c1, c2 = st.columns([2, 1])
        t_name = c1.text_input("Tournament name", value=_tval("name"), placeholder="e.g. Boys 14 Local Tournament",
                               key=f"t_name{sfx}")
        _kinds = ["Local", "Travel"]
        t_kind = c2.radio("Type", _kinds, horizontal=True, key=f"t_kind{sfx}",
                          index=1 if _tval("type").lower().startswith(("trav", "away")) else 0)
        c1, c2 = st.columns(2)
        t_start = c1.date_input("Start date", value=_tdate("start_date", start), format="MM/DD/YYYY",
                                key=f"t_start{sfx}")
        t_end = c2.date_input("End date", value=_tdate("end_date", t_start), format="MM/DD/YYYY", key=f"t_end{sfx}")
        if t_kind == "Local":
            c1, c2 = st.columns(2)
            _prog_opts = ["(none)"] + program_names + ([_tval("program")] if _tval("program") and
                                                       _tval("program") not in program_names else [])
            t_prog = c1.selectbox("Locals program", _prog_opts, key=f"t_prog{sfx}",
                                  index=_prog_opts.index(_tval("program")) if _tval("program") in _prog_opts else 0,
                                  help="Its coaches are blocked for the day.")
            _c_def = parse_courts(_tval("courts")) if _tval("courts") else list(COURTS)
            t_courts = c2.multiselect("Courts used", ALL_COURTS, default=[c for c in _c_def if c in ALL_COURTS],
                                      key=f"t_courts{sfx}", format_func=court_name)
            prog_coaches = sorted({c for lp in (cleaned.locals if cleaned else []) if lp.name == t_prog
                                   for c in lp.coaches})
            _all_c = cleaned.coaches if cleaned else []
            _c_now = parse_names(_tval("coaches")) if editing else prog_coaches
            t_coaches = st.multiselect("Coaches working it", sorted(set(_all_c) | set(_c_now)),
                                       default=[c for c in _c_now if c in set(_all_c) | set(_c_now)],
                                       key=f"t_coaches_{t_prog}{sfx}",
                                       help="Busy until 7 PM that day. A coach also going to a travel tournament "
                                            "then gets flagged.")
            t_club, t_ages, t_teams = "", [], []
        else:
            c1, c2 = st.columns(2)
            _club_opts = ["Any"] + clubs
            t_club = c1.selectbox("Club", _club_opts, key=f"t_club{sfx}",
                                  index=_club_opts.index(_tval("club")) if _tval("club") in _club_opts else 0)
            _ages_now = [a for a in ages_all if str(a) in re.findall(r"\d+", _tval("ages"))] if editing else []
            t_ages = c2.multiselect("Age groups", ages_all, default=_ages_now, key=f"t_ages{sfx}",
                                    format_func=lambda a: f"{a}s")
            match = [] if (t_club == "Any" and not t_ages) else [
                t.name for t in team_list if (t_club == "Any" or t.club == t_club) and (not t_ages or t.age in t_ages)]
            _names = [t.name for t in team_list]
            _listed = [n for n in parse_list(_tval("teams")) if n in _names] if editing else []
            if editing and not _listed:  # saved by club + ages: the teams that matched
                _listed = next((tn.teams for tn in (cleaned.tournaments if cleaned else [])
                                if tn.name == _tval("name") and tn.kind == "travel"), [])
            t_teams = st.multiselect("Teams going", _names, default=_listed if editing else match,
                                     key=f"t_teams_{t_club}_{'-'.join(map(str, t_ages))}{sfx}",
                                     help="Filled in from the club and ages; remove teams that aren't going.")
            t_prog, t_courts, t_coaches = "(none)", [], []
        if editing:
            c1, c2, c3 = st.columns([2, 2, 1])
            add_t = c1.button("Save changes", type="primary", width="stretch")
            rm_t = c3.button("Delete", width="stretch", key="t_delete")
        else:
            add_t, rm_t = st.button("Add tournament", type="primary"), False

    if rm_t:
        inp.tournaments = inp.tournaments.drop(inp.tournaments.index[t_sel]).reset_index(drop=True)
        st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
        st.session_state.resolve = True
        st.session_state.flash = f"Deleted {_tval('name')}. Schedule rebuilt."
        st.rerun()
    if add_t:
        problems = []
        if not t_name.strip():
            problems.append("Give the tournament a name.")
        if t_end < t_start:
            problems.append("The end date is before the start date.")
        if t_kind == "Travel" and not t_teams:
            problems.append("Pick at least one team that's going.")
        if t_kind == "Local" and not t_courts:
            problems.append("Pick the courts it uses.")
        if problems:
            st.error(" ".join(problems))
        else:
            row = {"name": t_name.strip(), "type": t_kind,
                   "start_date": f"{t_start:%m/%d/%Y}", "end_date": f"{t_end:%m/%d/%Y}",
                   "club": "" if t_club in ("", "Any") else t_club,
                   "ages": ", ".join(map(str, t_ages)), "teams": ", ".join(t_teams),
                   "program": "" if t_prog == "(none)" else t_prog,
                   "courts": ", ".join("P" if c not in COURTS else str(c) for c in sorted(t_courts))
                   if t_kind == "Local" else "", "coaches": "; ".join(t_coaches),
                   "note": _tval("note") if editing else ""}
            span = f"{t_start:%b %-d}" + ("" if t_end == t_start else f"–{t_end:%b %-d}")
            if editing:
                tdf = inp.tournaments.copy().astype(object)
                for k_, v_ in row.items():
                    tdf.at[tdf.index[t_sel], k_] = v_
                inp.tournaments = tdf
                st.session_state.flash = f"Saved {row['name']} ({t_kind.lower()}, {span}). Schedule rebuilt."
            else:
                inp.tournaments = pd.concat([inp.tournaments, pd.DataFrame([row])], ignore_index=True)
                st.session_state.flash = f"Added {row['name']} ({t_kind.lower()}, {span}). Schedule rebuilt around it."
                st.session_state.pop("t_name", None)  # clear the form for the next one
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.resolve = True
            st.rerun()

    st.subheader("Tournaments")
    flags_panel("tournament", key="flags_tourn", expanded=True)
    if inp.tournaments.empty:
        st.caption("None yet.")
    else:
        tv = inp.tournaments.copy().fillna("")
        _tl = [f"{r['name']} · {r['type']} · {r['start_date']}" for _, r in tv.iterrows()]
        tv.index = [lab if _tl.count(lab) == 1 else f"{lab} (#{i + 1})" for i, lab in enumerate(_tl)]
        st.dataframe(tv[["name", "type", "start_date", "end_date", "club", "ages", "teams", "program", "courts", "coaches"]]
                     .rename(columns=lambda c: c.replace("_", " ").title()), hide_index=True, width="stretch")
        c1, c2 = st.columns([3, 1])
        drop_t = c1.multiselect("Remove tournaments", list(tv.index), placeholder="Choose tournaments to remove")
        if c2.button("Remove", disabled=not drop_t, width="stretch", key="rm_tourn"):
            keep = [i for i, lab in enumerate(tv.index) if lab not in drop_t]
            inp.tournaments = inp.tournaments.iloc[keep].reset_index(drop=True)
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.resolve = True
            st.session_state.flash = f"Removed {plural(len(drop_t), 'tournament')}. Schedule rebuilt."
            st.rerun()

# ----------------------------------------------------------- open days --
OPEN_HOURS = [("9AM", "9 AM"), ("11AM", "11 AM"), ("1PM", "1 PM"), ("3PM", "3 PM"), ("5PM", "5 PM"),
              ("7PM", "7 PM"), ("9PM", "9 PM")]

if VIEW == TAB_OPEN:
    st.markdown("Days the gym runs in **2-hour blocks** (9–11, 11–1 … 7–9): open **weekends**, and **school "
                "breaks**, when weekday practices switch to blocks too (e.g. Dec 21 – Jan 2). Closed days "
                "(Christmas, New Year's) stay closed. Break **camps** are programs: add one on the **Programs** tab "
                "with the days (Mon–Fri), the time **9AM-11AM** and its courts, and it's blocked off like any program. "
                "Weekday programs that don't run over a break need their dates or a skip set there too.")
    with st.container(border=True):
        c1, c2 = st.columns(2)
        o_from = c1.date_input("From", value=start, format="MM/DD/YYYY", key="o_from")
        o_to = c2.date_input("To", value=o_from, format="MM/DD/YYYY", key="o_to",
                             help="The same as From for a single day.")
        c1, c2 = st.columns(2)
        _hk = [k for k, _ in OPEN_HOURS]
        o_a = c1.selectbox("Opens", _hk[:-1], format_func=dict(OPEN_HOURS).get, key="o_a")
        o_b = c2.selectbox("Closes", _hk[1:], index=len(_hk) - 2, format_func=dict(OPEN_HOURS).get, key="o_b")
        c1, c2 = st.columns([2, 1])
        o_courts = c1.multiselect("Courts", COURTS, default=COURTS, format_func=court_name, key="o_courts")
        o_note = c2.text_input("Note", placeholder="e.g. Winter break", key="o_note")
        o_wk = st.checkbox("Include the weekend days in that range", value=True, key="o_wk",
                           help="Off: only the weekdays open (weekends stay as they are).")
        add_o = st.button("Open these days", type="primary")
    if add_o:
        if o_to < o_from:
            st.error("The end date is before the start date.")
        elif _hk.index(o_b) <= _hk.index(o_a):
            st.error("It has to close after it opens.")
        elif not o_courts:
            st.error("Pick the courts.")
        else:
            days_ = [d.date() for d in pd.date_range(o_from, o_to) if o_wk or d.weekday() < 5]
            courts_txt = "" if sorted(o_courts) == list(COURTS) else ", ".join(map(str, sorted(o_courts)))
            rows_ = []
            # One row per run of consecutive days, so a break reads as "12/21/2026-01/02/2027".
            run = []
            for d in days_ + [None]:
                if run and (d is None or (d - run[-1]).days != 1):
                    txt = f"{run[0]:%m/%d/%Y}" + ("" if len(run) == 1 else f"-{run[-1]:%m/%d/%Y}")
                    rows_.append({"date": txt, "open": f"{o_a}-{o_b}", "courts": courts_txt, "group": "",
                                  "required": "", "note": o_note.strip()})
                    run = []
                if d is not None:
                    run.append(d)
            trial = inp.weekends.copy()
            trial = pd.concat([trial, pd.DataFrame(rows_, columns=COLUMNS["Weekends"])], ignore_index=True)
            try:
                clean(ScheduleInput(inp.teams, inp.availability, inp.wishlist, inp.locals, trial, inp.closures,
                                    tournaments=inp.tournaments, coaches=inp.coaches, overrides=inp.overrides),
                      start, end)
            except ValueError as e:
                st.error(f"Couldn't add those days: {e}")
            else:
                inp.weekends = trial.astype(object)
                st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
                st.session_state.resolve = True
                st.session_state.flash = (f"Opened {plural(len(days_), 'day')} in 2-hour blocks "
                                          f"({dict(OPEN_HOURS)[o_a]}–{dict(OPEN_HOURS)[o_b]}). Schedule rebuilt.")
                st.rerun()

    st.subheader("Open days")
    wk = inp.weekends.dropna(how="all")
    if wk.empty:
        st.caption("None yet. Weekdays not listed here use the usual 3:45 / 5:30 / 7:20 times; weekends not listed "
                   "are closed for practices.")
    else:
        view_ = wk.fillna("").astype(str)
        _lab = [f"{r['date']} · {r['open'] or 'all day'}" + (f" · {r['note']}" if r["note"] else "")
                for _, r in view_.iterrows()]
        view_.index = [lab if _lab.count(lab) == 1 else f"{lab} (#{i + 1})" for i, lab in enumerate(_lab)]
        st.dataframe(view_[["date", "open", "courts", "note"]].rename(columns=str.title), hide_index=True,
                     width="stretch")
        if cleaned:
            in_range = sorted(d for d in cleaned.weekends if start <= d <= end)
            n_wd = sum(1 for d in in_range if d.weekday() < 5 and d not in closures)
            if n_wd:
                st.caption(f"In these dates: {plural(n_wd, 'weekday')} run in 2-hour blocks.")
        c1, c2 = st.columns([3, 1])
        drop_o = c1.multiselect("Remove", list(view_.index), placeholder="Choose open days to remove")
        if c2.button("Remove", disabled=not drop_o, width="stretch", key="rm_open"):
            keep = [i for i, lab in enumerate(view_.index) if lab not in drop_o]
            inp.weekends = wk.iloc[keep].reset_index(drop=True)
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.resolve = True
            st.session_state.flash = f"Removed {plural(len(drop_o), 'row')}. Schedule rebuilt."
            st.rerun()

# ------------------------------------------------------------ privates --
if VIEW == TAB_PRIV:
    st.markdown("Book **private lessons** as 1-hour sessions. Each one is checked against the schedule, so a court "
                "or coach that's already busy then is flagged. Privates show on the weekly court view.")
    coach_opts = cleaned.coaches if cleaned else sorted({p["coach"] for p in privs})
    a_now = result.assignments if result is not None else None
    if not CAN:
        st.info("To book a private, ask a scheduling admin for now. Booking requests in the app are coming next.")
    if CAN:
        with st.container(border=True):
            c1, c2, h1, h2, h3 = st.columns([2.2, 1.3, 0.9, 0.9, 1.2], vertical_alignment="bottom")
            p_coach = c1.selectbox("Coach", coach_opts + ["Someone else…"], key="pv_coach",
                                   index=None, placeholder="Choose a coach")
            p_date = c2.date_input("Date", value=start, format="MM/DD/YYYY", key="pv_date")
            p_hr = h1.selectbox("Start hour", list(range(1, 13)), index=3, key="pv_hour",
                                help="Any start time; privates are always 1 hour.")
            p_mn = h2.selectbox("Minutes", list(range(0, 60, 5)), format_func=lambda x: f":{x:02d}", key="pv_min")
            p_ap = h3.radio("AM / PM", ["AM", "PM"], index=1, horizontal=True, key="pv_ampm")
            if p_coach == "Someone else…":
                p_coach = st.text_input("Coach name", key="pv_coach_new").strip() or None
            p_start = (p_hr % 12 + (12 if p_ap == "PM" else 0)) * 60 + p_mn
            p_time = pv.clock(p_start)
            free = pv.free_courts(p_date, p_start, a_now, privs, ALL_COURTS)
            c1, c2, c3 = st.columns([1, 1, 2])
            p_team_private = c1.toggle("Team private", key="pv_is_team",
                                       help="On: a private for one of the club's teams. Off: a personal (non-team) private.")
            court_opts = [PRACTICE_COURT] + [c for c in COURTS]
            p_court = c2.selectbox("Court", court_opts, key="pv_court",
                                   format_func=lambda c: court_name(c) + ("" if c in free else " (taken)"))
            my_teams = [t.name for t in cleaned.teams if p_coach in t.staff] if cleaned and p_coach else []
            all_teams = [t.name for t in cleaned.teams] if cleaned else []
            if p_team_private:
                p_team = c3.selectbox("Team", my_teams + [t for t in all_teams if t not in my_teams], key="pv_team")
            else:
                p_team = ""
                c3.caption("Personal private (not for a team).")
            c1, c2 = st.columns(2)
            p_player = c1.text_input("Player(s) (optional)", key="pv_player")
            p_note = c2.text_input("Note (optional)", key="pv_note")
            trial = {"row": -1, "coach": p_coach or "", "date": p_date, "day": DAYS[p_date.weekday()], "start": p_start,
                     "end": p_start + 60, "time": p_time, "end_label": pv.clock(p_start + 60), "court": p_court,
                     "type": "Team" if p_team_private else "Personal", "team": p_team, "player": p_player, "note": p_note,
                     "what": ""}
            clash = pv.conflicts(privs + [trial], a_now, closures).get(-1, []) if p_coach else []
            if clash:
                st.warning("Heads up: " + "; ".join(clash) + ".")
            if st.button("Book private", type="primary", disabled=not p_coach):
                row = {"coach": p_coach, "date": f"{p_date:%m/%d/%Y}", "time": p_time, "court": court_short(p_court),
                       "type": trial["type"], "team": p_team, "player": p_player.strip(), "note": p_note.strip()}
                inp.privates = pd.concat([inp.privates, pd.DataFrame([row])], ignore_index=True)
                st.session_state.flash = (f"Booked a {trial['type'].lower()} private: {p_coach}, {p_date:%a %b %-d} "
                                          f"{p_time}, {court_name(p_court)}" + (" (flagged: " + "; ".join(clash) + ")" if clash else "") + ".")
                st.rerun()

    for msg in priv_problems:
        st.error(msg)
    st.subheader("Privates")
    if not privs:
        st.caption("None booked yet.")
    else:
        flags = pv.conflicts(privs, a_now, closures)
        c1, c2, c3 = st.columns([2, 1, 1])
        who = c1.multiselect("Only show these coaches", sorted({p["coach"] for p in privs}), key="pv_filter")
        kind = c2.radio("Type", ["All", "Team", "Personal"], horizontal=True, key="pv_kind")
        only_flag = c3.toggle("Only conflicts", key="pv_only_flag", disabled=not flags)
        shown = [p for p in privs if (not who or p["coach"] in who) and (kind == "All" or p["type"] == kind)
                 and (not only_flag or p["row"] in flags)]
        if flags:
            st.warning(f"{plural(len(flags), 'private')} {'clashes' if len(flags) == 1 else 'clash'} with the "
                       "schedule or another private (see the Conflict column).")
        st.dataframe(pd.DataFrame([{
            "Coach": p["coach"], "Date": f"{p['date']:%a %b %-d}", "Time": f"{p['time']}–{p['end_label']}",
            "Court": court_name(p["court"]), "Type": p["type"], "Team": p["team"], "Player": p["player"],
            "Note": p["note"], "Conflict": "; ".join(flags.get(p["row"], []))} for p in shown]),
            hide_index=True, width="stretch")
        lab = {p["row"]: f"{p['coach']} · {p['date']:%a %b %-d} {p['time']} · {court_name(p['court'])}" for p in shown}
        c1, c2 = st.columns([3, 1])
        drop = c1.multiselect("Remove privates", list(lab), format_func=lambda i: lab[i], key="pv_drop") if CAN else []
        if CAN and c2.button("Remove", disabled=not drop, width="stretch", key="pv_drop_btn"):
            inp.privates = inp.privates.reset_index(drop=True).drop(index=drop).reset_index(drop=True)
            st.session_state.flash = f"Removed {plural(len(drop), 'private')}."
            st.rerun()
        n_team = sum(p["type"] == "Team" for p in privs)
        st.caption(f"{plural(len(privs), 'private')} booked · {n_team} team, {len(privs) - n_team} personal")
    st.download_button("Download every coach's privates (Excel)",
                       lambda: pv.to_excel(privs, coach_opts, pv.conflicts(privs, a_now, closures)),
                       f"privates_{start:%Y%m%d}.xlsx",
                       help="A Coaches sheet with every coach and their team / personal private counts, one list of "
                            "all privates, and a sheet per coach.")

# ------------------------------------------------------------- coaches --
if VIEW == TAB_COACH:
    if CAN:
        with st.expander("Add a coach", expanded=False):
            st.caption("For coaches who aren't on a team yet (e.g. they only work programs). "
                       "They'll show up in the program and tournament forms.")
            with st.form("add_coach", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                nc_name = c1.text_input("Name")
                nc_email = c2.text_input("Email (optional)")
                nc_phone = c3.text_input("Phone (optional)")
                nc_note = st.text_input("Note (optional)", placeholder="e.g. JumpStart coach")
                if st.form_submit_button("Add coach", type="primary"):
                    existing = set(cleaned.coaches) if cleaned else set()
                    if not nc_name.strip():
                        st.error("Enter a name.")
                    elif nc_name.strip() in existing:
                        st.warning(f"{nc_name.strip()} is already a coach.")
                    else:
                        inp.coaches = pd.concat([inp.coaches, pd.DataFrame([{
                            "coach": nc_name.strip(), "email": nc_email.strip(), "phone": nc_phone.strip(),
                            "note": nc_note.strip()}])], ignore_index=True)
                        st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
                        st.session_state.flash = f"Added coach {nc_name.strip()}."
                        st.rerun()
    if cleaned is None:
        st.caption("Fix the input errors first.")
    else:
        _me_i = cleaned.coaches.index(MY_COACH) if MY_COACH in cleaned.coaches else 0
        coach = st.selectbox("Coach", cleaned.coaches, index=_me_i, key=f"coach_pick_{MY_COACH}")
        heads = [t.name for t in cleaned.teams if coach in t.heads]
        assists = [t.name for t in cleaned.teams if coach in t.assistants and coach not in t.heads]
        grouped = {}
        for lp in cleaned.locals:
            if coach in lp.coaches:
                grouped.setdefault((lp.name, lp.slot, lp.on_site), []).append(lp.day)
        my_locals = [f"{n} ({' & '.join(ds)} {sl}){'' if on else ' · off-site'}" for (n, sl, on), ds in grouped.items()]
        mine_names = set(heads + assists + [coach])
        rules = [r for r in cleaned.rules if r.who in mine_names]

        c1, c2, c3 = st.columns(3)
        c1.markdown("**Head coach of**\n\n" + ("\n".join(f"- {t}" for t in heads) or "_none_"))
        c1.markdown("**Assistant on**\n\n" + ("\n".join(f"- {t}" for t in assists) or "_none_"))
        c2.markdown("**Locals / other programs**\n\n" + ("\n".join(f"- {x}" for x in my_locals) or "_none_"))
        c3.markdown("**No-dates & not-preferred**\n\n" + ("\n".join(
            f"- {r.who}: {'no' if r.kind == 'no' else 'not preferred'} {r.describe()}" for r in rules) or "_none_"))

        # ---- a coach's own no-dates / times (any coach for scheduling admins)
        if CAN or (MY_COACH and coach == MY_COACH):
            with st.expander(f"{'Your' if coach == MY_COACH else coach + chr(39) + 's'} no-dates and times",
                             expanded=coach == MY_COACH and not CAN):
                st.caption("Days and times you can't coach. The scheduler keeps your teams off them (a head "
                           "coach's are only broken as a last resort, and flagged). “Rather not” is a softer "
                           "preference it weighs against everyone else's.")
                nk = f"nd_{coach}"
                c1, c2 = st.columns(2)
                nd_how = c1.radio("When", ["Every week", "Specific dates"], horizontal=True, key=f"{nk}_how")
                nd_kind = c2.radio("How strict", ["Can't make it", "Would rather not"], horizontal=True,
                                   key=f"{nk}_kind")
                c1, c2 = st.columns(2)
                if nd_how == "Every week":
                    nd_days = c1.multiselect("Days", DAYS, key=f"{nk}_days", placeholder="e.g. Tue")
                    nd_from = nd_to = None
                else:
                    nd_days = []
                    cc1, cc2 = c1.columns(2)
                    nd_from = cc1.date_input("From", value=start, format="MM/DD/YYYY", key=f"{nk}_from")
                    nd_to = cc2.date_input("To", value=nd_from, format="MM/DD/YYYY", key=f"{nk}_to")
                nd_times = c2.multiselect("Times", WEEKDAY_KEYS + WEEKEND_KEYS, key=f"{nk}_times",
                                          format_func=lambda k: SLOTS[k] + (" (weekend)" if k in WEEKEND_KEYS else ""),
                                          placeholder="All day")
                nd_note = st.text_input("Note (optional)", key=f"{nk}_note", placeholder="e.g. work, travel")
                nd_bad = (nd_how == "Every week" and not nd_days) or (nd_to is not None and nd_to < nd_from)
                if st.button("Add", type="primary", disabled=nd_bad, key=f"{nk}_add",
                             help="Pick at least one day." if nd_how == "Every week" and not nd_days else None):
                    dates_txt = "" if nd_from is None else (
                        f"{nd_from:%m/%d/%Y}" + ("" if nd_to == nd_from else f"-{nd_to:%m/%d/%Y}"))
                    arow = {c: "" for c in COLUMNS["Availability"]}
                    arow.update({"who": coach, "type": "No" if nd_kind.startswith("Can") else "Not preferred",
                                 "days": ", ".join(d for d in DAYS if d in nd_days), "dates": dates_txt,
                                 "times": ", ".join(sorted(nd_times, key=SLOT_KEYS.index)), "note": nd_note.strip()})
                    shown_dates = "" if nd_from is None else (
                        f"{nd_from:%-m/%-d}" + ("" if nd_to == nd_from else f"–{nd_to:%-m/%-d}"))
                    when = " ".join(x for x in (arow["days"].replace(", ", "/"), shown_dates,
                                                arow["times"] or "all day") if x)
                    what = f"{'no' if arow['type'] == 'No' else 'rather not'}: {when}"
                    change_prefs(coach, None, arow, [], [], f"Saved {coach}'s {what}.", what)
                av_ = inp.availability.fillna("")
                av_ = av_[av_["who"].astype(str).str.strip() == coach]
                if len(av_):
                    def _nd_label(r) -> str:
                        kind = "Rather not" if str(r["type"]).strip().lower().startswith("not") else "No"
                        when = " ".join(str(x).strip() for x in (r["days"], r["dates"], r["times"] or "all day")
                                        if str(x).strip())
                        return f"{kind}: {when}" + (f" ({r['note']})" if str(r["note"]).strip() else "")
                    cur = {i: _nd_label(r) for i, r in av_.iterrows()}
                    drop_nd = st.multiselect("Remove", list(cur), format_func=lambda i: cur[i], key=f"{nk}_drop")
                    if st.button("Remove selected", disabled=not drop_nd, key=f"{nk}_dropbtn"):
                        change_prefs(coach, None, None, [], [_pref_key(av_.loc[i].to_dict(), "who") for i in drop_nd],
                                     f"Removed {plural(len(drop_nd), 'no-date')} for {coach}.",
                                     "removed " + "; ".join(cur[i] for i in drop_nd))

        if result is not None:
            a = result.assignments
            mine = a[a["coach"].fillna("").map(lambda s: coach in [x.strip() for x in s.split(";")])]
            if len(mine):
                mine = (mine.groupby(["date", "day", "slot", "slot_label", "kind", "name", "on_pattern"], as_index=False)
                        ["court"].agg(lambda cs: ", ".join(court_short(c) for c in sorted(cs))))
                mine["o"] = mine["slot"].map(SLOT_KEYS.index)
                mine = mine.sort_values(["date", "o"])
                mine["role"] = mine["name"].map(lambda n: "Head" if n in heads else ("Assistant" if n in assists else ""))
            st.markdown(f"**{coach}'s schedule** · {plural(len(mine), 'session')}")
            if len(mine):
                show = mine.assign(date=mine["date"].map(lambda d: f"{d:%a %b} {d.day}"))
                st.dataframe(
                    show[["date", "slot_label", "court", "kind", "name", "role"]].rename(columns={
                        "date": "Date", "slot_label": "Time", "court": "Court", "kind": "Type",
                        "name": "Team / Program", "role": "Role"}),
                    hide_index=True, width="stretch")
                mine_p = [p for p in privs if p["coach"] == coach]
                if mine_p:
                    st.markdown(f"**{coach}'s privates** · {plural(len(mine_p), 'session')}")
                    st.dataframe(pd.DataFrame([{"Date": f"{p['date']:%a %b %-d}", "Time": f"{p['time']}–{p['end_label']}",
                                                "Court": court_name(p["court"]), "Type": p["type"], "Team": p["team"],
                                                "Player": p["player"]} for p in mine_p]),
                                 hide_index=True, width="stretch")
                st.download_button(f"Add {coach}'s schedule to Google Calendar (.ics)",
                                   lambda: to_ics(result, coach), f"{coach.replace(' ', '_')}_{start:%Y%m%d}-{end:%Y%m%d}.ics",
                                   mime="text/calendar",
                                   help="In Google Calendar: Settings → Import & export → Import this file.")

# ------------------------------------------------------- pecking order --
def _rebuild(message: str):
    st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
    st.session_state.resolve = True
    st.session_state.flash = message
    st.rerun()


if VIEW == TAB_PECK:
    st.markdown("The club's pecking order: **rank within each age group**, written like `16-3` (the third 16s "
                "team) or just `3`. Boys and girls teams can share a rank; at the same rank the boys team goes just "
                "above the girls (`14-1` boys, then `14-1` girls). Edit any rank in the table and press **Save ranks**. "
                "Higher teams get the better courts (1 → 7 → 6 → 5 → 3 → 4 → 2), and when Court 2 is needed, "
                "the lowest team gets it.")
    order = pk.order_table(inp.teams)
    if order.empty:
        st.caption("No teams yet.")
    else:
        missing = order[~order["_set"]]["team"].tolist()
        if missing:
            st.info(f"**{plural(len(missing), 'team')} {'has' if len(missing) == 1 else 'have'} no rank yet**, so they're placed between their age group's "
                    f"1s and 2s: {', '.join(missing)}.", icon="ℹ️")
        sig_key = abs(hash((st.session_state.get("inp_sig"), st.session_state.get("locals_ver", 0)))) % 10**8
        shown = order[["order", "rank", "team", "club", "head_coach", "assistants"]]
        edited = st.data_editor(
            shown, hide_index=True, width="stretch", key=f"peck_edit_{sig_key}",
            disabled=["order", "team", "club", "head_coach", "assistants"],
            column_config={
                "order": st.column_config.NumberColumn("#", help="Position in the pecking order (1 = top). "
                                                       "At a shared rank, boys come before girls.", width="small"),
                "rank": st.column_config.TextColumn("Rank", help="Like 16-3, or just 3", width="small"),
                "team": "Team", "club": "Club", "head_coach": "Head coach", "assistants": "Assistants"})
        changed = {r.team: r.rank for r, (_, o) in zip(edited.itertuples(), shown.iterrows())
                   if str(r.rank or "").strip() != str(o["rank"] or "").strip()}
        if st.button("Save ranks", type="primary", disabled=not changed,
                     help="Saves the ranks you changed above and rebuilds the schedule"):
            t = inp.teams.copy()
            if "rank" not in t.columns:
                t["rank"] = ""
            for team, rank in changed.items():
                t.loc[t["team"].astype(str).str.strip() == team, "rank"] = str(rank or "").strip()
            inp.teams = t
            _rebuild(f"Saved {plural(len(changed), 'rank')}. Schedule rebuilt.")

        dl = order.rename(columns={"rank": "Rank", "team": "Team", "club": "Club", "head_coach": "Head Coach",
                                   "assistants": "Asst. Coach"})[["Rank", "Club", "Team", "Head Coach", "Asst. Coach"]]
        buf = io.BytesIO()
        dl.to_excel(buf, index=False, sheet_name="Pecking Order")
        st.download_button("Download pecking order (Excel)", buf.getvalue(), "pecking_order.xlsx")

    with st.expander("Import a pecking order from a spreadsheet", expanded=order.empty or bool(
            len(order) and (~order["_set"]).all())):
        st.caption("Upload any spreadsheet with a **Rank** column and a **Team** column (Head Coach, Asst. Coach "
                   "and Gender help match teams but are optional). Teams are matched by name first "
                   "(`18-Black` = `18 Black`), then by age and head coach, so nicknames like `16-D&D` still work. "
                   "Ranks Excel turned into dates (`12-1` → Dec 1) are read back correctly.")
        up = st.file_uploader("Pecking order (.xlsx or .csv)", type=["xlsx", "csv"], key="peck_upload")
        if up is not None:
            try:
                table = pk.read_pecking(up, up.name)
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
                table = None
            if table is not None:
                matches = pk.match_teams(table, inp.teams)
                team_opts = ["(skip)"] + [str(t) for t in inp.teams["team"] if not blank(t)]
                preview = pd.DataFrame([{
                    "In the sheet": m.sheet_team, "Rank": m.rank_text,
                    "Team in the app": m.team or "(skip)",
                    "Matched by": m.how or "not matched", "Note": m.note} for m in matches])
                st.caption(f"From the **{table.attrs.get('sheet', 'sheet')}** tab: "
                           f"{sum(1 for m in matches if m.team)} of {len(matches)} rows matched. "
                           "Fix any match in the **Team in the app** column.")
                fixed = st.data_editor(
                    preview, hide_index=True, width="stretch", key=f"peck_preview_{up.file_id}",
                    disabled=["In the sheet", "Matched by", "Note"],
                    column_config={"Team in the app": st.column_config.SelectboxColumn(options=team_opts),
                                   "Rank": st.column_config.TextColumn(help="Like 16-3")})
                for m, (_, r) in zip(matches, fixed.iterrows()):
                    m.team = None if r["Team in the app"] == "(skip)" else r["Team in the app"]
                    if str(r["Rank"]).strip() != m.rank_text:
                        m.rank_text = str(r["Rank"]).strip()
                        m.rank, _ = parse_rank(m.rank_text)
                picked = [m.team for m in matches if m.team]
                dupes = sorted({t for t in picked if picked.count(t) > 1})
                left = pk.unranked(matches, inp.teams)
                if dupes:
                    st.error(f"Matched more than once: {', '.join(dupes)}. Fix the Team in the app column.")
                if left:
                    st.warning(f"Not in this sheet, so their ranks stay as they are: {', '.join(left)}")
                changes = pk.assistant_changes(matches, inp.teams)
                upd = False
                if changes:
                    upd = st.checkbox(f"Also update assistant coaches from this sheet ({plural(len(changes), 'team')} differ{'s' if len(changes) == 1 else ''})",
                                      key="peck_upd_asst")
                    st.dataframe(pd.DataFrame(changes, columns=["Team", "Assistants now", "In the sheet"]),
                                 hide_index=True, width="stretch")
                if st.button("Use this pecking order", type="primary", disabled=bool(dupes)):
                    inp.teams = pk.apply(inp.teams, matches, update_assistants=upd)
                    n = sum(1 for m in matches if m.team and m.rank is not None)
                    _rebuild(f"Pecking order set for {plural(n, 'team')}"
                             + (f", assistants updated for {len(changes)}" if upd and changes else "")
                             + ". Schedule rebuilt.")

# ------------------------------------------------------------- publish --
if VIEW == TAB_PUB:
    st.markdown("Publish the schedule when you send it to coaches. Later rebuilds keep practices where the "
                "published version had them where possible, and this page lists everything that changed since.")
    if result is None:
        st.caption("Build the schedule first.")
    else:
        with st.container(border=True):
            c1, c2 = st.columns([1, 2])
            p_label = c1.text_input("Version name", value=f"{range_label(start, end)} · v{len(published) + 1}",
                                    key=f"pub_label_{len(published)}")
            p_note = c2.text_input("Note (optional)", placeholder="e.g. sent to coaches Friday", key="pub_note")
            if st.button("Publish this schedule", type="primary"):
                st.session_state.published = add_version(published, snapshot(result, p_label.strip() or "Published",
                                                                             p_note.strip()))
                st.session_state.base_version = None
                st.session_state.flash = f"Published “{p_label.strip() or 'Published'}”."
                st.rerun()
        if not published:
            st.info("Nothing published yet.")
        else:
            labels = {i: f"{v['label']} · {published_at(v):%b %-d, %-I:%M %p}"
                      + (f" · {v['note']}" if v.get("note") else "") for i, v in enumerate(published)}
            order = list(reversed(range(len(published))))
            vi = st.selectbox("Compare the current schedule with", order, format_func=lambda i: labels[i],
                              key="pub_cmp")
            ver_ = published[vi]
            r1, r2 = st.columns([1, 3], vertical_alignment="center")
            if r1.button("Restore this version", key="pub_restore",
                         help="Make this version the current schedule again"):
                st.session_state.restore_version = ver_["id"]
                st.session_state.resolve = True
                st.session_state.flash = (f"Restored “{ver_['label']}”. Anything that can't happen with the "
                                          "current inputs is listed in the notes at the top and on the Review tab.")
                st.rerun()
            r2.caption("Brings back this version's practices exactly, rebuilt around your current inputs. "
                       "The current schedule is replaced, so publish it first if you want to keep it.")
            ch = diff(practices_of(ver_), result.assignments, cleaned, start, end,
                      pinned={(r.name, r.date, r.slot) for r in result.assignments.itertuples()
                              if getattr(r, "pinned", False) == True})  # noqa: E712
            if ch.empty:
                st.success("No changes: the current schedule matches this version.")
            else:
                counts = ch["change"].value_counts()
                c = st.columns(5)
                c[0].metric("Teams affected", ch["team"].nunique())
                for i, k in enumerate(["Moved", "Added", "Removed", "Court"]):
                    c[i + 1].metric({"Court": "Court changes"}.get(k, k), int(counts.get(k, 0)))
                who = st.multiselect("Only show these teams", sorted(ch["team"].unique()), key="pub_teams")
                view = ch[ch["team"].isin(who)] if who else ch
                st.dataframe(view.assign(week_of=view["week_of"].map(lambda d: f"{d:%b %-d}")).rename(columns={
                    "team": "Team", "week_of": "Week of", "change": "Change", "was": "Was", "now": "Now",
                    "why": "Why"}), hide_index=True, width="stretch")
                st.download_button("Download changes (CSV)", view.to_csv(index=False).encode(),
                                   f"changes_since_{ver_['id'][:8]}.csv", mime="text/csv")
            with st.expander(f"All published versions ({len(published)})"):
                st.dataframe(pd.DataFrame([{"Version": v["label"], "Published": f"{published_at(v):%b %-d, %Y %-I:%M %p}",
                                            "Note": v.get("note", ""), "Practices": len(v.get("practices", []))}
                                           for v in reversed(published)]), hide_index=True, width="stretch")
                drop = st.multiselect("Remove versions", order, format_func=lambda i: labels[i], key="pub_drop")
                if st.button("Remove selected", disabled=not drop):
                    st.session_state.published = [v for i, v in enumerate(published) if i not in drop]
                    if any(published[i]["id"] == st.session_state.get("base_version") for i in drop):
                        st.session_state.base_version = None
                    st.rerun()

# ------------------------------------------------------ google calendar --
if VIEW == TAB_PUB and result is not None and cleaned is not None:
    st.subheader("Google Calendar")
    st.markdown("Push a published version to the club's Google Calendars: each team's calendar gets its practices "
                "and tournaments, each club calendar gets every practice and tournament on that side, and each "
                "program's calendar gets its sessions. Only events this app made are ever changed or removed; "
                "anything added by hand is left alone.")
    sa = gcal.service_account_info(SECRETS)
    client = None
    if sa:
        try:
            client = gcal.CalendarClient(sa)
        except Exception as e:  # noqa: BLE001
            st.error(f"The Google key in the secrets didn't work: {e}")
    if client is None:
        st.info("Google Calendar isn't connected yet. You can link calendars below now; previews and syncing "
                "start working once the app's Google key is added (steps in `docs/SETUP_CALENDAR.md`).")
    else:
        st.caption(f"Share each calendar with **{client.robot}** (\u201cMake changes to events\u201d).")

    with st.expander("Calendar links", expanded=not gcal.links_from(inp.calendars)):
        st.caption("Paste each calendar's ID (Google Calendar → the calendar's ⋮ → Settings and sharing → "
                   "Integrate calendar → Calendar ID). Leave a row blank to skip it.")
        clubs_ = sorted({t.club for t in cleaned.teams if t.club})
        progs_ = sorted({lp.name for lp in cleaned.locals})
        rows_ = gcal.link_rows([t.name for t in cleaned.teams], clubs_, progs_, inp.calendars)
        ed_links = st.data_editor(rows_, hide_index=True, width="stretch", disabled=["kind", "name"],
                                  key=f"cal_links_{st.session_state.get('locals_ver', 0)}",
                                  column_config={"kind": "Calendar for", "name": "Name",
                                                 "calendar_id": st.column_config.TextColumn("Calendar ID", width="large")})
        c1, c2 = st.columns(2)
        if c1.button("Save calendar links", type="primary", width="stretch", key="cal_save"):
            inp.calendars = ed_links[ed_links["calendar_id"].astype(str).str.strip() != ""].reset_index(drop=True)
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.flash = f"Saved {plural(len(inp.calendars), 'calendar link')}."
            st.rerun()
        if c2.button("Check access", width="stretch", disabled=client is None, key="cal_check"):
            checks = []
            for (kind, name), cid in gcal.links_from(ed_links).items():
                try:
                    checks.append({"Calendar for": f"{kind}: {name}", "Google calendar": client.calendar_name(cid),
                                   "OK": "✓"})
                except Exception as e:  # noqa: BLE001
                    checks.append({"Calendar for": f"{kind}: {name}", "Google calendar": str(e), "OK": "✗"})
            st.dataframe(pd.DataFrame(checks), hide_index=True, width="stretch")

    if not published:
        st.caption("Publish the schedule first; only published versions go to Google Calendar.")
    else:
        links = gcal.links_from(inp.calendars)
        c1, c2 = st.columns([2, 1])
        vlabels = {i: f"{v['label']} · {published_at(v):%b %-d, %-I:%M %p}" for i, v in enumerate(published)}
        vi_ = c1.selectbox("Version to send", list(reversed(range(len(published)))), format_func=lambda i: vlabels[i],
                           key="cal_ver")
        test_mode = c2.toggle("Test mode", value=True, key="cal_test",
                              help="Send everything to one test calendar instead, with the calendar it would have "
                                   "gone to in front of each title. Nothing real is touched.")
        test_cal = st.text_input("Test calendar ID", key="cal_test_id",
                                 placeholder="ID of a throwaway calendar shared with the app") if test_mode else ""
        ver_ = published[vi_]
        pr_ = practices_of(ver_)
        loc_ = result.assignments[result.assignments["kind"] == "Local"]
        side_ = {lp.name: lp.side for lp in cleaned.locals}
        evs, missing = gcal.desired_events(pr_, loc_, cleaned.tournaments, cleaned.teams, links, start, end,
                                           test_cal.strip() if test_mode else "", secret("CLUB_ADDRESS") or "",
                                           side_)
        if missing:
            st.caption(f"No calendar linked (skipped): {', '.join(missing[:12])}"
                       + (f" and {len(missing) - 12} more" if len(missing) > 12 else ""))
        all_cals = sorted({test_cal.strip()} if test_mode and test_cal.strip() else set(links.values()))
        ready = client is not None and (not test_mode or bool(test_cal.strip())) and bool(evs)
        sig_ = (ver_["id"], test_mode, test_cal.strip(), tuple(sorted(links.items())))
        if st.button(f"Preview changes ({plural(len(evs), 'event')})", disabled=not ready, key="cal_preview",
                     help=None if ready else "Connect Google, link calendars (and a test calendar in test mode) first."):
            with st.spinner("Reading the calendars…"):
                st.session_state.cal_plan = (sig_, gcal.plan(evs, client, start, end, all_cals))
        held = st.session_state.get("cal_plan")
        if held and held[0] == sig_:
            plan_ = held[1]
            for err in plan_.errors:
                st.error(err)
            c = st.columns(4)
            c[0].metric("Add", len(plan_.create))
            c[1].metric("Update", len(plan_.update))
            c[2].metric("Remove", len(plan_.delete))
            c[3].metric("Already right", plan_.unchanged)
            if plan_.n_changes:
                st.dataframe(plan_.table().rename(columns=str.title), hide_index=True, width="stretch",
                             height=min(400, 38 + 35 * plan_.n_changes))
                where = "the test calendar" if test_mode else f"{plural(len(all_cals), 'calendar')}"
                if st.button(f"Sync {plural(plan_.n_changes, 'change')} to {where}", type="primary", key="cal_go"):
                    bar = st.progress(0.0)
                    n, errs = gcal.apply(plan_, client, progress=bar.progress)
                    store.log(VIEWER.log_name, "Google Calendar",
                              f"{'test mode, ' if test_mode else ''}{ver_['label']}: {len(plan_.create)} added, "
                              f"{len(plan_.update)} updated, {len(plan_.delete)} removed"
                              + (f", {len(errs)} failed" if errs else ""), st.session_state.project_id)
                    st.session_state.pop("cal_plan", None)
                    if errs:
                        st.error(f"{plural(len(errs), 'change')} failed:\n" + "\n".join(f"- {e}" for e in errs[:10]))
                    st.session_state.flash = f"Synced {plural(n, 'change')} to {where}."
                    if not errs:
                        st.rerun()
            elif not plan_.errors:
                st.success("Google Calendar already matches this version.")


# -------------------------------------------------------------- review --
if VIEW == TAB_REVIEW:
    if result is None:
        st.caption("No schedule yet.")
    else:
        st.subheader("Practices per team, per week")
        st.caption("Red = short of the team's weekly target. Grey = no target that week (cut off by the date "
                   "range, or before the team's season starts).")
        if len(result.practice_counts):
            hl_r = set(st.multiselect("Highlight teams", sorted(result.practice_counts["team"].unique()),
                                      default=sorted(MY_TEAMS & set(result.practice_counts["team"])),
                                      key=f"counts_hl_{MY_COACH}"))
            st.markdown(practice_counts_html(result.practice_counts, hl_r or None), unsafe_allow_html=True)
        else:
            st.caption("Rebuild the schedule to see practice counts.")
        st.subheader("Who gets the less-wanted spots")
        st.caption("Per team: practices on Court 2, at 3:45, on weekends, off the usual schedule (makeups and "
                   "moves) and on back-to-back days. Older and 1s teams get the better courts and times on "
                   "purpose, so this is a check, not a target.")
        scope = st.radio("Count", ["This schedule", "Every published version"], horizontal=True,
                         key="burden_scope", disabled=not published)
        if scope == "This schedule" or not published:
            src = result.assignments
        else:  # each date counted once, from the newest version that has it
            allp = pd.concat([practices_of(v).assign(_v=i) for i, v in enumerate(published)], ignore_index=True)
            allp = allp.sort_values("_v").drop_duplicates(["name", "date", "slot"], keep="last")
            latest_dates = allp.groupby("date")["_v"].transform("max")
            src = allp[allp["_v"] == latest_dates]
        bt = burden(src)
        if len(bt):
            ages = {t.name: t.age for t in cleaned.teams} if cleaned else {}
            bt = bt.assign(age=bt["team"].map(ages)).sort_values(["age", "team"]).drop(columns="age")
            top = {c: max(1, int(bt[c].max())) for c in ["court_2", "at_3_45", "weekend", "off_usual", "back_to_back"]}
            bar = lambda lab, c: st.column_config.ProgressColumn(lab, format="%d", min_value=0, max_value=top[c])  # noqa: E731
            st.dataframe(bt, hide_index=True, width="stretch", column_config={
                "team": "Team", "practices": "Practices", "court_2": bar("Court 2", "court_2"),
                "at_3_45": bar("3:45", "at_3_45"), "weekend": bar("Weekend", "weekend"),
                "off_usual": bar("Off usual", "off_usual"), "back_to_back": bar("Back-to-back", "back_to_back")})
        st.subheader("Everything to review")
        if result.issues.empty:
            st.success("Nothing to review: every team has its practices on its usual schedule.")
        else:
            st.markdown("Everything that differs from the ideal schedule, most important first.")
            sev = st.multiselect("Severity", ["High", "Medium", "Low"], default=["High", "Medium", "Low"])
            iss = result.issues[result.issues["severity"].isin(sev)]
            if COACH_VIEW:
                st.caption("Showing your teams only.")
                iss = iss[iss["team"].map(mine)]
            st.dataframe(iss.rename(columns={"severity": "Severity", "team": "Team", "week_of": "Week of",
                                             "issue": "Issue", "detail": "Why"}),
                         hide_index=True, width="stretch")

# -------------------------------------------------------------- inputs --
if VIEW == TAB_INPUTS:
    st.markdown("Edit any table, then press **Auto-schedule** again. "
                "Download the tables as a spreadsheet to keep your changes.")
    help_ = {
        "Teams": "Girls teams can leave head_coach blank (taken from the name, e.g. 18-Jordan). "
                 "rank = pecking order within the age group, like `16-3` (easier: the Pecking order tab). "
                 "start_date = first day the team can practice. weekend_only limits weekends to listed options "
                 "(`11/1 AM; 11/8 3PM`); weekend_pref is a soft preference (`AM`, `Sat AM; Sun PM`). "
                 "late_ok = Yes lets a 14s-and-under team practice at 7:20 (avoided unless needed). "
                 "practices = practices a week (blank = 2).",
        "Availability": "One row per rule. who = team or coach. type = No (hard) or Not preferred. "
                        "days like `Mon` / `Weekdays`, dates like `11/10-11/16`, times like `5:30` or `3:45` "
                        "(blank = any).",
        "Wishlist": "Ranked day/time wishes. choice 1 or 2. Blank days = any day.",
        "Locals": "Programs. side = Girls / Boys (colors). on_site = No for programs elsewhere (blocks the coach "
                  "only). Times like `5:30`, `3:45-7:20`, JumpStart `3:30` / `4:30`, weekend `9AM-1PM`. "
                  "Courts like `1-4`, `P` for the practice court; blank/TBD until known. skip = single dates it doesn't "
                  "run (`11/12; 11/26`). Easier: use the Programs tab.",
        "Weekends": "Open days in 2-hour blocks (easier: the Open days tab): weekends, and weekdays over a school "
                    "break. date like `11/7` or a range `12/21-1/2`; open like `9AM-9PM`. Days sharing a group with "
                    "required = Yes mean 'at least one of these must be used'.",
        "Resolved": "Coverage flags marked resolved (easier: Resolve on the flag). flag = the flag's id; note = how "
                    "it's covered.",
        "Closures": "Club closed all day.",
        "Coaches": "Coach roster: anyone not on a team yet (e.g. coaches who only work programs), plus contact info.",
        "Overrides": "Manual changes the scheduler always keeps (easier from the Edit practices tab). action = Add "
                     "(date + time, optional court), Extra (same, on top of the week's practices), Remove (date, optional time; a makeup is found), Cancel "
                     "(no makeup) or Usual (weekdays + time, optional court).",
        "Calendars": "Google Calendar links (easier: the Publish tab). kind = Team, Club or Program; name = the "
                     "team / club / program; calendar_id = the calendar's ID from its Google Calendar settings.",
        "Privates": "1-hour private lessons (easier: the Privates tab). time = start, like 4:00 PM. court = P "
                    "(practice court) or 1-7. type = Team or Personal; team = which team, for team privates.",
        "Tournaments": "type = Local (hosted here: blocks its courts and coaches until 7 PM; blank courts = all; "
                       "coaches = who's working it) or Travel "
                       "(away: the teams matching club + ages, or the listed teams, can't practice those days "
                       "and avoid the days right before and after). Ages like `15-16` or `14U`.",
    }
    sig_key = abs(hash((st.session_state.get("inp_sig"), st.session_state.get("locals_ver", 0)))) % 10**8
    for sheet, attr in ATTRS.items():
        st.subheader(sheet)
        st.caption(help_.get(sheet, ""))
        df = getattr(inp, attr).copy()
        for c in COLUMNS[sheet]:
            if c not in df.columns:
                df[c] = None
        df = df[COLUMNS[sheet]].astype(object)
        df = df.map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) or str(v) in ("None", "nan", "NaT")
                    else str(v)).astype(object)
        edited = st.data_editor(df, num_rows="dynamic", width="stretch",
                                key=f"edit_{attr}_{sig_key}", hide_index=True)
        setattr(inp, attr, edited)
    st.download_button("Download inputs (Excel)", inp.to_excel_bytes, "club_inputs.xlsx")


# -------------------------------------------------------------- access --
if VIEW == TAB_ACCESS:
    st.markdown("Who can use the app and what they can do. **Coaches** see the whole schedule and change their own "
                "teams' preferences. **Scheduling admins** edit the schedule, rebuild, publish and (next) approve "
                "coaches' requests. **App admins** manage this page and can use *View as* to test any role.")
    if not AUTH_ON:
        st.warning("Google sign-in isn't set up on this copy of the app yet, so anyone with the link can still do "
                   "everything. The setup steps are in `docs/SETUP_LOGINS.md`.")
    always = set(access.app_admins(SECRETS))
    people = store.users()
    names_ = _coach_names()
    link_opts = [""] + sorted(set(names_) | {p_["coach"] for p_ in people if p_["coach"]})
    table = pd.DataFrame([{"email": p_["email"], "name": p_["name"], "coach": p_["coach"],
                           "app_admin": bool(p_["app_admin"]) or p_["email"] in always,
                           "sched_admin": bool(p_["sched_admin"])} for p_ in people],
                         columns=["email", "name", "coach", "app_admin", "sched_admin"])
    st.subheader("People")
    if table.empty:
        st.caption("Nobody has signed in yet.")
    else:
        unlinked = table[(table["coach"] == "") & ~table["sched_admin"]]["email"].tolist()
        if unlinked:
            st.info(f"Not linked to a coach name yet: {', '.join(unlinked)}. Pick their name in the **Coach** column "
                    "so they can edit their teams' preferences.")
        edited = st.data_editor(
            table, hide_index=True, width="stretch", key=f"access_{st.session_state.get('access_ver', 0)}",
            disabled=["email"],
            column_config={
                "email": "Email", "name": "Name",
                "coach": st.column_config.SelectboxColumn("Coach", options=link_opts,
                                                          help="Their name on the schedule (Teams / Locals sheets)"),
                "app_admin": st.column_config.CheckboxColumn("App admin", help="Manages this page"),
                "sched_admin": st.column_config.CheckboxColumn("Scheduling admin",
                                                               help="Edits the schedule and approves requests")})
        changes = []
        for (_, old), (_, new) in zip(table.iterrows(), edited.iterrows()):
            diff_ = [f"{lab} {old[c]!s} → {new[c]!s}" for c, lab in (("name", "name"), ("coach", "coach"),
                     ("app_admin", "app admin"), ("sched_admin", "scheduling admin")) if old[c] != new[c]]
            if diff_:
                changes.append((new, diff_))
        if st.button("Save changes", type="primary", disabled=not changes, key="access_save"):
            kept = []
            for new, diff_ in changes:
                admin = bool(new["app_admin"])
                if new["email"] in always and not admin:
                    admin = True
                    kept.append(new["email"])
                store.save_user(new["email"], new["name"] or "", new["coach"] or "", admin, bool(new["sched_admin"]))
                store.log(VIEWER.log_name, "Access", f"{new['email']}: " + "; ".join(diff_))
            st.session_state.pop("_me", None)
            st.session_state.access_ver = st.session_state.get("access_ver", 0) + 1
            st.session_state.flash = (f"Saved {plural(len(changes), 'change')}."
                                      + (f" {', '.join(kept)} stays an app admin (set in the app's secrets, so the "
                                         "club can't get locked out)." if kept else ""))
            st.rerun()

    with st.expander("Add someone before they sign in"):
        with st.form("access_add", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            n_email = c1.text_input("Club email")
            n_name = c2.text_input("Name")
            n_coach = c3.selectbox("Coach name on the schedule", link_opts)
            c1, c2 = st.columns(2)
            n_app = c1.checkbox("App admin")
            n_sched = c2.checkbox("Scheduling admin")
            if st.form_submit_button("Add", type="primary"):
                e_ = n_email.strip().lower()
                if not e_ or "@" not in e_:
                    st.error("Enter an email address.")
                elif not access.email_allowed(e_, SECRETS):
                    st.error(f"Only @{', @'.join(access.allowed_domains(SECRETS))} addresses can sign in.")
                elif store.get_user(e_):
                    st.warning(f"{e_} is already listed above.")
                else:
                    store.save_user(e_, n_name.strip(), n_coach, n_app, n_sched)
                    store.log(VIEWER.log_name, "Access", f"added {e_}"
                              + "".join(f"; {r}" for r, on in (("app admin", n_app), ("scheduling admin", n_sched)) if on))
                    st.session_state.access_ver = st.session_state.get("access_ver", 0) + 1
                    st.session_state.flash = f"Added {e_}."
                    st.rerun()

    removable = [p_["email"] for p_ in people if p_["email"] not in always and (ME is None or p_["email"] != ME.email)]
    if removable:
        c1, c2 = st.columns([3, 1])
        gone = c1.multiselect("Remove access", removable, key="access_rm",
                              help="They can still sign in with a club address, but come back as a plain coach.")
        if c2.button("Remove", disabled=not gone, width="stretch", key="access_rm_btn"):
            for e_ in gone:
                store.delete_user(e_)
                store.log(VIEWER.log_name, "Access", f"removed {e_}")
            st.session_state.access_ver = st.session_state.get("access_ver", 0) + 1
            st.session_state.flash = f"Removed {plural(len(gone), 'person', 'people')}."
            st.rerun()

    st.subheader("Activity")
    st.caption("Every role change and every preference a coach changes, newest first. Approvals will be listed "
               "here too, and nobody will be able to approve their own request.")
    log_ = store.activity(300)
    if not log_:
        st.caption("Nothing yet.")
    else:
        lg = pd.DataFrame(log_)
        lg["at"] = pd.to_datetime(lg["at"], utc=True).dt.tz_convert("America/Los_Angeles").dt.strftime("%b %-d, %-I:%M %p")
        pnames = {p_["id"]: p_["name"] for p_ in saved}
        lg["project"] = lg["project"].map(lambda x: pnames.get(x, "") if x else "")
        st.dataframe(lg[["at", "who", "action", "detail", "project"]].rename(columns={
            "at": "When", "who": "Who", "action": "What", "detail": "Details", "project": "Schedule"}),
            hide_index=True, width="stretch", height=min(500, 38 + 35 * len(lg)))


# ----------------------------------------------------------- autosave --
# Save the inputs, dates, settings and latest schedule whenever anything
# changed this run, so a refresh or a new visit picks up where you left off.
_settings = {"time_limit": time_limit, "makeup": makeup, "keep_prev": st.session_state.get("keep_prev", True),
             "base_version": st.session_state.get("base_version"),
             "pending_prefs": list(st.session_state.get("pending_prefs") or [])}
# Building the whole payload is the slow part, so only do it when something
# could have changed: the solved schedule, published versions, settings,
# dates or any input table.
_sig = (id(st.session_state.get("result")), len(st.session_state.get("published", [])),
        tuple(v["id"] for v in st.session_state.get("published", [])), json.dumps(_settings), str(start), str(end),
        hashlib.sha1(json.dumps(inp.to_records(), sort_keys=True, default=str).encode()).hexdigest(),
        tuple(sorted(st.session_state.get("periods", {}))))
if CAN and (_sig != st.session_state.get("_save_sig") or st.session_state.pop("_force_save", False)):
    try:
        _db = store.get(MAIN_ID)
        _payload = make_payload(inp, start, end, _settings, st.session_state.get("result"),
                                st.session_state.get("published", []),
                                periods=periods_for_save(_db["payload"] if _db else None))
        _h = payload_hash(_payload)
        if _h == st.session_state.get("last_saved"):
            st.session_state._save_sig = _sig
        else:
            store.save(MAIN_ID, st.session_state.project_name, _payload)
            st.session_state.last_saved = _h
            st.session_state._save_sig = _sig
            st.session_state.periods_dirty = set()
            st.session_state.periods_deleted = set()
            st.session_state.periods = {k: v for k, v in (_payload.get("periods") or {}).items()}
    except Exception as e:  # noqa: BLE001  (not marked saved, so the next click tries again)
        st.toast(f"Couldn't save: {e}", icon="⚠️")
