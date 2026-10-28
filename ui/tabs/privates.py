"""Privates: 1-hour private lessons, checked against the schedule."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from scheduler import privates as pv
from scheduler.config import ALL_COURTS, COURTS, DAYS, PRACTICE_COURT, court_name, court_short
from ui import dates
from ui.common import plural
from ui.edits import submit_request
from ui.state import S


def render():
    """Privates section."""
    CAN = S.CAN
    inp = S.inp
    start = S.start
    result = S.result
    cleaned = S.cleaned
    closures = S.closures
    privs = S.privs
    priv_problems = S.priv_problems
    MY_COACH = S.MY_COACH
    st.markdown("Book **private lessons** as 1-hour sessions. Each one is checked against the schedule, so a court "
                "or coach that's already busy then is flagged. Privates show on the weekly court view.")
    coach_opts = cleaned.coaches if cleaned else sorted({p["coach"] for p in privs})
    _span = dates.season()  # every scheduled date, so a private on any of them is checked
    _all = dates.window(*_span, with_preview=False)[0] if _span else None
    a_now = _all.assignments if _all is not None else (result.assignments if result is not None else None)
    if not CAN and not MY_COACH:
        st.info("Your login isn't linked to a coach name yet, so you can't ask for a private here. Ask an app "
                "admin to link it.")
    if CAN or MY_COACH:
        if not CAN:
            st.caption("Ask for a private: the scheduling admins approve it (you'll get an email), then it's booked.")
        with st.container(border=True):
            c1, c2, h1, h2, h3 = st.columns([2.2, 1.3, 0.9, 0.9, 1.2], vertical_alignment="bottom")
            if CAN:
                p_coach = c1.selectbox("Coach", coach_opts + ["Someone else…"], key="pv_coach",
                                       index=None, placeholder="Choose a coach")
            else:
                p_coach = MY_COACH
                c1.text_input("Coach", value=MY_COACH, disabled=True, key="pv_coach_me")
            p_date = c2.date_input("Date", value=dates.view_day(), format="MM/DD/YYYY", key="pv_date")
            p_hr = h1.selectbox("Start hour", list(range(1, 13)), index=3, key="pv_hour",
                                help="Any start time; privates are always 1 hour.")
            p_mn = h2.selectbox("Minutes", list(range(0, 60, 5)), format_func=lambda x: f":{x:02d}", key="pv_min")
            p_ap = h3.radio("AM / PM", ["AM", "PM"], index=1, horizontal=True, key="pv_ampm")
            if p_coach == "Someone else…":
                p_coach = st.text_input("Coach name", key="pv_coach_new").strip() or None
            p_start = (p_hr % 12 + (12 if p_ap == "PM" else 0)) * 60 + p_mn
            p_time = pv.clock(p_start)
            _day = dates.window(p_date, p_date)[0]  # whatever's on the calendar that day
            a_day = _day.assignments if _day is not None else None
            free = pv.free_courts(p_date, p_start, a_day, privs, ALL_COURTS)
            c1, c2, c3 = st.columns([1, 1, 2])
            p_team_private = c1.toggle("Team private", key="pv_is_team",
                                       help="On: a private for one of the club's teams. "
                                            "Off: a personal (non-team) private.")
            court_opts = [PRACTICE_COURT] + [c for c in COURTS]
            p_court = c2.selectbox("Court", court_opts, key="pv_court",
                                   format_func=lambda c: court_name(c) + ("" if c in free else " (taken)"))
            my_teams = [t.name for t in cleaned.teams if p_coach in t.staff] if cleaned and p_coach else []
            all_teams = [t.name for t in cleaned.teams] if cleaned else []
            if p_team_private:
                p_team = c3.selectbox("Team", my_teams + [t for t in all_teams if t not in my_teams], key="pv_team")
            else:
                p_team = ""
                c3.caption("Personal private (not for a team).")
            c1, c2 = st.columns(2)
            p_player = c1.text_input("Player(s) (optional)", key="pv_player")
            p_note = c2.text_input("Note (optional)", key="pv_note")
            trial = {"row": -1, "coach": p_coach or "", "date": p_date, "day": DAYS[p_date.weekday()], "start": p_start,
                     "end": p_start + 60, "time": p_time, "end_label": pv.clock(p_start + 60), "court": p_court,
                     "type": "Team" if p_team_private else "Personal", "team": p_team, "player": p_player,
                     "note": p_note, "what": ""}
            clash = pv.conflicts(privs + [trial], a_day, closures).get(-1, []) if p_coach else []
            if clash:
                st.warning("Heads up: " + "; ".join(clash) + ".")
            if not CAN and st.button("Send request", type="primary", key="pv_request"):
                row = {"coach": p_coach, "date": f"{p_date:%m/%d/%Y}", "time": p_time, "court": court_short(p_court),
                       "type": trial["type"], "team": p_team, "player": p_player.strip(), "note": p_note.strip()}
                submit_request("private", p_team,
                               f"{trial['type']} private for {p_coach}: {p_date:%a %b %-d} {p_time}, "
                               f"{court_name(p_court)}"
                               + (f" ({p_team})" if p_team else ""), {"private": row},
                               ("; ".join(clash) + ". " if clash else "") + p_note.strip())
            if CAN and st.button("Book private", type="primary", disabled=not p_coach):
                row = {"coach": p_coach, "date": f"{p_date:%m/%d/%Y}", "time": p_time, "court": court_short(p_court),
                       "type": trial["type"], "team": p_team, "player": p_player.strip(), "note": p_note.strip()}
                inp.privates = pd.concat([inp.privates, pd.DataFrame([row])], ignore_index=True)
                st.session_state.flash = (f"Booked a {trial['type'].lower()} private: {p_coach}, {p_date:%a %b %-d} "
                                          f"{p_time}, {court_name(p_court)}"
                                          + (" (flagged: " + "; ".join(clash) + ")" if clash else "") + ".")
                st.rerun()

    for msg in priv_problems:
        st.error(msg)
    st.subheader("Privates")
    if not privs:
        st.caption("None booked yet.")
    else:
        flags = pv.conflicts(privs, a_now, closures)
        c1, c2, c3 = st.columns([2, 1, 1])
        who = c1.multiselect("Only show these coaches", sorted({p["coach"] for p in privs}), key="pv_filter")
        kind = c2.radio("Type", ["All", "Team", "Personal"], horizontal=True, key="pv_kind")
        only_flag = c3.toggle("Only conflicts", key="pv_only_flag", disabled=not flags)
        shown = [p for p in privs if (not who or p["coach"] in who) and (kind == "All" or p["type"] == kind)
                 and (not only_flag or p["row"] in flags)]
        if flags:
            st.warning(f"{plural(len(flags), 'private')} {'clashes' if len(flags) == 1 else 'clash'} with the "
                       "schedule or another private (see the Conflict column).")
        st.dataframe(pd.DataFrame([{
            "Coach": p["coach"], "Date": f"{p['date']:%a %b %-d}", "Time": f"{p['time']}–{p['end_label']}",
            "Court": court_name(p["court"]), "Type": p["type"], "Team": p["team"], "Player": p["player"],
            "Note": p["note"], "Conflict": "; ".join(flags.get(p["row"], []))} for p in shown]),
            hide_index=True, width="stretch")
        lab = {p["row"]: f"{p['coach']} · {p['date']:%a %b %-d} {p['time']} · {court_name(p['court'])}" for p in shown}
        c1, c2 = st.columns([3, 1])
        drop = c1.multiselect("Remove privates", list(lab), format_func=lambda i: lab[i], key="pv_drop") if CAN else []
        if CAN and c2.button("Remove", disabled=not drop, width="stretch", key="pv_drop_btn"):
            inp.privates = inp.privates.reset_index(drop=True).drop(index=drop).reset_index(drop=True)
            st.session_state.flash = f"Removed {plural(len(drop), 'private')}."
            st.rerun()
        n_team = sum(p["type"] == "Team" for p in privs)
        st.caption(f"{plural(len(privs), 'private')} booked · {n_team} team, {len(privs) - n_team} personal")
    st.download_button("Download every coach's privates (Excel)",
                       lambda: pv.to_excel(privs, coach_opts, pv.conflicts(privs, a_now, closures)),
                       f"privates_{start:%Y%m%d}.xlsx",
                       help="A Coaches sheet with every coach and their team / personal private counts, one list of "
                            "all privates, and a sheet per coach.")
