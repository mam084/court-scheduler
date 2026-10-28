"""Which dates the court view and calendar show: these dates, every date scheduled, or a
preview of the programs before Auto-schedule."""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import streamlit as st

from scheduler import ScheduleInput, solve
from scheduler.data import empty
from ui.state import S


# The court view and calendar can show every date scheduled so far, not just
# the dates being worked on: each other run of dates shows its latest build.
def season_result(result):
    """Every date scheduled so far as one result: `result` (these dates' build, or the programs
    preview) plus each other run's last build. Returns (result, start, end)."""
    start, end = S.start, S.end
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
    inp, start, end = S.inp, S.start, S.end
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


def view_dates():
    """The schedule and dates the court view or calendar should show:
    - not auto-scheduled yet: the programs on their own (a preview note says so);
    - "Show every date scheduled" on: every run of dates together;
    - otherwise these dates as built.
    Returns (result, start, end)."""
    result, start, end, cleaned, CAN = S.result, S.start, S.end, S.cleaned, S.CAN
    if result is None and cleaned is not None:
        result = programs_preview()
        if result is not None:
            st.info("**Not auto-scheduled yet.** Showing the programs, tournaments and open days for these dates. "
                    + ("Press **Auto-schedule** when the teams are in to add practices." if CAN else
                       "Practices show up once the scheduling admins build these dates."))
    if result is not None and st.session_state.get("periods"):
        if st.toggle("Show every date scheduled", key="all_dates",
                     help="Include the other dates in the Dates menu (each as last built), so you can look back or "
                          "ahead while working on these."):
            result, start, end = season_result(result)
    return result, start, end
