"""Practices with their own start and end times (Nov 18, 2026): a 9-11:30 practice holds its court from
team practices in the 11-1 block, but a private can still go on that court after 11:30."""

from datetime import date

import pandas as pd

from scheduler import clean, gcal, simulate, solve
from scheduler import hours as H
from scheduler import privates as pv
from scheduler.paper import paper_weeks_html

S, E = date(2026, 12, 6), date(2026, 12, 12)


def test_parse_and_labels():
    assert H.parse_minutes("9") == 540 and H.parse_minutes("11:30") == 690 and H.parse_minutes("2 PM") == 840
    assert H.parse_minutes("11:30 AM") == 690 and H.parse_minutes("6") == 1080
    assert H.label(540, 690) == "9–11:30 AM" and H.label(690, 780) == "11:30 AM–1 PM"
    assert H.label(945, 1080) == "3:45–6 PM" and H.short_label(540, 690) == "9-11:30"


def test_held_courts_and_with_hours():
    d = date(2026, 11, 27)
    hrs = H.from_table(pd.DataFrame([H.row("S14-B", d, "9AM", 3, 540, 690)]))
    assert H.held_courts(hrs, d, "11AM") == {3}            # runs into 11-1: Court 3 held then
    assert H.held_courts(hrs, d, "9AM") == set()           # its own time isn't "held", it's its own
    assert H.held_courts(hrs, d, "1PM") == set()
    a = pd.DataFrame([{"kind": "Practice", "name": "S14-B", "date": d, "slot": "9AM", "court": 3,
                       "slot_label": "9–11 AM", "coach": "Matt"},
                      {"kind": "Practice", "name": "S15-B", "date": d, "slot": "11AM", "court": 4,
                       "slot_label": "11 AM–1 PM", "coach": "Leo"}])
    out = H.with_hours(a, hrs)
    assert list(out["slot_label"]) == ["9–11:30 AM", "11 AM–1 PM"]
    assert H.span_of(out.iloc[0].to_dict()) == (540, 690) and H.span_of(out.iloc[1].to_dict()) == (660, 780)
    # a private on Court 3 at 11:30 is fine; at 11:00 it overlaps the practice
    privs = [{"row": 0, "coach": "Cliff", "date": d, "start": 690, "end": 750, "time": "11:30 AM", "court": 3},
             {"row": 1, "coach": "Jo", "date": d, "start": 660, "end": 720, "time": "11:00 AM", "court": 3}]
    assert pv.conflicts(privs[:1], out) == {}
    assert any("S14-B" in p for p in pv.conflicts(privs[1:], out)[1])
    assert pv.free_courts(d, 690, out, [], [3, 4]) == [3]    # Court 4 has S15-B 11-1


def _first_530(r):
    a = r.assignments
    p = a[(a["kind"] == "Practice") & (a["slot"] == "5:30")].sort_values(["date", "court"])
    return p.iloc[0]


def test_solver_keeps_the_held_court_free_and_the_practice_on_it():
    inp = simulate(8, 4, 0, seed=4, start=S, end=E)
    r = solve(inp, S, E, time_limit_s=10)
    first = _first_530(r)
    team, d, court = first["name"], first["date"], int(first["court"])
    inp.hours = pd.DataFrame([H.row(team, d, "5:30", court, 1050, 1200)])   # 5:30-8 PM
    data = clean(inp, S, E)
    assert data.hours[team, d, "5:30"].court == court
    pins = pd.DataFrame([{"team": team, "action": "Add", "date": f"{d:%m/%d/%Y}", "days": "", "time": "5:30",
                          "court": str(court), "note": ""}])
    inp.overrides = pd.concat([inp.overrides, pins], ignore_index=True)
    r2 = solve(inp, S, E, time_limit_s=10)
    a = r2.assignments
    mine = a[(a["name"] == team) & (a["date"] == d) & (a["slot"] == "5:30")]
    assert len(mine) == 1 and int(mine.iloc[0]["court"]) == court
    assert mine.iloc[0]["slot_label"] == "5:30–8 PM"
    at_720 = a[(a["date"] == d) & (a["slot"] == "7:20") & (a["court"] == court)]
    assert at_720.empty                                    # held: nobody at 7:20 on that court
    # the paper sheet writes its hours, and the 7:20 line says it's still going
    h = paper_weeks_html(a, S, E, data.closures, data.tournaments)
    assert "5:30-8" in h and "until 8" in h
    # Google Calendar: the event runs 5:30-8
    evs, _ = gcal.desired_events(a[a["kind"] == "Practice"], None, [], data.teams, {("Team", team): "cal"}, S, E,
                                 hours=data.hours)
    ev = next(e for e in evs if e.start.date() == d and e.target == f"Team: {team}")
    assert (ev.start.hour, ev.start.minute, ev.end.hour, ev.end.minute) == (17, 30, 20, 0)
