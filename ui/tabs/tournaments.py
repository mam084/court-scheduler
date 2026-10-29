"""Tournaments: add, edit and remove local and travel tournaments."""

from __future__ import annotations

import re

import pandas as pd
import streamlit as st

from scheduler.config import ALL_COURTS, COURTS, court_name
from scheduler.data import blank, parse_courts, parse_list, parse_names, to_date
from ui import dates
from ui.common import plural
from ui.flags import flags_panel
from ui.state import S


def render():
    """Tournaments section."""
    inp, start, cleaned = S.inp, S.start, S.cleaned
    st.markdown("**Local** tournaments are hosted here and take over their courts until 7 PM (7:20 practices still "
                "happen), along with the coaches working them. "
                "**Travel** tournaments don't use our courts, but the teams going can't practice those days, "
                "avoid the days right before and after, and get priority for their two practices that week.")
    team_list = cleaned.teams if cleaned else []
    clubs = sorted({t.club for t in team_list if t.club})
    ages_all = sorted({t.age for t in team_list})
    program_names = sorted({lp.name for lp in cleaned.locals}) if cleaned else []

    # Add a new tournament, or pick one to edit (the form fills in with it).
    _trows = inp.tournaments.fillna("")
    _tlab = [f"{r['name']} · {r['type']} · {r['start_date']}" for _, r in _trows.iterrows()]
    _tlab = [lab if _tlab.count(lab) == 1 else f"{lab} (#{i + 1})" for i, lab in enumerate(_tlab)]
    t_sel = st.selectbox("Tournament", ["__new__"] + list(range(len(_trows))),
                         format_func=lambda o: "➕ New tournament" if o == "__new__" else f"Edit: {_tlab[o]}",
                         key=f"t_sel_{len(_trows)}")
    editing = t_sel != "__new__"
    cur_t = _trows.iloc[t_sel] if editing else None
    sfx = f"_{t_sel}_{st.session_state.get('locals_ver', 0)}" if editing else ""

    def _tval(col, default=""):
        return str(cur_t[col]).strip() if editing and not blank(cur_t[col]) else default

    def _tdate(col, default):
        try:
            return to_date(_tval(col), start.year) if _tval(col) else default
        except (ValueError, TypeError):
            return default

    with st.container(border=True):
        c1, c2 = st.columns([2, 1])
        t_name = c1.text_input("Tournament name", value=_tval("name"), placeholder="e.g. Boys 14 Local Tournament",
                               key=f"t_name{sfx}")
        _kinds = ["Local", "Travel"]
        t_kind = c2.radio("Type", _kinds, horizontal=True, key=f"t_kind{sfx}",
                          index=1 if _tval("type").lower().startswith(("trav", "away")) else 0)
        c1, c2 = st.columns(2)
        t_start = c1.date_input("Start date", value=_tdate("start_date", start), format="MM/DD/YYYY",
                                key=f"t_start{sfx}")
        t_end = c2.date_input("End date", value=_tdate("end_date", t_start), format="MM/DD/YYYY", key=f"t_end{sfx}")
        if t_kind == "Local":
            c1, c2 = st.columns(2)
            _prog_opts = ["(none)"] + program_names + ([_tval("program")] if _tval("program") and
                                                       _tval("program") not in program_names else [])
            t_prog = c1.selectbox("Locals program", _prog_opts, key=f"t_prog{sfx}",
                                  index=_prog_opts.index(_tval("program")) if _tval("program") in _prog_opts else 0,
                                  help="Its coaches are blocked for the day.")
            _c_def = parse_courts(_tval("courts")) if _tval("courts") else list(COURTS)
            t_courts = c2.multiselect("Courts used", ALL_COURTS, default=[c for c in _c_def if c in ALL_COURTS],
                                      key=f"t_courts{sfx}", format_func=court_name)
            prog_coaches = sorted({c for lp in (cleaned.locals if cleaned else []) if lp.name == t_prog
                                   for c in lp.coaches})
            _all_c = cleaned.coaches if cleaned else []
            _c_now = parse_names(_tval("coaches")) if editing else prog_coaches
            t_coaches = st.multiselect("Coaches working it", sorted(set(_all_c) | set(_c_now)),
                                       default=[c for c in _c_now if c in set(_all_c) | set(_c_now)],
                                       key=f"t_coaches_{t_prog}{sfx}",
                                       help="Busy until 7 PM that day. A coach also going to a travel tournament "
                                            "then gets flagged.")
            t_club, t_ages, t_teams = "", [], []
        else:
            c1, c2 = st.columns(2)
            _club_opts = ["Any"] + clubs
            t_club = c1.selectbox("Club", _club_opts, key=f"t_club{sfx}",
                                  index=_club_opts.index(_tval("club")) if _tval("club") in _club_opts else 0)
            _ages_now = [a for a in ages_all if str(a) in re.findall(r"\d+", _tval("ages"))] if editing else []
            t_ages = c2.multiselect("Age groups", ages_all, default=_ages_now, key=f"t_ages{sfx}",
                                    format_func=lambda a: f"{a}s")
            match = [] if (t_club == "Any" and not t_ages) else [
                t.name for t in team_list if (t_club == "Any" or t.club == t_club) and (not t_ages or t.age in t_ages)]
            _names = [t.name for t in team_list]
            _listed = [n for n in parse_list(_tval("teams")) if n in _names] if editing else []
            if editing and not _listed:  # saved by club + ages: the teams that matched
                _listed = next((tn.teams for tn in (cleaned.tournaments if cleaned else [])
                                if tn.name == _tval("name") and tn.kind == "travel"), [])
            t_teams = st.multiselect("Teams going", _names, default=_listed if editing else match,
                                     key=f"t_teams_{t_club}_{'-'.join(map(str, t_ages))}{sfx}",
                                     help="Filled in from the club and ages; remove teams that aren't going.")
            t_prog, t_courts, t_coaches = "(none)", [], []
        if editing:
            c1, c2, c3 = st.columns([2, 2, 1])
            add_t = c1.button("Save changes", type="primary", width="stretch")
            rm_t = c3.button("Delete", width="stretch", key="t_delete")
        else:
            add_t, rm_t = st.button("Add tournament", type="primary"), False

    if rm_t:
        before = inp.tournaments.copy()
        inp.tournaments = inp.tournaments.drop(inp.tournaments.index[t_sel]).reset_index(drop=True)
        st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
        st.session_state.flash = (f"Deleted {_tval('name')}. "
                                  + dates.rebuild_for([(before, inp.tournaments, ("start_date", "end_date"))]))
        st.rerun()
    if add_t:
        problems = []
        if not t_name.strip():
            problems.append("Give the tournament a name.")
        if t_end < t_start:
            problems.append("The end date is before the start date.")
        if t_kind == "Travel" and not t_teams:
            problems.append("Pick at least one team that's going.")
        if t_kind == "Local" and not t_courts:
            problems.append("Pick the courts it uses.")
        if problems:
            st.error(" ".join(problems))
        else:
            row = {"name": t_name.strip(), "type": t_kind,
                   "start_date": f"{t_start:%m/%d/%Y}", "end_date": f"{t_end:%m/%d/%Y}",
                   "club": "" if t_club in ("", "Any") else t_club,
                   "ages": ", ".join(map(str, t_ages)), "teams": ", ".join(t_teams),
                   "program": "" if t_prog == "(none)" else t_prog,
                   "courts": ", ".join("P" if c not in COURTS else str(c) for c in sorted(t_courts))
                   if t_kind == "Local" else "", "coaches": "; ".join(t_coaches),
                   "note": _tval("note") if editing else ""}
            span = f"{t_start:%b %-d}" + ("" if t_end == t_start else f"–{t_end:%b %-d}")
            before = inp.tournaments.copy()
            if editing:
                tdf = inp.tournaments.copy().astype(object)
                for k_, v_ in row.items():
                    tdf.at[tdf.index[t_sel], k_] = v_
                inp.tournaments = tdf
                st.session_state.flash = f"Saved {row['name']} ({t_kind.lower()}, {span})."
            else:
                inp.tournaments = pd.concat([inp.tournaments, pd.DataFrame([row])], ignore_index=True)
                st.session_state.flash = f"Added {row['name']} ({t_kind.lower()}, {span})."
                st.session_state.pop("t_name", None)  # clear the form for the next one
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.flash += " " + dates.rebuild_for([(before, inp.tournaments, ("start_date", "end_date"))])
            st.rerun()

    st.subheader("Tournaments")
    flags_panel("tournament", key="flags_tourn", expanded=True)
    if inp.tournaments.empty:
        st.caption("None yet.")
    else:
        tv = inp.tournaments.copy().fillna("")
        _tl = [f"{r['name']} · {r['type']} · {r['start_date']}" for _, r in tv.iterrows()]
        tv.index = [lab if _tl.count(lab) == 1 else f"{lab} (#{i + 1})" for i, lab in enumerate(_tl)]
        _tcols = ["name", "type", "start_date", "end_date", "club", "ages", "teams", "program", "courts", "coaches"]
        st.dataframe(tv[_tcols]
                     .rename(columns=lambda c: c.replace("_", " ").title()), hide_index=True, width="stretch")
        c1, c2 = st.columns([3, 1])
        drop_t = c1.multiselect("Remove tournaments", list(tv.index), placeholder="Choose tournaments to remove")
        if c2.button("Remove", disabled=not drop_t, width="stretch", key="rm_tourn"):
            keep = [i for i, lab in enumerate(tv.index) if lab not in drop_t]
            before = inp.tournaments.copy()
            inp.tournaments = inp.tournaments.iloc[keep].reset_index(drop=True)
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.flash = (f"Removed {plural(len(drop_t), 'tournament')}. "
                                      + dates.rebuild_for([(before, inp.tournaments, ("start_date", "end_date"))]))
            st.rerun()
