"""Each run's schedule state (inputs, dates, settings) and building the schedule when asked."""

from __future__ import annotations

import contextlib

import pandas as pd
import streamlit as st

from scheduler import ScheduleInput, solve
from scheduler.changes import pattern_of, practices_of
from scheduler.data import COLUMNS, empty
from scheduler.storage import MAIN_ID
from scheduler.views import range_label
from ui.common import decided, plural
from ui.session import open_project
from ui.state import S


def _held(assignments: pd.DataFrame, req: dict) -> pd.DataFrame:
    """The practices to keep exactly as they are while a change goes in (`change`: {"detail": {"rows",
    "replace_pin"}}): all of them, minus the ones it moves or cancels."""
    from datetime import date as _date
    p = assignments[assignments["kind"] == "Practice"]
    det = req.get("detail") or {}
    gone = set()
    for r in det.get("rows") or []:
        if str(r.get("action", "")).strip().lower() in ("remove", "cancel"):
            with contextlib.suppress(ValueError, TypeError, KeyError):
                gone.add((r["team"], pd.to_datetime(str(r["date"])).date(), str(r["time"]).strip()))
    rp = det.get("replace_pin")
    if rp:
        gone.add((rp[0], _date.fromisoformat(rp[1]), rp[2]))
    # New usual days for a team: that team's practices are free to move (everyone else's stay).
    loose = {r["team"] for r in det.get("rows") or [] if str(r.get("action", "")).strip().lower().startswith("usual")}
    keep = [(n, d, s) not in gone and n not in loose for n, d, s in zip(p["name"], p["date"], p["slot"])]
    return p[keep]


def run():
    """Load these dates' inputs and settings, run the solver if Auto-schedule (or a change) asked
    for it, and record the result."""
    CAN, saved = S.CAN, S.saved
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

    # Auto-schedule rebuilds. So does a change (a program, a tournament, a hand edit, an approved
    # request), but only on dates that have been scheduled: picking or changing dates never
    # schedules practices by itself.
    resolve = st.session_state.pop("resolve", False)
    built = (st.session_state.get("result") is not None
             and st.session_state.get("result_period") == (start, end))
    if resolve and not built and not S.run_pressed:
        resolve = False
        st.session_state.pop("overrides_backup", None)
        st.session_state.pop("holding", None)
        _appr = st.session_state.pop("approving", None)
        if _appr is not None:  # nothing built on its dates yet: it goes in when they're scheduled
            st.session_state.flash = decided(_appr, "approved", st.session_state.pop("approving_note", ""))
    if S.run_pressed or resolve:
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
                    seed = st.session_state.pop("seed_prev", None)
                    if base is not None:
                        prev, prev_pat = practices_of(base), pattern_of(base)
                    elif st.session_state.get("result") is not None and st.session_state.result.ok:
                        prev, prev_pat = st.session_state.result.assignments, st.session_state.result.pattern
                    elif seed is not None:  # new dates: start from what was there (and the usual week before)
                        prev, prev_pat = seed
                holding = st.session_state.get("holding")
                if (restore is None and holding is not None and built
                        and (st.session_state.get("approving") is not None or published)):
                    # A coach's request, or a hand edit once these dates are published: every other
                    # practice stays exactly where it is (same time, same court), so nobody's pushed out.
                    restore = _held(st.session_state.result.assignments, holding)
                st.session_state.result = solve(inp, start, end, time_limit_s=time_limit, makeup_closures=makeup,
                                                previous=prev, previous_pattern=prev_pat, restore=restore)
                if restore_ver is not None:
                    st.session_state.base_version = restore_ver["id"]
                st.session_state.pop("pending_rebuild", None)
                st.session_state.pending_prefs = []
                st.session_state.result_period = (start, end)
                _appr = st.session_state.pop("approving", None)
                if _appr is not None and st.session_state.result.ok:  # the change built fine: approved
                    st.session_state.flash = decided(_appr, "approved", st.session_state.pop("approving_note", ""))
                elif _appr is not None:  # no schedule with it: take the change back out, keep the request waiting
                    if st.session_state.get("overrides_backup") is not None:
                        inp.overrides = st.session_state.overrides_backup
                        st.session_state.result = solve(inp, start, end, time_limit_s=time_limit,
                                                        makeup_closures=makeup, previous=prev,
                                                        previous_pattern=prev_pat)
                    st.session_state.pop("flash", None)
                    st.error("That request can't fit in the schedule, so it wasn't applied. It's still waiting.")
                sf = st.session_state.result.shortfalls if st.session_state.result.ok else None
                if sf is not None and len(sf):
                    st.toast(f"{plural(sf['team'].nunique(), 'team')} short of practices in "
                             f"{plural(sf['week_of'].nunique(), 'week')}. See the warning at the top.", icon="⚠️")
                elif sf is not None:
                    st.toast("Every team got all its practices.")
            except ValueError as e:
                backup = st.session_state.pop("overrides_backup", None)
                if backup is not None:  # a manual change that can't work: undo it, keep the schedule
                    inp.overrides = backup
                    st.session_state.pop("flash", None)  # its "done" message no longer applies
                    _appr = st.session_state.pop("approving", None)
                    st.session_state.pop("approving_note", None)
                    st.error(f"That change wasn't applied. {e}"
                             + (" The request is still waiting; decline it with a note, or change something "
                                "else first." if _appr else ""))
                else:
                    st.session_state.result = None
                    st.error(str(e))
            else:
                st.session_state.pop("overrides_backup", None)
        if not st.session_state.get("rebuild_queue"):
            st.session_state.pop("holding", None)
        # Auto-schedule pressed and it built: start the page again so everything (the sidebar's dates
        # too) shows the new schedule.
        _r = st.session_state.get("result")
        if S.run_pressed and _r is not None and _r.ok and not st.session_state.get("rebuild_queue"):
            sf = _r.shortfalls
            st.session_state.flash = (f"Scheduled {range_label(start, end)}. "
                                      + (f"{plural(sf['team'].nunique(), 'team')} short of practices in "
                                         f"{plural(sf['week_of'].nunique(), 'week')}: see the warning above."
                                         if len(sf) else "Every team got all its practices."))
            st.rerun()
        # A change that touched other scheduled dates too (e.g. a practice moved into the next run of
        # dates): rebuild those next.
        queue = st.session_state.get("rebuild_queue") or []
        if queue and st.session_state.get("result") is not None and st.session_state.result.ok:
            nxt = queue.pop(0)
            st.session_state.rebuild_queue = queue
            if nxt in st.session_state.periods:
                from ui.session import switch_period
                switch_period(nxt)
                st.session_state.resolve = True
                st.rerun()
        elif queue:
            st.session_state.rebuild_queue = []

    result = st.session_state.get("result")
    if result is not None and st.session_state.get("result_period") != (start, end):
        result = None  # dates changed since the last run
    S.update(inp=inp, start=start, end=end, time_limit=time_limit, makeup=makeup, keep_prev=keep_prev,
             published=published, result=result)
