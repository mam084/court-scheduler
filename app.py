"""Court Scheduler – Streamlit app.

Run locally:  streamlit run app.py
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from scheduler import ScheduleInput, clean, month_range, simulate, solve
from scheduler.config import SLOT_KEYS
from scheduler.data import ATTRS, COLUMNS
from scheduler.export import to_excel, to_ics
from scheduler.solver import week_start
from scheduler.views import club_stripes, court_grid_html, legend_html, month_calendar_html, month_name, week_dates

st.set_page_config(page_title="Court Scheduler", page_icon="🏐", layout="wide")


def next_month(today: date) -> tuple[int, int]:
    return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)


def blank_template(start: date) -> bytes:
    """Template workbook with one example row per sheet."""
    ex = simulate(2, 1, 1, start.year, start.month, seed=1)
    for attr in ATTRS.values():
        setattr(ex, attr, getattr(ex, attr).head(2))
    return ex.to_excel_bytes()


# ---------------------------------------------------------------- sidebar --
with st.sidebar:
    st.title("🏐 Court Scheduler")

    ny, nm = next_month(date.today())
    c1, c2 = st.columns(2)
    month = c1.selectbox("Month", range(1, 13), index=nm - 1,
                         format_func=lambda m: date(2000, m, 1).strftime("%B"))
    year = c2.number_input("Year", 2024, 2035, ny, step=1)
    start, end = month_range(int(year), int(month))

    source = st.radio("Input data", ["Upload spreadsheet", "Simulated club"], horizontal=True)

    if source == "Simulated club":
        with st.expander("Simulation settings", expanded=False):
            n_girls = st.slider("Girls teams", 0, 24, 6)
            n_boys = st.slider("Boys teams", 0, 12, 4)
            n_locals = st.slider("Locals programs", 0, 6, 4)
            seed = st.number_input("Random seed", 0, 9999, 7, help="Change for a different simulated club")
        sig = ("sim", n_girls, n_boys, n_locals, seed, start)
        if st.session_state.get("inp_sig") != sig:
            st.session_state.inp = simulate(n_girls, n_boys, n_locals, start.year, start.month, int(seed))
            st.session_state.inp_sig = sig
            st.session_state.pop("result", None)
    else:
        up = st.file_uploader("Club spreadsheet (.xlsx)", type=["xlsx"])
        if up is not None:
            sig = ("upload", up.name, up.size)
            if st.session_state.get("inp_sig") != sig:
                try:
                    st.session_state.inp = ScheduleInput.from_excel(up)
                    st.session_state.inp_sig = sig
                    st.session_state.pop("result", None)
                except Exception as e:  # noqa: BLE001
                    st.error(f"Couldn't read that file: {e}")
        elif st.session_state.get("inp_sig", ("",))[0] != "upload":
            st.session_state.pop("inp", None)
        st.download_button("Download blank template", blank_template(start), "court_scheduler_template.xlsx",
                           help="Sheets: Teams, Availability, Wishlist, Locals, Weekends, Closures. "
                                "Example rows show the format.")

    with st.expander("Solver settings", expanded=False):
        time_limit = st.slider("Time limit (seconds)", 5, 120, 30)
        makeup = st.checkbox("Make up practices lost to closures", value=True,
                             help="If on, a team whose usual day is a closure gets a makeup that week "
                                  "(on another weekday or an open weekend).")

    run = st.button("⚡ Auto-schedule", type="primary", use_container_width=True,
                    disabled="inp" not in st.session_state)

inp: ScheduleInput | None = st.session_state.get("inp")

if run and inp is not None:
    with st.spinner("Building the schedule…"):
        try:
            st.session_state.result = solve(inp, start, end, time_limit_s=time_limit, makeup_closures=makeup)
            st.session_state.result_period = (start, end)
        except ValueError as e:
            st.session_state.pop("result", None)
            st.error(str(e))

result = st.session_state.get("result")
if result is not None and st.session_state.get("result_period") != (start, end):
    result = None  # month changed since the last run

# ------------------------------------------------------------------ header --
st.header(f"Practice schedule · {month_name(start)}")
if inp is None:
    st.info("Upload your club spreadsheet in the sidebar, or switch to the simulated club to try it out. "
            "Download the blank template there to see the format.")
    st.stop()

try:
    cleaned = clean(inp, start, end)
    closures = cleaned.closures
except ValueError as e:
    st.error(str(e))
    cleaned, closures = None, {}

if result is None:
    st.info("Press **⚡ Auto-schedule** in the sidebar to build the schedule. "
            "You can review and edit the inputs in the **Inputs** tab first.")
elif not result.ok:
    st.error(f"No schedule found ({result.status}). Check for more teams than open courts, "
             "or a coach whose no-dates block every slot.")
    result = None
else:
    m = result.metrics
    c = st.columns(6)
    c[0].metric("Practices", m["practices_scheduled"])
    c[1].metric("Missed", m["missed_practices"], help="Practices short of each team's weekly target")
    c[2].metric("Weekend practices", m["weekend_practices"])
    c[3].metric("Wishlist 1st choice", f"{m['wishlist_pct']}%" if m["wishlist_pct"] is not None else "–",
                help="Weekday practices matching the team's 1st-choice wishlist")
    c[4].metric("Coach double-ups", m["double_ups"], help="A coach's teams back-to-back on the same day")
    c[5].metric("On usual schedule", f"{m['on_pattern_pct']}%")
    if result.warnings:
        with st.expander(f"⚠️ {len(result.warnings)} note(s) about the input"):
            for w in result.warnings:
                st.markdown(f"- {w}")

tabs = st.tabs(["🏟️ Court view", "📅 Calendar", "🧑‍🏫 Coaches", "🔎 Review", "📝 Inputs"])


def people_options(a: pd.DataFrame) -> list[str]:
    clubs = sorted({c for c in a["club"] if isinstance(c, str) and c})
    teams_ = sorted(set(a.loc[a["kind"] == "Practice", "name"]))
    coaches = cleaned.coaches if cleaned else []
    return clubs + teams_ + coaches


# ---------------------------------------------------------- court view --
with tabs[0]:
    if result is None:
        st.caption("No schedule yet.")
    else:
        a = result.assignments
        stripes = club_stripes(a["club"])
        weeks = sorted({week_start(d) for d in a["date"]})
        options = ["Weekly template"] + [f"Week of {w:%b %-d}" for w in weeks]
        c1, c2 = st.columns([1, 2])
        pick = c1.selectbox("Show", options,
                            help="The template is each team's usual weekday slot; weeks show the actual dates, "
                                 "including weekends, makeups and closures.")
        hl = set(c2.multiselect("Highlight a club, team or coach", people_options(a)))

        st.markdown(legend_html(a.loc[a["kind"] == "Practice", "age"], stripes), unsafe_allow_html=True)
        if pick == "Weekly template":
            tmpl = result.pattern.rename(columns={"team": "name"}).assign(kind="Practice", on_pattern=True)
            loc = a[(a["kind"] == "Local") & a["day"].isin(["Mon", "Tue", "Wed", "Thu", "Fri"])]
            loc = loc.drop_duplicates(subset=["day", "slot", "court", "name"])
            book = pd.concat([tmpl, loc[[c for c in tmpl.columns if c in loc.columns]]], ignore_index=True)
            st.markdown(court_grid_html(book, None, closures, hl or None, stripes), unsafe_allow_html=True)
        else:
            ws = weeks[options.index(pick) - 1]
            dd = week_dates(ws, start, end, result.weekend_dates)
            book = a[a["date"].map(week_start) == ws]
            st.markdown(court_grid_html(book, dd, closures, hl or None, stripes), unsafe_allow_html=True)

        st.download_button("⬇️ Download schedule (Excel)", to_excel(result, closures), f"schedule_{start:%Y_%m}.xlsx")

# ------------------------------------------------------------ calendar --
with tabs[1]:
    if result is None:
        st.caption("No schedule yet.")
    else:
        a = result.assignments
        c1, c2 = st.columns([2, 1])
        hl = set(c1.multiselect("Only show a club, team or coach", people_options(a), key="cal_filter"))
        show_locals = c2.toggle("Include locals programs", value=False)
        st.markdown(month_calendar_html(a, start, end, closures, hl or None, show_locals, result.weekend_dates),
                    unsafe_allow_html=True)

# ------------------------------------------------------------- coaches --
with tabs[2]:
    if cleaned is None:
        st.caption("Fix the input errors first.")
    else:
        coach = st.selectbox("Coach", cleaned.coaches)
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

        if result is not None:
            a = result.assignments
            mine = a[a["coach"].fillna("").map(lambda s: coach in [x.strip() for x in s.split(";")])]
            if len(mine):
                mine = (mine.groupby(["date", "day", "slot", "slot_label", "kind", "name", "on_pattern"], as_index=False)
                        ["court"].agg(lambda cs: ", ".join(str(c) for c in sorted(cs))))
                mine["o"] = mine["slot"].map(SLOT_KEYS.index)
                mine = mine.sort_values(["date", "o"])
                mine["role"] = mine["name"].map(lambda n: "Head" if n in heads else ("Assistant" if n in assists else ""))
            st.markdown(f"**{coach}'s schedule** · {len(mine)} sessions")
            if len(mine):
                st.dataframe(
                    mine[["date", "day", "slot_label", "court", "kind", "name", "role"]].rename(columns={
                        "date": "Date", "day": "Day", "slot_label": "Time", "court": "Court(s)", "kind": "Type",
                        "name": "Team / Program", "role": "Role"}),
                    hide_index=True, use_container_width=True)
                st.download_button(f"📆 Add {coach}'s schedule to Google Calendar (.ics)",
                                   to_ics(result, coach), f"{coach.replace(' ', '_')}_{start:%Y_%m}.ics",
                                   mime="text/calendar",
                                   help="In Google Calendar: Settings → Import & export → Import this file.")

# -------------------------------------------------------------- review --
with tabs[3]:
    if result is None:
        st.caption("No schedule yet.")
    elif result.issues.empty:
        st.success("Nothing to review: every team has its practices on its usual schedule.")
    else:
        st.markdown("Everything that differs from the ideal schedule, most important first.")
        sev = st.multiselect("Severity", ["High", "Medium", "Low"], default=["High", "Medium", "Low"])
        iss = result.issues[result.issues["severity"].isin(sev)]
        st.dataframe(iss.rename(columns={"severity": "Severity", "team": "Team", "week_of": "Week of",
                                         "issue": "Issue", "detail": "Why"}),
                     hide_index=True, use_container_width=True)

# -------------------------------------------------------------- inputs --
with tabs[4]:
    st.markdown("Edit any table, then press **⚡ Auto-schedule** again. "
                "Download the tables as a spreadsheet to keep your changes.")
    help_ = {
        "Teams": "Girls teams can leave head_coach blank (taken from the name, e.g. 18-Jordan). "
                 "start_date = first day the team can practice. weekend_only limits weekends to listed options "
                 "(`11/1 AM; 11/8 3PM`); weekend_pref is a soft preference (`AM`, `Sat AM; Sun PM`). "
                 "late_ok = Yes lets a 14s-and-under team practice at 7:20 (avoided unless needed).",
        "Availability": "One row per rule. who = team or coach. type = No (hard) or Not preferred. "
                        "days like `Mon` / `Weekdays`, dates like `11/10-11/16`, times like `5:30` or `3:45` "
                        "(blank = any).",
        "Wishlist": "Ranked day/time wishes. choice 1 or 2. Blank days = any day.",
        "Locals": "Recurring programs. on_site = No for programs elsewhere (blocks the coach only). "
                  "Courts like `1-4`; leave blank/TBD until known. Weekend times like `5PM` go on their own row.",
        "Weekends": "Open weekend days. open like `9AM-9PM`. Days sharing a group with required = Yes mean "
                    "'at least one of these must be used'.",
        "Closures": "Club closed all day.",
    }
    sig_key = abs(hash(st.session_state.get("inp_sig"))) % 10**8
    for sheet, attr in ATTRS.items():
        st.subheader(sheet)
        st.caption(help_[sheet])
        df = getattr(inp, attr).copy()
        for c in COLUMNS[sheet]:
            if c not in df.columns:
                df[c] = None
        df = df[COLUMNS[sheet]].astype(object)
        df = df.map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) or str(v) in ("None", "nan", "NaT")
                    else str(v)).astype(object)
        edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
                                key=f"edit_{attr}_{sig_key}", hide_index=True)
        setattr(inp, attr, edited)
    st.download_button("⬇️ Download inputs (Excel)", inp.to_excel_bytes(), "club_inputs.xlsx")
