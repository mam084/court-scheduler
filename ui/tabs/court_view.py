"""Court view: courts as rows, days as columns, any week or the usual week; printouts and exports."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

from scheduler.changes import diff, practices_of
from scheduler.export import team_dates_excel, to_excel
from scheduler.paper import paper_weeks_html
from scheduler.solver import week_start
from scheduler.views import (
    club_stripes,
    court_grid_html,
    legend_html,
    open_slot_map,
    range_label,
    short_list_text,
    week_dates,
    weeks_print_html,
)
from ui import dates
from ui.common import mine, people_options, plural, team_order, team_sections, template_locals
from ui.state import S


def render():
    """Court view section."""
    CAN = S.CAN
    published = S.published
    cleaned = S.cleaned
    closures = S.closures
    privs = S.privs
    COACH_VIEW = S.COACH_VIEW
    MY_COACH = S.MY_COACH
    MY_TEAMS = S.MY_TEAMS
    c1, c2 = st.columns([1, 2])
    pick = c1.selectbox("Show", ["Week", "Weekly template"], key="cv_show",
                        help="Weeks show the actual dates, including weekends, makeups and closures; the template "
                             "is each team's usual weekday slot.")
    if pick == "Week":
        ws = dates.week_picker("cv")
        we = ws + timedelta(days=6)
        result, gaps = dates.window(ws, we)
        dates.where_note(ws, we, gaps)
    else:
        result, gaps = S.result, []
        ws = we = None
        if result is not None:
            st.caption(f"The usual week for {range_label(S.start, S.end)}.")
    if result is None:
        st.caption("Nothing on the calendar here yet.")
        return
    a = result.assignments
    stripes = club_stripes(a["club"]) if "club" in a else {}
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
    if published and S.result is not None and pick == "Week" and S.start <= we and ws <= S.end:
        _s0, _e0 = S.start, S.end
        ch = diff(practices_of(published[-1]), S.result.assignments, cleaned, _s0, _e0)
        if len(ch):
            st.caption(f"{plural(len(ch), 'change')} in {range_label(_s0, _e0)} since “{published[-1]['label']}” "
                       "was published. See **Publish** for the list.")

    st.markdown(legend_html(a.loc[a["kind"] == "Practice", "age"], stripes, show_moved), unsafe_allow_html=True)
    if pick == "Weekly template":
        tmpl = result.pattern.rename(columns={"team": "name"}).assign(kind="Practice", on_pattern=True)
        loc = template_locals(a, S.start, S.end)
        book = pd.concat([tmpl, loc[[c for c in list(tmpl.columns) + ["side"] if c in loc.columns]]],
                         ignore_index=True)
        st.markdown(court_grid_html(book, None, closures, hl or None, stripes,
                                    open_slots=open_slot_map(None, cleaned) if show_open else None,
                                    local_coaches=show_lc),
                    unsafe_allow_html=True)
        st.caption("Courts are ranked 1 → 7 → 6 → 5 → 3 → 4 → 2; older and 1s teams get the better courts.")
    else:
        dd = week_dates(ws, ws, we, result.weekend_dates)
        wk_short = result.shortfalls[result.shortfalls["week_of"] == ws] if len(result.shortfalls) else []
        if len(wk_short) and COACH_VIEW:
            wk_short = wk_short[wk_short["team"].map(mine)]
        if len(wk_short):
            st.error(f"Short of practices this week: {short_list_text(wk_short)}", icon="⚠️")
        st.markdown(court_grid_html(a, dd, closures, hl or None, stripes, result.tournaments,
                                    result.weekend_dates,
                                    open_slots=open_slot_map(dd, cleaned) if show_open else None,
                                    privates=[p for p in week_privs if week_start(p["date"]) == ws],
                                    local_coaches=show_lc),
                    unsafe_allow_html=True)

    # Printouts and downloads: any dates (the scheduled run this week is in, by default).
    with st.expander("Print or download", expanded=False):
        hit = dates.run_at(ws) or dates.run_at(we) if ws else None
        start, end = dates.download_range("cv", (hit[1], hit[2]) if hit else
                                          ((ws, we) if ws else (S.start, S.end)))
        title_ = st.session_state.get("project_name") or "Court schedule"

        # Gathered now: the file itself is built when the button is clicked, after this page run,
        # when the page's values (S) are gone.
        _w = dates.window(start, end)[0]
        if _w is None:
            st.caption("Nothing on the calendar on those dates.")
            return

        def _win():
            return _w

        d0, d1, d2 = st.columns(3)
        d0.download_button("Printable weeks · paper style", type="primary", width="stretch",
                           data=lambda: (lambda r: paper_weeks_html(
                               r.assignments, start, end, closures, r.tournaments, title_, privs, r.practice_counts,
                               show_lc, block_days=r.weekend_dates).encode())(_win()),
                           file_name=f"court_sheets_{start:%Y%m%d}-{end:%Y%m%d}.html", mime="text/html",
                           help="Like the sheets on the board: every timeslot on every court is a line, filled in "
                                "where something's booked and blank where it's open, so privates and changes can "
                                "be written in. Two landscape pages a week, with each day's practice count and the "
                                "week's total at the bottom. Open it in your browser, then print or save as PDF.")
        d1.download_button("Download schedule (Excel)", lambda: to_excel(_win(), closures),
                           f"schedule_{start:%Y%m%d}-{end:%Y%m%d}.xlsx", width="stretch")
        d2.download_button("Printable weeks · color",
                           lambda: (lambda r: weeks_print_html(
                               r.assignments if show_moved else r.assignments.assign(on_pattern=True), start, end,
                               closures, hl or None, r.tournaments, r.weekend_dates, title_, week_privs, show_moved,
                               (lambda dd: open_slot_map(dd, cleaned)) if show_open else None,
                               show_lc).encode())(_win()),
                           f"court_weeks_{start:%Y%m%d}-{end:%Y%m%d}.html", mime="text/html", width="stretch",
                           help="Every week's court view in one page; each week prints on two landscape pages "
                                "(courts 1-4, then 5-7 and the practice court). "
                                "With **Show open court times** on, every open court time prints with a line to "
                                "write a name in. Open it in your browser, then print or save as PDF.")
        # Worked out now: the file itself is only built when the button is clicked, after this run.
        order, sections = team_order(), team_sections()
        st.download_button("Every team's dates (Excel)", lambda: team_dates_excel(
                               _win(), closures, order, title_, sections),
                           f"team_dates_{start:%Y%m%d}-{end:%Y%m%d}.xlsx", width="stretch",
                           help="One row per team (girls, then boys, each in the pecking order) with every "
                                "practice (time and court) and tournament date; tournament days are circled. "
                                "Prints landscape on one page wide.")
