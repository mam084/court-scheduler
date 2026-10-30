"""Calendar: a month at a time over every date, filterable, with calendar and printable downloads."""

from __future__ import annotations

import re

import streamlit as st

from scheduler.export import calendar_excel, calendar_ics, team_dates_excel
from scheduler.views import calendar_print_html, month_calendar_html
from ui import dates
from ui.common import is_app_admin, off_preference, people_options, team_order, team_sections
from ui.state import S


def render():
    """Calendar section."""
    closures, MY_COACH, MY_TEAMS = S.closures, S.MY_COACH, S.MY_TEAMS
    first, last = dates.month_picker("cal")
    result, gaps = dates.window(first, last)
    dates.where_note(first, last, gaps)
    if result is None:
        st.caption("Nothing on the calendar this month yet.")
        return
    a = result.assignments
    c1, c2 = st.columns([2, 1])
    hl = set(c1.multiselect("Only show a club, team or coach", people_options(a), key="cal_filter"))
    if MY_COACH and c2.toggle("Show only my practices", key="cal_mine",
                              help="Your teams' practices and the programs you work."):
        hl |= {MY_COACH} | MY_TEAMS
    show_locals = c2.toggle("Include locals programs", value=True)
    cal_lc = c2.toggle("Show program coaches", key="cal_lc", disabled=not show_locals,
                       help="List the coaches working each locals program.") and show_locals
    if is_app_admin() and c2.toggle("Highlight practices not at preferred days/times", key="cal_offpref",
                                    help="Only you (app admin) see this. Rings every practice on a no-date or "
                                         "'rather not', or off the team's wishlist; hover one to see why."):
        a = off_preference(a)
    st.markdown(month_calendar_html(a, first, last, closures,
                                    hl or None, show_locals, result.weekend_dates, result.tournaments, cal_lc),
                unsafe_allow_html=True)

    with st.expander("Download or print", expanded=False):
        span = dates.season()
        start, end = dates.download_range("cal", (min(first, span[0]), max(last, span[1])) if span
                                          else (first, last))
        st.caption("Downloads what's shown above" + (f" ({', '.join(sorted(hl))})" if hl else " (everyone)")
                   + " for these dates. Dates not scheduled yet have their programs only.")
        who = ("_" + "_".join(sorted(hl))) if hl else ""
        fname = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"calendar{who}_{start:%Y%m%d}-{end:%Y%m%d}")
        title = f"{st.session_state.get('project_name') or 'Practice calendar'}"

        # Gathered now: the file itself is built when the button is clicked, after this page run,
        # when the page's values (S) are gone.
        _w = dates.window(start, end)[0]
        if _w is None:
            st.caption("Nothing on the calendar on those dates.")
            return

        def _win():
            return _w

        d1, d2, d3 = st.columns(3)
        d1.download_button("Google Calendar (.ics)",
                           lambda: calendar_ics(_win(), hl or None, show_locals, title), f"{fname}.ics",
                           mime="text/calendar", width="stretch",
                           help="In Google Calendar: Settings → Import & export → Import. Tip: import into a new "
                                "calendar (e.g. 'Club practices') so it's easy to remove and re-import after "
                                "changes.")
        d2.download_button("Printable calendar",
                           lambda: (lambda r: calendar_print_html(
                               r.assignments, start, end, closures, hl or None, show_locals, r.weekend_dates,
                               r.tournaments, title, cal_lc).encode())(_win()),
                           f"{fname}.html", mime="text/html", width="stretch",
                           help="Opens in your browser; print it or save it as a PDF. One month per page.")
        d3.download_button("Calendar (Excel)", lambda: calendar_excel(_win(), closures, hl or None, show_locals),
                           f"{fname}.xlsx", width="stretch",
                           help="The club's calendar layout: a block per week, each day with time · what · courts.")
        # Worked out now: the file itself is only built when the button is clicked, after this run.
        order, sections, title_ = team_order(), team_sections(), st.session_state.get("project_name") or ""
        st.download_button("Every team's dates (Excel)", lambda: team_dates_excel(
                               _win(), closures, order, title_, sections),
                           f"team_dates_{start:%Y%m%d}-{end:%Y%m%d}.xlsx", key="team_dates_cal",
                           help="One row per team with every practice and tournament date; tournament days are "
                                "circled.")
