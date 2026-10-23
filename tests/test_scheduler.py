"""Checks every hard rule on solved schedules, plus input parsing."""

import io
from collections import Counter
from datetime import date

import pandas as pd
import pytest

from scheduler import ScheduleInput, clean, simulate, solve
from scheduler.closures import default_closures
from scheduler.data import (
    COLUMNS, parse_courts, parse_date_ranges, parse_days, parse_names, parse_slot, parse_times,
    parse_weekend_tokens,
)
from scheduler.solver import week_start

NOV = (date(2026, 11, 1), date(2026, 11, 30))


def _check_hard_rules(inp, r):
    data = clean(inp, r.start, r.end)
    teams = {t.name: t for t in data.teams}
    a = r.assignments
    pr = a[a["kind"] == "Practice"]

    # One booking per court per slot (practices and on-site locals together).
    dup = a.groupby(["date", "slot", "court"]).size()
    assert (dup <= 1).all(), dup[dup > 1]

    for _, row in pr.iterrows():
        t, d, s = teams[row["name"]], row["date"], row["slot"]
        assert d not in data.closures
        assert t.allowed(s), (t.name, s)
        assert not t.start or d >= t.start, (t.name, d)
        if d.weekday() >= 5:
            assert d in data.weekends and s in data.weekends[d].blocks
            if t.weekend_only:
                assert any(w.matches(d, s) for w in t.weekend_only), (t.name, d, s)
        for rule in data.rules:
            if rule.kind == "no" and rule.matches(d, s):
                assert rule.who != t.name and rule.who not in t.heads, (t.name, rule)
        for lp in data.locals:
            if lp.active(d, s):
                assert not set(lp.coaches) & set(t.heads), (t.name, lp.name, d, s)

    # Head coaches never in two places at once.
    seen = Counter()
    for _, row in pr.iterrows():
        for h in teams[row["name"]].heads:
            seen[(h, row["date"], row["slot"])] += 1
    assert not seen or max(seen.values()) == 1, [k for k, v in seen.items() if v > 1]

    # At most one practice per team per day, two per week.
    assert (pr.groupby(["name", "date"]).size() <= 1).all()
    assert (pr.groupby(["name", pr["date"].map(week_start)]).size() <= 2).all()

    # Never one team alone in a weekend block.
    we = pr[pr["date"].map(lambda d: d.weekday() >= 5)]
    if len(we):
        assert (we.groupby(["date", "slot"]).size() >= 2).all()

    # The weekly template is a valid week too.
    pt = r.pattern.assign(h=r.pattern["head"].str.split("; ")).explode("h")
    assert (pt.groupby(["h", "day", "slot"]).size() <= 1).all()


@pytest.mark.parametrize("girls,boys,seed", [(6, 4, 7), (8, 4, 1), (14, 8, 3), (21, 9, 11)])
def test_hard_rules_hold(girls, boys, seed):
    inp = simulate(girls, boys, 4, 2026, 11, seed)
    r = solve(inp, *NOV, time_limit_s=30)
    assert r.ok
    _check_hard_rules(inp, r)


def _team_frame(rows):
    df = pd.DataFrame(rows)
    for c in COLUMNS["Teams"]:
        if c not in df:
            df[c] = ""
    return df[COLUMNS["Teams"]]


def _inp(teams, availability=(), wishlist=(), locals_=(), weekends=(), closures=()):
    def frame(sheet, rows):
        df = pd.DataFrame(list(rows))
        for c in COLUMNS[sheet]:
            if c not in df:
                df[c] = ""
        return df[COLUMNS[sheet]]
    return ScheduleInput(_team_frame(teams), frame("Availability", availability), frame("Wishlist", wishlist),
                         frame("Locals", locals_), frame("Weekends", weekends), frame("Closures", closures))


def test_age_rules_are_hard():
    teams = [{"team": f"{a}-Coach{a}", "age": a} for a in (12, 13, 14, 15, 16, 17, 18)]
    r = solve(_inp(teams), *NOV, time_limit_s=10)
    pr = r.assignments
    assert not ((pr["age"] > 14) & (pr["slot"] == "3:45")).any()
    assert not ((pr["age"] <= 14) & (pr["slot"] == "7:20")).any()


def test_late_ok_exception_allows_7_20_but_avoids_it():
    teams = [{"team": "14 Black", "age": 14, "head_coach": "Sam", "late_ok": "Yes"},
             {"team": "14 Gold", "age": 14, "head_coach": "Lee"}]
    data = clean(_inp(teams), *NOV)
    assert data.teams[0].allowed("7:20") and not data.teams[1].allowed("7:20")
    # With 5:30 free, the exception isn't used...
    r = solve(_inp(teams), *NOV, time_limit_s=10)
    assert not (r.assignments["slot"] == "7:20").any()
    # ...but when 3:45 and 5:30 are blocked, 14 Black can go late and 14 Gold can't.
    avail = [{"who": t, "type": "No", "times": "3:45, 5:30"} for t in ("14 Black", "14 Gold")]
    r = solve(_inp(teams, avail), *NOV, time_limit_s=10)
    pr = r.assignments
    assert (pr[pr["name"] == "14 Black"]["slot"] == "7:20").all() and len(pr[pr["name"] == "14 Black"])
    assert pr[pr["name"] == "14 Gold"].empty


def test_assistant_can_overlap_but_head_cannot():
    # Pat heads A and B (never together) and assists C.
    teams = [{"team": "17 Black", "age": 17, "head_coach": "Pat"},
             {"team": "16 Black", "age": 16, "head_coach": "Pat"},
             {"team": "18-Jordan", "age": 18, "assistants": "Pat"}]
    inp = _inp(teams)
    r = solve(inp, *NOV, time_limit_s=10)
    _check_hard_rules(inp, r)
    assert clean(inp, *NOV).teams[2].heads == ["Jordan"]  # head taken from the team name


def test_locals_block_courts_and_coaches_off_site_only_coaches():
    teams = [{"team": "15-Sage", "age": 15}, {"team": "16-Ann", "age": 16}]
    locals_ = [{"program": "Clinic", "days": "Tue, Thu", "time": "5:30", "courts": "1-7", "coaches": "", "on_site": "Yes"},
               {"program": "Offsite HS", "days": "Wed", "time": "7:20", "courts": "", "coaches": "Sage", "on_site": "No"}]
    inp = _inp(teams, locals_=locals_)
    r = solve(inp, *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert pr[pr["day"].isin(["Tue", "Thu"]) & (pr["slot"] == "5:30")].empty
    sage = pr[pr["name"] == "15-Sage"]
    assert sage[(sage["day"] == "Wed") & (sage["slot"] == "7:20")].empty
    assert "Offsite HS" not in set(r.assignments["name"])  # off-site: no courts used
    _check_hard_rules(inp, r)


def test_recurring_and_ranged_no_dates():
    teams = [{"team": "14-Ari", "age": 14}]
    avail = [{"who": "14-Ari", "type": "No", "days": "Mon"},
             {"who": "14-Ari", "type": "No", "dates": "11/10-11/13"},
             {"who": "14-Ari", "type": "No", "days": "Wed", "times": "5:30"}]
    r = solve(_inp(teams, avail), *NOV, time_limit_s=10)
    pr = r.assignments
    assert not (pr["day"] == "Mon").any()
    assert not pr["date"].between(date(2026, 11, 10), date(2026, 11, 13)).any()
    assert pr[(pr["day"] == "Wed") & (pr["slot"] == "5:30")].empty


def test_start_dates_and_required_weekend():
    teams = [{"team": f"16-T{i}", "age": 16, "start_date": "11/28/2026"} for i in range(4)] + \
            [{"team": f"13-U{i}", "age": 13} for i in range(4)]
    weekends = [{"date": "11/28/2026", "open": "9AM-9PM", "group": "T", "required": "Yes"},
                {"date": "11/29/2026", "open": "9AM-9PM", "group": "T", "required": "Yes"}]
    closures = [{"date": d.strftime("%m/%d/%Y"), "reason": n} for d, n in default_closures(*NOV)]
    inp = _inp(teams, weekends=weekends, closures=closures)
    r = solve(inp, *NOV, time_limit_s=20)
    _check_hard_rules(inp, r)
    pr = r.assignments
    late = pr[pr["name"].str.startswith("16-")]
    assert late["date"].min() >= date(2026, 11, 28)
    assert pr["date"].isin([date(2026, 11, 28), date(2026, 11, 29)]).any()
    assert not (r.issues["issue"].str.startswith("Required weekend")).any()


def test_weekend_only_respected():
    teams = [{"team": "14-Kris", "age": 14, "weekend_only": "11/22 AM"},
             {"team": "14-Kim", "age": 14}, {"team": "13-Lee", "age": 13}]
    avail = [{"who": t, "type": "No", "dates": "11/16-11/20"} for t in ("14-Kris", "14-Kim", "13-Lee")]
    weekends = [{"date": "11/21/2026"}, {"date": "11/22/2026"}]
    inp = _inp(teams, avail, weekends=weekends)
    r = solve(inp, *NOV, time_limit_s=10)
    _check_hard_rules(inp, r)
    noah = r.assignments[(r.assignments["name"] == "14-Kris") & r.assignments["date"].map(lambda d: d.weekday() >= 5)]
    assert set(noah["date"]) <= {date(2026, 11, 22)} and set(noah["slot"]) <= {"9AM", "11AM"}


def test_double_up_rewarded():
    teams = [{"team": "15-Robin", "age": 15, "head_coach": "Robin"},
             {"team": "12-Robin", "age": 12, "head_coach": "Robin"}]
    wish = [{"team": "15-Robin", "choice": 1, "days": "Tue, Thu", "times": "7:20"},
            {"team": "12-Robin", "choice": 1, "days": "Tue, Thu", "times": "5:30"}]
    r = solve(_inp(teams, wishlist=wish), *NOV, time_limit_s=10)
    assert r.metrics["double_ups"] >= 6
    assert r.metrics["wishlist_pct"] == 100.0


def test_parsing():
    assert parse_days("Tue, Thu") == ["Tue", "Thu"]
    assert parse_days("Tuesdays and Thursdays") == ["Tue", "Thu"]
    assert parse_days("Weekdays") == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert parse_times("345pm") == {"3:45"}
    assert parse_times("5:30/7:20") == {"5:30", "7:20"}
    assert parse_times("AM") == {"9AM", "11AM"}
    assert parse_times("3PM") == {"3PM"}
    assert parse_times("All") is None
    assert parse_slot("5-7pm") == "5PM"
    with pytest.raises(ValueError):
        parse_times("6:00")
    assert parse_courts("1-3") == [1, 2, 3]
    assert parse_courts("TBD") == []
    assert parse_names("Jordan & Casey") == ["Jordan", "Casey"]
    assert parse_names("?") == []
    assert parse_date_ranges("11/2; 11/10-16; Nov 15-16", 2026) == [
        (date(2026, 11, 2),) * 2, (date(2026, 11, 10), date(2026, 11, 16)),
        (date(2026, 11, 15), date(2026, 11, 16))]
    toks = parse_weekend_tokens("11/1 AM; 11/8 3PM; Sat AM; Sun PM", 2026)
    assert toks[1].matches(date(2026, 11, 8), "3PM") and not toks[1].matches(date(2026, 11, 8), "9AM")
    assert toks[2].matches(date(2026, 11, 14), "11AM") and not toks[2].matches(date(2026, 11, 15), "11AM")


def test_closures_default_to_major_holidays_only():
    names = dict(default_closures(date(2026, 9, 1), date(2026, 12, 31)))
    assert date(2026, 11, 26) in names and date(2026, 12, 25) in names
    assert date(2026, 9, 7) not in names  # Labor Day: club practices


def test_bad_input_gives_readable_error():
    inp = _inp([{"team": "Mystery", "age": "old"}])
    with pytest.raises(ValueError, match="Mystery"):
        clean(inp, *NOV)


def test_excel_roundtrip():
    inp = simulate(5, 3, 3, 2026, 11, 4)
    back = ScheduleInput.from_excel(io.BytesIO(inp.to_excel_bytes()))
    assert len(back.teams) == 8 and len(back.locals) == 3
    r = solve(back, *NOV, time_limit_s=10)
    assert r.ok
    _check_hard_rules(back, r)


def test_program_with_dates_and_span_blocks_only_its_window():
    teams = [{"team": f"16-C{i}", "age": 16} for i in range(3)]
    prog = [{"program": "Fall Clinic", "days": "Mon, Wed", "time": "5:30-9:10", "courts": "1-7",
             "on_site": "Yes", "dates": "11/09/2026-11/20/2026"}]
    inp = _inp(teams, locals_=prog)
    r = solve(inp, *NOV, time_limit_s=10)
    _check_hard_rules(inp, r)
    a = r.assignments
    loc = a[a["kind"] == "Local"]
    assert set(loc["slot"]) == {"5:30", "7:20"} and set(loc["court"]) == set(range(1, 8))
    assert loc["date"].min() == date(2026, 11, 9) and loc["date"].max() == date(2026, 11, 18)
    pr = a[a["kind"] == "Practice"]
    inside = pr[pr["date"].between(date(2026, 11, 9), date(2026, 11, 20)) & pr["day"].isin(["Mon", "Wed"])]
    assert inside.empty


def test_overlapping_programs_on_same_court_rejected():
    teams = [{"team": "16-A", "age": 16}]
    progs = [{"program": "A", "days": "Tue", "time": "5:30", "courts": "1-3", "dates": "11/1/2026-11/15/2026"},
             {"program": "B", "days": "Tue", "time": "5:30", "courts": "3-4", "dates": "11/10/2026-11/30/2026"},
             {"program": "C", "days": "Tue", "time": "5:30", "courts": "1-4", "dates": "12/1/2026-12/15/2026"}]
    with pytest.raises(ValueError, match="'A' and 'B'"):
        clean(_inp(teams, locals_=progs[:2]), *NOV)
    clean(_inp(teams, locals_=[progs[0], progs[2]]), *NOV)  # different dates: fine


def test_weekend_program_shows_even_if_weekend_not_open():
    teams = [{"team": "16-A", "age": 16}]
    prog = [{"program": "Girls Local", "days": "Sun", "time": "9AM-1PM", "courts": "1-4",
             "dates": "11/8/2026-11/8/2026"}]
    r = solve(_inp(teams, locals_=prog), *NOV, time_limit_s=10)
    loc = r.assignments[r.assignments["kind"] == "Local"]
    assert set(loc["date"]) == {date(2026, 11, 8)} and set(loc["slot"]) == {"9AM", "11AM"}
    assert date(2026, 11, 8) in r.weekend_dates
