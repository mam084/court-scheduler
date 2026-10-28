"""Court view: courts as rows, days as columns, any week or the usual week; printouts and exports."""

from __future__ import annotations

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
    short_list_text,
    week_dates,
    weeks_print_html,
)
from ui.common import mine, people_options, plural, team_order, team_sections, template_locals
from ui.dates import view_dates
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
    _view_saved = (S.result, S.start, S.end)  # the dates being worked on
    result, start, end = view_dates()
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
            loc = template_locals(a, start, end)
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
                           help="Every week's court view in one page; each week prints on two landscape pages "
                                "(courts 1-4, then 5-7 and the practice court). "
                                "With **Show open court times** on, every open court time prints with a line to "
                                "write a name in. Open it in your browser, then print or save as PDF.")
        st.download_button("Every team's dates (Excel)", lambda: team_dates_excel(
                               result, closures, team_order(), st.session_state.get("project_name") or "",
                               team_sections()),
                           f"team_dates_{start:%Y%m%d}-{end:%Y%m%d}.xlsx", width="stretch",
                           help="One row per team (girls, then boys, each in the pecking order) with every "
                                "practice (time and court) and tournament date; tournament days are circled. "
                                "Prints landscape on one page wide.")
