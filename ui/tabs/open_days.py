"""Open days: weekends and school breaks run in 2-hour blocks."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from scheduler import ScheduleInput, clean
from scheduler.config import COURTS, court_name
from scheduler.data import COLUMNS
from ui import dates
from ui.common import plural
from ui.state import S

OPEN_HOURS = [("9AM", "9 AM"), ("11AM", "11 AM"), ("1PM", "1 PM"), ("3PM", "3 PM"), ("5PM", "5 PM"),
              ("7PM", "7 PM"), ("9PM", "9 PM")]



def render():
    """Open days section."""
    inp, start, end, cleaned, closures = S.inp, S.start, S.end, S.cleaned, S.closures
    st.markdown("**Weekends are open** 9 AM–9 PM on every court, in **2-hour blocks** (9–11, 11–1 … 7–9), and are "
                "used only when weekdays can't fit a practice. The big holidays (Thanksgiving and the day after, "
                "Christmas Eve and Day, New Year's Eve and Day, July 4th) are always closed. Add a weekend here only "
                "to change its hours or courts. **School breaks:** list the weekdays here (e.g. Dec 21 – Jan 2) and "
                "they run in blocks too; a week that's all open days gets no practices automatically, and coaches "
                "add the ones they want. Break **camps** are programs (the **Programs** tab, e.g. Mon–Fri 9–11 AM). "
                "To close any other day, use **Close a day** below.")
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
                before = inp.weekends.copy()
                inp.weekends = trial.astype(object)
                st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
                st.session_state.flash = (f"Opened {plural(len(days_), 'day')} in 2-hour blocks "
                                          f"({dict(OPEN_HOURS)[o_a]}–{dict(OPEN_HOURS)[o_b]}). "
                                          + dates.rebuild_for([(before, inp.weekends, ("date",))]))
                st.rerun()

    st.subheader("Open days")
    wk = inp.weekends.dropna(how="all")
    if wk.empty:
        st.caption("None yet. Weekdays not listed here use the usual 3:45 / 5:30 / 7:20 times; weekends not listed "
                   "are open 9 AM–9 PM on every court.")
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
            before = inp.weekends.copy()
            inp.weekends = wk.iloc[keep].reset_index(drop=True)
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.flash = (f"Removed {plural(len(drop_o), 'row')}. "
                                      + dates.rebuild_for([(before, inp.weekends, ("date",))]))
            st.rerun()

    _closed_days()


def _closed_days():
    """Closing a day (no practices, programs or privates): any date, weekends included."""
    from scheduler.closures import major_holidays
    inp, start = S.inp, S.start
    st.subheader("Closed days")
    with st.container(border=True):
        c1, c2, c3 = st.columns([1, 2, 1], vertical_alignment="bottom")
        c_day = c1.date_input("Close a day", value=start, format="MM/DD/YYYY", key="cl_day")
        c_why = c2.text_input("Why", key="cl_why", placeholder="e.g. gym rented out")
        if c3.button("Close it", type="primary", width="stretch", key="cl_go"):
            before = inp.closures.copy()
            row = pd.DataFrame([{"date": f"{c_day:%m/%d/%Y}", "reason": c_why.strip() or "Closed"}],
                               columns=COLUMNS["Closures"])
            inp.closures = pd.concat([inp.closures, row], ignore_index=True).astype(object)
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.flash = (f"Closed {c_day:%a %b %-d}. "
                                      + dates.rebuild_for([(before, inp.closures, ("date",))]))
            st.rerun()
    hol = sorted((d, n) for y in (start.year, start.year + 1) for d, n in major_holidays(y).items() if d >= start)
    st.caption("Always closed: " + ", ".join(f"{n} ({d:%a %b %-d})" for d, n in hol[:7]) + ".")
    cl = inp.closures.dropna(how="all")
    if not cl.empty:
        view_ = cl.fillna("").astype(str)
        labs = [f"{r['date']} · {r['reason']}" for _, r in view_.iterrows()]
        labs = [lab if labs.count(lab) == 1 else f"{lab} (#{i + 1})" for i, lab in enumerate(labs)]
        st.dataframe(view_[["date", "reason"]].rename(columns=str.title), hide_index=True, width="stretch")
        c1, c2 = st.columns([3, 1])
        drop_c = c1.multiselect("Reopen", labs, placeholder="Choose closed days to reopen", key="cl_drop")
        if c2.button("Reopen", disabled=not drop_c, width="stretch", key="cl_rm",
                     help="The big holidays stay closed even if they're listed here."):
            before = inp.closures.copy()
            inp.closures = cl.iloc[[i for i, lab in enumerate(labs) if lab not in drop_c]].reset_index(drop=True)
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.flash = (f"Reopened {plural(len(drop_c), 'day')}. "
                                      + dates.rebuild_for([(before, inp.closures, ("date",))]))
            st.rerun()
