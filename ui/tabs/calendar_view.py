"""Calendar: a month view, filterable, with calendar and printable downloads."""

from __future__ import annotations

import re

import streamlit as st

from scheduler.export import calendar_excel, calendar_ics, team_dates_excel
from scheduler.views import calendar_print_html, month_calendar_html
from ui.common import people_options, team_order, team_sections
from ui.dates import view_dates
from ui.state import S


def render():
    """Calendar section."""
    CAN, closures, MY_COACH, MY_TEAMS = S.CAN, S.closures, S.MY_COACH, S.MY_TEAMS
    result, start, end = view_dates()
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
        st.markdown(month_calendar_html(a if show_moved else a.assign(on_pattern=True), start, end, closures,
                                        hl or None, show_locals, result.weekend_dates, result.tournaments, cal_lc),
                    unsafe_allow_html=True)

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
        # Worked out now: the file itself is only built when the button is clicked, after this run.
        order, sections, title_ = team_order(), team_sections(), st.session_state.get("project_name") or ""
        st.download_button("Every team's dates (Excel)", lambda: team_dates_excel(
                               result, closures, order, title_, sections),
                           f"team_dates_{start:%Y%m%d}-{end:%Y%m%d}.xlsx", key="team_dates_cal",
                           help="One row per team with every practice and tournament date; tournament days are "
                                "circled.")
