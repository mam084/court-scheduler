"""Checks every hard rule on solved schedules, plus input parsing."""

from collections import Counter
from datetime import date

import pandas as pd
import pytest

from scheduler import clean, simulate, solve
from scheduler.closures import default_closures
from scheduler.data import parse_courts, parse_slot
from scheduler.solver import week_start


def _check_hard_rules(inp, r):
    data = clean(inp)
    a = r.assignments
    pr = a[a["kind"] == "Practice"]

    # One booking per court per slot (practices and locals together).
    dup = a.groupby(["date", "slot", "court"]).size()
    assert (dup <= 1).all(), dup[dup > 1]

    # Coach never in two places at once (teams + locals).
    seen = Counter()
    for _, row in a.drop_duplicates(subset=["date", "slot", "name"]).iterrows():
        for c in str(row["coach"]).split(";"):
            if c.strip():
                seen[(c.strip(), row["date"], row["slot"])] += 1
    assert max(seen.values()) == 1, [k for k, v in seen.items() if v > 1]

    # Nothing on coach no-dates or closures.
    for _, row in pr.iterrows():
        assert row["slot"] not in data.no_dates.get((row["coach"], row["date"]), set())
        assert row["date"] not in data.closures
        assert row["date"].weekday() < 5

    # At most one practice per team per day, two per week.
    assert (pr.groupby(["name", "date"]).size() <= 1).all()
    assert (pr.groupby(["name", pr["date"].map(week_start)]).size() <= 2).all()


@pytest.mark.parametrize("n_teams,n_coaches,seed", [(10, 10, 7), (10, 6, 1), (24, 14, 3)])
def test_hard_rules_hold(n_teams, n_coaches, seed):
    inp = simulate(n_teams, n_coaches, 4, 2026, 11, seed)
    r = solve(inp, inp.start, inp.end, time_limit_s=20)
    assert r.ok
    _check_hard_rules(inp, r)


def test_base_case_every_team_gets_two_per_full_week():
    inp = simulate(10, 10, 4, 2026, 11, 7)
    r = solve(inp, inp.start, inp.end, time_limit_s=20)
    m = r.metrics
    assert m["full_week_practices_scheduled"] == m["full_week_practices_required"]
    assert m["practices_at_3_45"] == 0  # plenty of room: 3:45 unused


def test_coach_with_two_teams_never_double_booked():
    inp = simulate(10, 3, 0, 2026, 11, 2)  # 3 coaches -> several run 2+ teams
    r = solve(inp, inp.start, inp.end, time_limit_s=20)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert (pr.groupby(["coach", "date", "slot"]).size() <= 1).all()


def test_closures_default_to_major_holidays_only():
    names = dict(default_closures(date(2026, 9, 1), date(2026, 12, 31)))
    assert date(2026, 11, 26) in names  # Thanksgiving
    assert date(2026, 12, 25) in names  # Christmas
    assert date(2026, 9, 7) not in names  # Labor Day: club practices


def test_parsing():
    assert parse_courts("1-3") == [1, 2, 3]
    assert parse_courts("1, 5,6") == [1, 5, 6]
    assert parse_courts("All") == [1, 2, 3, 4, 5, 6, 7]
    assert parse_slot("5:30-7:20") == "5:30"
    assert parse_slot("17:30") == "5:30"
    assert parse_slot("All") is None
    with pytest.raises(ValueError):
        parse_slot("6:00")


def test_bad_input_gives_readable_error():
    inp = simulate(4, 4, 2, 2026, 11, 0)
    inp.teams = pd.concat([inp.teams, pd.DataFrame([{"team": "Mystery", "age": "old", "coach": "X"}])])
    with pytest.raises(ValueError, match="Mystery"):
        clean(inp)


def test_excel_roundtrip():
    inp = simulate(6, 5, 3, 2026, 11, 4)
    from scheduler import ScheduleInput
    import io
    back = ScheduleInput.from_excel(io.BytesIO(inp.to_excel_bytes()))
    assert len(back.teams) == 6 and len(back.locals) == 3
    r = solve(back, date(2026, 11, 1), date(2026, 11, 30), time_limit_s=10)
    assert r.ok
