"""Court Scheduler – Streamlit app.

Run locally:  streamlit run app.py
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from scheduler import ScheduleInput, clean, month_range, simulate, solve
from scheduler.closures import default_closures
from scheduler.config import SLOT_KEYS
from scheduler.export import to_excel, to_ics
from scheduler.solver import week_start
from scheduler.views import (
    color_map, court_grid_html, legend_html, month_calendar_html, month_name, week_dates,
)

st.set_page_config(page_title="Court Scheduler", page_icon="🏐", layout="wide")


def next_month(today: date) -> tuple[int, int]:
    return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)


# ---------------------------------------------------------------- sidebar --
with st.sidebar:
    st.title("🏐 Court Scheduler")

    ny, nm = next_month(date.today())
    c1, c2 = st.columns(2)
    month = c1.selectbox("Month", range(1, 13), index=nm - 1,
                         format_func=lambda m: date(2000, m, 1).strftime("%B"))
    year = c2.number_input("Year", 2024, 2035, ny, step=1)
    start, end = month_range(int(year), int(month))

    source = st.radio("Input data", ["Simulated club", "Upload spreadsheet"], horizontal=True)

    if source == "Simulated club":
        with st.expander("Simulation settings", expanded=False):
            n_teams = st.slider("Teams", 2, 40, 10)
            n_coaches = st.slider("Coaches", 2, 25, 10)
            n_locals = st.slider("Locals programs", 0, 6, 4)
            seed = st.number_input("Random seed", 0, 9999, 7, help="Change for a different simulated club")
        sig = ("sim", n_teams, n_coaches, n_locals, seed, start)
        if st.session_state.get("inp_sig") != sig:
            st.session_state.inp = simulate(n_teams, n_coaches, n_locals, start.year, start.month, int(seed))
            st.session_state.inp_sig = sig
            st.session_state.pop("result", None)
    else:
        up = st.file_uploader("Club spreadsheet (.xlsx)", type=["xlsx"])
        if up is not None:
            sig = ("upload", up.name, up.size, start)
            if st.session_state.get("inp_sig") != sig:
                try:
                    inp = ScheduleInput.from_excel(up)
                    if inp.closures.empty:  # default to major US holidays
                        inp.closures = pd.DataFrame(default_closures(start, end), columns=["date", "reason"])
                    st.session_state.inp = inp
                    st.session_state.inp_sig = sig
                    st.session_state.pop("result", None)
                except Exception as e:  # noqa: BLE001
                    st.error(f"Couldn't read that file: {e}")
        st.download_button(
            "Download blank template",
            simulate(4, 4, 2, start.year, start.month).to_excel_bytes(),
            "court_scheduler_template.xlsx",
            help="The template is filled with example rows. Replace them with your club's data.",
        )

    with st.expander("Solver settings", expanded=False):
        time_limit = st.slider("Time limit (seconds)", 5, 120, 20)
        makeup = st.checkbox("Make up practices lost to closures", value=True,
                             help="If on, a team whose usual day falls on a closure gets a makeup that week.")

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
    st.info("Upload your club spreadsheet in the sidebar (or switch to the simulated club).")
    st.stop()

try:
    cleaned = clean(inp)
    closures = cleaned.closures
except ValueError as e:
    st.error(str(e))
    closures = {}
    cleaned = None

if result is None:
    st.info("Press **⚡ Auto-schedule** in the sidebar to build the schedule. "
            "You can review and edit the inputs in the **Inputs** tab first.")
elif not result.ok:
    st.error(f"No schedule found ({result.status}). Check for too many teams for the open courts, "
             "or a coach whose no-dates block every slot.")
    result = None
else:
    m = result.metrics
    c = st.columns(5)
    c[0].metric("Practices scheduled", m["practices_scheduled"])
    short = m["full_week_practices_required"] - m["full_week_practices_scheduled"]
    c[1].metric("Missed practices", short, help="Full weeks where a team got fewer than 2 practices")
    c[2].metric("On usual schedule", f"{m['on_pattern_pct']}%")
    c[3].metric("3:45 practices", m["practices_at_3_45"])
    c[4].metric("Back-to-back days", m["back_to_back_weeks"])
    for w in result.warnings:
        st.warning(w)

tabs = st.tabs(["🏟️ Court view", "📅 Calendar", "🧑‍🏫 Coaches", "🔎 Review", "📝 Inputs"])

# ---------------------------------------------------------- court view --
with tabs[0]:
    if result is None:
        st.caption("No schedule yet.")
    else:
        a = result.assignments
        colors = color_map(a.loc[a["kind"] == "Practice", "name"])
        weeks = sorted({week_start(d) for d in a["date"]})
        options = ["Weekly template"] + [f"Week of {w:%b %-d}" for w in weeks]
        c1, c2 = st.columns([1, 2])
        pick = c1.selectbox("Show", options,
                            help="The template is each team's usual weekly slot; weeks show the actual dates, "
                                 "including makeups and closures.")
        people = sorted(set(a.loc[a["kind"] == "Practice", "name"])) + sorted(cleaned.coaches) if cleaned else []
        hl = set(c2.multiselect("Highlight teams or coaches", people))

        st.markdown(legend_html(colors), unsafe_allow_html=True)
        if pick == "Weekly template":
            tmpl = result.pattern.rename(columns={"team": "name"}).assign(kind="Practice", on_pattern=True)
            loc = a[a["kind"] == "Local"].drop_duplicates(subset=["day", "slot", "court", "name"])
            book = pd.concat([tmpl, loc[["day", "slot", "slot_label", "court", "kind", "name", "coach", "on_pattern"]]],
                             ignore_index=True)
            st.markdown(court_grid_html(book, None, closures, hl or None, colors), unsafe_allow_html=True)
        else:
            ws = weeks[options.index(pick) - 1]
            dd = week_dates(ws, start, end)
            book = a[a["date"].map(week_start) == ws]
            st.markdown(court_grid_html(book, dd, closures, hl or None, colors), unsafe_allow_html=True)

        st.download_button("⬇️ Download schedule (Excel)", to_excel(result, closures),
                           f"schedule_{start:%Y_%m}.xlsx", type="secondary")

# ------------------------------------------------------------ calendar --
with tabs[1]:
    if result is None:
        st.caption("No schedule yet.")
    else:
        a = result.assignments
        c1, c2 = st.columns([2, 1])
        names = sorted(set(a.loc[a["kind"] == "Practice", "name"])) + sorted(cleaned.coaches) if cleaned else []
        hl = set(c1.multiselect("Only show teams or coaches", names, key="cal_filter"))
        show_locals = c2.toggle("Include locals programs", value=False)
        st.markdown(month_calendar_html(a, start, end, closures, hl or None, include_locals=show_locals),
                    unsafe_allow_html=True)

# ------------------------------------------------------------- coaches --
with tabs[2]:
    if cleaned is None:
        st.caption("Fix the input errors first.")
    else:
        coach = st.selectbox("Coach", sorted(cleaned.coaches))
        prof = cleaned.coaches[coach]
        my_teams = [t.name for t in cleaned.teams if t.coach == coach]
        grouped = {}
        for lp in cleaned.locals:
            if coach in lp.coaches:
                grouped.setdefault((lp.name, lp.slot), []).append(lp.day)
        my_locals = [f"{n} ({' & '.join(ds)} {sl})" for (n, sl), ds in grouped.items()]
        nds = sorted((d, s) for (c, d), s in cleaned.no_dates.items() if c == coach)

        c1, c2, c3 = st.columns(3)
        c1.markdown("**Teams**\n\n" + ("\n".join(f"- {t}" for t in my_teams) or "_none_"))
        c1.markdown("**Locals**\n\n" + ("\n".join(f"- {x}" for x in my_locals) or "_none_"))
        c2.markdown("**Preferences**\n\n"
                    f"- Days: {', '.join(prof.preferred_days) or 'no preference'}\n"
                    f"- Times: {', '.join(prof.preferred_slots) or 'no preference'}")
        c3.markdown("**No-dates this period**\n\n" + ("\n".join(
            f"- {d:%a %b %-d}" + ("" if len(s) == len(SLOT_KEYS) else f" ({', '.join(sorted(s))})")
            for d, s in nds if start <= d <= end) or "_none_"))

        if result is not None:
            a = result.assignments
            mine = a[a["coach"].fillna("").map(lambda s: coach in [x.strip() for x in s.split(";")])]
            mine = (mine.groupby(["date", "day", "slot", "slot_label", "kind", "name", "on_pattern"], as_index=False)
                    ["court"].agg(lambda cs: ", ".join(str(c) for c in sorted(cs))))
            mine["o"] = mine["slot"].map(SLOT_KEYS.index)
            mine = mine.sort_values(["date", "o"])
            st.markdown(f"**{coach}'s schedule** · {len(mine)} sessions")
            st.dataframe(
                mine[["date", "day", "slot_label", "court", "kind", "name", "on_pattern"]].rename(columns={
                    "date": "Date", "day": "Day", "slot_label": "Time", "court": "Court(s)", "kind": "Type",
                    "name": "Team / Program", "on_pattern": "Usual slot"}),
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
        st.success("Nothing to review: every team has its 2 practices on its usual schedule.")
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
    labels = {
        "teams": ("Teams", "Priority 1-3 (3 = scheduled first if courts run out). Blank = by age."),
        "coaches": ("Coaches", "Preferred days like `Mon, Wed`; preferred times like `5:30, 7:20`."),
        "no_dates": ("No-dates", "Dates a coach can't make. Slot `All` blocks the whole day."),
        "locals": ("Locals programs", "Fixed weekly programs. Days like `Tue, Thu`; courts like `1-4` or `1,3,5`; coaches separated by `;`."),
        "closures": ("Closures", "Club closed. Prefilled with major US holidays."),
    }
    for attr, (title, help_) in labels.items():
        st.subheader(title)
        st.caption(help_)
        df = getattr(inp, attr).copy()
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        sig_key = abs(hash(st.session_state.get("inp_sig"))) % 10**8
        edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
                                key=f"edit_{attr}_{sig_key}", hide_index=True)
        setattr(inp, attr, edited)
    st.download_button("⬇️ Download inputs (Excel)", inp.to_excel_bytes(), "club_inputs.xlsx")
