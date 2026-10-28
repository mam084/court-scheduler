"""Edit practices: move, add or remove practices, usual days, practices a week, coaches' preferences and requests."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from scheduler.config import (
    COURTS,
    MAX_PRACTICES_PER_WEEK,
    PRACTICES_PER_WEEK,
    SLOT_KEYS,
    SLOTS,
    WEEKDAY_KEYS,
    WEEKDAYS,
    WEEKEND_KEYS,
)
from scheduler.data import COLUMNS
from ui import dates
from ui.common import plural
from ui.edits import apply_overrides, change_prefs, open_courts, pref_key, submit_request
from ui.state import S

TEAM_SLOT_CHOICES = WEEKDAY_KEYS + WEEKEND_KEYS


def team_slot_label(k: str) -> str:
    """A practice time for pickers, marking weekend blocks."""
    return SLOTS[k] + (" (weekend)" if k in WEEKEND_KEYS else "")






def _from_view(tp: pd.DataFrame) -> int:
    """The practice list opens on the first practice on or after the day being looked at."""
    d = dates.view_day()
    after = [i for i, x in enumerate(tp["date"]) if x >= d] if len(tp) else []
    return after[0] if after else max(0, len(tp) - 1)


def court_pick(d, slot: str, key: str, team: str, moving: tuple | None = None):
    """A court for a request: 'Any' or a court number, with which courts are open then."""
    free = open_courts(d, slot, team, moving)
    opts = ["Any"] + COURTS
    lab = {c: ("Any open court" if c == "Any" else f"Court {c}" + ("" if free is None or c in free else " (taken)"))
           for c in opts}
    c = st.selectbox("Court", opts, format_func=lab.get, key=key,
                     help="Ask for a particular court if it matters. Your request never bumps another team: if the "
                          "court's taken then, the admins put it on an open one or get back to you.")
    if free is not None:
        st.caption("Open then: " + (", ".join(f"Court {x}" for x in free) if free else
                                    "no courts (everything's booked then, so the admins may suggest another time)"))
    return c


def _not_scheduled(d):
    if dates.run_at(d) is None:
        st.caption(f"{d:%b %-d} isn't scheduled yet: this goes in when it is.")


def render():
    """Edit practices section."""
    VIEWER, CAN, inp, result, cleaned = S.VIEWER, S.CAN, S.inp, S.result, S.cleaned
    span = dates.season()
    if span is None or cleaned is None:
        st.caption("Nothing's scheduled yet.")
    else:
        everything = dates.window(*span, with_preview=False)[0]
        start = dates.view_day()
        if CAN:
            st.markdown("Change practices by hand. The scheduler **keeps these changes every time it rebuilds**, "
                        "and fits everything else around them.")
            team_names = [t.name for t in cleaned.teams]
        else:
            st.markdown("Tell the scheduler what your teams would rather have, or ask for a change. Preferences "
                        "save right away and are weighed against every other team's at the next rebuild. Moves, "
                        "extra practices and cancellations go to the scheduling admins to approve (you'll get an "
                        "email either way). Your own no-dates and times are on the **Coaches** tab.")
            team_names = VIEWER.my_teams(cleaned.teams)
            if not team_names:
                st.info("Your login isn't linked to a team yet. Ask an app admin to link it to your coach name."
                        if not VIEWER.coach_name else f"{VIEWER.coach_name} isn't on any team in this schedule.")
                st.stop()
        e_team = st.selectbox("Team", team_names, key="ed_team")
        e_per_week = next((t.per_week for t in cleaned.teams if t.name == e_team), PRACTICES_PER_WEEK)
        a = everything.assignments
        tp = a[(a["kind"] == "Practice") & (a["name"] == e_team)].copy()
        if len(tp):
            tp["o"] = tp["slot"].map(SLOT_KEYS.index)
            tp = tp.sort_values(["date", "o"])
        pat = result.pattern if result is not None else pd.DataFrame(columns=["team", "day", "slot", "court"])
        usual_rows = pat[pat["team"] == e_team] if len(pat) else pat
        usual_txt = ", ".join(f"{r.day} {r.slot}" for r in usual_rows.itertuples()) or "none"
        _hc = "home_court" if "home_court" in usual_rows.columns else "court"  # schedules saved before home_court
        home = f" · home Court {usual_rows[_hc].iloc[0]}" if len(usual_rows) else ""
        st.caption(f"Usual days: **{usual_txt}**{home} · **{plural(e_per_week, 'practice')} a week**")

        c_left, c_right = st.columns([3, 2])
        with c_left:
            st.markdown("**Practices**")
            if len(tp):
                _flag = lambda col: (tp[col] if col in tp else pd.Series(False, index=tp.index)).map(  # noqa: E731
                    lambda v: "Yes" if v == True else "")  # noqa: E712
                st.dataframe(pd.DataFrame({
                    "Date": tp["date"].map(lambda d: f"{d:%a %b %-d}"), "Time": tp["slot_label"],
                    "Court": tp["court"], "Extra": _flag("extra"),
                    "Paired": (tp["paired"] if "paired" in tp else pd.Series("", index=tp.index)).fillna(""),
                }), hide_index=True, width="stretch", height=min(420, 38 + 35 * len(tp)))
            else:
                st.caption("No practices in this schedule.")

        with c_right:
            mode = st.radio("What do you want to do?", ["Suggest a preference", "Move a practice", "Remove a practice",
                                                        "Add a practice", "Set usual days", "Practices per week"]
                            if CAN else ["Suggest a preference", "Ask to move a practice", "Ask for an extra practice",
                                         "Ask to cancel a practice"],
                            key="ed_mode",
                            help="A preference is weighed against everyone else's and kept when possible. "
                                 "The other options pin a practice exactly where you put it.")
            labels = [f"{r.date:%a %b %-d} · {r.slot_label} · Court {r.court}" for r in tp.itertuples()]
            if mode in ("Move a practice", "Remove a practice"):
                if not labels:
                    st.caption("No practices to change.")
                else:
                    lab_i = st.selectbox("Practice", labels, key=f"ed_pick_{e_team}", index=_from_view(tp))
                    old = tp.iloc[labels.index(lab_i) if lab_i in labels else 0]
                    if mode == "Move a practice":
                        pk = f"{e_team}_{old['date']}_{old['slot']}"  # fields reset to the chosen practice
                        nd = st.date_input("New date", value=old["date"], format="MM/DD/YYYY", key=f"ed_nd_{pk}")
                        _not_scheduled(nd)
                        ns = st.selectbox("New time", TEAM_SLOT_CHOICES, format_func=team_slot_label,
                                          index=(TEAM_SLOT_CHOICES.index(old["slot"])
                                                 if old["slot"] in TEAM_SLOT_CHOICES else 0),
                                          key=f"ed_ns_{pk}")
                        nc = st.selectbox("Court", ["Any"] + COURTS, key=f"ed_nc_{pk}",
                                          format_func=lambda c: "Any open court" if c == "Any" else f"Court {c}")
                        same = nd == old["date"] and ns == old["slot"]
                        same_court = same and (nc == "Any" or str(nc) == str(old["court"]))
                        if st.button("Move it", type="primary", width="stretch", disabled=same_court,
                                     help="Pick a different date, time or court first." if same_court else None):
                            where = f" at {ns}" + ("" if nc == "Any" else f", Court {nc}")
                            apply_overrides([
                                {"team": e_team, "action": "Remove", "date": f"{old['date']:%m/%d/%Y}", "days": "",
                                 "time": old["slot"], "court": "", "note": f"moved to {nd:%a %-m/%-d} {ns}"},
                                {"team": e_team, "action": "Add", "date": f"{nd:%m/%d/%Y}", "days": "", "time": ns,
                                 "court": "" if nc == "Any" else str(nc),
                                 "note": f"moved from {old['date']:%a %-m/%-d}"},
                            ], f"Moved {e_team} from {old['date']:%a %b %-d} {old['slot']} to {nd:%a %b %-d}{where}.",
                                replace_pin=(e_team, old["date"], old["slot"]))
                    else:
                        how = st.radio("Then", ["Find a makeup that week if possible", "Cancel it (no makeup)"],
                                       key="ed_how")
                        if st.button("Remove it", type="primary", width="stretch"):
                            act = "Remove" if how.startswith("Find") else "Cancel"
                            apply_overrides([{"team": e_team, "action": act, "date": f"{old['date']:%m/%d/%Y}",
                                              "days": "", "time": old["slot"], "court": "", "note": "removed by hand"}],
                                            f"{'Removed' if act == 'Remove' else 'Cancelled'} {e_team}'s "
                                            f"{old['date']:%a %b %-d} practice.",
                                            replace_pin=(e_team, old["date"], old["slot"]))
            elif mode.startswith("Ask to move") or mode.startswith("Ask to cancel"):
                if not labels:
                    st.caption("No practices to change.")
                else:
                    lab_i = st.selectbox("Practice", labels, key=f"rq_pick_{e_team}", index=_from_view(tp))
                    old = tp.iloc[labels.index(lab_i) if lab_i in labels else 0]
                    od = old["date"]
                    rk = f"rq_{e_team}_{od}_{old['slot']}"
                    if mode.startswith("Ask to move"):
                        nd = st.date_input("New date", value=od, format="MM/DD/YYYY", key=f"{rk}_nd")
                        ns = st.selectbox("New time", TEAM_SLOT_CHOICES, format_func=team_slot_label,
                                          index=TEAM_SLOT_CHOICES.index(old["slot"]) if old["slot"] in TEAM_SLOT_CHOICES
                                          else 0, key=f"{rk}_ns")
                        nc = court_pick(nd, ns, f"{rk}_nc", e_team, (od, old["slot"]))
                        why = st.text_input("Why (helps the admins decide)", key=f"{rk}_why",
                                            placeholder="e.g. gym conflict at school")
                        same = nd == od and ns == old["slot"] and (nc == "Any" or str(nc) == str(old["court"]))
                        where = "" if nc == "Any" else f", Court {nc}"
                        if st.button("Send request", type="primary", width="stretch", disabled=same, key=f"{rk}_go"):
                            submit_request("move", e_team,
                                           f"Move {e_team} from {od:%a %b %-d} {old['slot']} to "
                                           f"{nd:%a %b %-d} {ns}{where}",
                                           {"rows": [
                                               {"team": e_team, "action": "Remove", "date": f"{od:%m/%d/%Y}",
                                                "days": "", "time": old["slot"], "court": "",
                                                "note": f"moved to {nd:%a %-m/%-d} {ns} (request)"},
                                               {"team": e_team, "action": "Add", "date": f"{nd:%m/%d/%Y}", "days": "",
                                                "time": ns, "court": "" if nc == "Any" else str(nc),
                                                "note": f"moved from {od:%a %-m/%-d} (request)"}],
                                            "replace_pin": [e_team, od.isoformat(), old["slot"]]}, why)
                    else:
                        how = st.radio("Then", ["Make it up another day that week if possible", "No makeup needed"],
                                       key=f"{rk}_how")
                        why = st.text_input("Why", key=f"{rk}_why", placeholder="e.g. team is at a school event")
                        if st.button("Send request", type="primary", width="stretch", key=f"{rk}_go"):
                            act = "Remove" if how.startswith("Make") else "Cancel"
                            submit_request("cancel", e_team,
                                           f"Cancel {e_team}'s {od:%a %b %-d} {old['slot']} practice"
                                           + (" (with a makeup)" if act == "Remove" else " (no makeup)"),
                                           {"rows": [{"team": e_team, "action": act, "date": f"{od:%m/%d/%Y}",
                                                      "days": "", "time": old["slot"], "court": "",
                                                      "note": "cancelled (request)"}],
                                            "replace_pin": [e_team, od.isoformat(), old["slot"]]}, why)
            elif mode.startswith("Ask for an extra"):
                ad = st.date_input("Date", value=start, format="MM/DD/YYYY", key=f"rq_x_d_{e_team}")
                as_ = st.selectbox("Time", TEAM_SLOT_CHOICES, format_func=team_slot_label, index=1,
                                   key=f"rq_x_t_{e_team}")
                xc = court_pick(ad, as_, f"rq_x_c_{e_team}", e_team)
                why = st.text_input("Why", key=f"rq_x_why_{e_team}",
                                    placeholder="e.g. extra practice before a tournament")
                where = "" if xc == "Any" else f", Court {xc}"
                if st.button("Send request", type="primary", width="stretch", key=f"rq_x_go_{e_team}"):
                    submit_request("extra", e_team, f"Extra practice for {e_team} on {ad:%a %b %-d} at {as_}{where}",
                                   {"rows": [{"team": e_team, "action": "Extra", "date": f"{ad:%m/%d/%Y}", "days": "",
                                              "time": as_, "court": "" if xc == "Any" else str(xc),
                                              "note": "extra practice (request)"}]}, why)
            elif mode == "Suggest a preference":
                st.caption("Tell the scheduler what this team would rather have. It's weighed against every other "
                           "team's requests, so it's kept when it fits, not forced.")
                pk = f"pref_{e_team}"
                p_days = st.multiselect("Would rather practice on", WEEKDAYS, key=f"{pk}_pd",
                                        placeholder="Any day")
                p_times = st.multiselect("at", WEEKDAY_KEYS, format_func=lambda k: SLOTS[k], key=f"{pk}_pt",
                                         placeholder="Any time")
                p_rank = st.radio("How much", ["1st choice", "2nd choice"], horizontal=True, key=f"{pk}_pr")
                a_days = st.multiselect("Would rather not practice on", WEEKDAYS, key=f"{pk}_ad",
                                        placeholder="Any day")
                a_times = st.multiselect("at ", WEEKDAY_KEYS, format_func=lambda k: SLOTS[k], key=f"{pk}_at",
                                         placeholder="Any time")
                p_note = st.text_input("Note (optional)", key=f"{pk}_note", placeholder="e.g. coach works until 4")
                want = bool(p_days or p_times)
                avoid = bool(a_days or a_times)
                if st.button("Save preference", type="primary", width="stretch", disabled=not (want or avoid)):
                    bits, wrow, arow = [], None, None
                    if want:
                        wrow = {c: "" for c in COLUMNS["Wishlist"]}
                        wrow.update({"team": e_team, "choice": 1 if p_rank.startswith("1") else 2,
                                     "days": ", ".join(p_days), "times": ", ".join(p_times), "note": p_note})
                        bits.append(f"prefers {' / '.join(p_days) or 'any day'} {', '.join(p_times)}".strip())
                    if avoid:
                        arow = {c: "" for c in COLUMNS["Availability"]}
                        arow.update({"who": e_team, "type": "Not preferred", "days": ", ".join(a_days),
                                     "times": ", ".join(a_times), "note": p_note})
                        bits.append(f"would rather not have {' / '.join(a_days) or 'any day'} "
                                    f"{', '.join(a_times)}".strip())
                    change_prefs(e_team, wrow, arow, [], [], f"Saved: {e_team} " + " and ".join(bits) + ".",
                                 " and ".join(bits))

                # This team's current preferences, removable here.
                wl = inp.wishlist.fillna("")
                wl = wl[wl["team"] == e_team]
                av = inp.availability.fillna("")
                av = av[(av["who"] == e_team) & av["type"].astype(str).str.lower().str.startswith("not")]
                prefs = {("w", i): f"Prefers ({'1st' if str(r['choice']).startswith('1') else '2nd'}): "
                                   f"{r['days'] or 'any day'} {r['times'] or 'any time'}" for i, r in wl.iterrows()}
                prefs.update({("a", i): f"Rather not: {r['days'] or 'any day'} {r['times'] or 'any time'}"
                              for i, r in av.iterrows()})
                if prefs:
                    st.markdown("**Current preferences**")
                    for lab in prefs.values():
                        st.caption("• " + lab)
                    drop = st.multiselect("Remove", list(prefs), format_func=lambda k: prefs[k], key=f"{pk}_drop")
                    if st.button("Remove selected", disabled=not drop, key=f"{pk}_dropbtn"):
                        change_prefs(e_team, None, None,
                                     [pref_key(wl.loc[i].to_dict(), "team") for k, i in drop if k == "w"],
                                     [pref_key(av.loc[i].to_dict(), "who") for k, i in drop if k == "a"],
                                     f"Removed {plural(len(drop), 'preference')} for {e_team}.",
                                     "removed " + "; ".join(prefs[k] for k in drop))
            elif mode == "Add a practice":
                ad = st.date_input("Date", value=start, format="MM/DD/YYYY", key="ed_ad")
                _not_scheduled(ad)
                as_ = st.selectbox("Time", TEAM_SLOT_CHOICES, format_func=team_slot_label, index=1, key="ed_as")
                ac = st.selectbox("Court", ["Any"] + COURTS, key="ed_ac",
                                  format_func=lambda c: "Any open court" if c == "Any" else f"Court {c}")
                how_add = st.radio("This practice is", [f"Extra (on top of the week's {e_per_week})",
                                                        "Instead of one of the week's practices"], key="ed_add_how",
                                   help="Extra: the team keeps its usual practices and gets this one too. "
                                        "Instead: the scheduler drops another practice that week to make room.")
                extra = how_add.startswith("Extra")
                if st.button("Add it", type="primary", width="stretch"):
                    apply_overrides([{"team": e_team, "action": "Extra" if extra else "Add", "date": f"{ad:%m/%d/%Y}",
                                      "days": "", "time": as_, "court": "" if ac == "Any" else str(ac),
                                      "note": "extra practice" if extra else "added by hand"}],
                                    f"Added {'an extra' if extra else 'a'} practice for {e_team} "
                                    f"on {ad:%a %b %-d} at {as_}.")
            elif mode == "Practices per week":
                st.caption("For a team that wants more (or fewer) practices every week. The scheduler gives the "
                           "first two the usual priority; a 3rd or more is added wherever there's room without "
                           "breaking anyone's no-dates. For a single extra practice, use **Add a practice**.")
                n_pw = st.number_input("Practices a week", 1, MAX_PRACTICES_PER_WEEK, value=e_per_week,
                                       key=f"ed_pw_{e_team}")
                if st.button("Save", type="primary", width="stretch", disabled=n_pw == e_per_week):
                    t = inp.teams.copy()
                    t["practices"] = t["practices"].astype(object) if "practices" in t.columns else ""
                    t.loc[t["team"].astype(str).str.strip() == e_team, "practices"] = (
                        "" if n_pw == PRACTICES_PER_WEEK else str(int(n_pw)))
                    inp.teams = t
                    st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
                    st.session_state.resolve = True
                    st.session_state.flash = (f"{e_team} now has {plural(int(n_pw), 'practice')} a week. "
                                              "Schedule rebuilt.")
                    st.rerun()
            else:
                n_ud = min(e_per_week, len(WEEKDAYS))
                ud = st.multiselect(f"Days (pick {n_ud})", WEEKDAYS, max_selections=n_ud, key=f"ed_ud_{e_team}",
                                    default=[r.day for r in usual_rows.itertuples()][:n_ud])
                us = st.selectbox("Time", WEEKDAY_KEYS, format_func=lambda k: SLOTS[k], key=f"ed_us_{e_team}",
                                  index=WEEKDAY_KEYS.index(usual_rows["slot"].iloc[0])
                                  if len(usual_rows) and usual_rows["slot"].iloc[0] in WEEKDAY_KEYS else 1)
                uc = st.selectbox("Home court", ["Any"] + COURTS, key=f"ed_uc_{e_team}",
                                  format_func=lambda c: "Scheduler picks" if c == "Any" else f"Court {c}")
                if st.button("Set usual days", type="primary", width="stretch", disabled=not ud):
                    keep = inp.overrides[~((inp.overrides["team"] == e_team) &
                                           inp.overrides["action"].astype(str).str.lower().str.startswith("usual"))]
                    original = inp.overrides.copy()
                    inp.overrides = keep.reset_index(drop=True)
                    apply_overrides([{"team": e_team, "action": "Usual", "date": "", "days": ", ".join(ud),
                                      "time": us, "court": "" if uc == "Any" else str(uc), "note": "set by hand"}],
                                    f"{e_team} now practices {' & '.join(ud)} at {us}.", backup=original)

        if CAN:
            st.subheader("Manual changes")
        ov = inp.overrides.fillna("")
        if not CAN:
            pass
        elif ov.empty:
            st.caption("None yet.")
        else:
            show_all = st.toggle("Show all teams", value=False, key="ed_all")
            view = ov if show_all else ov[ov["team"] == e_team]
            if view.empty:
                st.caption(f"No manual changes for {e_team}.")
            else:
                lab = {i: f"{r['team']} · {r['action']} · " + " ".join(x for x in (r["date"], r["days"], r["time"],
                       f"Court {r['court']}" if str(r["court"]).strip() else "") if str(x).strip())
                       for i, r in view.iterrows()}
                st.dataframe(view.rename(columns=str.title), hide_index=True, width="stretch")
                undo = st.multiselect("Undo changes", list(lab), format_func=lambda i: lab[i], key="ed_undo")
                if st.button("Undo selected", disabled=not undo):
                    inp.overrides = inp.overrides.drop(index=undo).reset_index(drop=True)
                    st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
                    st.session_state.resolve = True
                    st.session_state.flash = f"Undid {plural(len(undo), 'change')}. Schedule rebuilt."
                    st.rerun()
