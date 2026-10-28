"""Coaches: each coach's teams, programs, no-dates, privates and schedule."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from scheduler.config import DAYS, SLOT_KEYS, SLOTS, WEEKDAY_KEYS, WEEKEND_KEYS, court_name, court_short
from scheduler.data import COLUMNS
from scheduler.export import to_ics
from ui.common import plural
from ui.edits import change_prefs, pref_key
from ui.state import S


def render():
    """Coaches section."""
    CAN = S.CAN
    inp = S.inp
    start = S.start
    end = S.end
    result = S.result
    cleaned = S.cleaned
    privs = S.privs
    MY_COACH = S.MY_COACH
    if CAN:
        with st.expander("Add a coach", expanded=False):
            st.caption("For coaches who aren't on a team yet (e.g. they only work programs). "
                       "They'll show up in the program and tournament forms.")
            with st.form("add_coach", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                nc_name = c1.text_input("Name")
                nc_email = c2.text_input("Email (optional)")
                nc_phone = c3.text_input("Phone (optional)")
                nc_note = st.text_input("Note (optional)", placeholder="e.g. JumpStart coach")
                if st.form_submit_button("Add coach", type="primary"):
                    existing = set(cleaned.coaches) if cleaned else set()
                    if not nc_name.strip():
                        st.error("Enter a name.")
                    elif nc_name.strip() in existing:
                        st.warning(f"{nc_name.strip()} is already a coach.")
                    else:
                        inp.coaches = pd.concat([inp.coaches, pd.DataFrame([{
                            "coach": nc_name.strip(), "email": nc_email.strip(), "phone": nc_phone.strip(),
                            "note": nc_note.strip()}])], ignore_index=True)
                        st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
                        st.session_state.flash = f"Added coach {nc_name.strip()}."
                        st.rerun()
    if cleaned is None:
        st.caption("Fix the input errors first.")
    else:
        _me_i = cleaned.coaches.index(MY_COACH) if MY_COACH in cleaned.coaches else 0
        coach = st.selectbox("Coach", cleaned.coaches, index=_me_i, key=f"coach_pick_{MY_COACH}")
        heads = [t.name for t in cleaned.teams if coach in t.heads]
        assists = [t.name for t in cleaned.teams if coach in t.assistants and coach not in t.heads]
        grouped = {}
        for lp in cleaned.locals:
            if coach in lp.coaches:
                grouped.setdefault((lp.name, lp.slot, lp.on_site), []).append(lp.day)
        my_locals = [f"{n} ({' & '.join(ds)} {sl}){'' if on else ' · off-site'}" for (n, sl, on), ds in grouped.items()]
        mine_names = set(heads + assists + [coach])
        rules = [r for r in cleaned.rules if r.who in mine_names]

        c1, c2, c3 = st.columns(3)
        c1.markdown("**Head coach of**\n\n" + ("\n".join(f"- {t}" for t in heads) or "_none_"))
        c1.markdown("**Assistant on**\n\n" + ("\n".join(f"- {t}" for t in assists) or "_none_"))
        c2.markdown("**Locals / other programs**\n\n" + ("\n".join(f"- {x}" for x in my_locals) or "_none_"))
        c3.markdown("**No-dates & not-preferred**\n\n" + ("\n".join(
            f"- {r.who}: {'no' if r.kind == 'no' else 'not preferred'} {r.describe()}" for r in rules) or "_none_"))

        # ---- a coach's own no-dates / times (any coach for scheduling admins)
        if CAN or (MY_COACH and coach == MY_COACH):
            with st.expander(f"{'Your' if coach == MY_COACH else coach + chr(39) + 's'} no-dates and times",
                             expanded=coach == MY_COACH and not CAN):
                st.caption("Days and times you can't coach. The scheduler keeps your teams off them (a head "
                           "coach's are only broken as a last resort, and flagged). “Rather not” is a softer "
                           "preference it weighs against everyone else's.")
                nk = f"nd_{coach}"
                c1, c2 = st.columns(2)
                nd_how = c1.radio("When", ["Every week", "Specific dates"], horizontal=True, key=f"{nk}_how")
                nd_kind = c2.radio("How strict", ["Can't make it", "Would rather not"], horizontal=True,
                                   key=f"{nk}_kind")
                c1, c2 = st.columns(2)
                if nd_how == "Every week":
                    nd_days = c1.multiselect("Days", DAYS, key=f"{nk}_days", placeholder="e.g. Tue")
                    nd_from = nd_to = None
                else:
                    nd_days = []
                    cc1, cc2 = c1.columns(2)
                    nd_from = cc1.date_input("From", value=start, format="MM/DD/YYYY", key=f"{nk}_from")
                    nd_to = cc2.date_input("To", value=nd_from, format="MM/DD/YYYY", key=f"{nk}_to")
                nd_times = c2.multiselect("Times", WEEKDAY_KEYS + WEEKEND_KEYS, key=f"{nk}_times",
                                          format_func=lambda k: SLOTS[k] + (" (weekend)" if k in WEEKEND_KEYS else ""),
                                          placeholder="All day")
                nd_note = st.text_input("Note (optional)", key=f"{nk}_note", placeholder="e.g. work, travel")
                nd_bad = (nd_how == "Every week" and not nd_days) or (nd_to is not None and nd_to < nd_from)
                if st.button("Add", type="primary", disabled=nd_bad, key=f"{nk}_add",
                             help="Pick at least one day." if nd_how == "Every week" and not nd_days else None):
                    dates_txt = "" if nd_from is None else (
                        f"{nd_from:%m/%d/%Y}" + ("" if nd_to == nd_from else f"-{nd_to:%m/%d/%Y}"))
                    arow = {c: "" for c in COLUMNS["Availability"]}
                    arow.update({"who": coach, "type": "No" if nd_kind.startswith("Can") else "Not preferred",
                                 "days": ", ".join(d for d in DAYS if d in nd_days), "dates": dates_txt,
                                 "times": ", ".join(sorted(nd_times, key=SLOT_KEYS.index)), "note": nd_note.strip()})
                    shown_dates = "" if nd_from is None else (
                        f"{nd_from:%-m/%-d}" + ("" if nd_to == nd_from else f"–{nd_to:%-m/%-d}"))
                    when = " ".join(x for x in (arow["days"].replace(", ", "/"), shown_dates,
                                                arow["times"] or "all day") if x)
                    what = f"{'no' if arow['type'] == 'No' else 'rather not'}: {when}"
                    change_prefs(coach, None, arow, [], [], f"Saved {coach}'s {what}.", what)
                av_ = inp.availability.fillna("")
                av_ = av_[av_["who"].astype(str).str.strip() == coach]
                if len(av_):
                    def _nd_label(r) -> str:
                        kind = "Rather not" if str(r["type"]).strip().lower().startswith("not") else "No"
                        when = " ".join(str(x).strip() for x in (r["days"], r["dates"], r["times"] or "all day")
                                        if str(x).strip())
                        return f"{kind}: {when}" + (f" ({r['note']})" if str(r["note"]).strip() else "")
                    cur = {i: _nd_label(r) for i, r in av_.iterrows()}
                    drop_nd = st.multiselect("Remove", list(cur), format_func=lambda i: cur[i], key=f"{nk}_drop")
                    if st.button("Remove selected", disabled=not drop_nd, key=f"{nk}_dropbtn"):
                        change_prefs(coach, None, None, [], [pref_key(av_.loc[i].to_dict(), "who") for i in drop_nd],
                                     f"Removed {plural(len(drop_nd), 'no-date')} for {coach}.",
                                     "removed " + "; ".join(cur[i] for i in drop_nd))

        if result is not None:
            a = result.assignments
            mine = a[a["coach"].fillna("").map(lambda s: coach in [x.strip() for x in s.split(";")])]
            if len(mine):
                _keys = ["date", "day", "slot", "slot_label", "kind", "name", "on_pattern"]
                mine = (mine.groupby(_keys, as_index=False)
                        ["court"].agg(lambda cs: ", ".join(court_short(c) for c in sorted(cs))))
                mine["o"] = mine["slot"].map(SLOT_KEYS.index)
                mine = mine.sort_values(["date", "o"])
                mine["role"] = mine["name"].map(
                    lambda n: "Head" if n in heads else ("Assistant" if n in assists else ""))
            st.markdown(f"**{coach}'s schedule** · {plural(len(mine), 'session')}")
            if len(mine):
                show = mine.assign(date=mine["date"].map(lambda d: f"{d:%a %b} {d.day}"))
                st.dataframe(
                    show[["date", "slot_label", "court", "kind", "name", "role"]].rename(columns={
                        "date": "Date", "slot_label": "Time", "court": "Court", "kind": "Type",
                        "name": "Team / Program", "role": "Role"}),
                    hide_index=True, width="stretch")
                mine_p = [p for p in privs if p["coach"] == coach]
                if mine_p:
                    st.markdown(f"**{coach}'s privates** · {plural(len(mine_p), 'session')}")
                    st.dataframe(pd.DataFrame([{"Date": f"{p['date']:%a %b %-d}",
                                                "Time": f"{p['time']}–{p['end_label']}",
                                                "Court": court_name(p["court"]), "Type": p["type"], "Team": p["team"],
                                                "Player": p["player"]} for p in mine_p]),
                                 hide_index=True, width="stretch")
                st.download_button(f"Add {coach}'s schedule to Google Calendar (.ics)",
                                   lambda: to_ics(result, coach),
                                   f"{coach.replace(' ', '_')}_{start:%Y%m%d}-{end:%Y%m%d}.ics",
                                   mime="text/calendar",
                                   help="In Google Calendar: Settings → Import & export → Import this file.")
