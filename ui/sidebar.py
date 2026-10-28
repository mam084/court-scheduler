"""The sidebar: who's signed in, View as, and the admins' tools (Auto-schedule some dates, import,
solver settings)."""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from scheduler import ScheduleInput, access
from scheduler.data import merge_import
from scheduler.solver import week_start
from scheduler.storage import MAIN_ID, current_period, database_url, period_key
from scheduler.views import range_label
from ui import dates
from ui.common import blank_template, bound, coach_names, pending_requests_count, plural, saved_list
from ui.session import period_label, schedule_dates
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
        dates.follow()  # work on the dates holding the day being looked at

        if CAN and ME is not None:
            _n_req = pending_requests_count()
            if _n_req:
                st.info(f"📬 **{plural(_n_req, 'change request')}** waiting on the **Requests** tab.")
        run = False
        if not CAN:
            st.caption("Change your teams' preferences in **Edit practices**, and ask for moves, extra practices, "
                       "cancellations and privates there or in **Privates**. The scheduling admins approve those on "
                       "the **Requests** tab.")
        else:
            run = _auto_schedule()
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
                        st.session_state.inp_sig = ("project", MAIN_ID, rep.name, rep.size)
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
                            if k == dates.active_key() or k in st.session_state.periods:
                                st.error("Those dates are already on the calendar.")
                            else:
                                st.session_state.periods[k] = ent
                                st.session_state.periods_dirty.add(k)
                                st.session_state._force_save = True
                                st.session_state.flash = f"Added {period_label(ent)} to the calendar."
                                st.rerun()
            if not store.permanent:
                st.caption("⚠️ Saved on the app server only: it survives refreshes but can be cleared when the app "
                           "restarts. Connect a database (see README) to keep schedules permanently.")
    S.update(VIEWER=VIEWER, CAN=CAN, saved=saved, names=names, run_pressed=run)


def _auto_schedule() -> bool:
    """Pick dates and Auto-schedule them; the dates being looked at are filled in. Also: take
    scheduled dates off the calendar. Returns whether Auto-schedule was pressed (and the dates are
    ready for it)."""
    d = dates.view_day()
    hit = dates.run_at(d)
    st.markdown("**Auto-schedule**")
    if hit:
        k, a0, b0 = hit
        st.caption(f"Looking at {d:%b %-d}: scheduled with **{range_label(a0, b0)}**"
                   + ("" if dates.is_published(k) else " (not published)") + ".")
    else:
        # Not scheduled yet: two weeks from that week's Sunday, up to the next scheduled dates.
        rs = dates.runs()
        a0 = max([week_start(d)] + [e_ + timedelta(days=1) for _, _, e_ in rs if e_ < d])
        b0 = min([a0 + timedelta(days=13)] + [s_ - timedelta(days=1) for _, s_, _ in rs if s_ > a0])
        st.caption(f"Looking at {d:%b %-d}: not scheduled yet.")
    c1, c2 = st.columns(2)
    a = c1.date_input("From", value=a0, format="MM/DD/YYYY", key=f"as_from_{a0}_{b0}")
    b = c2.date_input("To", value=b0, format="MM/DD/YYYY", key=f"as_to_{a0}_{b0}")
    rebuild = hit is not None and (a, b) == (hit[1], hit[2])
    pressed = st.button("Rebuild these dates" if rebuild else "Auto-schedule these dates", type="primary",
                        width="stretch", key="as_go",
                        help="Builds practices for exactly these dates around the programs, tournaments and "
                             "open days. Dates already scheduled inside them are rebuilt, keeping practices "
                             "where they are unless something has to move. Weeks that are all open days "
                             "(school breaks) get no practices; coaches add the ones they want.")
    if pressed:
        problem = schedule_dates(a, b)
        if problem:
            st.error(problem)
            return False
        dates.set_day(a if not (a <= d <= b) else d)
        return True
    if hit:
        with st.expander(f"Take {range_label(hit[1], hit[2])} off the calendar"):
            st.caption("Removes these dates' practices and published versions. Programs, tournaments and "
                       "everything else stay.")
            sure = st.checkbox("Yes, remove them", key=f"del_{hit[0]}")
            if st.button("Remove these dates", disabled=not sure, width="stretch", key="as_del"):
                _unschedule(hit[0])
    return False


def _unschedule(key: str):
    """Takes a run of dates off the calendar."""
    from ui.dates import active_key
    if key == active_key():
        st.session_state.update(result=None, result_period=None, published=[], base_version=None)
        st.session_state.periods_deleted.add(key)
    else:
        st.session_state.periods.pop(key, None)
        st.session_state.periods_dirty.discard(key)
        st.session_state.periods_deleted.add(key)
    st.session_state._force_save = True
    st.session_state.flash = "Removed those dates from the calendar."
    st.rerun()
