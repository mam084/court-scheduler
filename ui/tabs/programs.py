"""Programs: locals, JumpStart, leagues and camps, with their local tournament dates."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from scheduler import ScheduleInput, clean
from scheduler.config import ALL_COURTS, COURTS, DAYS, PROGRAM_KEYS, SLOT_KEYS, SLOTS, court_name
from scheduler.data import (
    COLUMNS,
    blank,
    parse_courts,
    parse_date_ranges,
    parse_days,
    parse_names,
    parse_times,
    program_side,
)
from ui import dates
from ui.common import plural
from ui.flags import flags_panel
from ui.state import S

SLOT_CHOICES = list(SLOT_KEYS)  # every time, in time order: any of them on any day


def slot_label(k: str) -> str:
    """A program time for pickers: '5:30–7:20', '9–11 AM', '3:30–4:30 (JumpStart)'."""
    return SLOTS[k] + (" (JumpStart)" if k in PROGRAM_KEYS else "")


SIDES = ["Girls", "Boys", "Other"]


def program_defaults(row: dict | None) -> dict:
    """Form values for a new program (row=None) or an existing Locals row."""
    start, cleaned = S.start, S.cleaned
    if row is None:
        return {"name": "", "side": "Girls", "on_site": True, "days": [],
                "p_start": None, "p_end": None, "times": [], "courts": [], "coaches_sel": [], "coaches_new": "",
                "tourns": []}
    yr = start.year
    try:
        ranges = parse_date_ranges(row.get("dates"), yr)
    except ValueError:
        ranges = []
    try:
        times = sorted(parse_times(row.get("time")) or [], key=SLOT_KEYS.index)
    except ValueError:
        times = []
    try:
        courts = parse_courts(row.get("courts"))
    except ValueError:
        courts = []
    names = parse_names(row.get("coaches"))
    known = set(cleaned.coaches) if cleaned else set()
    side = program_side(row.get("side"), str(row.get("program", ""))) or "Other"
    on = str(row.get("on_site", "")).strip().lower()
    return {
        "name": str(row.get("program", "")), "side": side,
        "on_site": on in ("", "yes", "y", "true", "1", "nan", "none"),
        "days": [d for d in parse_days(row.get("days")) if d in DAYS],
        "p_start": min(a for a, _ in ranges) if ranges else None,
        "p_end": max(b for _, b in ranges) if ranges else None,
        "times": [t for t in times if t in SLOT_CHOICES], "courts": courts,
        "coaches_sel": [n for n in names if n in known],
        "coaches_new": ", ".join(n for n in names if n not in known),
        "tourns": program_tournaments(str(row.get("program", ""))),
    }


TOURN_COLS = ["date", "last_day", "name", "courts"]


def _is_program_tourn(df: pd.DataFrame, program: str) -> pd.Series:
    """Local tournaments that belong to a program (its tournament dates)."""
    if df.empty or not program:
        return pd.Series(False, index=df.index)
    return (df["type"].astype(str).str.strip().str.lower().str.startswith("loc")
            & (df["program"].fillna("").astype(str).str.strip().str.lower() == program.strip().lower()))


def program_tournaments(program: str) -> list[dict]:
    """A program's local tournaments, as rows for its form's tournament-dates table."""
    inp, start = S.inp, S.start
    tdf = inp.tournaments.fillna("")
    out = []
    for _, r in tdf[_is_program_tourn(tdf, program)].iterrows():
        try:
            a = parse_date_ranges(r["start_date"], start.year)[0][0]
            b = a if blank(r["end_date"]) else parse_date_ranges(r["end_date"], start.year)[0][0]
        except (ValueError, IndexError):
            continue
        out.append({"date": a, "last_day": None if b == a else b, "name": str(r["name"]),
                    "courts": str(r["courts"]).strip()})
    return out


def tournament_rows(program: str, table: pd.DataFrame) -> tuple[list[str], list[dict]]:
    """Tournaments rows from a program form's tournament-dates table."""
    problems, rows = [], []
    for i, r in table.reset_index(drop=True).iterrows():
        d = r.get("date")
        if d is None or pd.isna(d):
            if not blank(r.get("name")) or not blank(r.get("courts")):
                problems.append(f"Tournament row {i + 1} needs a date.")
            continue
        d = pd.Timestamp(d).date()
        last = r.get("last_day")
        last = d if last is None or pd.isna(last) else pd.Timestamp(last).date()
        if last < d:
            problems.append(f"Tournament on {d:%b %-d}: the last day is before the first.")
            continue
        ct = "" if blank(r.get("courts")) else str(r["courts"]).strip()
        try:
            cl = parse_courts(ct) if ct else []
        except ValueError as e:
            problems.append(f"Tournament on {d:%b %-d}: {e}")
            continue
        rows.append({"name": (str(r["name"]).strip() if not blank(r.get("name")) else "") or f"{program} Tournament",
                     "type": "Local", "start_date": f"{d:%m/%d/%Y}", "end_date": f"{last:%m/%d/%Y}",
                     "club": "", "ages": "", "teams": "", "program": program,
                     "courts": ", ".join("P" if c not in COURTS else str(c) for c in sorted(cl)), "coaches": "",
                     "note": ""})
    return problems, rows


def program_form(key: str, dv: dict, submit_labels: list[str]):
    """Renders the program form; returns (clicked label or None, values)."""
    cleaned = S.cleaned
    known_coaches = cleaned.coaches if cleaned else []
    with st.form(key, clear_on_submit=key == "add_program"):
        c1, c2 = st.columns([2, 1])
        name = c1.text_input("Program name", value=dv["name"], placeholder="e.g. G 14 League or JumpStart")
        side = c2.radio("Side", SIDES, index=SIDES.index(dv["side"]), horizontal=True,
                        help="Girls programs show in blue, boys in green.")
        on_site = True  # every program on the calendar uses our courts
        c1, c2, c3 = st.columns([2, 1, 1])
        days = c1.multiselect("Days", DAYS, default=dv["days"], placeholder="e.g. Tue, Thu")
        p_start = c2.date_input("Start date", value=dv["p_start"], format="MM/DD/YYYY",
                                help="The first day it runs. Every program needs a start and an end date.")
        p_end = c3.date_input("End date", value=dv["p_end"], format="MM/DD/YYYY", help="The last day it runs.")
        c1, c2 = st.columns(2)
        times = c1.multiselect("Time", SLOT_CHOICES, default=dv["times"], format_func=slot_label,
                               help="Pick every time it uses, on any day: 3:45 and 5:30 for a 3:45–7:20 program, "
                                    "9–11 AM for a morning camp. JumpStart times (3:30–4:30, 4:30–5:30) block 3:45 "
                                    "practices on those courts.")
        courts = c2.multiselect("Courts used", ALL_COURTS, default=[c for c in dv["courts"] if c in ALL_COURTS],
                                format_func=court_name,
                                help="Remove a court here if sign-ups are low. Leave empty if not known yet "
                                     "(it then blocks only the coaches). The practice court is for programs "
                                     "only; team practices never use it.")
        c1, c2 = st.columns(2)
        coaches_sel = c1.multiselect("Coaches working it", known_coaches,
                                     default=[c for c in dv["coaches_sel"] if c in known_coaches])
        coaches_new = c2.text_input("Other coaches", value=dv["coaches_new"], placeholder="Names separated by commas")
        st.markdown("**Local tournament dates**")
        st.caption("Tournaments this program hosts here. Each row is one tournament (add as many as you like, "
                   "several on the same day is fine). They take their courts and this program's coaches until "
                   "7 PM. Courts like `1-4` or `1, 2, P`; blank = courts 1–7.")
        tdf = pd.DataFrame(dv["tourns"], columns=TOURN_COLS)
        for c in ("date", "last_day"):
            tdf[c] = pd.to_datetime(tdf[c], errors="coerce")
        tdf["name"] = tdf["name"].astype(object).fillna("")
        tdf["courts"] = tdf["courts"].astype(object).fillna("")
        tourns = st.data_editor(
            tdf, num_rows="dynamic", hide_index=True, width="stretch", key=f"{key}_tourns",
            column_config={
                "date": st.column_config.DateColumn("Date", format="MM/DD/YYYY", required=True),
                "last_day": st.column_config.DateColumn("Last day (if more than one)", format="MM/DD/YYYY"),
                "name": st.column_config.TextColumn("Name (optional)", help="Blank: “<program> Tournament”"),
                "courts": st.column_config.TextColumn("Courts", help="Like 1-4 or 1, 2, P. Blank = courts 1-7.")})
        cols = st.columns(len(submit_labels) + 3)
        clicked = None
        for i, lab in enumerate(submit_labels):
            if cols[i].form_submit_button(lab, type="primary" if i == 0 else "secondary", width="stretch"):
                clicked = lab
    return clicked, dict(name=name, side=side, on_site=on_site, days=days, p_start=p_start,
                         p_end=p_end, times=times, courts=courts, coaches_sel=coaches_sel, coaches_new=coaches_new,
                         tourns=tourns)


def program_row(v: dict) -> tuple[list[str], dict]:
    """The Programs form's values -> (problems, a Locals row)."""
    problems = []
    if not v["name"].strip():
        problems.append("Give the program a name.")
    if not v["days"]:
        problems.append("Pick at least one day.")
    if not v["times"]:
        problems.append("Pick at least one timeslot.")
    if v["p_start"] is None or v["p_end"] is None:
        problems.append("Give it a start date and an end date.")
    elif v["p_end"] < v["p_start"]:
        problems.append("The end date is before the start date.")
    row = {
        "program": v["name"].strip(), "side": "" if v["side"] == "Other" else v["side"],
        "days": ", ".join(d for d in DAYS if d in v["days"]),
        "time": ", ".join(sorted(v["times"], key=SLOT_KEYS.index)),
        "courts": ", ".join("P" if c not in COURTS else str(c) for c in sorted(v["courts"])) or "TBD",
        "coaches": "; ".join(dict.fromkeys(v["coaches_sel"] + parse_names(v["coaches_new"]))),
        "on_site": "Yes" if v["on_site"] else "No",
        "dates": (f"{v['p_start']:%m/%d/%Y}-{v['p_end']:%m/%d/%Y}" if v["p_start"] and v["p_end"] else ""),
    }
    return problems, row


def try_locals(new_locals: pd.DataFrame, what: str, new_tourn: pd.DataFrame | None = None) -> bool:
    """Swap in new programs (and tournaments) if they pass validation; schedules a rebuild. False + an error if not."""
    inp, start, end = S.inp, S.start, S.end
    tourn = inp.tournaments if new_tourn is None else new_tourn
    trial = ScheduleInput(inp.teams, inp.availability, inp.wishlist, new_locals, inp.weekends, inp.closures,
                          tournaments=tourn, coaches=inp.coaches, overrides=inp.overrides)
    try:
        clean(trial, start, end)
    except ValueError as e:
        st.error(f"Couldn't {what}: {e}")
        return False
    before_l, before_t = inp.locals.copy(), inp.tournaments.copy()
    inp.locals = new_locals.reset_index(drop=True)
    if new_tourn is not None:
        inp.tournaments = new_tourn.reset_index(drop=True)
    st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
    # Rebuild the scheduled dates the program runs on (none, if they aren't scheduled yet).
    st.session_state.rebuild_note = dates.rebuild_for(
        [(before_l, inp.locals, ("dates",)), (before_t, inp.tournaments, ("start_date", "end_date"))],
        autobuild=st.session_state.get("prog_autobuild", True))
    return True


def replace_program_tournaments(old_name: str, new_rows: list[dict]) -> pd.DataFrame:
    """The Tournaments table with a program's local tournaments swapped for new_rows."""
    inp = S.inp
    keep = inp.tournaments[~_is_program_tourn(inp.tournaments, old_name)] if old_name else inp.tournaments
    return pd.concat([keep, pd.DataFrame(new_rows, columns=COLUMNS["Tournaments"])], ignore_index=True)


def done_msg(what: str) -> str:
    """A confirmation that says whether the schedule was rebuilt or is waiting for Auto-schedule."""
    return f"{what}. " + st.session_state.pop("rebuild_note", "")




def render():
    """Programs section."""
    inp, start, end, cleaned, closures = S.inp, S.start, S.end, S.cleaned, S.closures
    st.markdown("Programs (locals, JumpStart, leagues…) are blocked off on their courts, and the practice "
                "schedule is rebuilt around them when you save.")
    flags_panel("program", key="flags_prog", expanded=True)
    st.toggle("Rebuild the schedule each time I save a program", value=True, key="prog_autobuild",
              help="Turn off to change several programs in a row, then press Auto-schedule once at the end.")
    ver = st.session_state.get("locals_ver", 0)

    # ---- edit an existing program
    if not inp.locals.empty:
        st.subheader("Edit a program")
        labels = [f"{r['program']} · {r['days']} · {r['time']}"
                  + (f" · {r['dates']}" if str(r["dates"]).strip() else "")
                  for _, r in inp.locals.fillna("").iterrows()]
        labels = [lab if labels.count(lab) == 1 else f"{lab} (#{i + 1})" for i, lab in enumerate(labels)]
        pick_lab = st.selectbox("Program", labels, index=None,
                                placeholder="Choose a program to change (e.g. drop a court)", key=f"edit_pick_{ver}")
        pick = labels.index(pick_lab) if pick_lab in labels else None
        if pick is not None:
            row = inp.locals.fillna("").iloc[pick].to_dict()
            clicked, v = program_form(f"edit_program_{pick}_{ver}", program_defaults(row),
                                      ["Save changes", "Delete program"])
            old_name = str(row["program"]).strip()
            others = (inp.locals["program"].astype(str).str.strip() == old_name).sum() > 1  # other rows, same program
            if clicked == "Delete program":
                n_t = 0 if others else int(_is_program_tourn(inp.tournaments, old_name).sum())
                if try_locals(inp.locals.drop(inp.locals.index[pick]), "delete it",
                              replace_program_tournaments(old_name, []) if n_t else None):
                    st.session_state.flash = done_msg(f"Deleted {row['program']}"
                                                      + (f" and its {plural(n_t, 'tournament')}" if n_t else ""))
                    st.rerun()
            elif clicked:
                problems, new_row = program_row(v)
                t_problems, t_rows = tournament_rows(new_row["program"], v["tourns"])
                if problems or t_problems:
                    st.error(" ".join(problems + t_problems))
                else:
                    nl = inp.locals.copy()
                    for k, val in new_row.items():
                        nl.at[nl.index[pick], k] = val
                    if new_row["program"] != old_name and others:
                        # renaming one row of a program: keep its tournaments with the other rows
                        new_t = replace_program_tournaments("", t_rows) if t_rows else None
                    else:
                        new_t = replace_program_tournaments(old_name, t_rows)
                    if try_locals(nl, "save the changes", new_t):
                        st.session_state.flash = done_msg(f"Updated {new_row['program']}"
                                                          + (f" ({plural(len(t_rows), 'tournament date')})"
                                                             if t_rows else ""))
                        st.rerun()

    # ---- one session more or less
    if not inp.locals.empty and cleaned is not None:
        st.subheader("Add or cancel one session")
        st.caption("For a single week: cancel one session (e.g. a holiday or low sign-ups) or add one extra. "
                   "The rest of the program stays as it is.")
        prog_names = list(dict.fromkeys(str(n).strip() for n in inp.locals["program"] if not blank(n)))
        c1, c2 = st.columns([2, 1])
        one_prog = c1.selectbox("Program", prog_names, key=f"one_prog_{ver}")
        one_mode = c2.radio("Change", ["Cancel one session", "Add one session"], key=f"one_mode_{ver}",
                            horizontal=True)
        rows_of = inp.locals.index[inp.locals["program"].astype(str).str.strip() == one_prog].tolist()
        lps = [lp for lp in cleaned.locals if lp.name == one_prog]
        if one_mode == "Cancel one session":
            # Every session it has left, over its own dates (not just the dates being scheduled).
            spans = [r for lp in lps for r in lp.ranges] or [(start, end)]
            run_dates = sorted({d for a_, b_ in spans for d in pd.date_range(a_, b_).date
                                for lp in lps if lp.runs_on(d) and d not in closures})
            if not run_dates:
                st.caption(f"{one_prog} has no sessions left in these dates.")
            else:
                c_day = st.selectbox("Session", run_dates, format_func=lambda d: f"{d:%a %b %-d}",
                                     key=f"one_cancel_{ver}")
                if st.button("Cancel this session", type="primary", key=f"one_cancel_btn_{ver}"):
                    nl = inp.locals.copy()
                    nl["skip"] = nl["skip"].astype(object) if "skip" in nl.columns else ""
                    for i in rows_of:
                        if DAYS[c_day.weekday()] in parse_days(nl.at[i, "days"]):
                            old = "" if blank(nl.at[i, "skip"]) else str(nl.at[i, "skip"]).strip()
                            nl.at[i, "skip"] = "; ".join(x for x in (old, f"{c_day:%m/%d/%Y}") if x)
                    if try_locals(nl, "cancel that session"):
                        st.session_state.flash = done_msg(f"Cancelled {one_prog} on {c_day:%a %b %-d}")
                        st.rerun()
        else:
            dv = program_defaults(inp.locals.fillna("").loc[rows_of[0]].to_dict() if rows_of else None)
            c1, c2, c3 = st.columns(3)
            a_day = c1.date_input("Date", value=dv["p_start"] or start, format="MM/DD/YYYY",
                                  key=f"one_add_day_{ver}")
            a_times = c2.multiselect("Time", SLOT_CHOICES, default=[t for t in dv["times"] if t in SLOT_CHOICES],
                                     format_func=slot_label, key=f"one_add_t_{ver}")
            a_courts = c3.multiselect("Courts", ALL_COURTS, default=[c for c in dv["courts"] if c in ALL_COURTS],
                                      format_func=court_name, key=f"one_add_c_{ver}")
            if st.button("Add this session", type="primary", disabled=not a_times, key=f"one_add_btn_{ver}"):
                base = inp.locals.fillna("").loc[rows_of[0]].to_dict() if rows_of else {}
                row = {c: base.get(c, "") for c in COLUMNS["Locals"]}
                row.update({"days": DAYS[a_day.weekday()], "time": ", ".join(sorted(a_times, key=SLOT_KEYS.index)),
                            "courts": ", ".join("P" if c not in COURTS else str(c) for c in sorted(a_courts)) or "TBD",
                            "dates": f"{a_day:%m/%d/%Y}", "skip": ""})
                if try_locals(pd.concat([inp.locals, pd.DataFrame([row])], ignore_index=True), "add that session"):
                    st.session_state.flash = done_msg(f"Added a {one_prog} session on {a_day:%a %b %-d}")
                    st.rerun()
        skipped = [(i, d) for i in rows_of if "skip" in inp.locals.columns and not blank(inp.locals.at[i, "skip"])
                   for d in str(inp.locals.at[i, "skip"]).split(";") if d.strip()]
        if skipped:
            labels_sk = {f"{i}|{d.strip()}": f"{d.strip()} ({inp.locals.at[i, 'days']})" for i, d in skipped}
            undo_sk = st.multiselect(f"Cancelled {one_prog} sessions · pick any to bring back", list(labels_sk),
                                     format_func=lambda k: labels_sk[k], key=f"one_undo_{ver}")
            if st.button("Bring back", disabled=not undo_sk, key=f"one_undo_btn_{ver}"):
                nl = inp.locals.copy()
                for k in undo_sk:
                    i, d = k.split("|", 1)
                    i = int(i)
                    nl.at[i, "skip"] = "; ".join(x.strip() for x in str(nl.at[i, "skip"]).split(";")
                                                 if x.strip() and x.strip() != d)
                if try_locals(nl, "bring those sessions back"):
                    st.session_state.flash = done_msg(f"Brought back {plural(len(undo_sk), 'session')} of {one_prog}")
                    st.rerun()

    # ---- add a program
    st.subheader("Add a program")
    clicked, v = program_form("add_program", program_defaults(None), ["Add program"])
    if clicked:
        problems, row = program_row(v)
        t_problems, t_rows = tournament_rows(row["program"], v["tourns"])
        if problems or t_problems:
            st.error(" ".join(problems + t_problems))
        elif try_locals(pd.concat([inp.locals, pd.DataFrame([row])], ignore_index=True), f"add {row['program']}",
                        replace_program_tournaments("", t_rows) if t_rows else None):
            when = f"{v['p_start']:%b %-d}–{v['p_end']:%b %-d}"
            st.session_state.flash = done_msg(f"Added {row['program']} ({row['days']}, {when})"
                                              + (f" with {plural(len(t_rows), 'tournament date')}" if t_rows else ""))
            st.rerun()

    # ---- list
    st.subheader("Current programs")
    if inp.locals.empty:
        st.caption("No programs yet.")
    else:
        view = inp.locals.copy().fillna("")
        undated = sorted({str(r["program"]).strip() for _, r in view.iterrows() if not str(r["dates"]).strip()})
        if undated:
            st.warning(f"**No start and end date:** {', '.join(undated)}. Until they have dates they block their "
                       "courts every week; pick one under **Edit a program** to set them.")
        # Show the side the colors actually use (guessed from the name when blank).
        view["side"] = [program_side(s, n) or "Other" for s, n in zip(view["side"], view["program"])]
        st.dataframe(view.rename(columns={"program": "Program", "side": "Side", "days": "Days", "time": "Time",
                                          "courts": "Courts", "coaches": "Coaches",
                                          "dates": "Dates", "skip": "Cancelled"}),
                     hide_index=True, width="stretch")
