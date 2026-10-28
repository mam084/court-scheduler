"""The sidebar: who's signed in, View as, the Dates menu and the admins' tools
(new dates, import, solver settings, Auto-schedule)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from scheduler import ScheduleInput, access
from scheduler.data import merge_import
from scheduler.storage import MAIN_ID, current_period, database_url, period_key
from scheduler.views import range_label
from ui.common import blank_template, bound, coach_names, plural, saved_list
from ui.session import period_entry, period_label, switch_period
from ui.state import S


def render():
    """Draw the sidebar; records the viewer, their permissions and whether Auto-schedule was pressed."""
    AUTH_ON, store, ME = S.AUTH_ON, S.store, S.ME
    with st.sidebar:
        st.title("Court Scheduler")
        acting = None
        if ME is not None:
            st.caption(f"Signed in as **{ME.label}** · {', '.join(ME.roles)}"
                       + ("" if ME.coach or ME.sched_admin else " · not linked to a coach name yet"))
            if ME.app_admin:
                _opts = ["Myself", "Scheduling admin", "Coach (not linked)"] + [f"Coach: {c}" for c in coach_names()]
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
        S.update(VIEWER=VIEWER, CAN=CAN)  # switch_period below needs them
        saved = saved_list(database_url(), st.session_state.get("saved_ver", 0))
        names = {p["id"]: p["name"] for p in saved}
        if not CAN and not st.session_state.get("_coach_default"):
            # A coach opens on the dates covering today (else the latest published ones).
            st.session_state._coach_default = True
            _cur = period_key(st.session_state.range_start, st.session_state.range_end)
            _all = dict(st.session_state.periods, **{_cur: period_entry()})
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

        if CAN and ME is not None:
            _n_req = len(store.requests("pending"))
            if _n_req:
                st.info(f"📬 **{plural(_n_req, 'change request')}** waiting on the **Requests** tab.")
        run = False
        if not CAN:
            st.caption("Change your teams' preferences in **Edit practices**, and ask for moves, extra practices, "
                       "cancellations and privates there or in **Privates**. The scheduling admins approve those on "
                       "the **Requests** tab.")
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
                    _these = range_label(st.session_state.range_start, st.session_state.range_end)
                    sure = st.checkbox(f"Yes, remove {_these} for everyone", key=f"del_{cur_key}")
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
    S.update(VIEWER=VIEWER, CAN=CAN, saved=saved, names=names, run_pressed=run)
