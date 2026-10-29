"""Inputs: every input table, editable in place."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from scheduler.data import ATTRS, COLUMNS
from ui.state import S


def render():
    """Inputs section."""
    inp = S.inp
    st.markdown("Edit any table, then press **Rebuild these dates** in the sidebar. "
                "Download the tables as a spreadsheet to keep your changes.")
    help_ = {
        "Teams": "Girls teams can leave head_coach blank (taken from the name, e.g. 18-Jordan). "
                 "rank = pecking order within the age group, like `16-3` (easier: the Pecking order tab). "
                 "start_date = first day the team can practice. weekend_only limits weekends to listed options "
                 "(`11/1 AM; 11/8 3PM`); weekend_pref is a soft preference (`AM`, `Sat AM; Sun PM`). "
                 "late_ok = Yes lets a 14s-and-under team practice at 7:20 (avoided unless needed). "
                 "practices = practices a week (blank = 2).",
        "Availability": "One row per rule. who = team or coach. type = No (hard) or Not preferred. "
                        "days like `Mon` / `Weekdays`, dates like `11/10-11/16`, times like `5:30` or `3:45` "
                        "(blank = any).",
        "Wishlist": "Ranked day/time wishes. choice 1 or 2. Blank days = any day.",
        "Locals": "Programs. side = Girls / Boys (colors). Every program uses our courts (on_site is ignored). "
                  "Times like `5:30`, `3:45-7:20`, JumpStart `3:30` / `4:30`, weekend `9AM-1PM`. "
                  "Courts like `1-4`, `P` for the practice court; blank/TBD until known. "
                  "skip = single dates it doesn't run (`11/12; 11/26`). Easier: use the Programs tab.",
        "Weekends": "Open days in 2-hour blocks (easier: the Open days tab): weekends, and weekdays over a school "
                    "break. date like `11/7` or a range `12/21-1/2`; open like `9AM-9PM`. Days sharing a group with "
                    "required = Yes mean 'at least one of these must be used'.",
        "Resolved": "Coverage flags marked resolved (easier: Resolve on the flag). flag = the flag's id; note = how "
                    "it's covered.",
        "Closures": "Club closed all day.",
        "Coaches": "Coach roster: anyone not on a team yet (e.g. coaches who only work programs), plus contact info.",
        "Overrides": "Manual changes the scheduler always keeps (easier from the Edit practices tab). action = Add "
                     "(date + time, optional court), Extra (same, on top of the week's practices), "
                     "Remove (date, optional time; a makeup is found), Cancel (no makeup) "
                     "or Usual (weekdays + time, optional court).",
        "Calendars": "Google Calendar links (easier: the Publish tab). kind = Team, Club or Program; name = the "
                     "team / club / program; calendar_id = the calendar's ID from its Google Calendar settings.",
        "Privates": "1-hour private lessons (easier: the Privates tab). time = start, like 4:00 PM. court = P "
                    "(practice court) or 1-7. type = Team or Personal; team = which team, for team privates.",
        "Tournaments": "type = Local (hosted here: blocks its courts and coaches until 7 PM; blank courts = all; "
                       "coaches = who's working it) or Travel "
                       "(away: the teams matching club + ages, or the listed teams, can't practice those days "
                       "and avoid the days right before and after). Ages like `15-16` or `14U`.",
    }
    sig_key = abs(hash((st.session_state.get("inp_sig"), st.session_state.get("locals_ver", 0)))) % 10**8
    for sheet, attr in ATTRS.items():
        st.subheader(sheet)
        st.caption(help_.get(sheet, ""))
        df = getattr(inp, attr).copy()
        for c in COLUMNS[sheet]:
            if c not in df.columns:
                df[c] = None
        df = df[COLUMNS[sheet]].astype(object)
        df = df.map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v))
                    or str(v) in ("None", "nan", "NaT") else str(v)).astype(object)
        edited = st.data_editor(df, num_rows="dynamic", width="stretch",
                                key=f"edit_{attr}_{sig_key}", hide_index=True)
        setattr(inp, attr, edited)
    st.download_button("Download inputs (Excel)", inp.to_excel_bytes, "club_inputs.xlsx")
