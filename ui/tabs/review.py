"""Review: practices per week and everything that differs from the ideal schedule."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from scheduler.changes import burden, practices_of
from scheduler.views import practice_counts_html
from ui import dates
from ui.common import mine
from ui.state import S


def render():
    """Review section."""
    dates.run_picker("rev_run")
    published = S.published
    result = S.result
    cleaned = S.cleaned
    COACH_VIEW = S.COACH_VIEW
    MY_COACH = S.MY_COACH
    MY_TEAMS = S.MY_TEAMS
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
