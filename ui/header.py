"""The top of the page: the title, summary numbers, coverage flags, warnings and the section menu."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from scheduler import privates as pv
from scheduler.coach_load import two_places
from scheduler.data import program_coach_conflicts, tournament_conflicts
from scheduler.labels import set_program_shorts
from scheduler.views import range_label, short_list_text
from ui.common import cleaned_inputs, mine, plural, tournament_note
from ui.flags import compute, flags_panel
from ui.nav import ADMIN_ONLY, ALL_VIEWS, TAB_ACCESS
from ui.state import S


def _alert(show, kind: str, head, lines: list[str], tail: str = "") -> dict:
    """One warning: its kind (st.error / st.warning), a heading made from how many lines are still
    showing, one line per thing to look at (each can be dismissed), and a closing note."""
    return {"show": show, "kind": kind, "head": head, "lines": list(lines), "tail": tail}


def _plain(text: str) -> str:
    return text.replace("**", "")


def _dismissed_key(coach_view: bool) -> str:
    """Admins' dismissals are saved with these dates; a coach's last only for their visit."""
    return "dismissed_mine" if coach_view else "dismissed"


def warnings_box(alerts: list[dict], coach_view: bool = False):
    """Every warning in one box. Each line can be dismissed (or a whole warning at once); dismissed
    lines move to a list at the bottom where they can be brought back. A line that changes (a
    different date, team or reason) is new, so it shows again."""
    key = _dismissed_key(coach_view)
    gone = set(st.session_state.get(key) or [])

    def tag(a, line):
        return f"{a['kind']}|{line}"

    live = []
    for a in alerts:
        left = [ln for ln in a["lines"] if tag(a, ln) not in gone]
        if left:
            live.append((a, left))
    hidden = [(a, ln) for a in alerts for ln in a["lines"] if tag(a, ln) in gone]

    def dismiss(tags):
        st.session_state[key] = sorted(gone | set(tags))
        st.rerun()

    def restore(tags):
        st.session_state[key] = sorted(gone - set(tags))
        st.rerun()

    n_red = sum(a["show"] is st.error for a, _ in live)
    label = (plural(len(live), "warning") if live else "No warnings") + (f" · {n_red} to fix" if n_red else "") \
        + (f" · {len(hidden)} dismissed" if hidden else "")
    with st.expander(label, expanded=bool(live), key="warnings_box", icon="🚩" if n_red else "⚠️"):
        for i, (a, left) in enumerate(live):
            a["show"](a["head"](len(left)) + "\n" + "\n".join(f"- {ln}" for ln in left)
                      + (f"\n\n{a['tail']}" if a["tail"] else ""))
            with st.popover("Dismiss…", key=f"wd_pop_{a['kind']}_{i}"):
                pick = st.multiselect("Lines to dismiss", left, format_func=_plain, key=f"wd_pick_{a['kind']}_{i}",
                                      placeholder="Pick one or more")
                c1, c2 = st.columns(2)
                if c1.button("Dismiss selected", disabled=not pick, key=f"wd_some_{a['kind']}_{i}", width="stretch"):
                    dismiss(tag(a, ln) for ln in pick)
                if c2.button(f"Dismiss all {len(left)}", key=f"wd_all_{a['kind']}_{i}", width="stretch"):
                    dismiss(tag(a, ln) for ln in left)
        if hidden:
            st.caption(f"**{plural(len(hidden), 'dismissed line')}**"
                       + ("" if coach_view else " (dismissed for these dates; anyone can bring them back)"))
            lab = {tag(a, ln): _plain(ln) for a, ln in hidden}
            back = st.multiselect("Bring back", list(lab), format_func=lab.get, key="wd_back",
                                  placeholder="Pick lines to show again")
            c1, c2 = st.columns(2)
            if c1.button("Bring back selected", disabled=not back, key="wd_back_go", width="stretch"):
                restore(back)
            if c2.button("Bring back all", key="wd_back_all", width="stretch"):
                restore(lab)


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
        set_program_shorts(cleaned.locals)  # programs' shorthand, for the printouts
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
        if CAN:  # the summary numbers are for the admins
            c = st.columns(6)
            c[0].metric("Practices", m["practices_scheduled"])
            c[1].metric("Missed", m["missed_practices"], help="Practices short of each team's weekly target")
            c[2].metric("Weekend practices", m["weekend_practices"])
            c[3].metric("Wishlist 1st choice", f"{m['wishlist_pct']}%" if m["wishlist_pct"] is not None else "–",
                        help="Weekday practices matching the team's 1st-choice wishlist")
            c[4].metric("Coach double-ups", m["double_ups"], help="A coach's teams back-to-back on the same day")
            c[5].metric("On usual schedule", f"{m['on_pattern_pct']}%" if m.get("on_pattern_pct") is not None else "–",
                        help="Weekday practices on the team's usual day and time (break days in 2-hour blocks "
                             "don't count)")
        short = result.shortfalls
        if len(short) and COACH_VIEW:
            short = short[short["team"].map(mine)]
        if len(short):
            n_teams, n_missed = short["team"].nunique(), int(short["short"].sum())
            lines = [f"**Week of {ws:%b %-d}:** {short_list_text(g)}" for ws, g in short.groupby("week_of")]
            alerts.append(_alert(st.error, "short", lambda n: (f"**{plural(n_teams, 'team')} "
                                 f"{'is' if n_teams == 1 else 'are'} short {plural(n_missed, 'practice')}:**")
                                 if n == len(lines) else f"**Teams short of practices ({plural(n, 'week')}):**",
                                 lines, "" if COACH_VIEW else "Why each one happened is in the **Review** tab, and "
                                                              "the full week-by-week counts are there too."))
        my_broken = [b for b in result.broken if mine(b["team"])]
        if my_broken:
            lines = [f"**{b['team']}** · {b['date']:%a %b %-d} {b['slot_label']}: {b['who']}" for b in my_broken]
            alerts.append(_alert(st.warning, "nodate", lambda n: f"**{plural(n, 'practice')} had to go on a "
                                 "no-date** so the team still gets 2 practices that week:", lines))
        # Practices placed by hand on or next to the team's own travel tournament (pins skip that rule).
        a_ = result.assignments
        pins = a_[(a_["kind"] == "Practice") & (a_["pinned"] == True)] if "pinned" in a_ else a_.iloc[0:0]  # noqa: E712
        t_lines = [f"**{r.name}** · {r.date:%a %b %-d} {r.slot_label}: {why}"
                   for r in pins.itertuples() if mine(r.name) and (why := tournament_note(r.name, r.date))]
        if t_lines:
            alerts.append(_alert(st.warning, "tourn", lambda n: f"**{plural(n, 'practice')} placed by hand next to "
                                 "or during the team's tournament** (the scheduler never puts one there; undo it in "
                                 "**Edit practices** → Manual changes if it's a mistake):", t_lines))
        iss = result.issues if result.issues is not None else pd.DataFrame(columns=["issue"])
        if COACH_VIEW and len(iss):
            iss = iss[iss["team"].map(mine)]
        satsun = iss[iss["issue"].astype(str).str.startswith("Saturday then Sunday")] if len(iss) else iss
        if len(satsun):
            lines = [f"**{r.team}** · {r.issue.split(': ', 1)[-1]}" for r in satsun.itertuples()]
            alerts.append(_alert(st.warning, "satsun", lambda n: f"**{plural(n, 'team')} practice"
                                 f"{'s' if n == 1 else ''} Saturday then Sunday**, the only way to avoid a missed "
                                 "practice or a head coach's no-date:", lines))
        b2b = iss[iss["issue"].astype(str).str.startswith("Back-to-back days")] if len(iss) else iss
        asked = b2b["detail"].astype(str).str.contains("asked") if len(b2b) else b2b
        if len(b2b) and (~asked).any():
            lines = [f"**{r.team}** · {r.issue.split(': ', 1)[-1]}" for r in b2b[~asked].itertuples()]
            more = f"({int(asked.sum())} more the team asked for, e.g. Tue/Wed: see Review.)" if asked.any() else ""
            alerts.append(_alert(st.warning, "b2b", lambda n: f"**{plural(n, 'time')} a team practices two days "
                                 "in a row:**", lines, more))
        pair = iss[iss["issue"].astype(str).str.startswith("Paired:")] if len(iss) else iss
        if len(pair):
            pair = pair.assign(_o=pair["issue"].map(lambda x: x.split(": ", 1)[-1])).sort_values(["week_of", "_o"])
            lines = [f"**{r.issue.split(': ', 1)[-1]}** · {r.detail.split(';')[0]}: {r.team}"
                     for r in pair.itertuples()]
            alerts.append(_alert(st.warning, "pair", lambda n: f"**{plural(n, 'timeslot')} had to pair teams (3 "
                                 "teams on 2 courts)**, the only way to keep those practices without breaking a "
                                 "no-date:", lines))
        asst = iss[iss["issue"].astype(str).str.startswith("Assistant no-date broken")] if len(iss) else iss
        if len(asst):
            alerts.append(_alert(st.warning, "asst", lambda n: f"**{plural(n, 'practice')} without an assistant "
                                 "who marked a no-date:**",
                                 [f"**{r.team}** · {r.issue.split(': ', 1)[-1]}: {r.detail}"
                                  for r in asst.itertuples()]))
        # A coach on two things at once (two teams, or a team and a program they work), live from the
        # schedule, so hand edits and imported weeks count too. A coach sees only their own.
        clash = two_places(result.assignments)
        if COACH_VIEW:
            clash = [x for x in clash if MY_COACH and x["coach"] == MY_COACH]
        if clash:
            alerts.append(_alert(st.warning, "twoplaces", lambda n: f"**{plural(n, 'time')} a coach is "
                                 "scheduled in two places at once:**",
                                 [f"**{x['coach']}** · {x['date']:%a %b %-d}: "
                                  + " and ".join(f"{nm} ({sl})" for nm, sl in x["what"]) for x in clash],
                                 "" if COACH_VIEW else "Move one, or find someone to cover it."))
        if result.warnings and CAN:
            alerts.append(_alert(st.warning, "input", lambda n: f"**{plural(n, 'note')} about the input:**",
                                 list(result.warnings)))
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

    flags_panel(key="flags_top", result=result)

    if alerts:
        warnings_box(alerts, COACH_VIEW)

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
