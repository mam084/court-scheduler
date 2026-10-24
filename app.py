"""Court Scheduler – Streamlit app.

Run locally:  streamlit run app.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date

import pandas as pd
import streamlit as st

# Always load the current scheduler code. After a push, Streamlit Cloud can
# rerun app.py while older scheduler modules are still in memory, which shows
# up as an ImportError until the app is rebooted.
for _mod in [m for m in list(sys.modules) if m == "scheduler" or m.startswith("scheduler.")]:
    del sys.modules[_mod]

from scheduler import ScheduleInput, clean, month_range, simulate, solve
from scheduler.config import COURTS, DAYS, SLOT_KEYS, SLOTS, WEEKDAY_KEYS, WEEKDAYS, WEEKEND_KEYS
from scheduler.data import ATTRS, COLUMNS, empty, parse_names
from scheduler.export import to_excel, to_ics
from scheduler.solver import week_start
from scheduler.storage import Store, database_url, make_payload, new_id, read_payload
from scheduler.views import (
    club_stripes, court_grid_html, legend_html, month_calendar_html, practice_counts_html, range_label,
    short_list_text, week_dates,
)

st.set_page_config(page_title="Court Scheduler", page_icon="🏐", layout="wide")


def next_month(today: date) -> tuple[date, date]:
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return month_range(y, m)


def blank_template(start: date, end: date) -> bytes:
    """Template workbook with example rows on each sheet."""
    ex = simulate(2, 1, 1, seed=1, start=start, end=end)
    for attr in ATTRS.values():
        setattr(ex, attr, getattr(ex, attr).head(2))
    return ex.to_excel_bytes()


def secret(name: str):
    try:
        return st.secrets.get(name)
    except Exception:  # noqa: BLE001  (no secrets file)
        return None


# ------------------------------------------------------------ password --
APP_PASSWORD = secret("APP_PASSWORD")
if APP_PASSWORD and not st.session_state.get("authed"):
    st.title("🏐 Court Scheduler")
    pw = st.text_input("Club password", type="password")
    if pw and pw == str(APP_PASSWORD):
        st.session_state.authed = True
        st.rerun()
    elif pw:
        st.error("That password isn't right.")
    st.stop()


# ------------------------------------------------------- saved schedules --
@st.cache_resource
def get_store(url: str | None) -> Store:
    return Store(url)


store = get_store(database_url())
PROJECT_KEYS = ["project_id", "project_name", "inp", "result", "result_period", "inp_sig", "last_saved",
                "range_start", "range_end", "time_limit", "makeup"]


def payload_hash(payload: dict) -> str:
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def open_project(pid: str) -> bool:
    rec = store.get(pid)
    if rec is None:
        return False
    inp_, s_, e_, settings, result_ = read_payload(rec["payload"])
    st.session_state.update(
        project_id=pid, project_name=rec["name"], inp=inp_, result=result_,
        result_period=(result_.start, result_.end) if result_ else None,
        inp_sig=("project", pid, str(rec["updated_at"])),
        range_start=s_, range_end=e_,
        time_limit=int(settings.get("time_limit", 30)), makeup=bool(settings.get("makeup", True)),
        last_saved=payload_hash(rec["payload"]),
    )
    st.query_params["project"] = pid
    return True


def close_project():
    for k in PROJECT_KEYS:
        st.session_state.pop(k, None)
    st.query_params.clear()


# Opening a link like ...?project=abc (or refreshing) reopens that schedule.
qp = st.query_params.get("project")
if qp and st.session_state.get("project_id") != qp:
    if not open_project(qp):
        st.query_params.clear()
        st.session_state.flash_warn = "That saved schedule wasn't found (it may have been deleted)."

# ---------------------------------------------------------------- sidebar --
with st.sidebar:
    st.title("🏐 Court Scheduler")
    saved = store.list()
    names = {p["id"]: p["name"] for p in saved}
    current = st.session_state.get("project_id")
    options = ["__new__"] + list(names)
    choice = st.selectbox("Schedule", options, index=options.index(current) if current in options else 0,
                          format_func=lambda o: "➕ New schedule" if o == "__new__" else names[o],
                          key=f"picker_{current}")
    if choice != (current or "__new__"):
        if choice == "__new__":
            close_project()
        else:
            open_project(choice)
        st.rerun()

    if current is None:
        # ------------------------------------------------- new schedule --
        st.subheader("New schedule")
        d_start, d_end = next_month(date.today())
        new_name = st.text_input("Name", placeholder="e.g. Nov 3 – Dec 4 practices")
        new_start = st.date_input("From", value=d_start, format="MM/DD/YYYY")
        new_end = st.date_input("To", value=d_end, format="MM/DD/YYYY")
        source = st.radio("Start from", ["Upload spreadsheet", "Simulated club"], horizontal=True)
        up = None
        if source == "Upload spreadsheet":
            up = st.file_uploader("Club spreadsheet (.xlsx)", type=["xlsx"])
            st.download_button("Download blank template", blank_template(new_start, new_end),
                               "court_scheduler_template.xlsx",
                               help="Sheets: Teams, Availability, Wishlist, Locals, Weekends, Closures, Tournaments. "
                                    "Example rows show the format.")
        else:
            with st.expander("Simulation settings", expanded=False):
                n_girls = st.slider("Girls teams", 0, 24, 6)
                n_boys = st.slider("Boys teams", 0, 12, 4)
                n_locals = st.slider("Locals programs", 0, 6, 4)
                seed = st.number_input("Random seed", 0, 9999, 7, help="Change for a different simulated club")
        if st.button("Create schedule", type="primary", use_container_width=True):
            problems = []
            if new_end < new_start:
                problems.append("The end date is before the start date.")
            if source == "Upload spreadsheet" and up is None:
                problems.append("Upload the club spreadsheet first.")
            new_inp = None
            if not problems:
                try:
                    if source == "Upload spreadsheet":
                        new_inp = ScheduleInput.from_excel(up)
                    else:
                        new_inp = simulate(n_girls, n_boys, n_locals, seed=int(seed), start=new_start, end=new_end)
                except Exception as e:  # noqa: BLE001
                    problems.append(f"Couldn't read that file: {e}")
            if problems:
                st.error(" ".join(problems))
            else:
                pid = new_id()
                label = new_name.strip() or range_label(new_start, new_end)
                store.save(pid, label, make_payload(new_inp, new_start, new_end,
                                                    {"time_limit": 30, "makeup": True}, None))
                open_project(pid)
                st.session_state.resolve = True  # build it right away
                st.session_state.flash = f"Created “{label}”. It saves automatically; bookmark this page to come back."
                st.rerun()
        st.caption(f"Schedules are saved to a {store.kind}" +
                   ("." if store.permanent else " on the app server and can be cleared when the app restarts."))
    else:
        # ---------------------------------------------- open schedule --
        st.caption("✅ Saved automatically. Bookmark or share this page's link to come back to it.")
        st.date_input("From", key="range_start", format="MM/DD/YYYY")
        st.date_input("To", key="range_end", format="MM/DD/YYYY")
        with st.expander("Replace data with a spreadsheet"):
            rep = st.file_uploader("Club spreadsheet (.xlsx)", type=["xlsx"], key=f"replace_{current}")
            if rep is not None and st.button("Replace all input tables", use_container_width=True):
                try:
                    st.session_state.inp = ScheduleInput.from_excel(rep)
                    st.session_state.inp_sig = ("project", current, rep.name, rep.size)
                    st.session_state.result = None
                    st.session_state.resolve = True
                    st.session_state.flash = f"Replaced the inputs with {rep.name}."
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(f"Couldn't read that file: {e}")
        with st.expander("Solver settings", expanded=False):
            st.slider("Time limit (seconds)", 5, 120, key="time_limit")
            st.checkbox("Make up practices lost to closures", key="makeup",
                        help="If on, a team whose usual day is a closure gets a makeup that week "
                             "(on another weekday or an open weekend).")
        run = st.button("⚡ Auto-schedule", type="primary", use_container_width=True)
        with st.expander("Rename or delete"):
            nn = st.text_input("Name", value=st.session_state.get("project_name", ""), key=f"rename_{current}")
            if st.button("Rename", use_container_width=True) and nn.strip():
                store.rename(current, nn.strip())
                st.session_state.project_name = nn.strip()
                st.rerun()
            sure = st.checkbox("Yes, delete this schedule for everyone", key=f"del_{current}")
            if st.button("🗑️ Delete schedule", disabled=not sure, use_container_width=True):
                store.delete(current)
                close_project()
                st.session_state.flash = "Schedule deleted."
                st.rerun()
        if not store.permanent:
            st.caption("⚠️ Saved on the app server only: it survives refreshes but can be cleared when the app "
                       "restarts. Connect a database (see README) to keep schedules permanently.")

if "flash_warn" in st.session_state:
    st.warning(st.session_state.pop("flash_warn"))

if st.session_state.get("project_id") is None:
    st.header("🏐 Court Scheduler")
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))
    st.info("Create a new schedule in the sidebar (pick its dates, then upload your club spreadsheet or try the "
            "simulated club), or open a saved one from the **Schedule** menu.")
    if saved:
        st.markdown("**Saved schedules**")
        for p in saved:
            st.markdown(f"- [{p['name']}](?project={p['id']}) · updated {p['updated_at']:%b %-d, %-I:%M %p}")
    st.stop()

inp: ScheduleInput = st.session_state.inp
if not isinstance(getattr(inp, "tournaments", None), pd.DataFrame):
    inp.tournaments = empty("Tournaments")
start, end = st.session_state.range_start, st.session_state.range_end
time_limit, makeup = st.session_state.time_limit, st.session_state.makeup
if end < start:
    st.error("The schedule's end date is before its start date.")
    st.stop()
inp.start, inp.end = start, end

# Auto-schedule button, or adding/removing a program or tournament, rebuilds.
if run or st.session_state.pop("resolve", False):
    with st.spinner("Building the schedule…"):
        try:
            st.session_state.result = solve(inp, start, end, time_limit_s=time_limit, makeup_closures=makeup)
            st.session_state.result_period = (start, end)
            sf = st.session_state.result.shortfalls if st.session_state.result.ok else None
            if sf is not None and len(sf):
                st.toast(f"{sf['team'].nunique()} team(s) short of practices in {sf['week_of'].nunique()} week(s). "
                         "See the warning at the top.", icon="⚠️")
            elif sf is not None:
                st.toast("Every team got all its practices.", icon="✅")
        except ValueError as e:
            st.session_state.result = None
            st.error(str(e))

result = st.session_state.get("result")
if result is not None and st.session_state.get("result_period") != (start, end):
    result = None  # dates changed since the last run

# ------------------------------------------------------------------ header --
st.header(f"{st.session_state.project_name} · {range_label(start, end)}")

try:
    cleaned = clean(inp, start, end)
    closures = cleaned.closures
except ValueError as e:
    st.error(str(e))
    cleaned, closures = None, {}

if result is None:
    st.info("Press **⚡ Auto-schedule** in the sidebar to build the schedule for these dates. "
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
    short = result.shortfalls
    if len(short):
        n_teams, n_missed = short["team"].nunique(), int(short["short"].sum())
        lines = []
        for ws, g in short.groupby("week_of"):
            lines.append(f"- **Week of {ws:%b %-d}:** {short_list_text(g)}")
        st.error(f"**{n_teams} team(s) are short {n_missed} practice(s):**\n" + "\n".join(lines) +
                 "\n\nWhy each one happened is in the **Review** tab, and the full week-by-week counts are there too.",
                 icon="⚠️")
    if result.warnings:
        with st.expander(f"⚠️ {len(result.warnings)} note(s) about the input"):
            for w in result.warnings:
                st.markdown(f"- {w}")

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

tabs = st.tabs(["🏟️ Court view", "📅 Calendar", "🗓️ Programs", "🏆 Tournaments", "🧑‍🏫 Coaches", "🔎 Review",
                "📝 Inputs"])
TAB_COURT, TAB_CAL, TAB_PROG, TAB_TOURN, TAB_COACH, TAB_REVIEW, TAB_INPUTS = tabs


def template_locals(a: pd.DataFrame) -> pd.DataFrame:
    """Locals that are part of the usual week: running on most of that weekday's dates."""
    loc = a[(a["kind"] == "Local") & a["day"].isin(WEEKDAYS)]
    if loc.empty:
        return loc
    per_day = {wd: len({d for d in pd.date_range(start, end).date if DAYS[d.weekday()] == wd and d not in closures})
               for wd in WEEKDAYS}
    counts = loc.groupby(["name", "day", "slot", "court"])["date"].nunique().reset_index(name="n")
    keep = counts[counts.apply(lambda r: r["n"] * 2 >= per_day[r["day"]], axis=1)]
    return loc.drop_duplicates(subset=["day", "slot", "court", "name"]).merge(
        keep[["name", "day", "slot", "court"]], on=["name", "day", "slot", "court"])


def people_options(a: pd.DataFrame) -> list[str]:
    clubs = sorted({c for c in a["club"] if isinstance(c, str) and c})
    teams_ = sorted(set(a.loc[a["kind"] == "Practice", "name"]))
    coaches = cleaned.coaches if cleaned else []
    return clubs + teams_ + coaches


# ---------------------------------------------------------- court view --
with TAB_COURT:
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
            loc = template_locals(a)
            book = pd.concat([tmpl, loc[[c for c in tmpl.columns if c in loc.columns]]], ignore_index=True)
            st.markdown(court_grid_html(book, None, closures, hl or None, stripes), unsafe_allow_html=True)
            st.caption("Courts are ranked 1 → 7 → 6 → 5 → 3 → 4 → 2; older and 1s teams get the better courts.")
        else:
            ws = weeks[options.index(pick) - 1]
            dd = week_dates(ws, start, end, result.weekend_dates)
            wk_short = result.shortfalls[result.shortfalls["week_of"] == ws] if len(result.shortfalls) else []
            if len(wk_short):
                st.error(f"Short of practices this week: {short_list_text(wk_short)}", icon="⚠️")
            book = a[a["date"].map(week_start) == ws]
            st.markdown(court_grid_html(book, dd, closures, hl or None, stripes, result.tournaments),
                        unsafe_allow_html=True)

        st.download_button("⬇️ Download schedule (Excel)", to_excel(result, closures), f"schedule_{start:%Y%m%d}-{end:%Y%m%d}.xlsx")

# ------------------------------------------------------------ calendar --
with TAB_CAL:
    if result is None:
        st.caption("No schedule yet.")
    else:
        a = result.assignments
        c1, c2 = st.columns([2, 1])
        hl = set(c1.multiselect("Only show a club, team or coach", people_options(a), key="cal_filter"))
        show_locals = c2.toggle("Include locals programs", value=True)
        st.markdown(month_calendar_html(a, start, end, closures, hl or None, show_locals, result.weekend_dates,
                                        result.tournaments), unsafe_allow_html=True)


# ------------------------------------------------------------ programs --
SLOT_CHOICES = [f"{k}" for k in WEEKDAY_KEYS + WEEKEND_KEYS]


def slot_label(k: str) -> str:
    return f"{'Weekday' if k in WEEKDAY_KEYS else 'Weekend'} · {SLOTS[k]}"


with TAB_PROG:
    st.markdown("Add a locals program and it's blocked off on the courts and calendar right away; "
                "the practice schedule is rebuilt around it.")
    known_coaches = cleaned.coaches if cleaned else []
    with st.form("add_program", clear_on_submit=True):
        c1, c2 = st.columns([2, 1])
        name = c1.text_input("Program name", placeholder="e.g. Girls 14 League")
        on_site = c2.checkbox("Uses our courts", value=True,
                              help="Uncheck for programs somewhere else (e.g. a high school team). "
                                   "Those only block the coaches.")
        c1, c2, c3 = st.columns(3)
        days = c1.multiselect("Days", DAYS, placeholder="e.g. Tue, Thu")
        p_start = c2.date_input("Start date", value=start, format="MM/DD/YYYY")
        p_end = c3.date_input("End date", value=end, format="MM/DD/YYYY")
        c1, c2 = st.columns(2)
        times = c1.multiselect("Time", SLOT_CHOICES, format_func=slot_label,
                               help="Pick every timeslot the program uses (e.g. 3:45 and 5:30 for a 3:45–7:20 program).")
        courts = c2.multiselect("Courts used", COURTS, format_func=lambda c: f"Court {c}",
                                help="Leave empty if not known yet (it then blocks only the coaches).")
        c1, c2 = st.columns(2)
        coaches_sel = c1.multiselect("Coaches working it", known_coaches)
        coaches_new = c2.text_input("Other coaches", placeholder="Names separated by commas")
        submitted = st.form_submit_button("➕ Add program", type="primary")

    if submitted:
        problems = []
        if not name.strip():
            problems.append("Give the program a name.")
        if not days:
            problems.append("Pick at least one day.")
        if not times:
            problems.append("Pick at least one timeslot.")
        if p_end < p_start:
            problems.append("The end date is before the start date.")
        for d in days:
            ok = [t for t in times if (t in WEEKEND_KEYS) == (d in ("Sat", "Sun"))]
            if times and not ok:
                problems.append(f"{d} needs a {'weekend' if d in ('Sat', 'Sun') else 'weekday'} timeslot.")
        if problems:
            st.error(" ".join(problems))
        else:
            row = {
                "program": name.strip(), "days": ", ".join(days),
                "time": ", ".join(sorted(times, key=SLOT_KEYS.index)),
                "courts": ", ".join(str(c) for c in sorted(courts)) or "TBD",
                "coaches": "; ".join(dict.fromkeys(coaches_sel + parse_names(coaches_new))),
                "on_site": "Yes" if on_site else "No",
                "dates": f"{p_start:%m/%d/%Y}-{p_end:%m/%d/%Y}",
            }
            new_locals = pd.concat([inp.locals, pd.DataFrame([row])], ignore_index=True)
            trial = ScheduleInput(inp.teams, inp.availability, inp.wishlist, new_locals, inp.weekends, inp.closures,
                                  tournaments=inp.tournaments)
            try:
                clean(trial, start, end)
            except ValueError as e:
                st.error(f"Couldn't add {row['program']}: {e}")
            else:
                inp.locals = new_locals
                st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
                st.session_state.resolve = True
                st.session_state.flash = (f"Added {row['program']} ({row['days']}, "
                                          f"{p_start:%b %-d}–{p_end:%b %-d}). Schedule rebuilt around it.")
                st.rerun()

    st.subheader("Current programs")
    if inp.locals.empty:
        st.caption("No programs yet.")
    else:
        view = inp.locals.copy().fillna("")
        view.index = [f"{r['program']} · {r['days']} · {r['time']}" + (f" · {r['dates']}" if r["dates"] else "")
                      for _, r in view.iterrows()]
        st.dataframe(view.rename(columns={"program": "Program", "days": "Days", "time": "Time", "courts": "Courts",
                                          "coaches": "Coaches", "on_site": "On site", "dates": "Dates"}),
                     hide_index=True, use_container_width=True)
        c1, c2 = st.columns([3, 1])
        drop = c1.multiselect("Remove programs", list(view.index), placeholder="Choose programs to remove")
        if c2.button("🗑️ Remove", disabled=not drop, use_container_width=True):
            keep = [i for i, lab in enumerate(view.index) if lab not in drop]
            inp.locals = inp.locals.iloc[keep].reset_index(drop=True)
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.resolve = True
            st.session_state.flash = f"Removed {len(drop)} program(s). Schedule rebuilt."
            st.rerun()
    st.caption("Programs you add live in this session. Download the inputs spreadsheet to keep them "
               "(and upload it next time).")
    st.download_button("⬇️ Download inputs (Excel)", inp.to_excel_bytes(), "club_inputs.xlsx", key="dl_inputs_prog")

# --------------------------------------------------------- tournaments --
with TAB_TOURN:
    st.markdown("**Local** tournaments are hosted here and take over courts for the whole day. "
                "**Travel** tournaments don't use our courts, but the teams going can't practice those days, "
                "avoid the days right before and after, and get priority for their two practices that week.")
    team_list = cleaned.teams if cleaned else []
    clubs = sorted({t.club for t in team_list if t.club})
    ages_all = sorted({t.age for t in team_list})
    program_names = sorted({lp.name for lp in cleaned.locals}) if cleaned else []

    with st.container(border=True):
        c1, c2 = st.columns([2, 1])
        t_name = c1.text_input("Tournament name", placeholder="e.g. Boys 14 Local Tournament", key="t_name")
        t_kind = c2.radio("Type", ["Local", "Travel"], horizontal=True, key="t_kind")
        c1, c2 = st.columns(2)
        t_start = c1.date_input("Start date", value=start, format="MM/DD/YYYY", key="t_start")
        t_end = c2.date_input("End date", value=t_start, format="MM/DD/YYYY", key="t_end")
        if t_kind == "Local":
            c1, c2 = st.columns(2)
            t_prog = c1.selectbox("Locals program", ["(none)"] + program_names, key="t_prog",
                                  help="Its coaches are blocked for the day.")
            t_courts = c2.multiselect("Courts used", COURTS, default=COURTS, key="t_courts",
                                      format_func=lambda c: f"Court {c}")
            t_club, t_ages, t_teams = "", [], []
        else:
            c1, c2 = st.columns(2)
            t_club = c1.selectbox("Club", ["Any"] + clubs, key="t_club")
            t_ages = c2.multiselect("Age groups", ages_all, key="t_ages", format_func=lambda a: f"{a}s")
            match = [] if (t_club == "Any" and not t_ages) else [
                t.name for t in team_list if (t_club == "Any" or t.club == t_club) and (not t_ages or t.age in t_ages)]
            t_teams = st.multiselect("Teams going", [t.name for t in team_list], default=match,
                                     key=f"t_teams_{t_club}_{'-'.join(map(str, t_ages))}",
                                     help="Filled in from the club and ages; remove teams that aren't going.")
            t_prog, t_courts = "(none)", []
        add_t = st.button("➕ Add tournament", type="primary")

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
                   "courts": ", ".join(map(str, sorted(t_courts))) if t_kind == "Local" else "", "note": ""}
            inp.tournaments = pd.concat([inp.tournaments, pd.DataFrame([row])], ignore_index=True)
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.resolve = True
            span = f"{t_start:%b %-d}" + ("" if t_end == t_start else f"–{t_end:%b %-d}")
            st.session_state.flash = f"Added {row['name']} ({t_kind.lower()}, {span}). Schedule rebuilt around it."
            st.session_state.pop("t_name", None)  # clear the form for the next one
            st.rerun()

    st.subheader("Tournaments")
    if inp.tournaments.empty:
        st.caption("None yet.")
    else:
        tv = inp.tournaments.copy().fillna("")
        tv.index = [f"{r['name']} · {r['type']} · {r['start_date']}" for _, r in tv.iterrows()]
        st.dataframe(tv[["name", "type", "start_date", "end_date", "club", "ages", "teams", "program", "courts"]]
                     .rename(columns=lambda c: c.replace("_", " ").title()), hide_index=True, use_container_width=True)
        c1, c2 = st.columns([3, 1])
        drop_t = c1.multiselect("Remove tournaments", list(tv.index), placeholder="Choose tournaments to remove")
        if c2.button("🗑️ Remove", disabled=not drop_t, use_container_width=True, key="rm_tourn"):
            keep = [i for i, lab in enumerate(tv.index) if lab not in drop_t]
            inp.tournaments = inp.tournaments.iloc[keep].reset_index(drop=True)
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.resolve = True
            st.session_state.flash = f"Removed {len(drop_t)} tournament(s). Schedule rebuilt."
            st.rerun()
    st.caption("Tournaments you add live in this session. Download the inputs spreadsheet to keep them.")
    st.download_button("⬇️ Download inputs (Excel)", inp.to_excel_bytes(), "club_inputs.xlsx", key="dl_inputs_tourn")

# ------------------------------------------------------------- coaches --
with TAB_COACH:
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
                                   to_ics(result, coach), f"{coach.replace(' ', '_')}_{start:%Y%m%d}-{end:%Y%m%d}.ics",
                                   mime="text/calendar",
                                   help="In Google Calendar: Settings → Import & export → Import this file.")

# -------------------------------------------------------------- review --
with TAB_REVIEW:
    if result is None:
        st.caption("No schedule yet.")
    else:
        st.subheader("Practices per team, per week")
        st.caption("Red = short of the team's weekly target. Grey = no target that week (cut off by the date "
                   "range, or before the team's season starts).")
        if len(result.practice_counts):
            hl_r = set(st.multiselect("Highlight teams", sorted(result.practice_counts["team"].unique()),
                                      key="counts_hl"))
            st.markdown(practice_counts_html(result.practice_counts, hl_r or None), unsafe_allow_html=True)
        else:
            st.caption("Rebuild the schedule to see practice counts.")
        st.subheader("Everything to review")
        if result.issues.empty:
            st.success("Nothing to review: every team has its practices on its usual schedule.")
        else:
            st.markdown("Everything that differs from the ideal schedule, most important first.")
            sev = st.multiselect("Severity", ["High", "Medium", "Low"], default=["High", "Medium", "Low"])
            iss = result.issues[result.issues["severity"].isin(sev)]
            st.dataframe(iss.rename(columns={"severity": "Severity", "team": "Team", "week_of": "Week of",
                                             "issue": "Issue", "detail": "Why"}),
                         hide_index=True, use_container_width=True)

# -------------------------------------------------------------- inputs --
with TAB_INPUTS:
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
        "Tournaments": "type = Local (hosted here: blocks its courts all day; blank courts = all) or Travel "
                       "(away: the teams matching club + ages, or the listed teams, can't practice those days "
                       "and avoid the days right before and after). Ages like `15-16` or `14U`.",
    }
    sig_key = abs(hash((st.session_state.get("inp_sig"), st.session_state.get("locals_ver", 0)))) % 10**8
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


# ----------------------------------------------------------- autosave --
# Save the inputs, dates, settings and latest schedule whenever anything
# changed this run, so a refresh or a new visit picks up where you left off.
_payload = make_payload(inp, start, end, {"time_limit": time_limit, "makeup": makeup},
                        st.session_state.get("result"))
_h = payload_hash(_payload)
if _h != st.session_state.get("last_saved"):
    try:
        store.save(st.session_state.project_id, st.session_state.project_name, _payload)
        st.session_state.last_saved = _h
    except Exception as e:  # noqa: BLE001
        st.toast(f"Couldn't save: {e}", icon="⚠️")
