"""Programs in numbered sessions (Jump Start, Jump Start Volley Up)."""

from datetime import date

import pandas as pd

from scheduler import clean, gcal, simulate, solve
from scheduler.data import parse_sessions, session_meetings

S, E = date(2026, 11, 1), date(2026, 11, 30)
SESS = "9: 11/02/2026-11/27/2026; 10: 11/30/2026-12/31/2026"


def _inp():
    inp = simulate(4, 2, 2, seed=11, start=S, end=E)
    rows = [
        {"program": "Jump Start", "side": "Both", "days": "Wed", "time": "3:30", "courts": "1, 2",
         "coaches": "", "dates": "", "sessions": SESS, "tag": "A", "short": "JS"},
        {"program": "Jump Start", "side": "Girls", "days": "Wed", "time": "4:30", "courts": "1",
         "coaches": "", "dates": "", "sessions": SESS, "tag": "B-GIRLS", "short": "JS"},
        {"program": "Jump Start", "side": "Boys", "days": "Wed", "time": "4:30", "courts": "2",
         "coaches": "", "dates": "", "sessions": SESS, "tag": "B-BOYS", "short": "JS"},
        {"program": "Jump Start Volley Up", "side": "Both", "days": "Mon", "time": "4:30", "courts": "3",
         "coaches": "", "dates": "", "sessions": SESS, "tag": "", "meeting": "Practice", "short": "JSVU"},
        {"program": "Jump Start Volley Up", "side": "Both", "days": "Fri", "time": "7:20-8:30", "courts": "3",
         "coaches": "", "dates": "", "sessions": SESS, "tag": "", "meeting": "Game", "short": "JSVU"},
    ]
    inp.locals = pd.concat([inp.locals, pd.DataFrame(rows)], ignore_index=True)
    return inp


def test_sessions_parse_and_number_meetings():
    assert parse_sessions(SESS, 2026)[0] == ("9", date(2026, 11, 2), date(2026, 11, 27))
    data = clean(_inp(), S, E)
    js = [lp for lp in data.locals if lp.name == "Jump Start"]
    assert {lp.side for lp in js} == {"Both", "Girls", "Boys"} and {lp.tag for lp in js} == {"A", "B-GIRLS", "B-BOYS"}
    m = session_meetings(data.locals, data.closures)
    a_row = next(lp.row for lp in js if lp.tag == "A")
    # Session 9 Wednesdays: 11/4, 11/11, 11/18, 11/25 (Thanksgiving is Thursday): 4 meetings
    assert m[a_row, date(2026, 11, 4)] == ("9", 1, 4) and m[a_row, date(2026, 11, 25)] == ("9", 4, 4)
    assert m[a_row, date(2026, 12, 2)][0:2] == ("10", 1)
    vu_fri = next(lp for lp in data.locals if lp.name == "Jump Start Volley Up" and lp.day == "Fri")
    assert vu_fri.slot == "7:20s" and vu_fri.meeting == "Game"


def test_calendar_titles_and_paper_marks():
    from scheduler.paper import paper_weeks_html
    inp = _inp()
    r = solve(inp, S, E, time_limit_s=10)
    data = clean(inp, S, E)
    loc = r.assignments[r.assignments["kind"] == "Local"]
    links = {("Program", "Jump Start"): "js", ("Program", "Jump Start Volley Up"): "vu"}
    evs, _ = gcal.desired_events(None, loc, [], data.teams, links, S, E, locals_=data.locals,
                                 closures=data.closures)
    titles = {e.summary for e in evs if e.start.date() == date(2026, 11, 11)}
    assert {"Jump Start Session 9A (Practice 2 of 4)", "Jump Start Session 9B-GIRLS (Practice 2 of 4)",
            "Jump Start Session 9B-BOYS (Practice 2 of 4)"} <= titles
    fri = [e for e in evs if e.start.date() == date(2026, 11, 6)]
    assert [e.summary for e in fri] == ["Jump Start Volley Up Session 9 (Game 1 of 3)"]  # 11/27 is a holiday
    assert fri[0].start.hour == 19 and fri[0].end.hour == 20 and fri[0].end.minute == 30
    assert any(e.summary == "Jump Start Volley Up Session 9 (Practice 1 of 4)" for e in evs)
    from scheduler.labels import set_program_shorts
    set_program_shorts(data.locals)
    page = paper_weeks_html(r.assignments, S, E, data.closures, data.tournaments, locals_=data.locals)
    assert "JS #9" in page and "JSVU #9" in page


def test_volley_up_fridays_take_their_court_at_7_20():
    inp = _inp()
    r = solve(inp, S, E, time_limit_s=10)
    a = r.assignments
    clash = a[(a["kind"] == "Practice") & (a["day"] == "Fri") & (a["slot"] == "7:20") & (a["court"] == 3)]
    assert clash.empty


def test_program_form_sessions_are_shared_by_the_programs_rows():
    from ui.tabs.programs import program_row, share_sessions
    sess = pd.DataFrame([{"session": "9", "first_day": pd.Timestamp("2026-11-02"),
                          "last_day": pd.Timestamp("2026-11-27")},
                         {"session": "#10", "first_day": pd.Timestamp("2026-11-30"),
                          "last_day": pd.Timestamp("2026-12-31")}])
    v = dict(name="Jump Start", short="JS", side="Both", on_site=True, holidays=False, days=["Wed"],
             p_start=None, p_end=None, times=["3:30"], courts=[1], coaches_sel=[], coaches_new="",
             tourns=None, sessions=sess, tag="A", meeting="Practice")
    problems, row = program_row(v)
    assert not problems                                   # the sessions give it its dates
    assert row["side"] == "Both" and row["tag"] == "A" and row["dates"] == "11/02/2026-12/31/2026"
    assert row["sessions"] == "9: 11/02/2026-11/27/2026; 10: 11/30/2026-12/31/2026"
    other = {"program": "Jump Start", "days": "Wed", "time": "4:30", "tag": "B-BOYS", "sessions": "", "dates": ""}
    nl = share_sessions(pd.DataFrame([other, row]), row)
    assert set(nl["sessions"]) == {row["sessions"]} and list(nl["tag"]) == ["B-BOYS", "A"]
    new = dict(other, tag="B-GIRLS")
    nl2 = share_sessions(pd.concat([nl, pd.DataFrame([new])], ignore_index=True), new, 2)
    assert nl2.at[2, "sessions"] == row["sessions"]       # a new time of the program takes its sessions


def test_adding_a_program_only_counts_that_program_as_changed():
    """A new column (sessions/tag/meeting) on the added row doesn't make every other program look changed,
    which rebuilt every scheduled run of dates (Nov 16, 2026)."""
    from ui.dates import _changed
    old = pd.DataFrame([{"program": "Girls Locals", "days": "Tue", "time": "5:30", "dates": "11/1/2026-11/30/2026"}])
    new = pd.concat([old, pd.DataFrame([{"program": "Open Gym", "days": "Sun", "time": "1PM",
                                         "dates": "1/4/2027-3/1/2027", "sessions": "", "tag": "", "meeting": ""}])],
                    ignore_index=True)
    assert [r["program"] for r in _changed(old, new)] == ["Open Gym"]
