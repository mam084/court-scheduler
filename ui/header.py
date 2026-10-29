"""The top of the page: the title, summary numbers, coverage flags, warnings and the section menu."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from scheduler import privates as pv
from scheduler.data import program_coach_conflicts, tournament_conflicts
from scheduler.views import range_label, short_list_text
from ui.common import cleaned_inputs, mine, plural
from ui.flags import compute, flags_panel
from ui.nav import ADMIN_ONLY, ALL_VIEWS, TAB_ACCESS
from ui.state import S


def render():
    """Title, numbers, flags and warnings; then the section menu. Records the chosen section."""
    ME = S.ME
    VIEWER = S.VIEWER
    CAN = S.CAN
    inp = S.inp
    start = S.start
    end = S.end
    time_limit = S.time_limit
    result = S.result
    st.header(st.session_state.project_name)

    try:
        cleaned = cleaned_inputs(inp, start, end)
        closures = cleaned.closures
    except ValueError as e:
        st.error(str(e))
        cleaned, closures = None, {}
    privs, priv_problems = pv.rows(inp.privates, start.year)
    # A coach sees only the warnings about their own teams and themselves.
    COACH_VIEW = not CAN
    MY_COACH = VIEWER.coach_name
    MY_TEAMS = set(VIEWER.my_teams(cleaned.teams)) if cleaned else set()

    t_conflicts = tournament_conflicts(cleaned.teams, cleaned.tournaments) if cleaned else []
    if COACH_VIEW:
        t_conflicts = [c for c in t_conflicts if (MY_COACH and c["coach"] == MY_COACH)
                       or set(c["teams"]) & MY_TEAMS or set(c.get("teams2", [])) & MY_TEAMS]
    p_conflicts = program_coach_conflicts(cleaned.locals, start, end, closures) if cleaned else []
    if COACH_VIEW:
        p_conflicts = [c for c in p_conflicts if MY_COACH and c["coach"] == MY_COACH]

    S.update(cleaned=cleaned, closures=closures, privs=privs, priv_problems=priv_problems, COACH_VIEW=COACH_VIEW,
             MY_COACH=MY_COACH, MY_TEAMS=MY_TEAMS, t_conflicts=t_conflicts, p_conflicts=p_conflicts)
    compute()  # the coverage flags (S.FLAGS, S.RESOLVED)
    # Everything that needs a look goes in one collapsible box: (st.error | st.warning, markdown).
    alerts: list[tuple] = []

    if CAN and inp.teams.dropna(how="all").empty:
        st.info("**Getting started:** 1) set up the locals in **Programs**, tournaments in "
                "**Tournaments** and any weekends or school-break days in **Open days**; 2) **Import coaches' "
                "spreadsheet** in the sidebar for the teams, no-dates and wishlists; 3) pick dates under "
                "**Auto-schedule** in the sidebar.")
    elif result is None:
        if st.session_state.get("nav") not in ("Court view", "Calendar"):  # those show their own note
            st.info("These dates aren't scheduled yet. Pick them under **Auto-schedule** in the sidebar to build "
                    "their practices." if CAN else "These dates haven't been scheduled yet.")
    elif not result.ok:
        st.error(f"No schedule found ({result.status}). Check for more teams than open courts, "
                 "or a coach whose no-dates block every slot.")
        result = None
    else:
        m = result.metrics
        st.caption(f"**{range_label(start, end)}** (the scheduled dates you're looking at)"
                   + ("" if st.session_state.get("published") else " · not published yet"))
        c = st.columns(6)
        c[0].metric("Practices", m["practices_scheduled"])
        c[1].metric("Missed", m["missed_practices"], help="Practices short of each team's weekly target")
        c[2].metric("Weekend practices", m["weekend_practices"])
        c[3].metric("Wishlist 1st choice", f"{m['wishlist_pct']}%" if m["wishlist_pct"] is not None else "–",
                    help="Weekday practices matching the team's 1st-choice wishlist")
        c[4].metric("Coach double-ups", m["double_ups"], help="A coach's teams back-to-back on the same day")
        c[5].metric("On usual schedule", f"{m['on_pattern_pct']}%" if m.get("on_pattern_pct") is not None else "–",
                    help="Weekday practices on the team's usual day and time (break days in 2-hour blocks don't count)")
        short = result.shortfalls
        if len(short) and COACH_VIEW:
            short = short[short["team"].map(mine)]
        if len(short):
            n_teams, n_missed = short["team"].nunique(), int(short["short"].sum())
            lines = [f"- **Week of {ws:%b %-d}:** {short_list_text(g)}" for ws, g in short.groupby("week_of")]
            alerts.append((st.error, f"**{plural(n_teams, 'team')} {'is' if n_teams == 1 else 'are'} short "
                                     f"{plural(n_missed, 'practice')}:**\n" + "\n".join(lines)
                           + ("" if COACH_VIEW else "\n\nWhy each one happened is in the **Review** tab, and the full "
                                                     "week-by-week counts are there too.")))
        my_broken = [b for b in result.broken if mine(b["team"])]
        if my_broken:
            lines = [f"- **{b['team']}** · {b['date']:%a %b %-d} {b['slot_label']}: {b['who']}" for b in my_broken]
            alerts.append((st.warning, f"**{plural(len(my_broken), 'practice')} had to go on a no-date** so the team "
                                       "still gets 2 practices that week:\n" + "\n".join(lines)))
        iss = result.issues if result.issues is not None else pd.DataFrame(columns=["issue"])
        if COACH_VIEW and len(iss):
            iss = iss[iss["team"].map(mine)]
        satsun = iss[iss["issue"].astype(str).str.startswith("Saturday then Sunday")] if len(iss) else iss
        if len(satsun):
            lines = [f"- **{r.team}** · {r.issue.split(': ', 1)[-1]}" for r in satsun.itertuples()]
            alerts.append((st.warning, f"**{plural(len(satsun), 'team')} practice{'s' if len(satsun) == 1 else ''} "
                                       "Saturday then Sunday**, the only way to avoid a missed practice or a head "
                                       "coach's no-date:\n" + "\n".join(lines)))
        pair = iss[iss["issue"].astype(str).str.startswith("Paired:")] if len(iss) else iss
        if len(pair):
            pair = pair.assign(_o=pair["issue"].map(lambda x: x.split(": ", 1)[-1])).sort_values(["week_of", "_o"])
            lines = [f"- **{r.issue.split(': ', 1)[-1]}** · {r.detail.split(';')[0]}: {r.team}"
                     for r in pair.itertuples()]
            alerts.append((st.warning, f"**{plural(len(pair), 'timeslot')} had to pair teams (3 teams on 2 courts)**, "
                                       "the only way to keep those practices without breaking a no-date:\n"
                           + "\n".join(lines)))
        asst = iss[iss["issue"].astype(str).str.startswith("Assistant no-date broken")] if len(iss) else iss
        if len(asst):
            alerts.append((st.warning, f"**{plural(len(asst), 'practice')} without an assistant who marked a "
                                       "no-date:**\n" + "\n".join(f"- **{r.team}** · {r.issue.split(': ', 1)[-1]}: "
                                                                   f"{r.detail}" for r in asst.itertuples())))
        if result.warnings and CAN:
            alerts.append((st.warning, f"**{plural(len(result.warnings), 'note')} about the input:**\n"
                           + "\n".join(f"- {w}" for w in result.warnings)))
        if m.get("status") == "FEASIBLE" and CAN:
            st.caption(f"The solver hit its {time_limit}-second limit, so this is a good schedule but maybe not "
                       "the best one. Raise the time limit in **Solver settings** for a better one.")
        if CAN and st.session_state.get("pending_prefs"):
            _pp = st.session_state.pending_prefs
            st.info(f"**Coaches changed preferences since the last build:** {'; '.join(_pp[-6:])}"
                    + (f" (+{len(_pp) - 6} more)" if len(_pp) > 6 else "")
                    + ". Press **Rebuild these dates** in the sidebar to fit them in.")
        if CAN and st.session_state.get("pending_rebuild"):
            st.info(f"**{st.session_state.pending_rebuild}** since the last build. Press **Rebuild these dates** "
                    "in the sidebar "
                    "when you're done making changes.")

    flags_panel(key="flags_top")

    if alerts:
        n_red = sum(f is st.error for f, _ in alerts)
        with st.expander(f"{plural(len(alerts), 'warning')}" + (f" · {n_red} to fix" if n_red else ""),
                         expanded=True, key="warnings_box", icon="🚩" if n_red else "⚠️"):
            for show, text in alerts:
                show(text)

    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))

    staged = st.session_state.get("staged")
    if CAN and staged and staged.get("what"):
        from ui.edits import update_schedule
        with st.container(border=True):
            c1, c2 = st.columns([4, 1], vertical_alignment="center")
            n = len(staged["what"])
            c1.markdown(f"**{plural(n, 'change')} waiting:** " + "; ".join(staged["what"][-5:])
                        + (f" (+{n - 5} more)" if n > 5 else "") + ". They're on the schedule already; "
                        "**Update schedule** fits everything else around them"
                        + (" (nothing else moves, since these dates are published)." if S.published else "."))
            if c2.button("Update schedule", type="primary", width="stretch", key="upd_go"):
                update_schedule()

    # Navigation. Only the chosen section is built on each click (tabs would
    # build every section every time), and the choice is remembered, so saving a
    # program or removing something keeps you where you were.
    VIEWS = [v for v in ALL_VIEWS if (CAN or v not in ADMIN_ONLY)
             and (v != TAB_ACCESS or (ME is not None and VIEWER.can_manage_access and VIEWER.acting_as is None))]
    if st.session_state.get("nav") not in VIEWS:
        st.session_state.nav = VIEWS[0]
    VIEW = st.radio("Section", VIEWS, key="nav", horizontal=True, label_visibility="collapsed")
    S.update(result=result, VIEW=VIEW)
