"""Checks every hard rule on solved schedules, plus input parsing."""

import io
import itertools
from collections import Counter
from datetime import date, timedelta

import pandas as pd
import pytest

from scheduler import ScheduleInput, clean, simulate, solve
from scheduler.closures import default_closures
from scheduler.config import slots_overlap
from scheduler.data import (
    COLUMNS,
    parse_courts,
    parse_date_ranges,
    parse_days,
    parse_names,
    parse_slot,
    parse_times,
    parse_weekend_tokens,
    tournament_conflicts,
)
from scheduler.solver import week_start


@pytest.fixture()
def alone_ok(monkeypatch):
    """For tests of other rules in tiny setups (a team or two): lets a team practice alone on a
    weekday, so the never-alone rule (Nov 2026) doesn't empty the schedule they're checking."""
    # The solver's own copy of the config (the app tests reload the package mid-run).
    monkeypatch.setattr(solve.__globals__["_cfg"], "NEVER_ALONE_WEEKDAYS", False)

NOV = (date(2026, 11, 1), date(2026, 11, 30))


def _check_hard_rules(inp, r):
    data = clean(inp, r.start, r.end)
    teams = {t.name: t for t in data.teams}
    a = r.assignments
    pr = a[a["kind"] == "Practice"]

    # One booking per court per slot (practices and on-site locals together),
    # except pairing: two flagged practices on one court (3 teams on 2 courts).
    dup = a.groupby(["date", "slot", "court"]).size()
    for (d, s, c), n in dup[dup > 1].items():
        here = a[(a["date"] == d) & (a["slot"] == s) & (a["court"] == c)]
        assert n == 2 and (here["kind"] == "Practice").all(), here
        assert here["paired"].fillna("").str.startswith("3 teams on").all(), here
    paired = pr[pr["paired"].fillna("") != ""] if "paired" in pr else pr.iloc[0:0]
    for _, g in paired.groupby(["date", "slot"]):
        assert len(g) % 3 == 0 and g["court"].nunique() == 2 * len(g) // 3, g

    for _, row in pr.iterrows():
        t, d, s = teams[row["name"]], row["date"], row["slot"]
        assert d not in data.closures
        assert t.allowed(s), (t.name, s)
        assert not t.start or d >= t.start, (t.name, d)
        if d.weekday() >= 5:
            assert d in data.weekends and s in data.weekends[d].blocks
            if t.weekend_only:
                assert any(w.matches(d, s) for w in t.weekend_only), (t.name, d, s)
        broken = {(b["team"], b["date"], b["slot"]) for b in r.broken}
        for rule in data.rules:
            if rule.kind == "no" and rule.matches(d, s):
                if rule.who == t.name or rule.who in t.heads:
                    # Only allowed as a flagged last resort, never over a hand removal.
                    assert not rule.manual, (t.name, rule)
                    assert (t.name, d, s) in broken, (t.name, rule)
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
    # These tests were written for weekends that are closed unless listed, and only listed closures.
    return ScheduleInput(_team_frame(teams), frame("Availability", availability), frame("Wishlist", wishlist),
                         frame("Locals", locals_), frame("Weekends", weekends), frame("Closures", closures),
                         weekends_open=False, holidays_closed=False, holidays_open=False)


def test_age_rules_are_hard():
    teams = [{"team": f"{a}-Coach{a}", "age": a} for a in (12, 13, 14, 15, 16, 17, 18)]
    r = solve(_inp(teams), *NOV, time_limit_s=10)
    pr = r.assignments
    assert not ((pr["age"] > 14) & (pr["slot"] == "3:45")).any()
    assert not ((pr["age"] <= 14) & (pr["slot"] == "7:20")).any()


@pytest.mark.usefixtures("alone_ok")
def test_late_ok_exception_allows_7_20_but_avoids_it():
    teams = [{"team": "14 Black", "age": 14, "head_coach": "Sam", "late_ok": "Yes"},
             {"team": "14 Gold", "age": 14, "head_coach": "Lee"}]
    data = clean(_inp(teams), *NOV)
    assert data.teams[0].allowed("7:20") and not data.teams[1].allowed("7:20")
    # With 5:30 free, the exception isn't used...
    r = solve(_inp(teams), *NOV, time_limit_s=10)
    assert not (r.assignments["slot"] == "7:20").any()
    # ...but when 3:45 and 5:30 are blocked, 14 Black goes late. 14 Gold can't
    # go late, so its no-dates are broken (and flagged) to keep 2 a week.
    avail = [{"who": t, "type": "No", "times": "3:45, 5:30"} for t in ("14 Black", "14 Gold")]
    r = solve(_inp(teams, avail), *NOV, time_limit_s=10)
    pr = r.assignments
    assert (pr[pr["name"] == "14 Black"]["slot"] == "7:20").all() and len(pr[pr["name"] == "14 Black"])
    gold = pr[pr["name"] == "14 Gold"]
    assert len(gold) and not (gold["slot"] == "7:20").any()
    assert {b["team"] for b in r.broken} == {"14 Gold"}
    assert r.shortfalls.empty


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
    locals_ = [{"program": "Clinic", "days": "Tue, Thu", "time": "5:30", "courts": "1-7", "coaches": "",
                "on_site": "Yes"},
               {"program": "Offsite HS", "days": "Wed", "time": "7:20", "courts": "", "coaches": "Sage",
                "on_site": "No"}]
    inp = _inp(teams, locals_=locals_)
    r = solve(inp, *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert pr[pr["day"].isin(["Tue", "Thu"]) & (pr["slot"] == "5:30")].empty
    sage = pr[pr["name"] == "15-Sage"]
    assert sage[(sage["day"] == "Wed") & (sage["slot"] == "7:20")].empty
    assert "Offsite HS" not in set(r.assignments["name"])  # off-site: no courts used
    _check_hard_rules(inp, r)


@pytest.mark.usefixtures("alone_ok")
def test_recurring_and_ranged_no_dates():
    teams = [{"team": "14-Ari", "age": 14}]
    avail = [{"who": "14-Ari", "type": "No", "days": "Mon"},
             {"who": "14-Ari", "type": "No", "dates": "11/10-11/13"},
             {"who": "14-Ari", "type": "No", "days": "Wed", "times": "5:30"}]
    r = solve(_inp(teams, avail), *NOV, time_limit_s=10)
    pr = r.assignments
    # The week of 11/8 is blocked Mon-Fri, so two no-dates there are broken
    # (and flagged) to keep 2 practices. Everywhere else they hold.
    broken = {(b["date"], b["slot"]) for b in r.broken}
    assert len(broken) == 2 and all(date(2026, 11, 9) <= d <= date(2026, 11, 13) for d, _ in broken)
    rest = pr[~pr.apply(lambda x: (x["date"], x["slot"]) in broken, axis=1)]
    assert not (rest["day"] == "Mon").any()
    assert not rest["date"].between(date(2026, 11, 10), date(2026, 11, 13)).any()
    assert rest[(rest["day"] == "Wed") & (rest["slot"] == "5:30")].empty
    assert r.shortfalls.empty
    assert r.issues["issue"].str.startswith("No-date broken").sum() == 2


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


@pytest.mark.usefixtures("alone_ok")
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


def _with_tournaments(inp, rows):
    df = pd.DataFrame(rows)
    for c in COLUMNS["Tournaments"]:
        if c not in df:
            df[c] = ""
    inp.tournaments = df[COLUMNS["Tournaments"]]
    return inp


def test_local_tournament_blocks_its_courts_until_7pm():
    teams = [{"team": f"16-T{i}", "age": 16} for i in range(4)]
    inp = _with_tournaments(_inp(teams), [
        {"name": "Boys 14 Local", "type": "Local", "start_date": "11/18/2026", "end_date": "11/18/2026"},
        {"name": "Girls Mini", "type": "Local", "start_date": "11/11/2026", "courts": "1-5"}])
    r = solve(inp, *NOV, time_limit_s=15)
    _check_hard_rules(inp, r)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    # Nothing before 7 PM; 7:20 practices still happen.
    assert set(pr[pr["date"] == date(2026, 11, 18)]["slot"]) <= {"7:20"}
    early = pr[(pr["date"] == date(2026, 11, 11)) & (pr["slot"] != "7:20")]
    assert set(early["court"]) <= {6, 7}
    assert {tn.name for tn in r.tournaments} == {"Boys 14 Local", "Girls Mini"}


def test_travel_tournament_keeps_teams_home_and_rested():
    teams = [{"team": "15 Black", "age": 15, "head_coach": "Al"}, {"team": "15 Green", "age": 15, "head_coach": "Bo"},
             {"team": "16 Black", "age": 16, "head_coach": "Cy"}, {"team": "15-Girls", "age": 15}]
    for t in teams:
        t["club"] = "Girls" if "-" in t["team"] else "Boys"
    weekends = [{"date": "11/14/2026"}, {"date": "11/15/2026"}]
    inp = _with_tournaments(_inp(teams, weekends=weekends), [
        {"name": "15/16 Boys Travel", "type": "Travel", "start_date": "11/13/2026", "end_date": "11/15/2026",
         "club": "Boys", "ages": "15-16", "teams": "15 Black, 16 Black"}])
    r = solve(inp, *NOV, time_limit_s=15)
    _check_hard_rules(inp, r)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    going = pr[pr["name"].isin(["15 Black", "16 Black"])]
    assert going[going["date"].between(date(2026, 11, 13), date(2026, 11, 15))].empty
    assert going[going["date"].isin([date(2026, 11, 12), date(2026, 11, 16)])].empty   # rest days
    # ...and they still get 2 practices the week leading in (Mon-Wed).
    lead = going[going["date"].between(date(2026, 11, 9), date(2026, 11, 11))]
    assert (lead.groupby("name").size() == 2).all() and len(lead) == 4


def test_court_quality_and_team_levels():
    data = clean(_inp([{"team": "16 Black", "age": 16, "head_coach": "A"},
                       {"team": "16 Green", "age": 16, "head_coach": "B"},
                       {"team": "16-Ann", "age": 16, "rank": "16-1"}]), *NOV)
    assert [t.level for t in data.teams] == [1, 2, 1]
    # Six teams wanting the same slot: Court 2 (worst) is never used, and the
    # older 1s team gets Court 1.
    teams = [{"team": "18 Black", "age": 18, "head_coach": "A"}] + \
            [{"team": f"15-G{i}", "age": 15} for i in range(5)]
    wish = [{"team": t["team"], "choice": 1, "days": "Tue, Thu", "times": "5:30"} for t in teams]
    r = solve(_inp(teams, wishlist=wish), *NOV, time_limit_s=15)
    pr = r.assignments
    assert 2 not in set(pr["court"])
    assert (pr[pr["name"] == "18 Black"]["court"] == 1).all()


# ------------------------------------------------------------- storage --

def _store_roundtrip(store):
    from scheduler.storage import make_payload, new_id, read_payload
    inp = simulate(5, 3, 3, seed=2, start=date(2026, 11, 3), end=date(2026, 12, 4))
    inp = _with_tournaments(inp, [{"name": "Boys Local", "type": "Local", "start_date": "11/18/2026"}])
    r = solve(inp, date(2026, 11, 3), date(2026, 12, 4), time_limit_s=10)
    pid = new_id()
    store.save(pid, "Nov 3 – Dec 4", make_payload(inp, date(2026, 11, 3), date(2026, 12, 4),
                                                  {"time_limit": 20, "makeup": False}, r))
    assert pid in [p["id"] for p in store.list()]
    rec = store.get(pid)
    inp2, s, e, settings, r2 = read_payload(rec["payload"])
    assert (s, e) == (date(2026, 11, 3), date(2026, 12, 4)) and settings == {"time_limit": 20, "makeup": False}
    assert len(inp2.teams) == len(inp.teams) and len(inp2.tournaments) == 1
    assert len(r2.assignments) == len(r.assignments)
    assert r2.metrics["practices_scheduled"] == r.metrics["practices_scheduled"]
    assert isinstance(r2.assignments["date"].iloc[0], date) and [t.name for t in r2.tournaments] == ["Boys Local"]
    # The reloaded inputs solve again just like the originals.
    r3 = solve(inp2, s, e, time_limit_s=10)
    assert r3.ok
    store.rename(pid, "Renamed")
    assert store.get(pid)["name"] == "Renamed"
    store.delete(pid)
    assert store.get(pid) is None


def test_storage_roundtrip_sqlite(tmp_path):
    from scheduler.storage import Store
    _store_roundtrip(Store(f"sqlite:///{tmp_path / 'test.db'}"))


@pytest.mark.skipif(not __import__("os").environ.get("TEST_DATABASE_URL"), reason="no Postgres configured")
def test_storage_roundtrip_postgres():
    import os

    from scheduler.storage import Store
    _store_roundtrip(Store(os.environ["TEST_DATABASE_URL"]))


@pytest.mark.usefixtures("alone_ok")
def test_date_range_partial_weeks_and_new_year():
    inp = simulate(4, 2, 2, seed=5, start=date(2026, 12, 15), end=date(2027, 1, 15))
    inp.availability = pd.concat([inp.availability, pd.DataFrame([{
        "who": inp.teams.iloc[0]["team"], "type": "No", "dates": "1/5-1/6", "days": "", "times": "", "note": ""}])],
        ignore_index=True)
    data = clean(inp, date(2026, 12, 15), date(2027, 1, 15))
    assert any(r.ranges == [(date(2027, 1, 5), date(2027, 1, 6))] for r in data.rules)  # 1/5 means next year
    r = solve(inp, date(2026, 12, 15), date(2027, 1, 15), time_limit_s=20)
    assert r.ok
    _check_hard_rules(inp, r)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert pr["date"].min() >= date(2026, 12, 15) and pr["date"].max() <= date(2027, 1, 15)


def test_court_2_used_before_missing_a_practice():
    # Seven teams that can only practice Tue/Thu 5:30: all seven courts,
    # including Court 2, get used rather than anyone missing practice.
    teams = [{"team": f"16-T{i}", "age": 16} for i in range(7)]
    avail = [{"who": t["team"], "type": "No", "days": "Mon, Wed, Fri"} for t in teams] + \
            [{"who": t["team"], "type": "No", "times": "7:20"} for t in teams]
    r = solve(_inp(teams, avail), *NOV, time_limit_s=15)
    assert r.metrics["missed_practices"] == 0
    assert 2 in set(r.assignments["court"])
    assert r.shortfalls.empty


@pytest.mark.usefixtures("alone_ok")
def test_practice_counts_flag_short_weeks():
    teams = [{"team": "15-A", "age": 15}, {"team": "15-B", "age": 15}]
    # 15-A is away at a travel tournament that whole week, so it can't practice.
    inp = _with_tournaments(_inp(teams), [{"name": "Away", "type": "Travel", "start_date": "11/9/2026",
                                           "end_date": "11/13/2026", "teams": "15-A"}])
    r = solve(inp, *NOV, time_limit_s=10)
    sf = r.shortfalls
    assert list(sf["team"]) == ["15-A"] and sf["week_of"].iloc[0] == date(2026, 11, 8) and sf["short"].iloc[0] == 2
    pc = r.practice_counts
    b = pc[pc["team"] == "15-B"]
    assert (b[b["target"] > 0]["short"] == 0).all()
    assert int(pc["short"].sum()) == r.metrics["missed_practices"]


def test_weeks_run_sunday_to_saturday():
    assert week_start(date(2026, 11, 8)) == date(2026, 11, 8)      # Sunday starts its own week
    assert week_start(date(2026, 11, 14)) == date(2026, 11, 8)     # Saturday ends it
    assert week_start(date(2026, 11, 16)) == date(2026, 11, 15)
    # A Sunday practice counts toward the week that follows it.
    teams = [{"team": "15-A", "age": 15}, {"team": "15-B", "age": 15}]
    avail = [{"who": t, "type": "No", "dates": "11/9-11/11"} for t in ("15-A", "15-B")]
    inp = _inp(teams, avail, weekends=[{"date": "11/8/2026"}])
    r = solve(inp, *NOV, time_limit_s=10)
    _check_hard_rules(inp, r)
    pc = r.practice_counts
    assert (pc[pc["week_of"] == date(2026, 11, 8)]["scheduled"] == 2).all()


def test_jumpstart_blocks_overlapping_3_45_and_side_colors():
    teams = [{"team": f"13-J{i}", "age": 13} for i in range(3)]
    avail = [{"who": t["team"], "type": "No", "times": "5:30"} for t in teams]  # force 3:45
    prog = [{"program": "JumpStart", "side": "", "days": "Mon, Wed", "time": "3:30-5:30", "courts": "1-7"},
            {"program": "G Skills", "days": "Fri", "time": "7:20", "courts": "6"}]
    inp = _inp(teams, avail, locals_=prog)
    data = clean(inp, *NOV)
    assert {lp.slot for lp in data.locals if lp.name == "JumpStart"} == {"3:30", "4:30"}
    assert {lp.side for lp in data.locals if lp.name == "G Skills"} == {"Girls"}   # guessed from "G ..."
    r = solve(inp, *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert pr[pr["day"].isin(["Mon", "Wed"])].empty   # 3:45 blocked by JumpStart on all courts
    js = r.assignments[r.assignments["name"] == "JumpStart"]
    assert set(js["slot"]) == {"3:30", "4:30"}
    # JumpStart and a 3:45 program on the same court conflict.
    clash = prog + [{"program": "Clinic", "days": "Mon", "time": "3:45", "courts": "2"}]
    with pytest.raises(ValueError, match="JumpStart"):
        clean(_inp(teams, avail, locals_=clash), *NOV)


def test_coach_roster_adds_coaches_without_teams():
    teams = [{"team": "15-A", "age": 15}]
    inp = _inp(teams)
    inp.coaches = pd.DataFrame([{"coach": "Dave", "email": "", "phone": "", "note": ""},
                                {"coach": "Cliff", "email": "c@x.com", "phone": "", "note": "JumpStart"}])
    data = clean(inp, *NOV)
    assert {"Dave", "Cliff", "A"} <= set(data.coaches)
    assert data.roster["Cliff"]["email"] == "c@x.com"


# ------------------------------------------------------- manual edits --

def _with_overrides(inp, rows):
    df = pd.DataFrame(rows)
    for c in COLUMNS["Overrides"]:
        if c not in df:
            df[c] = ""
    inp.overrides = df[COLUMNS["Overrides"]]
    return inp


def test_manual_add_remove_cancel_and_usual_days():
    teams = [{"team": "15-A", "age": 15}, {"team": "15-B", "age": 15}, {"team": "16-C", "age": 16}]
    inp = _inp(teams, [{"who": "15-A", "type": "No", "days": "Mon"}])
    inp = _with_overrides(inp, [
        # Add: on a Monday, which 15-A normally can't do, on Court 3.
        {"team": "15-A", "action": "Add", "date": "11/9/2026", "time": "5:30", "court": "3"},
        # Usual days: 16-C always Tue/Thu 7:20, home Court 1.
        {"team": "16-C", "action": "Usual", "days": "Tue, Thu", "time": "7:20", "court": "1"},
        # Remove 16-C's Tue 11/17 practice (find a makeup) and cancel Thu 11/19 (no makeup).
        {"team": "16-C", "action": "Remove", "date": "11/17/2026"},
        {"team": "16-C", "action": "Cancel", "date": "11/19/2026"},
    ])
    r = solve(inp, *NOV, time_limit_s=15)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    a = pr[(pr["name"] == "15-A") & (pr["date"] == date(2026, 11, 9))]
    assert list(a["slot"]) == ["5:30"] and list(a["court"]) == [3] and a["pinned"].all()
    pat = r.pattern[r.pattern["team"] == "16-C"]
    assert set(zip(pat["day"], pat["slot"])) == {("Tue", "7:20"), ("Thu", "7:20")} and set(pat["court"]) == {1}
    c = pr[pr["name"] == "16-C"]
    wk = c[c["date"].between(date(2026, 11, 15), date(2026, 11, 21))]
    assert date(2026, 11, 17) not in set(wk["date"]) and date(2026, 11, 19) not in set(wk["date"])
    assert len(wk) == 1                                  # one makeup; the cancelled one isn't made up
    assert r.shortfalls[r.shortfalls["team"] == "16-C"].empty  # cancelled practice isn't flagged as missing


def test_conflicting_manual_changes_explained():
    teams = [{"team": "16 Black", "age": 16, "head_coach": "Ben"}, {"team": "17-Ben", "age": 17}]
    inp = _with_overrides(_inp(teams), [
        {"team": "16 Black", "action": "Add", "date": "11/10/2026", "time": "5:30"},
        {"team": "17-Ben", "action": "Add", "date": "11/10/2026", "time": "5:30"}])
    with pytest.raises(ValueError, match="Ben would be coaching two teams at once"):
        solve(inp, *NOV, time_limit_s=10)


def test_weekdays_beat_weekends_and_days_apart():
    teams = [{"team": f"16-W{i}", "age": 16} for i in range(4)]
    weekends = [{"date": d} for d in ("11/7/2026", "11/8/2026", "11/14/2026", "11/15/2026")]
    r = solve(_inp(teams, weekends=weekends), *NOV, time_limit_s=15)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert not pr["date"].map(lambda d: d.weekday() >= 5).any()      # weekdays were free: no weekends
    assert r.metrics["back_to_back"] == 0 if "back_to_back" in r.metrics else True
    for name, g in pr.groupby("name"):
        ds = sorted(g["date"])
        assert all((b - a).days >= 2 for a, b in zip(ds, ds[1:])), name   # a day in between


def test_midweek_start_still_gets_two_practices():
    """A team starting on a Monday still gets its full two practices that week."""
    inp = ScheduleInput.from_excel("data/sample_input.xlsx")
    row = inp.teams.iloc[[7]].copy()
    row["team"], row["club"], row["head_coach"], row["assistants"] = "15-Madi", "Girls", "Madi", ""
    row["start_date"] = "11/9/2026"
    inp.teams = pd.concat([inp.teams, row], ignore_index=True)
    r = solve(inp, date(2026, 11, 1), date(2026, 12, 4), time_limit_s=15)
    pc = r.practice_counts.set_index(["team", "week_of"])
    assert pc.loc[("15-Madi", date(2026, 11, 8)), "target"] == 2
    assert pc.loc[("15-Madi", date(2026, 11, 1)), "target"] == 0


def test_views_always_show_weekends():
    from scheduler.views import court_grid_html, month_calendar_html, week_dates
    dd = week_dates(date(2026, 11, 8), date(2026, 11, 1), date(2026, 11, 30), [])
    assert list(dd) == ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    empty = pd.DataFrame(columns=["court", "day", "slot", "club", "kind", "date", "name"])
    grid = court_grid_html(empty, dd, weekend_dates=[])
    assert ">Sun<" in grid and ">Sat<" in grid and "not open for practices" in grid
    cal = month_calendar_html(empty, date(2026, 11, 1), date(2026, 11, 30))
    assert ">Sun<" in cal and ">Sat<" in cal


@pytest.mark.usefixtures("alone_ok")
def test_head_coach_no_dates_never_broken_team_flagged_with_openings():
    """A head coach's no-date is never broken, even with an assistant free (Nov 2026): the team
    comes up short, and the open times it could add one (with the no-date noted) are listed."""
    teams = [{"team": "15-Kit", "age": 15, "head_coach": "Kit", "assistants": "Ash"}]
    avail = [{"who": "Kit", "type": "No", "dates": "11/9-11/12"},    # head out Mon-Thu
             {"who": "Ash", "type": "No", "dates": "11/9-11/10"},    # assistant out Mon-Tue
             {"who": "15-Kit", "type": "No", "dates": "11/13"}]      # team out Fri
    r = solve(_inp(teams, avail), *NOV, time_limit_s=10)
    wk = r.assignments[r.assignments["date"].between(date(2026, 11, 9), date(2026, 11, 13))]
    assert set(wk["day"]) <= {"Fri"}  # only the team's own no-date can still give way
    assert all(not b["head_out"] for b in r.broken)
    short = r.shortfalls
    assert ((short["team"] == "15-Kit") & (short["week_of"] == date(2026, 11, 8))).any()
    op = [o for o in r.openings if o["team"] == "15-Kit" and o["week_of"] == date(2026, 11, 8)]
    assert op and op[0]["options"]
    assert all("Kit marked unavailable" in x["notes"] for x in op[0]["options"])


@pytest.mark.usefixtures("alone_ok")
def test_boys_head_coach_no_date_gives_way_rather_than_a_missed_practice():
    """Boys side rollback (Nov 4, 2026): the same week as above for a Seaside team. Only the
    head coach's no-dates block Mon-Thu and the team's own Fri, so the coach's no-date gives
    way (flagged) instead of the team going short. Girls teams keep it unbreakable."""
    teams = [{"team": "15 Black", "club": "Seaside", "age": 15, "head_coach": "Kit", "assistants": "Ash"}]
    avail = [{"who": "Kit", "type": "No", "dates": "11/9-11/12"},
             {"who": "Ash", "type": "No", "dates": "11/9-11/10"},
             {"who": "15 Black", "type": "No", "dates": "11/13"}]
    r = solve(_inp(teams, avail), *NOV, time_limit_s=10)
    wk = r.assignments[(r.assignments["kind"] == "Practice")
                       & r.assignments["date"].between(date(2026, 11, 9), date(2026, 11, 13))]
    assert len(wk) == 2
    assert not ((r.shortfalls["team"] == "15 Black") & (r.shortfalls["week_of"] == date(2026, 11, 8))).any()
    hb = [b for b in r.broken if b["head_out"]]
    assert hb and all(b["head_out"] == ["Kit"] for b in hb)
    # Still a last resort: breaking Kit's no-date wins over the team's own Fri no-date,
    # and with Ash free on Wed/Thu those days are used first.
    assert set(wk["day"]) <= {"Wed", "Thu", "Fri"}


def test_second_weekend_practice_avoided():
    """Weekdays full: a team gets 1 weekend practice a week, not 2."""
    teams = [{"team": f"16-W{i}", "age": 16} for i in range(4)]
    avail = [{"who": t["team"], "type": "No", "days": "Mon, Tue, Wed, Thu"} for t in teams]
    weekends = [{"date": d, "open": "9AM-9PM"} for d in ("11/7", "11/8", "11/14", "11/15", "11/21", "11/22")]
    r = solve(_inp(teams, avail, weekends=weekends), *NOV, time_limit_s=15)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    we = pr[pr["date"].map(lambda d: d.weekday() >= 5)]
    per = we.groupby(["name", we["date"].map(week_start)]).size()
    assert (per <= 1).all(), per[per > 1]


def test_rebuild_keeps_previous_schedule():
    """A small change (one new no-date) moves only what it has to."""
    inp = simulate(8, 4, 4, 2026, 11, 1)
    r1 = solve(inp, *NOV, time_limit_s=20)
    pr1 = r1.assignments[r1.assignments["kind"] == "Practice"]
    victim = pr1.iloc[len(pr1) // 2]
    inp.availability = pd.concat([inp.availability, pd.DataFrame([{
        "who": victim["name"], "type": "No", "dates": f"{victim['date']:%m/%d}", "times": victim["slot"]}])],
        ignore_index=True)
    r2 = solve(inp, *NOV, time_limit_s=20, previous=r1.assignments, previous_pattern=r1.pattern, seed=5)
    key = lambda df: set(zip(df["name"], df["date"], df["slot"]))  # noqa: E731
    pr2 = r2.assignments[r2.assignments["kind"] == "Practice"]
    moved = key(pr1) - key(pr2)
    assert (victim["name"], victim["date"], victim["slot"]) in moved
    assert len(moved) <= 3, moved
    assert r2.metrics["moved_from_previous"] == len(moved)
    same = pr1.merge(pr2, on=["name", "date", "slot"], suffixes=("1", "2"))
    assert (same["court1"] == same["court2"]).mean() > 0.9


def test_publish_and_what_changed(tmp_path):
    from scheduler.changes import add_version, burden, diff, practices_of, snapshot
    from scheduler.storage import Store, make_payload, new_id, read_published
    inp = simulate(6, 4, 4, 2026, 11, 7)
    r1 = solve(inp, *NOV, time_limit_s=20)
    v = add_version([], snapshot(r1, "Nov v1", "first send"))
    store = Store(f"sqlite:///{tmp_path / 'p.db'}")
    pid = new_id()
    store.save(pid, "Nov", make_payload(inp, *NOV, {}, r1, v))
    pub = read_published(store.get(pid)["payload"])
    assert len(pub) == 1 and pub[0]["label"] == "Nov v1"
    old = practices_of(pub[0])
    assert len(old) == (r1.assignments["kind"] == "Practice").sum()
    # Unchanged schedule: nothing to report.
    assert diff(old, r1.assignments, clean(inp, *NOV), *NOV).empty
    # A team's head coach becomes unavailable on one of its practices.
    pr = r1.assignments[r1.assignments["kind"] == "Practice"]
    victim = pr.iloc[5]
    head = victim["head"].split("; ")[0]
    inp.availability = pd.concat([inp.availability, pd.DataFrame([{
        "who": head, "type": "No", "dates": f"{victim['date']:%m/%d}", "note": "dentist"}])], ignore_index=True)
    r2 = solve(inp, *NOV, time_limit_s=20, previous=old)
    ch = diff(old, r2.assignments, clean(inp, *NOV), *NOV)
    row = ch[ch["team"] == victim["name"]]
    assert len(row) and row["change"].iloc[0] in ("Moved", "Removed")
    assert "dentist" in row["why"].iloc[0] and head in row["why"].iloc[0]
    b = burden(r2.assignments)
    assert set(b["team"]) == set(pr["name"]) and (b["practices"] > 0).all()
    # A readable list to print or paste: by week, then team, with the reason.
    from scheduler.changes import change_line, changes_text
    from scheduler.labels import label_map
    from scheduler.prints import changes_print_html
    labs = label_map((t.name, t.club) for t in clean(inp, *NOV).teams)
    txt = changes_text(ch, "Changes since Nov v1", labs)
    assert txt.startswith("Changes since Nov v1") and "Week of" in txt and "dentist" in txt
    assert labs[victim["name"]] in txt
    h = changes_print_html(ch, "Changes since Nov v1", "published Nov 1", labs)
    assert h.count('class="pc-week"') == ch["week_of"].nunique() and "dentist" in h and "Print" in h
    assert changes_text(ch.iloc[0:0], "x").endswith("No changes.\n")
    assert change_line({"change": "Court", "was": "Tue 11/3 5:30–7:20 · Court 3", "now": "Court 1"}) \
        == "Tue 11/3 5:30–7:20: Court 3 → Court 1"
    assert change_line({"change": "Moved", "was": "Tue 11/3 5:30–7:20 · Court 3",
                        "now": "Wed 11/4 5:30–7:20 · Court 1"}).startswith("Moved Tue 11/3")


def test_restore_published_version():
    """Restoring brings a version's practices back exactly, even after the
    schedule was rebuilt differently; what can't happen now is noted."""
    from scheduler.changes import diff, practices_of, snapshot
    inp = simulate(6, 4, 4, 2026, 11, 7)
    r1 = solve(inp, *NOV, time_limit_s=20)
    v1 = snapshot(r1, "Nov v1")
    old = practices_of(v1)
    r3 = solve(inp, *NOV, time_limit_s=20, previous=old, restore=old, seed=3)
    key = ["name", "date", "slot", "court"]
    got = r3.assignments[r3.assignments["kind"] == "Practice"]
    assert set(map(tuple, got[key].values)) == set(map(tuple, old[key].values))
    assert not got["pinned"].any()  # not shown as "set by hand"
    assert diff(old, r3.assignments, clean(inp, *NOV), *NOV).empty
    # The club closes on one of its dates: that practice is dropped with a
    # note and the team still gets its 2 practices that week.
    victim = old.iloc[3]
    inp.closures = pd.DataFrame([{"date": f"{victim['date']:%m/%d/%Y}", "reason": "Flood"}])
    r4 = solve(inp, *NOV, time_limit_s=20, previous=old, restore=old)
    assert any("Not restored" in w and victim["name"] in w and "Flood" in w for w in r4.warnings)
    sf = r4.shortfalls
    assert not ((sf["team"] == victim["name"]) & (sf["week_of"] == week_start(victim["date"]))).any()
    # A new no-date on a restored practice: kept, but flagged.
    inp.closures = pd.DataFrame(columns=["date", "reason"])
    other = old.iloc[7]
    inp.availability = pd.concat([inp.availability, pd.DataFrame([{
        "who": other["name"], "type": "No", "dates": f"{other['date']:%m/%d}"}])], ignore_index=True)
    r5 = solve(inp, *NOV, time_limit_s=20, previous=old, restore=old)
    got5 = r5.assignments[r5.assignments["kind"] == "Practice"]
    assert ((got5["name"] == other["name"]) & (got5["date"] == other["date"])).any()
    assert ((r5.issues["team"] == other["name"]) & r5.issues["issue"].str.startswith("Restored")).any()


def test_open_boxes_mark_free_court_times():
    from scheduler.config import COURTS, SLOTS, WEEKDAY_KEYS, slots_overlap
    from scheduler.views import court_grid_html, open_slot_map, week_dates
    inp = simulate(6, 4, 4, 2026, 11, 7)
    r = solve(inp, *NOV, time_limit_s=15)
    data = clean(inp, *NOV)
    ws = date(2026, 11, 8)
    dd = week_dates(ws, *NOV)
    book = r.assignments[r.assignments["date"].map(week_start) == ws]
    html_ = court_grid_html(book, dd, data.closures, weekend_dates=r.weekend_dates,
                            open_slots=open_slot_map(dd, data))
    # Every free weekday court/time gets exactly one "Open" box.
    expected = 0
    for d in dd.values():
        if d == date.min or d.weekday() >= 5 or d in data.closures:
            continue
        for c in COURTS:
            taken = list(book[(book["date"] == d) & (book["court"] == c)]["slot"])
            expected += sum(1 for s_ in WEEKDAY_KEYS if not any(slots_overlap(s_, x) for x in taken))
    shown = sum(html_.count(f'is free {SLOTS[s_]}"') for s_ in WEEKDAY_KEYS)
    assert shown == expected > 0
    assert 'class="cs-open"' not in court_grid_html(book, dd, data.closures)  # off unless asked


def test_practice_court_is_programs_only():
    from scheduler.config import PRACTICE_COURT
    from scheduler.views import court_grid_html, week_dates
    teams = [{"team": f"16-T{i}", "age": 16} for i in range(8)]
    locals_ = [{"program": "Girls Local", "days": "Tue", "time": "5:30", "courts": "P, 1", "coaches": "",
                "on_site": "Yes"},
               {"program": "Clinic", "days": "Thu", "time": "5:30", "courts": "Practice court", "coaches": "",
                "on_site": "Yes"}]
    assert parse_courts("1-2, P") == [1, 2, PRACTICE_COURT]
    inp = _inp(teams, locals_=locals_)
    r = solve(inp, *NOV, time_limit_s=15)
    a = r.assignments
    assert not ((a["kind"] == "Practice") & (a["court"] == PRACTICE_COURT)).any()
    loc = a[(a["kind"] == "Local") & (a["court"] == PRACTICE_COURT)]
    assert set(loc["name"]) == {"Girls Local", "Clinic"}
    # The Clinic sits only on the practice court, so it costs teams no court at all.
    thu = a[(a["kind"] == "Practice") & (a["day"] == "Thu") & (a["slot"] == "5:30")]
    assert (thu.groupby("date").size() <= 7).all() and len(thu)
    dd = week_dates(date(2026, 11, 8), *NOV)
    assert "Practice court" in court_grid_html(a[a["date"].map(week_start) == date(2026, 11, 8)], dd)
    # Two programs can't share the practice court at the same time.
    clash = locals_ + [{"program": "Privates", "days": "Tue", "time": "5:30", "courts": "P", "coaches": "",
                        "on_site": "Yes"}]
    with pytest.raises(ValueError):
        clean(_inp(teams, locals_=clash), *NOV)
    # Team practices can't be placed there by hand.
    inp.overrides = pd.DataFrame([{"team": "16-T0", "action": "Add", "date": "11/10", "days": "", "time": "5:30",
                                   "court": "P", "note": ""}])
    with pytest.raises(ValueError):
        clean(inp, *NOV)


def test_calendar_shows_local_tournament_on_closed_weekend():
    from scheduler.views import month_calendar_html
    teams = [{"team": "16-A", "age": 16}, {"team": "16-B", "age": 16}]
    inp = _with_tournaments(_inp(teams), [{"name": "Boys 14 Local", "type": "Local", "start_date": "11/14/2026",
                                           "end_date": "11/15/2026"}])
    r = solve(inp, *NOV, time_limit_s=10)
    cal = month_calendar_html(r.assignments, *NOV, weekend_dates=r.weekend_dates, tournaments=r.tournaments)
    assert cal.count(">Boys 14 Local<") == 2   # Sat and Sun, though neither weekend day is open for practices


@pytest.mark.usefixtures("alone_ok")
def test_breaking_a_no_date_beats_practicing_next_to_a_tournament():
    """Only Tue and Fri (the day before a Sat-Sun tournament) are free that
    week. A no-date is broken instead of practicing Friday."""
    teams = [{"team": "16-Ana", "age": 16, "head_coach": "Ana"}]
    avail = [{"who": "16-Ana", "type": "No", "dates": "11/9"},
             {"who": "16-Ana", "type": "No", "dates": "11/12"},
             {"who": "Ana", "type": "No", "dates": "11/11", "note": "work"}]
    inp = _with_tournaments(_inp(teams, avail), [{"name": "Far West", "type": "Travel", "start_date": "11/14/2026",
                                                  "end_date": "11/15/2026", "teams": "16-Ana"}])
    r = solve(inp, *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    wk = pr[pr["date"].between(date(2026, 11, 8), date(2026, 11, 14))]
    assert len(wk) == 2 and r.shortfalls.empty
    assert date(2026, 11, 13) not in set(wk["date"])            # not the day before
    assert date(2026, 11, 16) not in set(pr["date"])            # nor the day after
    # Ana's own no-date (Wed) is never broken now; the team's Thu no-date gives way instead.
    assert [(b["date"], b["head_out"], b["team_out"]) for b in r.broken] == [(date(2026, 11, 12), [], True)]


def test_back_to_backs_kept_to_the_unavoidable_on_fri_sat_sun():
    """Only Fri, Sat and Sun are open. Fri/Sat every week would be a back-to-back every week;
    one Sat->Sun breaks the chain and later weeks go Sun/Fri. Back-to-backs only when
    unavoidable (Nov 2026): the fewest in total wins, and Sat->Sun is still flagged."""
    teams = [{"team": f"16-W{i}", "age": 16} for i in range(4)]
    avail = [{"who": t["team"], "type": "No", "days": "Mon, Tue, Wed, Thu"} for t in teams]
    weekends = [{"date": d, "open": "9AM-9PM"} for d in ("11/7", "11/8", "11/14", "11/15", "11/21", "11/22", "11/28")]
    r = solve(_inp(teams, avail, weekends=weekends), *NOV, time_limit_s=15)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    for name, g in pr.groupby("name"):
        days = sorted(set(g["date"]))
        b2b = sum((b - a).days == 1 for a, b in zip(days, days[1:]))
        assert b2b <= 2, (name, days)
    assert r.shortfalls.empty


def _early_pressure_club(extra_avail=()):
    """Four young teams, with a clinic taking all but one court at 5:30 every
    weekday: 5 spots at 5:30 a week for 8 practices, so 3 have to go to 3:45."""
    teams = [{"team": "12-Ana", "age": 12}, {"team": "13-Bo", "age": 13},
             {"team": "14-Cy", "age": 14}, {"team": "14-Di", "age": 14}]
    locals_ = [{"program": "Clinic", "days": "Mon, Tue, Wed, Thu, Fri", "time": "5:30", "courts": "1-6",
                "coaches": "", "on_site": "Yes"}]
    return _inp(teams, list(extra_avail), locals_=locals_)


def _early_per_week(r):
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    early = pr[pr["slot"] == "3:45"]
    return early.groupby(["name", early["date"].map(week_start)]).size()


def test_3_45_rotates_and_youngest_go_first():
    inp = _early_pressure_club()
    r = solve(inp, *NOV, time_limit_s=20)
    _check_hard_rules(inp, r)
    assert r.shortfalls.empty
    per_week = _early_per_week(r)
    assert per_week.max() == 1, per_week[per_week > 1]  # shared, not doubled up on one team
    # The 3 early spots go to the youngest teams first.
    template = r.pattern
    early_teams = set(template[template["slot"] == "3:45"]["team"])
    assert {"12-Ana", "13-Bo"} <= early_teams


@pytest.mark.usefixtures("alone_ok")
def test_team_that_asks_for_3_45_gets_it():
    teams = [{"team": "13-Eve", "age": 13}, {"team": "13-Fay", "age": 13}]
    wish = [{"team": "13-Eve", "choice": 1, "times": "3:45"}]
    r = solve(_inp(teams, wishlist=wish), *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert (pr[pr["name"] == "13-Eve"]["slot"] == "3:45").all()
    assert not (pr[pr["name"] == "13-Fay"]["slot"] == "3:45").any()  # 3:45 is still a last resort


def test_not_preferred_3_45_kept_off_early_slot():
    avail = [{"who": "12-Ana", "type": "Not preferred", "times": "3:45"}]
    inp = _early_pressure_club(avail)
    r = solve(inp, *NOV, time_limit_s=20)
    _check_hard_rules(inp, r)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert not (pr[pr["name"] == "12-Ana"]["slot"] == "3:45").any()
    assert r.shortfalls.empty


def test_late_starting_team_template_follows_wishlist():
    # Starts on the last Saturday: only one weekday in range, but its usual
    # week should still be the Tue/Thu it asked for.
    teams = [{"team": "17-Lin", "age": 17, "start_date": "11/28/2026"}, {"team": "16-Bo", "age": 16}]
    wish = [{"team": "17-Lin", "choice": 1, "days": "Tue, Thu", "times": "5:30"}]
    r = solve(_inp(teams, wishlist=wish), *NOV, time_limit_s=10)
    lin = r.pattern[r.pattern["team"] == "17-Lin"]
    assert set(zip(lin["day"], lin["slot"])) == {("Tue", "5:30"), ("Thu", "5:30")}


def test_back_to_back_weekdays_when_asked_for():
    teams = [{"team": "14 Black", "age": 14, "head_coach": "Matt"}]
    wish = [{"team": "14 Black", "choice": 1, "days": "Tue, Wed", "times": "5:30"}]
    r = solve(_inp(teams, wishlist=wish), *NOV, time_limit_s=10)
    assert set(r.pattern["day"]) == {"Tue", "Wed"}


def test_wishlist_miss_explained_in_review():
    # Pat heads both teams and both want Tue/Thu 5:30: one can't have it.
    teams = [{"team": "16 Black", "age": 16, "head_coach": "Pat"}, {"team": "16-Pat", "age": 16}]
    wish = [{"team": t, "choice": 1, "days": "Tue, Thu", "times": "5:30"} for t in ("16 Black", "16-Pat")]
    r = solve(_inp(teams, wishlist=wish), *NOV, time_limit_s=10)
    rows = r.issues[r.issues["issue"].str.startswith("Usual week isn't the 1st choice")]
    assert len(rows) >= 1
    assert rows["detail"].str.contains("Pat coaches").any()


def test_wished_back_to_back_days_beat_a_neighbors_swap():
    # Seven 15s fill every court Tue-Thu 5:30 except one Wed spot, which
    # 15-Reb holds on Wed/Fri. 14 Black asked for Tue/Wed 5:30. Moving Reb to
    # the open Thu 5:30 gives 14 Black its Wednesday; asking for back-to-back
    # days shouldn't count against it.
    teams = [{"team": "14 Black", "age": 14, "head_coach": "Matt"},
             {"team": "15-Reb", "age": 15}] + [{"team": f"16-F{i}", "age": 16} for i in range(6)]
    wish = [{"team": "14 Black", "choice": 1, "days": "Tue, Wed", "times": "5:30"},
            {"team": "15-Reb", "choice": 1, "times": "5:30"}]
    wish += [{"team": f"16-F{i}", "choice": 1, "days": "Tue, Wed", "times": "5:30"} for i in range(5)]
    avail = [{"who": "15-Reb", "type": "No", "days": "Mon"}]
    r = solve(_inp(teams, avail, wish), *NOV, time_limit_s=15)
    mb = r.pattern[r.pattern["team"] == "14 Black"]
    assert set(zip(mb["day"], mb["slot"])) == {("Tue", "5:30"), ("Wed", "5:30")}
    mine = r.issues[r.issues["team"] == "14 Black"]
    b2b = mine[mine["issue"].str.contains("Back-to-back")]     # flagged (Nov 2026), but only as Low: asked for
    assert (b2b["severity"] == "Low").all() and b2b["detail"].str.contains("asked").all()


@pytest.mark.usefixtures("alone_ok")
def test_open_requested_slot_beats_sticking_to_usual():
    # 14 Black asked for Tue/Wed 5:30. A one-week clinic is the only thing on
    # Wednesdays except the first week, so its usual week is Tue/Thu; in the
    # week Wednesday is free, it should get Wednesday, not stick to Thursday.
    teams = [{"team": "14 Black", "age": 14, "head_coach": "Matt"}]
    wish = [{"team": "14 Black", "choice": 1, "days": "Tue, Wed", "times": "5:30"}]
    avail = [{"who": "14 Black", "type": "No", "dates": "11/11; 11/18; 11/25"}]
    r = solve(_inp(teams, avail, wish), *NOV, time_limit_s=15)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    first_week = pr[pr["date"].map(week_start) == date(2026, 11, 1)]
    assert set(first_week["date"].map(lambda d: d.strftime("%a"))) == {"Tue", "Wed"}


def _ladder_club(who_is_out):
    """Two weeks, 11/1-11/14. Week 1 only has Sat 11/7 open. Week 2 has
    Sun 11/8, Wed 11/11 and Fri 11/13. Each team needs Sat 11/7 (or misses a
    practice), then 2 of Sun / Wed / Fri, where Wed is a no-date for
    `who_is_out` ('head' or 'assistant'). Two teams so weekend blocks are shared."""
    teams = [{"team": "16-Ann", "age": 16, "assistants": "Abe"},
             {"team": "16-Bea", "age": 16, "assistants": "Bo"}]
    out = {"head": ["Ann", "Bea"], "assistant": ["Abe", "Bo"]}[who_is_out]
    avail = [{"who": w, "type": "No", "dates": "11/11"} for w in out]
    weekends = [{"date": d, "open": "9AM-9PM"} for d in ("11/7", "11/8")]
    closed = [2, 3, 4, 5, 6, 9, 10, 12]
    closures = [{"date": f"11/{n}/2026", "reason": "closed"} for n in closed]
    inp = _inp(teams, avail, weekends=weekends, closures=closures)
    return inp, solve(inp, date(2026, 11, 1), date(2026, 11, 14), time_limit_s=10)


def _days(r, team):
    pr = r.assignments[(r.assignments["kind"] == "Practice") & (r.assignments["name"] == team)]
    return {d.strftime("%a %-m/%-d") for d in pr["date"]}


def test_sat_sun_beats_breaking_a_head_coach_no_date():
    inp, r = _ladder_club("head")
    for team in ("16-Ann", "16-Bea"):
        assert _days(r, team) == {"Sat 11/7", "Sun 11/8", "Fri 11/13"}, _days(r, team)
    assert not r.broken
    flags = r.issues[r.issues["issue"].str.startswith("Saturday then Sunday")]
    assert set(flags["team"]) == {"16-Ann", "16-Bea"} and (flags["severity"] == "High").all()


def test_assistant_no_date_gives_way_before_a_weekend_back_to_back():
    """Nov 2026: assistants rank below weekends (and coach gaps / Thu-Fri). Rather than Sat->Sun,
    the assistant misses a weekday practice (flagged; the head coach runs it)."""
    inp, r = _ladder_club("assistant")
    for team in ("16-Ann", "16-Bea"):
        days = _days(r, team)
        assert not {"Sat 11/7", "Sun 11/8"} <= days, days
    assert r.issues["issue"].str.startswith("Assistant no-date broken").any()
    assert not r.issues["issue"].str.startswith("Saturday then Sunday").any()


def test_rule_order():
    """The club's order, most important first: each rule outweighs at least
    two breaks of the next one down."""
    from scheduler.config import WEIGHTS as W
    # Nov 2026: subs > weekend > coach gaps and Thu/Fri > assistants > other back-to-backs.
    order = [W["missed_practice"], W["tournament_rest"], W["break_team_nodate"], W["coach_late_eve"],
             W["locals_sub"], W["weekend"], W["consecutive_days"] + W["thu_fri"], W["assistant_nodate"],
             W["consecutive_days"], W["friday_late"],
             W["fill_530_first"], W["sat_sun"], W["wish_day_miss"], W["wish_time_miss"],
             W["second_weekend"]]
    assert W["weekend"] > W["coach_gap"] > W["assistant_nodate"] >= W["assistant_conflict"]
    # Back-to-back days beat every day/time preference put together, a weekend still beats them
    # plus a 7:20 with 5:30 open.
    assert W["consecutive_days"] > W["wish_day_miss"] + W["not_preferred_day"] + W["wish_time_miss"] + W["not_preferred"]
    assert W["weekend"] > W["consecutive_days"] + W["thu_fri"]
    for hi, lo in zip(order, order[1:]):
        assert hi > lo, (hi, lo)
    # Days before times, for both the wishlist and not-preferred.
    assert W["wish_day_miss"] > W["wish_time_miss"]
    assert W["not_preferred_day"] > W["not_preferred"] + W["not_preferred_345"]
    # Every "everything else" weight is below the weekend rules.
    for k in ("second_345", "late_exception", "off_pattern", "split_timeslot", "keep_previous", "keep_court",
              "weekend_block", "home_court", "move_to_wish", "boys_later"):
        assert W[k] < W["second_weekend"], k
    # Friday 7:20 beats every preference put together, but not a weekend.
    assert W["weekend"] > W["friday_late"] > W["wish_day_miss"] + W["not_preferred_day"] + W["wish_time_miss"]
    # Courts (Nov 8, 2026): never lower a net, then Courts 1 / 7 / Court 2 locked by rank, then
    # coaches staying put inside the middle tier (3, 6, 5, 4), then neighbors and same net heights.
    from scheduler.config import PECKING_COURT_COST as PC
    worst_pecking = W["pecking_court"] * max(PC.values()) * 18   # top vs bottom team on Court 2
    assert W["net_lower_court"] > worst_pecking
    tier_step = W["pecking_court"] * min(PC[7] - PC[1], PC[3] - PC[7], PC[2] - PC[4])
    mid_step = W["pecking_court"] * (PC[6] - PC[3])
    soft = W["coach_stay"] + 3 * W["near_side"] + W["net_raise_court"]
    assert tier_step > soft                       # nothing soft moves a team across a tier
    assert W["coach_stay"] > mid_step             # inside the middle tier a coach stays put...
    assert W["coach_stay"] < 3 * 3 * mid_step     # ...unless the gap is big (3 places, 3 courts)
    assert mid_step > W["net_raise_court"]
    assert W["weekend_required"] == 0  # a note for coaches, not a rule


def test_wishlist_days_count_more_than_times():
    from scheduler.data import Wish
    from scheduler.solver import _wish_cost
    w = [Wish("16-A", 1, ["Tue", "Thu"], {"5:30"})]
    assert _wish_cost(w, "Tue", "5:30") == 0
    assert _wish_cost(w, "Tue", "7:20") < _wish_cost(w, "Mon", "5:30") < _wish_cost(w, "Mon", "7:20")


@pytest.mark.usefixtures("alone_ok")
def test_coach_in_a_program_needs_a_sub_before_a_no_date_breaks():
    """Week of 11/8: Wed, Thu and Fri open, 5:30 only. The head coach works a
    program Wed 5:30 and the assistant can't do Thursday. The team gets Wed
    (flagged: possible with a sub) and Fri, instead of breaking the
    assistant's no-date on Thursday."""
    teams = [{"team": "16-Ann", "age": 16, "head_coach": "Ann", "assistants": "Abe"}]
    avail = [{"who": "16-Ann", "type": "No", "times": "7:20"}, {"who": "Abe", "type": "No", "dates": "11/12"}]
    locals_ = [{"program": "B 14 L", "days": "Wed", "time": "5:30", "courts": "1", "coaches": "Ann",
                "on_site": "Yes"}]
    closures = [{"date": f"11/{n}/2026", "reason": "closed"} for n in (9, 10)]
    r = solve(_inp(teams, avail, locals_=locals_, closures=closures), date(2026, 11, 8), date(2026, 11, 14),
              time_limit_s=10)
    assert _days(r, "16-Ann") == {"Wed 11/11", "Fri 11/13"}
    assert not r.issues["issue"].str.startswith("Assistant no-date broken").any()
    sub = r.issues[r.issues["issue"].str.startswith("Needs a sub")]
    assert len(sub) == 1 and "Ann gets a sub for B 14 L" in sub["detail"].iloc[0]
    # Without the program, no sub is needed.
    r2 = solve(_inp(teams, avail, closures=closures), date(2026, 11, 8), date(2026, 11, 14), time_limit_s=10)
    assert not r2.issues["issue"].str.startswith("Needs a sub").any()


def test_sat_sun_beats_a_missed_practice():
    # Only Sat 11/7 and Sun 11/8 are open: using both beats missing one.
    teams = [{"team": f"16-W{i}", "age": 16} for i in range(2)]
    weekends = [{"date": d, "open": "9AM-9PM"} for d in ("11/7", "11/8")]
    closures = [{"date": f"11/{n}/2026", "reason": "closed"} for n in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]
    r = solve(_inp(teams, weekends=weekends, closures=closures), date(2026, 11, 1), date(2026, 11, 14),
              time_limit_s=10)
    for team in ("16-W0", "16-W1"):
        assert _days(r, team) == {"Sat 11/7", "Sun 11/8"}
    assert r.issues["issue"].str.startswith("Saturday then Sunday").sum() == 2


def test_hand_placed_saturday_and_sunday_allowed_but_flagged():
    teams = [{"team": "16-W", "age": 16}, {"team": "16-X", "age": 16, "head_coach": "Xi"}]
    weekends = [{"date": d, "open": "9AM-9PM"} for d in ("11/7", "11/8")]
    inp = _with_overrides(_inp(teams, weekends=weekends), [
        {"team": "16-W", "action": "Add", "date": "11/7/2026", "time": "9AM"},
        {"team": "16-W", "action": "Add", "date": "11/8/2026", "time": "9AM"},
        {"team": "16-X", "action": "Add", "date": "11/7/2026", "time": "9AM"},
        {"team": "16-X", "action": "Add", "date": "11/8/2026", "time": "9AM"}])
    r = solve(inp, *NOV, time_limit_s=10)
    flags = r.issues[r.issues["issue"].str.startswith("Saturday then Sunday")]
    assert "16-W" in set(flags["team"])


def test_team_no_date_naming_an_assistant_is_pointed_out():
    teams = [{"team": "15-Madi", "age": 15, "assistants": "Cierra"}]
    avail = [{"who": "15-Madi", "type": "No", "days": "Mon", "note": "Cierra can't make Mondays"}]
    data = clean(_inp(teams, avail), *NOV)
    assert any("put Cierra in the who column" in w for w in data.warnings)



# -------------------------------------------------------- pecking order --

def test_rank_parsing_handles_club_formats_and_excel_dates():
    from datetime import datetime

    from scheduler.data import parse_rank
    assert parse_rank("16-3", 16) == (3, None)
    assert parse_rank("16 - 3", 16) == (3, None)
    assert parse_rank("3", 16) == (3, None)
    assert parse_rank(2.0, 16) == (2, None)
    assert parse_rank(datetime(2026, 12, 1), 12) == (1, None)   # Excel turned "12-1" into Dec 1
    assert parse_rank(datetime(2026, 1, 12), 12) == (1, None)   # ...or Jan 12
    assert parse_rank("Black", 14) == (1, None) and parse_rank("Green", 14) == (2, None)
    rank, note = parse_rank("17-1", 16)
    assert rank == 1 and "17s" in note


def test_old_level_column_still_read_as_rank():
    inp = simulate(4, 2, 2, start=date(2026, 11, 1), end=date(2026, 11, 30))
    rec = inp.to_records()
    for row in rec["Teams"]:
        row["level"] = row.pop("rank")
    rec["Teams"][0]["level"] = "3"
    back = ScheduleInput.from_records(rec)
    assert "level" not in back.teams.columns and back.teams["rank"].iloc[0] == "3"


def _pecking_xlsx(tmp_path, rows):
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.title = "Sheet1"
    wb.active.append(["unrelated", "stuff"])
    ws = wb.create_sheet("Pecking Order")
    ws.append(["Rank", "Gender", "Team Name", "Head Coach", "Asst. Coach"])
    for r in rows:
        ws.append(r)
    path = tmp_path / "pecking.xlsx"
    wb.save(path)
    return path


def test_pecking_import_matches_nicknames_and_excel_dates(tmp_path):
    from datetime import datetime

    from scheduler import pecking as pk
    teams = [{"team": "18 Black", "club": "Seaside", "age": 18, "head_coach": "Tyler", "assistants": "Alex"},
             {"team": "16-Danielle & Darci", "club": "SDVBC", "age": 16, "head_coach": "Danielle; Darci"},
             {"team": "15-Rebecca", "club": "SDVBC", "age": 15},
             {"team": "12-Madi", "club": "SDVBC", "age": 12, "assistants": "Lala"},
             {"team": "13-Bree", "club": "SDVBC", "age": 13}]
    path = _pecking_xlsx(tmp_path, [
        ["18-1", "Boy", "18-Black", "Tyler", "Alex"],
        ["16-1", "Girl", "16-D&D", "Danielle", "Darci"],
        ["15-3", "Girl", "15-Rebecca", "Rebecca", "Tyler Lyons"],
        [datetime(2026, 12, 1), "Girl", "12-Madi", "Madi", "Lala"],
        ["13-2", "Girl", "13-Bree", "Bree", "TBA"]])
    table = pk.read_pecking(str(path))
    assert table.attrs["sheet"] == "Pecking Order"
    ms = pk.match_teams(table, _team_frame(teams))
    got = {m.sheet_team: (m.team, m.rank_text, m.how) for m in ms}
    assert got["18-Black"] == ("18 Black", "18-1", "name")
    assert got["16-D&D"] == ("16-Danielle & Darci", "16-1", "age + head coach")
    assert got["12-Madi"][:2] == ("12-Madi", "12-1")
    changes = pk.assistant_changes(ms, _team_frame(teams))
    assert ("15-Rebecca", "(none)", "Tyler Lyons") in changes
    assert not any(c[0] == "16-Danielle & Darci" for c in changes)   # Darci is co-head, not a change
    assert not any(c[0] == "13-Bree" for c in changes)               # TBA = nobody
    out = pk.apply(_team_frame(teams), ms, update_assistants=True)
    data = clean(_inp(out.to_dict("records")), *NOV)
    by = {t.name: t for t in data.teams}
    assert by["16-Danielle & Darci"].level == 1 and by["12-Madi"].level == 1
    assert by["15-Rebecca"].assistants == ["Tyler Lyons"]
    # Tyler (18 Black's head) and Tyler Lyons are different people.
    assert "Tyler" in data.coaches and "Tyler Lyons" in data.coaches


def test_tyler_and_tyler_lyons_are_different_coaches():
    # Tyler heads 18 Black; Tyler Lyons assists 15-Rebecca. Both teams want the
    # same slot and only those two teams exist: if the names were confused,
    # it would count as a head coach double-booking or an assistant conflict.
    teams = [{"team": "18 Black", "age": 18, "head_coach": "Tyler"},
             {"team": "15-Rebecca", "age": 15, "assistants": "Tyler Lyons"}]
    wish = [{"team": t["team"], "choice": 1, "days": "Tue, Thu", "times": "7:20"} for t in teams]
    avail = [{"who": "Tyler Lyons", "type": "No", "days": "Mon"}]
    r = solve(_inp(teams, avail, wish), *NOV, time_limit_s=10)
    assert r.metrics["assistant_conflicts"] == 0
    assert set(zip(r.pattern["team"], r.pattern["day"])) >= {("18 Black", "Tue"), ("15-Rebecca", "Tue")}
    # A note naming "Tyler Lyons" on 15-Rebecca's row points at Tyler Lyons, not Tyler.
    avail = [{"who": "15-Rebecca", "type": "No", "days": "Fri", "note": "Tyler Lyons works Fridays"}]
    ws = clean(_inp(teams, avail), *NOV).warnings
    assert any("put Tyler Lyons in the who column" in w for w in ws)


def test_lowest_ranked_team_gets_court_2():
    # Seven teams want Tue/Thu 5:30 and nothing else: all 7 courts are used.
    # The lowest in the pecking order gets Court 2, the top two teams Courts 1 and 7 (equal, Nov 2026).
    teams = [{"team": f"16-T{i}", "age": 16, "rank": f"16-{i + 1}"} for i in range(7)]
    avail = [{"who": t["team"], "type": "No", "days": "Mon, Wed, Fri"} for t in teams] + \
            [{"who": t["team"], "type": "No", "times": "7:20"} for t in teams]
    r = solve(_inp(teams, avail), *NOV, time_limit_s=15)
    pr = r.assignments
    assert set(pr[pr["court"] == 2]["name"]) == {"16-T6"}
    assert set(pr[pr["court"].isin([1, 7])]["name"]) == {"16-T0", "16-T1"}


def test_calendar_exports(tmp_path):
    import io as _io

    from openpyxl import load_workbook

    from scheduler.export import calendar_excel, calendar_ics, to_excel
    from scheduler.prints import calendar_print_html
    inp = simulate(4, 2, 2, start=date(2026, 11, 1), end=date(2026, 11, 30))
    r = solve(inp, *NOV, time_limit_s=10)
    data = clean(inp, *NOV)
    team = r.pattern["team"].iloc[0]
    n = len(r.assignments[(r.assignments["kind"] == "Practice") & (r.assignments["name"] == team)])
    ics = calendar_ics(r, {team}, False).decode()
    assert ics.count("BEGIN:VEVENT") == n and team in ics
    everyone = calendar_ics(r, None, True).decode()
    assert everyone.count("BEGIN:VEVENT") > n
    wb = load_workbook(_io.BytesIO(calendar_excel(r, data.closures)))
    ws = wb.active
    assert [ws.cell(1, c).value for c in (2, 3, 5)] == ["Sunday", "Cts", "Monday"]
    courts = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, str) and "-" in c.value
              and c.value[0].isdigit() and len(c.value) <= 5]
    assert all(c.number_format == "@" for row in ws.iter_rows() for c in row if c.value in courts)
    assert "Calendar" in load_workbook(_io.BytesIO(to_excel(r, data.closures))).sheetnames
    page = calendar_print_html(r.assignments, *NOV, data.closures, {team}, False, r.weekend_dates)
    assert "November 2026" in page and team in page and "@media print" in page


def test_one_off_no_date_doesnt_move_the_usual_week():
    """A no-date in one week (here the team's Thanksgiving-week Wed) shouldn't
    push the usual week onto a day the team then leaves every other week."""
    inp = simulate(6, 4, 4, seed=7, start=NOV[0], end=NOV[1])
    r = solve(inp, *NOV, time_limit_s=60)
    pat = r.pattern[r.pattern["team"] == "13 Black"]
    assert sorted(pat["day"]) == ["Fri", "Wed"], pat
    # Every team's usual slots are used in most of its full weeks.
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    weekly = pr.assign(wk=pr["date"].map(week_start))
    for p in r.pattern.itertuples():
        played = weekly[weekly["name"] == p.team]
        full = played.groupby("wk").size()
        full = set(full[full == 2].index)
        if len(full) < 3:
            continue  # a late starter whose only weeks are disrupted (a no-date, Thanksgiving) proves nothing
        # A usual 7:20 moved up to an open 5:30 that night still follows the usual week (Nov 2026).
        ok_slots = {p.slot} | ({"5:30"} if p.slot == "7:20" else set())
        hits = played[(played["day"] == p.day) & played["slot"].isin(ok_slots) & played["wk"].isin(full)]
        assert len(hits) * 2 >= len(full), (p.team, p.day, p.slot, len(hits), len(full))


@pytest.mark.parametrize("seed", [3, 7, 11])
def test_usual_week_is_a_valid_week(seed):
    """The weekly template is read as a real week: one team per court, and
    never on a court a recurring program holds."""
    inp = simulate(8, 6, 4, seed=seed, start=NOV[0], end=NOV[1])
    r = solve(inp, *NOV, time_limit_s=30)
    pat = r.pattern
    assert (pat.groupby(["day", "slot", "court"]).size() <= 1).all(), pat.sort_values(["day", "slot", "court"])
    data = clean(inp, *NOV)
    for p in pat.itertuples():
        for lp in data.locals:
            if lp.on_site and not lp.ranges and lp.day == p.day and slots_overlap(lp.slot, p.slot):
                assert p.court not in lp.courts, (p.team, p.day, p.slot, lp.name)


def test_import_keeps_programs_and_tournaments():
    from scheduler.data import keep_programs
    old = _inp([{"team": "14-A", "age": 14}],
               locals_=[{"program": "G 14 L", "days": "Wed, Fri", "time": "3:45", "courts": "1-7", "on_site": "Yes"}])
    old.tournaments = pd.DataFrame([{"name": "Boys Local", "type": "Local", "start_date": "11/7/2026",
                                     "end_date": "11/7/2026"}])
    # A spreadsheet with no programs or tournaments: both are kept.
    new = _inp([{"team": "14-B", "age": 14}])
    notes = keep_programs(new, old)
    assert list(new.locals["program"]) == ["G 14 L"] and list(new.tournaments["name"]) == ["Boys Local"]
    assert len(notes) == 2 and list(new.teams["team"]) == ["14-B"]
    # One with its own programs: still kept, unless asked to use the file's.
    file_locals = [{"program": "B 14 L", "days": "Mon", "time": "5:30", "courts": "2-4", "on_site": "Yes"}]
    new = _inp([{"team": "14-B", "age": 14}], locals_=file_locals)
    keep_programs(new, old)
    assert list(new.locals["program"]) == ["G 14 L"]
    new = _inp([{"team": "14-B", "age": 14}], locals_=file_locals)
    keep_programs(new, old, use_file=True)
    assert list(new.locals["program"]) == ["B 14 L"]          # the file's programs
    assert list(new.tournaments["name"]) == ["Boys Local"]    # file had none: kept
    # Nothing to keep: the file's are used.
    new = _inp([{"team": "14-B", "age": 14}], locals_=file_locals)
    keep_programs(new, None)
    assert list(new.locals["program"]) == ["B 14 L"]


# ------------------------------------------------- Oct 26 meeting changes --

def _per_week(pr, name):
    g = pr[pr["name"] == name]
    return g.groupby(g["date"].map(week_start)).size()


def test_pairing_is_the_last_resort():
    teams = [{"team": f"16-C{i}", "age": 16} for i in range(9)]
    # Only Mon/Wed 5:30 suit everyone, but the other times are just no-dates:
    # those get broken (and flagged) before anyone is paired.
    avail = [{"who": t["team"], "type": "No", "days": "Tue, Thu, Fri"} for t in teams]
    avail += [{"who": t["team"], "type": "No", "times": "7:20"} for t in teams]
    r = solve(_inp(teams, avail), *NOV, time_limit_s=20)
    assert r.metrics["paired_slots"] == 0 and r.broken
    # Programs fill every court at every other time: no court space left, so
    # pairing beats a team missing a practice.
    locals_ = [{"program": "Clinic A", "days": "Tue, Thu, Fri", "time": "5:30-9:10", "courts": "1-7"},
               {"program": "Clinic B", "days": "Mon, Wed", "time": "7:20", "courts": "1-7"}]
    inp = _inp(teams, locals_=locals_)
    r = solve(inp, *NOV, time_limit_s=20)
    _check_hard_rules(inp, r)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert r.metrics["paired_slots"] > 0 and r.shortfalls.empty
    assert (r.issues["issue"].str.startswith("Paired:")).any()
    assert pr[pr["paired"] != ""].iloc[0]["paired"].startswith("3 teams on Court")
    # With 7 teams it all fits, so nobody is paired.
    r7 = solve(_inp(teams[:7], locals_=locals_), *NOV, time_limit_s=15)
    assert r7.metrics["paired_slots"] == 0


@pytest.mark.usefixtures("alone_ok")
def test_team_can_have_three_practices_a_week():
    teams = [{"team": "18 Black", "club": "Boys", "age": 18, "head_coach": "Al", "practices": "3"},
             {"team": "16-Bea", "age": 16}]
    r = solve(_inp(teams), *NOV, time_limit_s=15)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    full_weeks = [w for w, g in r.practice_counts.groupby("week_of") if (g["target"] >= 2).all()]
    counts = _per_week(pr, "18 Black")
    assert all(counts[w] == 3 for w in full_weeks)
    assert all(_per_week(pr, "16-Bea")[w] == 2 for w in full_weeks)
    assert len(r.pattern[r.pattern["team"] == "18 Black"]) == 3


@pytest.mark.usefixtures("alone_ok")
def test_extra_practice_goes_on_top():
    teams = [{"team": "16-Bea", "age": 16}]
    inp = _inp(teams)
    inp.overrides = pd.DataFrame([{"team": "16-Bea", "action": "Extra", "date": "11/13/2026", "days": "",
                                   "time": "7:20", "court": "", "note": ""}], columns=COLUMNS["Overrides"])
    r = solve(inp, *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    wk = week_start(date(2026, 11, 13))
    assert _per_week(pr, "16-Bea")[wk] == 3
    row = pr[(pr["date"] == date(2026, 11, 13))].iloc[0]
    assert row["extra"] == True and row["pinned"] == True  # noqa: E712


def test_flags_coach_at_local_and_travel_tournament():
    teams = [{"team": "15 Black", "club": "Boys", "age": 15, "head_coach": "Al"},
             {"team": "16-Bea", "age": 16}]
    inp = _with_tournaments(_inp(teams), [
        {"name": "Girls Local", "type": "Local", "start_date": "11/14/2026", "courts": "1-3", "coaches": "Al"},
        {"name": "Boys Travel", "type": "Travel", "start_date": "11/13/2026", "end_date": "11/15/2026",
         "teams": "15 Black"}])
    data = clean(inp, *NOV)
    conf = tournament_conflicts(data.teams, data.tournaments)
    assert conf and conf[0]["coach"] == "Al" and conf[0]["start"] == date(2026, 11, 14)
    r = solve(inp, *NOV, time_limit_s=10)
    assert r.issues["issue"].str.startswith("Tournament conflict: Al").any()


def test_locals_cancel_one_session_and_add_one():
    teams = [{"team": f"16-T{i}", "age": 16} for i in range(2)]
    locals_ = [{"program": "Big Clinic", "days": "Tue", "time": "5:30", "courts": "1-7", "skip": "11/10/2026"},
               {"program": "Big Clinic", "days": "Fri", "time": "5:30", "courts": "1-2", "dates": "11/20/2026"}]
    r = solve(_inp(teams, locals_=locals_), *NOV, time_limit_s=10)
    loc = r.assignments[r.assignments["kind"] == "Local"]
    assert date(2026, 11, 10) not in set(loc["date"])            # cancelled
    assert date(2026, 11, 17) in set(loc["date"])               # still runs other weeks
    assert set(loc[loc["date"] == date(2026, 11, 20)]["court"]) == {1, 2}  # the one extra session


def test_hs_boys_nets_never_come_down_from_530_to_720():
    teams = [{"team": f"{a} {c}", "club": "Boys", "age": a, "head_coach": f"B{a}{c}"}
             for a in (15, 16, 17, 18) for c in ("Black", "Green")]
    teams += [{"team": f"{a}-G{i}", "club": "Girls", "age": a} for a in (15, 16, 17) for i in range(2)]
    r = solve(_inp(teams), *NOV, time_limit_s=20)
    a = r.assignments
    pr = a[a["kind"] == "Practice"]
    boys = {t["team"] for t in teams if t["club"] == "Boys"}
    changes = 0
    for _, g in pr[pr["slot"].isin(["5:30", "7:20"])].groupby(["date", "court"]):
        kinds = {s: set(g[g["slot"] == s]["name"].isin(boys)) for s in ("5:30", "7:20")}
        if kinds["5:30"] and kinds["7:20"] and kinds["5:30"] != kinds["7:20"]:
            changes += 1
    assert changes == r.metrics["net_changes"]
    assert r.metrics["nets_lowered"] == 0                 # raising a net is fine, lowering isn't (Nov 8, 2026)
    for _, g in pr[pr["slot"].isin(["5:30", "7:20"])].groupby(["date", "court"]):
        if set(g["slot"]) == {"5:30", "7:20"}:
            early = g[g["slot"] == "5:30"]["name"].isin(boys).iloc[0]
            late = g[g["slot"] == "7:20"]["name"].isin(boys).iloc[0]
            assert late or not early                       # never men's at 5:30 then women's at 7:20


@pytest.mark.usefixtures("alone_ok")
def test_coach_stays_on_court_for_back_to_back_teams():
    # (With only two teams, the court ranks don't stand in the way.)
    teams = [{"team": "16-Ana", "club": "Girls", "age": 16, "head_coach": "Ana"},
             {"team": "18-Ana", "club": "Girls", "age": 18, "head_coach": "Ana"}]
    wish = [{"team": "16-Ana", "choice": 1, "days": "Mon, Wed", "times": "5:30"},
            {"team": "18-Ana", "choice": 1, "days": "Mon, Wed", "times": "7:20"}]
    r = solve(_inp(teams, wishlist=wish), *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    for _, g in pr.groupby("date"):
        if set(g["slot"]) == {"5:30", "7:20"}:
            assert g["court"].nunique() == 1, g


@pytest.mark.usefixtures("alone_ok")
def test_privates_conflicts_and_export():
    from openpyxl import load_workbook

    from scheduler import privates as pv
    teams = [{"team": "16-Bea", "age": 16, "head_coach": "Bea"}]
    r = solve(_inp(teams), *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"].iloc[0]
    df = pd.DataFrame([
        {"coach": "Bea", "date": f"{pr['date']:%m/%d/%Y}", "time": "6:00 PM", "court": "P", "type": "Personal"},
        {"coach": "Cal", "date": f"{pr['date']:%m/%d/%Y}", "time": "6:00 PM", "court": str(int(pr["court"])),
         "type": "Team", "team": "16-Bea"},
        {"coach": "Cal", "date": "11/30/2026", "time": "9:00 AM", "court": "", "type": "Personal", "player": "Jo"}],
        columns=COLUMNS["Privates"])
    privs, problems = pv.rows(df, 2026)
    assert not problems and len(privs) == 3 and privs[-1]["court"] == 8
    flags = pv.conflicts(privs, r.assignments)
    if pr["slot"] == "5:30":   # practice runs 5:30-7:20, so 6 PM overlaps
        assert any("coaches 16-Bea" in x for x in flags[0])
        assert any("has 16-Bea" in x for x in flags[1])
    assert 2 not in flags
    wb = load_workbook(io.BytesIO(pv.to_excel(privs, ["Bea", "Cal", "Dee"], flags)))
    assert {"Coaches", "All privates", "Bea", "Cal"} <= set(wb.sheetnames)
    names = [row[0].value for row in wb["Coaches"].iter_rows(min_row=2)]
    assert "Dee" in names                                         # coaches with none are listed too
    assert pv.parse_start("4pm") == 16 * 60 and pv.parse_start("4:30") == 16 * 60 + 30


def test_calendar_lists_local_courts_and_weeks_print():
    from scheduler.views import month_calendar_html, weeks_print_html
    teams = [{"team": "16-Bea", "age": 16}]
    locals_ = [{"program": "G HS L", "days": "Tue", "time": "5:30", "courts": "1-4"}]
    r = solve(_inp(teams, locals_=locals_), *NOV, time_limit_s=10)
    cal = month_calendar_html(r.assignments, *NOV, include_locals=True)
    assert "G HS L · Ct 1-4" in cal
    page = weeks_print_html(r.assignments, *NOV, title="Test")
    assert page.count('class="cs-week"') == 10   # Nov 1, 8, 15, 22, 29, two pages each
    assert "Courts 5–7 & practice court" in page
    assert "break-after: page" in page


def test_extra_practice_beside_one_already_that_day():
    """An extra on a day the team already practices keeps the other one."""
    teams = [{"team": "16-Bea", "age": 16}, {"team": "17-Cy", "age": 17}]
    inp = _inp(teams)
    r0 = solve(inp, *NOV, time_limit_s=10)
    pr0 = r0.assignments[(r0.assignments["kind"] == "Practice") & (r0.assignments["name"] == "16-Bea")]
    first = pr0[pr0["date"].map(lambda d: d.weekday() < 5)].sort_values("date").iloc[0]
    other = "7:20" if first["slot"] == "5:30" else "5:30"
    inp.overrides = pd.DataFrame([{"team": "16-Bea", "action": "Extra", "date": f"{first['date']:%m/%d/%Y}",
                                   "days": "", "time": other, "court": "", "note": ""}], columns=COLUMNS["Overrides"])
    r = solve(inp, *NOV, time_limit_s=10, previous=r0.assignments, previous_pattern=r0.pattern)
    pr = r.assignments[(r.assignments["kind"] == "Practice") & (r.assignments["name"] == "16-Bea")]
    that_day = pr[pr["date"] == first["date"]]
    assert set(that_day["slot"]) == {first["slot"], other}, that_day
    wk = week_start(first["date"])
    assert _per_week(pr, "16-Bea")[wk] == _per_week(pr0, "16-Bea")[wk] + 1


def test_two_hand_placed_practices_same_day_different_times():
    teams = [{"team": "16-Bea", "age": 16}]
    inp = _inp(teams)
    inp.overrides = pd.DataFrame([
        {"team": "16-Bea", "action": "Add", "date": "11/10/2026", "days": "", "time": "5:30", "court": "", "note": ""},
        {"team": "16-Bea", "action": "Extra", "date": "11/10/2026", "days": "", "time": "7:20", "court": "",
         "note": ""},
    ], columns=COLUMNS["Overrides"])
    r = solve(inp, *NOV, time_limit_s=10)
    pr = r.assignments[(r.assignments["kind"] == "Practice") & (r.assignments["date"] == date(2026, 11, 10))]
    assert set(pr["slot"]) == {"5:30", "7:20"}


def test_boys_above_girls_at_the_same_rank():
    from scheduler import pecking as pk
    teams = pd.DataFrame([{"team": "14-Ann", "club": "SDVBC", "age": 14, "rank": "14-1"},
                          {"team": "14 Black", "club": "Seaside", "age": 14, "rank": "14-1"},
                          {"team": "14 Green", "club": "Seaside", "age": 14, "rank": "14-2"}])
    order = pk.order_table(teams)
    assert list(order["team"]) == ["14 Black", "14-Ann", "14 Green"]
    data = clean(_inp([{"team": "14-Ann", "age": 14, "rank": "14-1"},
                       {"team": "14 Black", "club": "Boys", "age": 14, "rank": "14-1", "head_coach": "Al"}]), *NOV)
    from scheduler.config import pecking_weights
    w = pecking_weights(data.teams)
    assert w["14 Black"] > w["14-Ann"]


@pytest.mark.usefixtures("alone_ok")
def test_weekend_with_local_tournament_isnt_greyed_out_and_shows_under_the_day():
    from scheduler.views import court_grid_html, week_dates
    teams = [{"team": "16-Bea", "age": 16}]
    inp = _with_tournaments(_inp(teams), [
        {"name": "Girls Local A", "type": "Local", "start_date": "11/07/2026", "courts": "1-4"},
        {"name": "Girls Local B", "type": "Local", "start_date": "11/07/2026", "courts": "5-7"}])
    r = solve(inp, *NOV, time_limit_s=10)
    assert date(2026, 11, 7) not in r.weekend_dates   # weekend not open for practices
    ws = week_start(date(2026, 11, 7))
    a = r.assignments
    h = court_grid_html(a[a["date"].map(week_start) == ws], week_dates(ws, *NOV), {}, None, None, r.tournaments,
                        r.weekend_dates)
    assert h.count("not open for practices") == 1   # Sunday the 1st only; Saturday the 7th shows its tournaments
    head = h.split("</thead>")[0]
    assert "Girls Local A" in head and "Girls Local B" in head


def test_coach_on_two_programs_at_once_is_flagged():
    from scheduler.data import program_coach_conflicts
    teams = [{"team": "16-Bea", "age": 16}]
    inp = _inp(teams, locals_=[
        {"program": "G League", "days": "Tue", "time": "5:30", "courts": "1-2", "coaches": "Kai"},
        {"program": "JumpStart", "days": "Tue", "time": "4:30", "courts": "P", "coaches": "Kai; Lu"},
        {"program": "B League", "days": "Tue", "time": "3:45", "courts": "3", "coaches": "Lu"},
        {"program": "Late", "days": "Tue", "time": "7:20", "courts": "4", "coaches": "Kai"}])
    data = clean(inp, *NOV)
    conf = program_coach_conflicts(data.locals, *NOV, data.closures)
    pairs = {(c["coach"], c["programs"]) for c in conf}
    assert pairs == {("Lu", ("B League", "JumpStart"))}   # 4:30 JumpStart overlaps 3:45; 5:30 and 7:20 don't
    assert all(d.weekday() == 1 for c in conf for d in c["dates"])


def test_printed_weeks_have_open_courts_to_write_in_and_program_coaches():
    from scheduler.views import open_slot_map, weeks_print_html
    teams = [{"team": "16-Bea", "age": 16}]
    inp = _inp(teams, locals_=[{"program": "G League", "days": "Tue", "time": "5:30", "courts": "1-2",
                                "coaches": "Kai"}])
    r = solve(inp, *NOV, time_limit_s=10)
    data = clean(inp, *NOV)
    h = weeks_print_html(r.assignments, *NOV, data.closures, None, r.tournaments, r.weekend_dates,
                         open_for=lambda dd: open_slot_map(dd, data), local_coaches=True)
    assert 'class="cs-write"' in h and "Kai" in h
    plain = weeks_print_html(r.assignments, *NOV, data.closures, None, r.tournaments, r.weekend_dates)
    assert 'class="cs-write"' not in plain


def test_paper_print_has_every_slot_as_a_line():
    from scheduler.paper import paper_weeks_html
    teams = [{"team": "16-Bea", "age": 16}]
    inp = _with_tournaments(_inp(teams), [{"name": "Boys Local", "type": "Local", "start_date": "11/18/2026",
                                           "courts": "1-4"}])
    r = solve(inp, *NOV, time_limit_s=10)
    data = clean(inp, *NOV)
    h = paper_weeks_html(r.assignments, *NOV, data.closures, data.tournaments, "T", [], r.practice_counts)
    weeks = len({week_start(d) for d in pd.date_range(*NOV).date})
    assert h.count('<section class="cs-week">') == 2 * weeks
    assert "7:20-9:10" in h and "9-11" in h and "Boys Local" in h
    assert "needed" not in h and 'class="foot"' not in h   # practice counts are off for now
    assert "16-B" in h and "16-Bea" not in h   # short labels on the printed sheets


def test_weekend_teams_fill_one_block_before_opening_another():
    """Only Friday and Saturday are open: each team takes a Saturday. With
    room on the courts, they share one 2-hour block instead of splitting
    2 at 9-11 and 2 at 11-1 (and never one team alone)."""
    teams = [{"team": f"{a}-{n}", "age": a, "head_coach": n}
             for a, n in [(16, "Ann"), (15, "Bea"), (14, "Cat"), (13, "Dee"), (17, "Eve"), (12, "Fay")]]
    avail = [{"who": t["team"], "type": "No", "days": "Mon, Tue, Wed, Thu"} for t in teams]
    weekends = [{"date": d, "open": "9AM-1PM"} for d in ("11/7/2026", "11/14/2026")]
    inp = _inp(teams, avail, weekends=weekends)
    r = solve(inp, date(2026, 11, 1), date(2026, 11, 14), time_limit_s=20)
    _check_hard_rules(inp, r)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    we = pr[pr["date"].map(lambda d: d.weekday() >= 5)]
    assert len(we) == 12
    assert (we.groupby("date")["slot"].nunique() == 1).all(), we.groupby(["date", "slot"]).size()


def test_schedule_with_no_teams_yet_shows_its_locals():
    """A new schedule from the locals template has no teams until the
    coaches' spreadsheet is imported: it still solves and shows the locals."""
    from scheduler.data import ATTRS, empty
    src = simulate(4, 2, 3, seed=5, start=date(2026, 12, 5), end=date(2026, 12, 20))
    inp = ScheduleInput(**{a: empty(sh) for sh, a in ATTRS.items()})
    inp.locals = src.locals
    r = solve(inp, date(2026, 12, 5), date(2026, 12, 20), time_limit_s=5)
    assert r.ok and r.metrics["teams"] == 0
    assert set(r.assignments["kind"]) == {"Local"}


def test_team_dates_spreadsheet_circles_tournaments():
    import io
    import zipfile

    from openpyxl import load_workbook

    from scheduler.export import team_dates_excel
    s, e = date(2026, 12, 5), date(2026, 12, 20)
    inp = simulate(6, 4, 4, seed=7, start=s, end=e)
    r = solve(inp, s, e, time_limit_s=10)
    travel = [tn for tn in r.tournaments if tn.kind == "travel"]
    assert travel
    data = clean(inp, s, e)
    order = [t.name for t in data.teams][::-1]
    x = team_dates_excel(r, data.closures, order, "Dec")
    wb = load_workbook(io.BytesIO(x))
    sh = wb["Team Dates"]
    assert [sh.cell(5 + i, 1).value for i in range(len(order))] == order       # in the given order
    n_days = sum(len([d for d in pd.date_range(tn.start, tn.end).date if s <= d <= e]) * len(tn.teams)
                 for tn in travel)
    xml = zipfile.ZipFile(io.BytesIO(x)).read("xl/drawings/drawingCircles1.xml").decode()
    assert xml.count('prst="ellipse"') == n_days
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert sum(sh.cell(5 + i, 3 + 16).value for i in range(len(order))) == len(pr)
    assert wb["List"].max_row == 1 + len(pr) + n_days
    # Girls then boys, each under its own heading.
    from scheduler.config import team_is_boys
    sec = {t.name: "Boys" if team_is_boys(t.club, t.name) else "Girls" for t in data.teams}
    order2 = sorted(order, key=lambda t: sec[t] == "Boys")
    sh = load_workbook(io.BytesIO(team_dates_excel(r, data.closures, order2, "Dec", sec)))["Team Dates"]
    col_a = [sh.cell(i, 1).value for i in range(5, 5 + len(order2) + 2)]
    assert col_a[0] == "Girls" and "Boys" in col_a
    assert col_a.index("Boys") == 1 + sum(1 for t in order2 if sec[t] == "Girls")


def test_print_pages_have_a_print_button():
    from scheduler.paper import paper_weeks_html
    from scheduler.prints import calendar_print_html
    from scheduler.views import weeks_print_html
    s, e = date(2026, 11, 1), date(2026, 11, 14)
    r = solve(simulate(4, 2, 2, seed=3, start=s, end=e), s, e, time_limit_s=10)
    a = r.assignments
    for page in (paper_weeks_html(a, s, e), calendar_print_html(a, s, e), weeks_print_html(a, s, e)):
        assert 'onclick="window.print()"' in page and "@media print { .cs-print { display: none" in page


def test_merge_import_keeps_teams_and_replaces_no_dates():
    from scheduler.data import merge_import
    old = _inp([{"team": "14-A", "age": 14, "head_coach": "Ann"}, {"team": "16-B", "age": 16, "head_coach": "Bo"}],
               availability=[{"who": "Ann", "type": "No", "days": "Tue"},                  # standing: replaced
                             {"who": "Ann", "type": "No", "dates": "11/12/2026"},          # last period: gone
                             {"who": "Ann", "type": "No", "dates": "12/24/2026"},          # upcoming: kept
                             {"who": "Bo", "type": "No", "dates": "11/20/2026"},           # last period: gone
                             {"who": "Bo", "type": "No", "days": "Fri"}],                  # not in file: kept
               wishlist=[{"team": "14-A", "choice": 1, "days": "Mon"}, {"team": "16-B", "choice": 1, "days": "Wed"}],
               weekends=[{"date": "11/7/2026"}])
    new = _inp([{"team": "14-A", "age": 14, "head_coach": "Someone Else"}, {"team": "15-C", "age": 15}],
               availability=[{"who": "Ann", "type": "No", "days": "Thu"}],
               wishlist=[{"team": "14-A", "choice": 1, "days": "Tue"}],
               weekends=[{"date": "12/12/2026"}])
    notes = merge_import(new, old, date(2026, 12, 5))
    assert list(new.teams["team"]) == ["14-A", "16-B", "15-C"]
    assert new.teams.iloc[0]["head_coach"] == "Ann"                    # the club's team wasn't overwritten
    av = {(r.who, r.days or r.dates) for r in new.availability.fillna("").itertuples()}
    assert av == {("Ann", "Thu"), ("Ann", "12/24/2026"), ("Bo", "Fri")}
    assert {(r.team, r.days) for r in new.wishlist.itertuples()} == {("14-A", "Tue"), ("16-B", "Wed")}
    assert set(new.weekends["date"]) == {"11/7/2026", "12/12/2026"}
    assert any("added 1 new team" in n for n in notes)


def test_break_days_run_in_blocks_with_a_morning_camp():
    """Dec 21 - Jan 2 opened as block days: weekday practices go in 2-hour
    blocks, a 9-11 AM camp blocks its courts, closures stay closed."""
    s, e = date(2026, 12, 20), date(2027, 1, 2)
    inp = simulate(8, 4, 3, seed=7, start=s, end=e)
    inp.weekends = pd.concat([inp.weekends, pd.DataFrame([{"date": "12/21/2026-1/2/2027", "open": "9AM-9PM"}])],
                             ignore_index=True)
    inp.locals = pd.concat([inp.locals, pd.DataFrame([{
        "program": "Winter Camp", "side": "Girls", "days": "Mon-Fri, Weekdays", "time": "9AM-11AM", "courts": "1-4",
        "on_site": "Yes", "dates": "12/21/2026-1/2/2027"}])], ignore_index=True)
    # The weeks are all open days: nothing's scheduled on its own, so coaches add their practices.
    team = inp.teams["team"].iloc[0]
    inp.overrides = pd.DataFrame([{"team": team, "action": "Extra", "date": "12/22/2026", "days": "",
                                   "time": "11AM", "court": "", "note": ""},
                                  {"team": team, "action": "Extra", "date": "12/29/2026", "days": "",
                                   "time": "9AM", "court": "", "note": ""}])
    r = solve(inp, s, e, time_limit_s=20)
    _check_hard_rules(inp, r)
    a = r.assignments
    pr = a[a["kind"] == "Practice"]
    wk = pr[pr["date"].map(lambda d: d.weekday() < 5)]
    assert len(wk) and set(wk["slot"]) <= {"9AM", "11AM", "1PM", "3PM", "5PM", "7PM"}
    assert set(pr["name"]) == {team}
    assert not (pr["date"] == date(2026, 12, 25)).any()
    camp = a[a["name"] == "Winter Camp"]
    assert set(camp["slot"]) == {"9AM"} and date(2026, 12, 21) in set(camp["date"])
    nine = pr[(pr["slot"] == "9AM") & pr["date"].isin(set(camp["date"]))]
    assert len(nine) and not set(nine["court"]) & {1, 2, 3, 4}
    assert r.metrics["missed_practices"] == 0


def test_flags_coach_at_two_travel_tournaments_the_same_weekend():
    teams = [{"team": "15 Black", "club": "Boys", "age": 15, "head_coach": "Al"},
             {"team": "16-Bea", "age": 16, "assistants": "Al"},
             {"team": "14-Cy", "age": 14}]
    inp = _with_tournaments(_inp(teams), [
        {"name": "Vegas Classic", "type": "Travel", "start_date": "11/14/2026", "end_date": "11/15/2026",
         "teams": "15 Black"},
        {"name": "Desert Duel", "type": "Travel", "start_date": "11/15/2026", "end_date": "11/16/2026",
         "teams": "16-Bea, 14-Cy"},
        {"name": "Later Cup", "type": "Travel", "start_date": "11/21/2026", "teams": "16-Bea"}])
    data = clean(inp, *NOV)
    conf = tournament_conflicts(data.teams, data.tournaments)
    assert len(conf) == 1
    c = conf[0]
    assert c["kind"] == "two_travel" and c["coach"] == "Al" and c["start"] == c["end"] == date(2026, 11, 15)
    assert {c["travel"], c["travel2"]} == {"Vegas Classic", "Desert Duel"}
    assert c["teams"] == ["16-Bea"] and c["teams2"] == ["15 Black"]      # sorted by tournament name
    assert c["id"] == tournament_conflicts(data.teams, data.tournaments)[0]["id"]   # stable


def test_hand_practice_in_other_dates_doesnt_block_this_run():
    teams = [{"team": "16-Bea", "age": 16}]
    inp = _inp(teams)
    inp.overrides = pd.DataFrame([{"team": "16-Bea", "action": "Add", "date": "12/08/2026", "days": "",
                                   "time": "5:30", "court": "", "note": ""}], columns=COLUMNS["Overrides"])
    r = solve(inp, *NOV, time_limit_s=10)
    assert r.ok
    dec = solve(inp, date(2026, 12, 1), date(2026, 12, 20), time_limit_s=10)
    pr = dec.assignments[dec.assignments["kind"] == "Practice"]
    assert ((pr["date"] == date(2026, 12, 8)) & (pr["slot"] == "5:30")).any()


def test_open_weeks_get_no_automatic_practices():
    """A week whose weekdays are all open days (a school break) gets no practices from the solver
    and no shortfall; a practice added by hand still goes in."""
    from datetime import date

    import pandas as pd

    from scheduler import simulate, solve
    from scheduler.solver import week_start
    inp = simulate(3, 2, 1, seed=3, start=date(2026, 12, 6), end=date(2027, 1, 9))
    inp.weekends = pd.concat([inp.weekends, pd.DataFrame([{
        "date": "12/21/2026-01/02/2027", "open": "9AM-9PM", "courts": "", "group": "", "required": "",
        "note": "Winter break"}])], ignore_index=True)
    team = inp.teams["team"].iloc[0]
    inp.overrides = pd.DataFrame([{"team": team, "action": "Extra", "date": "12/22/2026", "days": "",
                                   "time": "11AM", "court": "", "note": "coach added"}])
    r = solve(inp, date(2026, 12, 6), date(2027, 1, 9), time_limit_s=20)
    assert r.ok
    p = r.assignments[r.assignments["kind"] == "Practice"]
    brk = p[p["date"].map(week_start).isin([date(2026, 12, 20), date(2026, 12, 27)])]
    assert list(zip(brk["name"], brk["date"])) == [(team, date(2026, 12, 22))]
    assert (p["date"].map(week_start) == date(2026, 12, 13)).sum() > 0
    assert r.shortfalls.empty or not r.shortfalls["week_of"].isin([date(2026, 12, 20), date(2026, 12, 27)]).any()


def test_day_ranges():
    from scheduler.data import parse_days
    assert parse_days("Mon-Fri") == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert parse_days("Tue - Thu") == ["Tue", "Wed", "Thu"]
    assert parse_days("Tuesday, Thursday") == ["Tue", "Thu"]


def test_form_names_match_the_club():
    import pandas as pd

    from scheduler.data import NameMatcher
    teams = pd.DataFrame([
        {"team": "15-Drea", "age": 15, "head_coach": "Drea", "assistants": "", "rank": "15-1"},
        {"team": "16-Hannah", "age": 16, "head_coach": "Hannah", "assistants": "", "rank": "16-4"},
        {"team": "14 Black", "age": 14, "head_coach": "Matthew Mitchell", "assistants": "", "rank": "14-1"},
        {"team": "16-D&D", "age": 16, "head_coach": "Danielle; Darci", "assistants": "", "rank": "16-2"},
        {"team": "12-Madi", "age": 12, "head_coach": "Madi Ines", "assistants": "", "rank": "12-1"}])
    m = NameMatcher(teams, pd.DataFrame([{"coach": "Ari", "email": "ari@sdvbc.org"}]))
    assert m.team("15-1 Drea") == "15-Drea" and m.team("16-4 hannah") == "16-Hannah"
    assert m.team("14-Black") == "14 Black" and m.team("16 D&D") == "16-D&D" and m.team("13-Zed") is None
    assert m.coach("Andrea Richardson", "drea@sdvbc.org") == "Drea"
    assert m.coach("Matthew Mitchell") == "Matthew Mitchell" and m.coach("Madison-Leigh Ines") == "Madi Ines"
    assert m.coach("Arianna Donnelly", "ari@sdvbc.org") == "Ari"
    assert m.coach("Someone New") is None



def test_weekends_and_holidays_open_by_default():
    """With nothing listed, weekends are open 9 AM-9 PM on every court and so are the big holidays
    (Nov 2026: open days in blocks, no locals programs); weekdays still come first. Closing a holiday
    on the Closures list closes it; the old holidays_closed rule still works."""
    teams = [{"team": f"16-T{i}", "age": 16} for i in range(4)]
    loc = [{"program": "Thu League", "days": "Thu", "time": "5:30", "courts": "1", "coaches": "Pat",
            "dates": "11/1/2026-1/9/2027"}]
    inp = _inp(teams, locals_=loc)
    inp.weekends_open, inp.holidays_open = True, True
    data = clean(inp, date(2026, 11, 1), date(2027, 1, 9))
    assert date(2026, 12, 19) in data.weekends and data.weekends[date(2026, 12, 19)].blocks[0] == "9AM"
    tg = date(2026, 11, 26)
    assert tg not in data.closures and date(2026, 12, 25) not in data.closures
    assert tg in data.weekends and data.weekends[tg].note == "Thanksgiving"
    assert data.weekends[tg].courts == [1, 2, 3, 4, 5, 6, 7]
    lp = data.locals[0]
    assert lp.runs_on(date(2026, 11, 19)) and not lp.runs_on(tg)     # no locals on a holiday
    r = solve(inp, *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert not pr["date"].map(lambda d: d.weekday() >= 5).any()       # open weekdays win
    lo = r.assignments[r.assignments["kind"] == "Local"]
    assert not (lo["date"] == tg).any()
    # The club can still close one.
    inp.closures = pd.DataFrame([{"date": "11/26/2026", "reason": "Closed for Thanksgiving"}])
    assert clean(inp, *NOV).closures[tg] == "Closed for Thanksgiving"
    inp.holidays_closed = True
    assert date(2026, 12, 25) in clean(inp, date(2026, 11, 1), date(2027, 1, 9)).closures



def test_weekend_wishlist_counts_on_weekends():
    """A team that asks for Sunday mornings gets a Sunday morning when it has to practice on a weekend."""
    teams = [{"team": "16-A", "age": 16, "practices": 1}, {"team": "16-B", "age": 16, "practices": 1}]
    no = [{"who": t, "type": "No", "days": "Mon-Fri"} for t in ("16-A", "16-B")]
    wish = [{"team": t, "choice": 1, "days": "Sun", "times": "9AM, 11AM"} for t in ("16-A", "16-B")]
    inp = _inp(teams, no, wish, weekends=[{"date": "11/01/2026", "open": "9AM-9PM"},
                                          {"date": "11/07/2026", "open": "9AM-9PM"}])
    r = solve(inp, date(2026, 11, 1), date(2026, 11, 7), time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert len(pr) == 2 and set(pr["date"]) == {date(2026, 11, 1)} and set(pr["slot"]) <= {"9AM", "11AM"}


def test_reimporting_the_same_file_replaces_instead_of_stacking():
    from scheduler.data import merge_import
    teams = [{"team": "16-D&D", "age": 16, "head_coach": "Danielle", "assistants": "Darci"}]
    club = _inp(teams, [{"who": "Darci", "type": "Not preferred", "dates": "12/19/2026-12/20/2026"},
                        {"who": "Darci", "type": "No", "dates": "12/16/2026", "note": float("nan")}])
    file = _inp(teams, [{"who": "Darci", "type": "No", "dates": "12/19/2026-12/20/2026"},
                        {"who": "Darci", "type": "No", "dates": "12/16/2026", "note": ""}])
    merge_import(file, club, date(2026, 12, 6))
    av = file.availability
    assert len(av) == 2 and set(av["type"]) == {"No"}


def test_makeup_adds_a_practice_that_week():
    teams = [{"team": f"16-T{i}", "age": 16} for i in range(3)]
    inp = _inp(teams)
    inp.overrides = pd.DataFrame([{"team": "16-T0", "action": "Makeup", "date": "11/03/2026", "days": "", "time": "",
                                   "court": "", "note": "short last week"}])
    r = solve(inp, *NOV, time_limit_s=10)
    pc = r.practice_counts.set_index(["team", "week_of"])
    assert pc.loc[("16-T0", date(2026, 11, 1)), "scheduled"] == 3
    assert pc.loc[("16-T0", date(2026, 11, 8)), "scheduled"] == 2
    assert pc.loc[("16-T1", date(2026, 11, 1)), "scheduled"] == 2


def test_stated_rather_not_outranks_a_time_nobody_mentioned():
    """A coach's 'rather not' time costs more than another team's time it just didn't pick; a young team
    moved to an unasked 3:45 costs more than a 15s team moved to an unasked 7:20."""
    from scheduler.config import WEIGHTS, slot_cost
    rather_not = WEIGHTS["not_preferred"] + WEIGHTS["slot_fit"] * slot_cost(14, "7:20") + WEIGHTS["late_exception"]
    unasked_345 = WEIGHTS["wish_time_miss"] + WEIGHTS["slot_fit"] * slot_cost(13, "3:45") + WEIGHTS["unasked_345"]
    assert rather_not > unasked_345 > WEIGHTS["wish_time_miss"]
    fifteen_720 = WEIGHTS["wish_time_miss"] + WEIGHTS["slot_fit"] * slot_cost(15, "7:20")
    assert unasked_345 > fifteen_720 + 50
    assert WEIGHTS["not_preferred"] < WEIGHTS["wish_day_miss"]   # still below a day they didn't pick


def test_team_dates_print_one_portrait_page_with_circled_tournaments():
    from scheduler.prints import team_dates_print_html
    s, e = date(2026, 12, 5), date(2026, 12, 20)
    inp = simulate(6, 4, 4, seed=7, start=s, end=e)
    r = solve(inp, s, e, time_limit_s=10)
    data = clean(inp, s, e)
    from scheduler.config import team_is_boys
    order = [t.name for t in sorted(data.teams, key=lambda t: team_is_boys(t.club, t.name))]
    sec = {t.name: "Boys" if team_is_boys(t.club, t.name) else "Girls" for t in data.teams}
    coaches = {t.name: t.staff for t in data.teams}
    h = team_dates_print_html(r, data.closures, order, "Dec", sec, coaches)
    assert "size: portrait" in h and h.count('class="pp-page"') == 1
    assert "SDVBC" in h and "Seaside" in h
    assert h.index(order[0]) < h.index(order[-1])                          # in the given order
    travel = [tn for tn in r.tournaments if tn.kind == "travel"]
    # one circle per team per run of tournament days
    assert h.count('class="pt-tn') == sum(len(tn.teams) for tn in travel)
    t0 = order[0]
    assert coaches[t0][0] in h


def test_flags_print_lists_open_then_covered():
    from scheduler.prints import flags_print_html
    flags = [{"id": "a", "when": "Sat Dec 12", "text": "**Kai** is on **X** and **Y**", "tag": "Two programs"},
             {"id": "b", "when": "Sun Dec 13", "text": "**Uma** has two travel tournaments", "tag": "Two travel"}]
    h = flags_print_html(flags, {"b": {"note": "Cliff subs", "who": "Matt", "at": "10/30/2026"}},
                         date(2026, 12, 1), date(2026, 12, 31))
    assert "size: portrait" in h and "<b>Kai</b>" in h
    assert h.index("Needs coverage (1)") < h.index("Already covered (1)") and "Cliff subs" in h


def test_color_calendar_one_week_a_page():
    from scheduler.prints import calendar_pretty_html
    s, e = date(2026, 12, 1), date(2026, 12, 31)
    inp = simulate(4, 2, 2, seed=3, start=s, end=e)
    r = solve(inp, s, e, time_limit_s=10)
    data = clean(inp, s, e)
    h = calendar_pretty_html(r.assignments, s, e, data.closures, None, True, r.weekend_dates, r.tournaments)
    assert h.count('class="pp-page"') == 5 and "size: landscape" in h        # Nov 29 – Jan 2 weeks
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert h.count('class="pw-c') >= len(pr) and "Christmas" in h
    t = pr.iloc[0]
    from scheduler.labels import short_label
    assert short_label(t["name"], t["club"]) in h and str(t["coach"]).split(";")[0].strip() in h


def test_paper_tournaments_on_first_page_and_locals_share_a_line():
    from scheduler.data import Tournament
    from scheduler.paper import paper_weeks_html
    s, e = date(2026, 12, 6), date(2026, 12, 12)
    t = [Tournament("Boys Local", "local", e, e, [], [1, 2], [], side="Boys"),
         Tournament("Girls Local", "local", e, e, [], [5], [], side="Girls"),
         Tournament("Far Away Cup", "travel", e, e, ["15 Black"], [], [])]
    h = paper_weeks_html(pd.DataFrame(columns=["date", "court", "slot", "kind", "name"]), s, e, {}, t)
    first, second = h.split('class="cs-week"')[1:3]
    only = '<span class="last">1</span><span class="last">ONLY</span>'   # each is its program's only one
    assert f'<span class="note">Boys Local{only} / Girls Local{only}</span>' in first
    assert "Far Away Cup (travel)" in first
    assert 'class="note"' not in second and "Girls Local" in second      # still on the court it uses


def test_paper_marks_a_programs_only_local_tournament():
    """Nov 2026: a local tournament that's its program's one and only gets a circled 1 and a circled
    ONLY on the paper sheet (by program; a 2-day tournament still counts once). Travel ones never do."""
    from scheduler.data import Tournament
    from scheduler.paper import ONLY_MARK, only_tournaments, paper_weeks_html
    s, e = date(2026, 12, 6), date(2026, 12, 12)
    g14 = Tournament("G 14 L", "local", e, e, [], [1, 2], [], program="G 14 L")
    b14a = Tournament("Boys 14s", "local", date(2026, 11, 7), date(2026, 11, 7), [], [1], [], program="B 14 L")
    b14b = Tournament("Boys 14s", "local", date(2026, 12, 7), date(2026, 12, 7), [], [3], [], program="B 14 L")
    two_day = Tournament("G HS", "local", date(2026, 12, 11), e, [], [5], [], program="G HS L")
    away = Tournament("Far Away Cup", "travel", e, e, ["15 Black"], [], [])
    t = [g14, b14a, b14b, two_day, away]
    assert only_tournaments(t) == {id(g14), id(two_day)}
    h = paper_weeks_html(pd.DataFrame(columns=["date", "court", "slot", "kind", "name"]), s, e, {}, t)
    assert f"<b>G 14 L{ONLY_MARK}</b>" in h and f"<b>G HS{ONLY_MARK}</b>" in h
    assert "<b>Boys 14s</b>" in h and f"Boys 14s{ONLY_MARK}" not in h
    assert f"Far Away Cup{ONLY_MARK}" not in h


def test_fill_530_before_720():
    """Four 16s teams, all asking for 7:20: with 5:30 courts empty, they practice at 5:30 (Nov 2026)."""
    teams = [{"team": f"16-F{i}", "age": 16, "head_coach": f"F{i}"} for i in range(4)]
    wish = [{"team": t["team"], "choice": 1, "days": "Tue, Thu", "times": "7:20"} for t in teams]
    r = solve(_inp(teams, wishlist=wish), date(2026, 11, 8), date(2026, 11, 14), time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    for d, g in pr.groupby("date"):
        n530, n720 = (g["slot"] == "5:30").sum(), (g["slot"] == "7:20").sum()
        assert n720 == 0 or n530 >= 7, (d, n530, n720)


def test_short_labels_for_print():
    from scheduler.labels import label_map
    got = label_map([("14-Connor", "SDVBC"), ("14-Cara", "SDVBC"), ("16-Edgar", "SDVBC"),
                     ("14 Black", "Seaside"), ("16 Green", "Seaside"), ("16-D&D", "SDVBC"),
                     ("15-Tyler", "SDVBC"), ("15-Tom", "SDVBC")])
    assert got["14-Connor"] == "14-C" and got["14-Cara"] == "14-Ca" and got["16-Edgar"] == "16-E"
    assert got["14 Black"] == "S14-B" and got["16 Green"] == "S16-G" and got["16-D&D"] == "16-D&D"
    assert got["15-Tyler"] != got["15-Tom"]


def test_prints_use_short_labels_online_does_not():
    from scheduler.paper import paper_weeks_html
    from scheduler.prints import calendar_print_html
    from scheduler.views import court_grid_html, week_dates
    a = pd.DataFrame([{"date": date(2026, 11, 10), "day": "Tue", "slot": "5:30", "slot_label": "5:30–7:20",
                       "court": 1, "kind": "Practice", "name": "14-Connor", "club": "SDVBC", "age": 14,
                       "coach": "Connor", "head": "Connor", "on_pattern": True},
                      {"date": date(2026, 11, 10), "day": "Tue", "slot": "7:20", "slot_label": "7:20–9:10",
                       "court": 1, "kind": "Practice", "name": "14 Black", "club": "Seaside", "age": 14,
                       "coach": "Matthew", "head": "Matthew", "on_pattern": True}])
    start, end = date(2026, 11, 8), date(2026, 11, 14)
    for h in (paper_weeks_html(a, start, end), calendar_print_html(a, start, end)):
        assert "14-C" in h and "S14-B" in h and "14-Connor" not in h
    online = court_grid_html(a, week_dates(start, start, end, []))
    assert "14-Connor" in online and "S14-B" not in online


@pytest.mark.usefixtures("alone_ok")
def test_never_practice_the_day_after_a_travel_tournament():
    """The day after a tournament is never used, even if it means a short week (Nov 2026)."""
    teams = [{"team": "16-Ana", "age": 16, "head_coach": "Ana"}]
    # Week of 11/15: only Mon 11/16 (day after Far West) and Tue 11/17 are open.
    avail = [{"who": "16-Ana", "type": "No", "dates": "11/18-11/20"}]
    inp = _with_tournaments(_inp(teams, avail), [{"name": "Far West", "type": "Travel", "start_date": "11/14/2026",
                                                  "end_date": "11/15/2026", "teams": "16-Ana"}])
    r = solve(inp, *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert date(2026, 11, 16) not in set(pr["date"])
    op = [o for o in r.openings if o["team"] == "16-Ana" and o["week_of"] == date(2026, 11, 15)]
    assert not op or all(x["date"] != date(2026, 11, 16) for x in op[0]["options"])


# ---------------------------------------------------------------- Nov 3, 2026 club changes --

def test_court_rank_order():
    from scheduler.config import COURT_COST, COURT_NEIGHBORS, COURT_RANK, PECKING_COURT_COST
    assert COURT_RANK == [1, 7, 3, 6, 5, 4, 2]                     # Nov 8, 2026
    assert [PECKING_COURT_COST[c] for c in COURT_RANK] == sorted(PECKING_COURT_COST.values())
    assert len(set(PECKING_COURT_COST.values())) == 7
    assert COURT_COST[1] == COURT_COST[7] < COURT_COST[3] <= COURT_COST[2] and max(COURT_COST.values()) == COURT_COST[2]
    assert {frozenset(e) for e in COURT_NEIGHBORS} == {frozenset(e) for e in
                                                       [(1, 7), (1, 5), (7, 6), (6, 5), (5, 4), (4, 3), (3, 2)]}


def _courts(n_teams, weights, ages=None, boys=None, nets=None, later=None):
    """Stage 2 alone on one day: teams 0..n-1 at 5:30 (and `later` at 7:20)."""
    from scheduler.solver import _assign_courts
    d = date(2026, 11, 16)
    booked = {(t, d, "5:30") for t in range(n_teams)}
    T = list(range(n_teams + len(later or {})))
    for t in (later or {}):
        booked.add((t, d, "7:20"))
    _, court_of, _ = _assign_courts(booked, T, lambda d_, s_: [1, 2, 3, 4, 5, 6, 7], weights, time_limit_s=5,
                                    net_of=nets or {t: 1 for t in T}, ages=ages or {t: 16 for t in T},
                                    boys_of=boys or {t: False for t in T})
    return court_of, d


def test_courts_follow_the_rank_1_7_3_6_5_4_2():
    w = {t: 20 - 3 * t for t in range(7)}          # team 0 top of the pecking order
    court_of, d = _courts(7, w)
    assert [court_of[t, d, "5:30"] for t in range(7)] == [1, 7, 3, 6, 5, 4, 2]


def test_same_side_and_age_on_neighboring_courts():
    """Boys and girls teams close in the pecking order: each side sits together on courts next to
    each other (1-7-6 / 5-4-3), rather than alternating down the rank."""
    w = {0: 20, 1: 19, 2: 18, 3: 17, 4: 16, 5: 15}
    boys = {0: True, 1: False, 2: True, 3: False, 4: True, 5: False}
    court_of, d = _courts(6, w, boys=boys)
    c = {t: court_of[t, d, "5:30"] for t in range(6)}
    from scheduler.config import COURT_NEIGHBORS
    nb = {frozenset(e) for e in COURT_NEIGHBORS}
    mixed = sum(1 for a in range(6) for b in range(a + 1, 6)
                if frozenset((c[a], c[b])) in nb and boys[a] != boys[b])
    assert c[0] == 1 and c[1] == 7 and 2 not in c.values()   # Courts 1 and 7 locked by rank
    # The middle tier groups the sides: the least possible with a boys team on 1 and girls on 7.
    best = min(sum(1 for e in nb if len(e) == 2 and all(x in arr for x in e)
                   and len({arr[x] for x in e}) == 2)
               for arr in ({1: True, 7: False} | dict(zip(p, sd))
                           for p in itertools.permutations([3, 6, 5, 4])
                           for sd in [(True, False, True, False)]))
    assert mixed == best, c


def test_nets_go_up_not_down_and_ranks_beat_same_height():
    """Court rank beats keeping the same net (a raise is fine); a net never comes down."""
    from scheduler.config import NET_MEN, NET_WOMEN
    # 5:30: a girls team (women's) at the top of the order and a low HS boys team (men's).
    # 7:20: a top HS boys team. Rank: it takes Court 1 even though the girls were there (raise: fine).
    w = {0: 20, 1: 4, 2: 20}
    nets = {0: NET_WOMEN, 1: NET_MEN, 2: NET_MEN}
    court_of, d = _courts(2, w, nets=nets, later={2: True})
    assert court_of[0, d, "5:30"] == 1 and court_of[2, d, "7:20"] == 1
    # 5:30: HS boys teams on the best courts; 7:20: the top girls team. It doesn't take a boys
    # court (that would lower the net), even though Courts 1 and 7 are better.
    w = {0: 20, 1: 19, 2: 2, 3: 15}
    nets = {0: NET_MEN, 1: NET_MEN, 2: NET_WOMEN, 3: NET_WOMEN}
    court_of, d = _courts(3, w, nets=nets, later={3: True})
    assert {court_of[0, d, "5:30"], court_of[1, d, "5:30"]} == {1, 7}
    assert court_of[3, d, "7:20"] not in (1, 7)


def test_coach_moves_courts_before_a_top_team_drops_a_court():
    """Nov 8, 2026: rather a coach move courts than 18 Black end up on Court 7 (Courts 1 and 7 are
    locked by rank)."""
    from scheduler.solver import _assign_courts
    d = date(2026, 11, 16)
    # 5:30: 18-girls (top girls) and the coach's low girls team; 7:20: 18 Black and 17 Black.
    booked = {(3, d, "5:30"), (2, d, "5:30"), (0, d, "7:20"), (1, d, "7:20")}
    w = {0: 20, 1: 19, 2: 5, 3: 18}
    links = [((2, d, "5:30"), (0, d, "7:20"))]          # same coach, back to back
    _, court_of, _ = _assign_courts(booked, [0, 1, 2, 3], lambda d_, s_: [1, 2, 3, 4, 5, 6, 7], w,
                                    time_limit_s=5, net_of={0: 2, 1: 2, 2: 1, 3: 1},
                                    ages={0: 18, 1: 17, 2: 14, 3: 18}, boys_of={0: True, 1: True, 2: False, 3: False},
                                    coach_links=links)
    assert court_of[0, d, "7:20"] == 1 and court_of[1, d, "7:20"] == 7
    assert court_of[2, d, "5:30"] != 1


def test_coach_stays_put_inside_the_middle_tier():
    """Nov 8, 2026: 14-2 and 14-3 next to each other in the order; 14-3's coach had 12s on Court 5
    at 3:45, so 14-3 takes Court 5 and 14-2 moves to Court 4. A big gap still beats the coach, and
    Courts 1 and 7 stay by rank."""
    from scheduler.solver import _assign_courts
    d = date(2026, 11, 16)
    # 5:30: four teams above (0-3), 14-2 (4) and 14-3 (5); the coach's 12s (6) at 3:45.
    w = {0: 20, 1: 19, 2: 18, 3: 17, 4: 9, 5: 8, 6: 3}
    booked = {(t, d, "5:30") for t in range(6)} | {(6, d, "3:45")}
    links = [((6, d, "3:45"), (5, d, "5:30"))]
    base = dict(time_limit_s=5, net_of={t: 1 for t in range(7)}, ages={t: 14 for t in range(7)},
                boys_of={t: False for t in range(7)}, coach_links=links)

    def run(c12):
        on = lambda d_, s_: [c12] if s_ == "3:45" else [1, 2, 3, 4, 5, 6, 7]  # noqa: E731
        _, c, _ = _assign_courts(booked, list(range(7)), on, w, **base)
        return {t: c[t, d, "5:30"] for t in range(6)}

    c = run(5)
    assert c[5] == 5 and c[4] == 4                     # coach stays; 14-2 one court down
    assert c[0] == 1 and c[1] == 7 and c[2] == 3 and c[3] == 6
    c = run(3)
    assert c[2] == 3 and c[5] != 3                     # big gap (18s vs 14-3): rank wins
    c = run(1)
    assert c[0] == 1                                   # Court 1 locked by rank


@pytest.mark.usefixtures("alone_ok")
def test_coach_girls_team_before_boys_team():
    """Nov 8, 2026: a coach with a girls and a boys team: on a day both practice, girls first."""
    teams = [{"team": "16-Leo", "club": "Girls", "age": 16, "head_coach": "Leo"},
             {"team": "16 Black", "club": "Boys", "age": 16, "head_coach": "Leo"}]
    wish = [{"team": "16 Black", "choice": 1, "days": "Mon, Wed", "times": "5:30, 7:20"},
            {"team": "16-Leo", "choice": 1, "days": "Mon, Wed", "times": "5:30, 7:20"}]
    r = solve(_inp(teams, wishlist=wish), *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    shared = 0
    for _, g in pr.groupby("date"):
        if set(g["name"]) == {"16-Leo", "16 Black"}:
            shared += 1
            sl = dict(zip(g["name"], g["slot"]))
            assert sl["16-Leo"] == "5:30" and sl["16 Black"] == "7:20", g
    assert shared
    # Not counted when the ages make it impossible (18s girls never 3:45, 13s boys never 7:20).
    from scheduler.solver import solve as _s  # noqa: F401
    teams = [{"team": "18-Wes", "club": "Girls", "age": 18, "head_coach": "Wes"},
             {"team": "13 Black", "club": "Boys", "age": 13, "head_coach": "Wes"}]
    wish = [{"team": "13 Black", "choice": 1, "days": "Mon, Wed", "times": "5:30"},
            {"team": "18-Wes", "choice": 1, "days": "Mon, Wed", "times": "7:20"}]
    r = solve(_inp(teams, wishlist=wish), *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert (pr["date"].map(lambda d: d.weekday()) .isin([0, 2])).mean() > 0.9   # still their asked-for days


def test_net_heights_and_order():
    from scheduler.config import NET_12S, NET_MEN, NET_WOMEN, net_height
    assert net_height("SDVBC", 12, "12-Kai") == NET_12S and net_height("Seaside", 12, "12 Black") == NET_12S
    assert net_height("SDVBC", 16, "16-Edgar") == NET_WOMEN and net_height("Seaside", 14, "14 Black") == NET_WOMEN
    assert net_height("Seaside", 15, "15 Black") == NET_MEN
    assert NET_12S < NET_WOMEN < NET_MEN                   # a net goes up through the day


def test_friday_720_avoided():
    teams = [{"team": f"{a}-G{i}", "club": "Girls", "age": a, "head_coach": f"C{a}{i}"}
             for a in (16, 17, 18) for i in range(3)]
    wish = [{"team": t["team"], "choice": 1, "days": "Fri", "times": "7:20"} for t in teams]
    r = solve(_inp(teams, wishlist=wish), *NOV, time_limit_s=15)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert not len(pr[(pr["day"] == "Fri") & (pr["slot"] == "7:20")])   # even when they ask for it


@pytest.mark.usefixtures("alone_ok")
def test_no_late_practice_night_before_a_coachs_tournament():
    # Ana coaches 16-Ana (at a travel tournament Sat 11/14) and 18-Ana (not going): 18-Ana
    # doesn't practice late on Fri 11/13, even though it asked for Fridays at 7:20.
    teams = [{"team": "16-Ana", "club": "Girls", "age": 16, "head_coach": "Ana"},
             {"team": "18-Ana", "club": "Girls", "age": 18, "head_coach": "Ana"}]
    wish = [{"team": "18-Ana", "choice": 1, "days": "Thu, Fri", "times": "7:20"}]
    inp = _inp(teams, wishlist=wish)
    inp.tournaments = pd.DataFrame([{"name": "Turkey Bash", "type": "Travel", "start_date": "11/14/2026",
                                     "end_date": "11/15/2026", "teams": "16-Ana"}])
    r = solve(inp, *NOV, time_limit_s=15)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    eve = pr[(pr["date"] == date(2026, 11, 13)) & (pr["name"] == "18-Ana")]
    assert not len(eve[eve["slot"] == "7:20"])


def test_boys_take_the_later_slot_when_tied():
    teams = [{"team": "16-Gina", "club": "Girls", "age": 16, "head_coach": "Gina"},
             {"team": "16 Black", "club": "Boys", "age": 16, "head_coach": "Bo"}]
    locals_ = [{"program": "Big Clinic", "days": "Mon, Tue, Wed, Thu, Fri", "time": "5:30", "courts": "1-6"},
               {"program": "Big Clinic", "days": "Mon, Tue, Wed, Thu, Fri", "time": "7:20", "courts": "1-6"}]
    wish = [{"team": t["team"], "choice": 1, "days": "Mon, Wed"} for t in teams]
    r = solve(_inp(teams, wishlist=wish, locals_=locals_), *NOV, time_limit_s=15)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    # one court a slot and the same days: one goes early and one late
    assert set(pr[pr["name"] == "16 Black"]["slot"]) == {"7:20"}
    assert set(pr[pr["name"] == "16-Gina"]["slot"]) == {"5:30"}


def test_team_dates_print_week_before_and_next_tournament():
    from copy import copy

    from scheduler.config import team_is_boys
    from scheduler.data import Tournament
    from scheduler.prints import team_dates_print_html
    s, e = date(2026, 11, 29), date(2026, 12, 19)
    inp = simulate(6, 4, 4, seed=7, start=s, end=e)
    r = solve(inp, s, e, time_limit_s=10)
    data = clean(inp, s, e)
    a = r.assignments
    first = date(2026, 12, 6)
    win = copy(r)
    win.start, win.assignments = first, a[a["date"] >= first]
    going = [t.name for t in data.teams][:2]
    win.tournaments = list(r.tournaments) + [Tournament("Jan Classic", "travel", date(2027, 1, 9),
                                                        date(2027, 1, 10), going, [], [])]
    order = [t.name for t in sorted(data.teams, key=lambda t: team_is_boys(t.club, t.name))]
    h = team_dates_print_html(win, data.closures, order, "Dec", None, None, before=a[a["date"] < first])
    assert "Week before" in h and "pt-prev" in h
    # One column for the week before, each team's dates there as a comma list (Nov 2026).
    import re
    assert h.count('<th class="pt-prev pt-wb">Week before</th>') == 1
    t0 = going[0]
    days0 = sorted({d for d, n, k in zip(a["date"], a["name"], a["kind"]) if n == t0 and d < first and k == "Practice"})
    wb = re.findall(r'<td class="pt-prev pt-wb">([^<]*)</td>', h)
    assert ", ".join(str(d.day) for d in days0) in wb
    assert not re.search(r'<th class="pt-prev">\d+</th>', h)  # no per-day columns for it any more
    assert h.count('class="pt-tn pt-next"') == len(going) and "Jan 9–10" in h
    assert h.count('class="pp-page"') == 1


def test_coach_schedule_flags():
    from scheduler.coach_load import bad_coaches, coach_flags
    from scheduler.data import Tournament
    d = date(2026, 11, 12)  # Thursday

    def row(dd, slot, kind, name, court, coach):
        return {"date": dd, "slot": slot, "kind": kind, "name": name, "court": court, "coach": coach}
    a = pd.DataFrame([
        row(d, "3:45", "Local", "Girls 12 League", 3, "Ana"),           # break, then 7:20
        row(d, "7:20", "Practice", "18-Ana", 1, "Ana"),                 # late, tournament tomorrow
        row(d + timedelta(days=1), "5:30", "Practice", "16-Ana", 4, "Ana"),
        row(d + timedelta(days=1), "7:20", "Practice", "18-Ana", 7, "Ana"),  # Fri 7:20, court change
        row(d, "5:30", "Practice", "16-Bea", 2, "Bea"),
    ])
    tn = [Tournament("Turkey Bash", "travel", d + timedelta(days=1), d + timedelta(days=2), ["16-Ana"], [], [])]
    f = coach_flags(a, tn)
    issues = set(f[f["coach"] == "Ana"]["issue"])
    assert {"Break between sessions", "Late the night before a tournament", "Friday 7:20",
            "Changes courts"} <= issues
    assert not len(f[f["coach"] == "Bea"])
    assert list(bad_coaches(f)["coach"]) == ["Ana"]


def test_team_back_to_back_days_flagged():
    from scheduler.coach_load import back_to_back_days, team_back_to_back
    from scheduler.views import court_grid_html
    d = date(2026, 11, 10)  # Tuesday
    a = pd.DataFrame([{"date": d, "day": "Tue", "slot": "5:30", "slot_label": "5:30–7:20", "kind": "Practice",
                       "name": "16-Ana", "club": "Girls", "age": 16, "court": 1, "coach": "Ana", "on_pattern": True},
                      {"date": d + timedelta(days=1), "day": "Wed", "slot": "5:30", "slot_label": "5:30–7:20",
                       "kind": "Practice", "name": "16-Ana", "club": "Girls", "age": 16, "court": 1, "coach": "Ana",
                       "on_pattern": True},
                      {"date": d, "day": "Tue", "slot": "5:30", "slot_label": "5:30–7:20", "kind": "Practice",
                       "name": "16-Bea", "club": "Girls", "age": 16, "court": 3, "coach": "Bea", "on_pattern": True}])
    assert team_back_to_back(a) == [("16-Ana", d, d + timedelta(days=1))]
    assert back_to_back_days(a) == {("16-Ana", d), ("16-Ana", d + timedelta(days=1))}
    from scheduler.config import WEEK_ORDER, day_in_week
    ws = date(2026, 11, 8)
    h = court_grid_html(a, {wd: day_in_week(ws, wd) for wd in WEEK_ORDER})
    assert h.count('class="cs-tag cs-b2b"') == 2


@pytest.mark.usefixtures("alone_ok")
def test_back_to_back_days_in_review():
    # Only Tue and Wed open for this team: every week is back-to-back, and each one is flagged.
    teams = [{"team": "16-Ana", "age": 16}]
    avail = [{"who": "16-Ana", "type": "No", "days": "Mon, Thu, Fri"}]
    r = solve(_inp(teams, avail), *NOV, time_limit_s=10)
    b2b = r.issues[r.issues["issue"].str.startswith("Back-to-back days")]
    assert len(b2b) >= 4 and (b2b["severity"] == "Medium").all()


def test_court_1_over_court_7_for_the_higher_mens_pair():
    """Two men's-net pairs (5:30 -> 7:20) on the best courts: the older pair gets Court 1 (Nov 2026)."""
    teams = [{"team": "18 Black", "club": "Seaside", "age": 18, "head_coach": "A"},
             {"team": "17 Black", "club": "Seaside", "age": 17, "head_coach": "B"},
             {"team": "16 Black", "club": "Seaside", "age": 16, "head_coach": "C"},
             {"team": "15 Black", "club": "Seaside", "age": 15, "head_coach": "D"}]
    wish = [{"team": "18 Black", "choice": 1, "days": "Tue, Thu", "times": "7:20"},
            {"team": "17 Black", "choice": 1, "days": "Tue, Thu", "times": "5:30"},
            {"team": "16 Black", "choice": 1, "days": "Tue, Thu", "times": "7:20"},
            {"team": "15 Black", "choice": 1, "days": "Tue, Thu", "times": "5:30"}]
    # Only Courts 1 and 7 open at 5:30 and 7:20 (a program holds the rest), so 5:30 is full.
    locals_ = [{"program": "Prog", "days": "Tue, Thu", "time": s, "courts": "2-6", "coaches": "Z", "on_site": "Yes"}
               for s in ("5:30", "7:20")]
    r = solve(_inp(teams, wishlist=wish, locals_=locals_), date(2026, 11, 8), date(2026, 11, 14), time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    checked = 0
    for d, g in pr.groupby("date"):
        ct = dict(zip(g["name"], g["court"]))
        if {"18 Black", "17 Black"} <= set(ct) and ct["18 Black"] == ct["17 Black"]:
            assert ct["18 Black"] == 1, ct
            checked += 1
    assert checked


def test_back_to_back_only_free_for_two_day_wishes():
    from scheduler.data import Wish
    from scheduler.solver import _asked_pairs
    assert ("Tue", "Wed") in _asked_pairs([Wish("14 Black", 1, ["Tue", "Wed"], {"5:30"})])
    assert ("Tue", "Wed") not in _asked_pairs([Wish("16-A", 1, ["Mon", "Tue", "Wed", "Thu"], {"5:30"})])


def test_coach_at_a_travel_tournament_cant_coach_another_team_that_day():
    """Tyler heads 18 Black (at a Fri-Sun tournament) and 15-Tyler: no 15-Tyler practice on the
    Friday (Nov 2026); an assistant away with another team is flagged, the head coach runs it."""
    teams = [{"team": "18 Black", "club": "Seaside", "age": 18, "head_coach": "Tyler"},
             {"team": "15-Tyler", "age": 15, "head_coach": "Tyler", "assistants": "Teun"},
             {"team": "15 Black", "club": "Seaside", "age": 15, "head_coach": "Miles", "assistants": "Teun"}]
    # 15-Tyler can only practice Fri that week (team no-dates Mon-Thu), so it'd love the Friday.
    avail = [{"who": "15-Tyler", "type": "No", "dates": "12/7-12/10"}]
    wish = [{"team": "15-Tyler", "choice": 1, "days": "Fri", "times": "5:30"},
            {"team": "15 Black", "choice": 1, "days": "Fri", "times": "5:30"}]
    inp = _with_tournaments(_inp(teams, avail, wishlist=wish), [
        {"name": "SoCal Cup", "type": "Travel", "start_date": "12/11/2026", "end_date": "12/13/2026",
         "teams": "18 Black"}])
    r = solve(inp, date(2026, 12, 6), date(2026, 12, 12), time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    assert date(2026, 12, 11) not in set(pr[pr["name"] == "15-Tyler"]["date"])
    op = [o for o in r.openings if o["team"] == "15-Tyler"]
    assert all(x["date"] != date(2026, 12, 11) or any("Tyler" in n for n in x["notes"])
               for o in op for x in o["options"])


@pytest.mark.usefixtures("alone_ok")
def test_no_thursday_friday_even_over_a_preferred_day():
    """Nov 2026: Thu->Fri is avoided harder than other back-to-backs (Wed/Thu is tolerable)."""
    teams = [{"team": "16-Tia", "age": 16, "head_coach": "Tia"}]
    avail = [{"who": "16-Tia", "type": "No", "days": "Mon, Tue"}]
    wish = [{"team": "16-Tia", "choice": 1, "days": "Wed, Thu, Fri", "times": "5:30"}]
    r = solve(_inp(teams, avail, wishlist=wish), date(2026, 11, 8), date(2026, 11, 21), time_limit_s=10)
    for ws, g in r.assignments[r.assignments["kind"] == "Practice"].groupby(
            r.assignments["date"].map(week_start)):
        assert set(g["day"]) != {"Thu", "Fri"}, g


def test_no_coach_gap_between_program_and_team():
    """Nov 2026: a coach working a 3:45 program isn't left waiting for a 7:20 practice, even
    when 5:30 is a time they'd rather not."""
    teams = [{"team": "16-Kim", "age": 16, "head_coach": "Kim"}]
    avail = [{"who": "Kim", "type": "Not preferred", "times": "5:30"}]
    wish = [{"team": "16-Kim", "choice": 1, "days": "Mon, Wed", "times": "7:20"}]
    locals_ = [{"program": "Clinic", "days": "Mon, Wed", "time": "3:45", "courts": "6", "coaches": "Kim",
                "on_site": "Yes"}]
    r = solve(_inp(teams, avail, wishlist=wish, locals_=locals_), date(2026, 11, 8), date(2026, 11, 14),
              time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    for row in pr.itertuples():
        if row.day in ("Mon", "Wed"):
            assert row.slot != "7:20", row


def _leo_case(thu_530_open: bool):
    """15-Olivia (assistant Leo) can practice Mon, Thu or Fri; Leo heads 15 Black, at a travel
    tournament Fri Nov 20."""
    teams = [{"team": "15 Black", "age": 15, "head_coach": "Leo", "club": "Boys"},
             {"team": "15-Olivia", "age": 15, "head_coach": "Olivia", "assistants": "Leo", "club": "Girls"}]
    avail = [{"who": "15-Olivia", "type": "no", "days": "Tue, Wed"}]
    if not thu_530_open:
        avail.append({"who": "15-Olivia", "type": "no", "days": "Thu", "times": "5:30"})
    inp = _with_tournaments(_inp(teams, availability=avail), [
        {"name": "Turkey Classic", "type": "Travel", "start_date": "11/20/2026", "end_date": "11/20/2026",
         "teams": "15 Black"}])
    r = solve(inp, *NOV, time_limit_s=15)
    _check_hard_rules(inp, r)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    wk = pr[(pr["name"] == "15-Olivia") & pr["date"].between(date(2026, 11, 16), date(2026, 11, 20))]
    return sorted(zip(wk["date"], wk["slot"]))


@pytest.mark.usefixtures("alone_ok")
def test_assistant_at_tournament_late_practice_beats_missing():
    # Thu 5:30 is out: Thu 7:20 the night before Leo's tournament, not Fri without Leo.
    assert (date(2026, 11, 19), "7:20") in _leo_case(thu_530_open=False)
    assert not any(d == date(2026, 11, 20) for d, _ in _leo_case(thu_530_open=False))
    # The late practice is a last resort: with Thu 5:30 open it goes there.
    got = _leo_case(thu_530_open=True)
    assert (date(2026, 11, 19), "5:30") in got and not any(d == date(2026, 11, 20) for d, _ in got)


def test_paper_print_local_tournament_blocks_and_last_session():
    from scheduler.paper import last_sessions, paper_weeks_html
    s, e = date(2026, 11, 29), date(2026, 12, 5)
    inp = simulate(4, 2, 2, seed=57, start=s, end=e)
    inp.tournaments = pd.DataFrame([{"name": "Boys Local Tourney", "type": "Local", "start_date": "12/05/2026",
                                     "end_date": "12/05/2026", "courts": "1-7"}])
    lp = clean(inp, s, e).locals[0]
    inp.locals.loc[inp.locals["program"] == lp.name, "dates"] = "11/01/2026-12/03/2026"
    r = solve(inp, s, e, time_limit_s=10)
    data = clean(inp, s, e)
    last = {d for n, d in last_sessions(data.locals) if n == lp.name}
    assert last == {date(2026, 12, 3)}
    h = paper_weeks_html(r.assignments, s, e, data.closures, r.tournaments, "T", [], r.practice_counts, False,
                         r.weekend_dates, data.locals)
    # Nov 2026: the tournament blocks out its times on each of its courts (1-7), not big on Court 1.
    from scheduler.paper import ONLY_MARK   # it's the only one, so it carries a circled 1 and ONLY
    assert h.count('<div class="tnb"') == 7 and h.count(f"<b>Boys Local Tourney{ONLY_MARK}</b>") == 7
    assert "tnbig" not in h
    n_last = len(r.assignments[(r.assignments["name"] == lp.name) & (r.assignments["date"] == date(2026, 12, 3))]
                 .drop_duplicates(["court", "slot"]))
    assert h.count('class="last">LAST') == n_last > 0
    assert "border-top: 1.5px solid #555; padding: 0 5px" not in h   # no line above the first practice time


def test_never_one_team_alone_any_day():
    """Nov 2026: no team practices by itself at any time on any day, weekdays included (a locals
    program in the gym then counts as company); it outranks assistant coaches' no-dates."""
    s, e = date(2026, 12, 6), date(2026, 12, 19)
    for seed in (3, 11):
        inp = simulate(5, 3, 2, seed=seed, start=s, end=e)
        r = solve(inp, s, e, time_limit_s=15)
        assert r.ok
        a = r.assignments
        pr = a[a["kind"] == "Practice"]
        other = a[a["kind"] != "Practice"]
        company = set(zip(other["date"], other["slot"]))
        per_slot = Counter(zip(pr["date"], pr["slot"]))
        alone = [k for k, n in per_slot.items() if n == 1 and k not in company]
        assert not alone, alone


def test_move_a_team_across_age_groups():
    """Nov 8, 2026: 17-James goes above 18-Danielle, which ranks (within an age) can't do. Courts
    follow it; a rename keeps the move; undoing puts it back by age and rank."""
    from scheduler import pecking as pk
    from scheduler.config import pecking_weights
    from scheduler.data import rename_team
    teams = [{"team": "18 Black", "club": "Boys", "age": 18, "rank": "18-1", "head_coach": "Tyler"},
             {"team": "18-Danielle", "club": "Girls", "age": 18, "rank": "18-1", "head_coach": "Danielle"},
             {"team": "18-Two", "club": "Girls", "age": 18, "rank": "18-2", "head_coach": "Ana"},
             {"team": "17-James", "club": "Girls", "age": 17, "rank": "17-1", "head_coach": "James",
              "peck_move": "above 18-Danielle"},
             {"team": "17 Black", "club": "Boys", "age": 17, "rank": "17-1", "head_coach": "Leo"}]
    inp = _inp(teams)
    o = pk.order_table(inp.teams)
    assert o["team"].tolist() == ["18 Black", "17-James", "18-Danielle", "18-Two", "17 Black"]
    assert o.set_index("team").loc["17-James", "moved"] == "above 18-Danielle"
    w = pecking_weights(clean(inp, *NOV).teams)
    assert w["18 Black"] > w["17-James"] > w["18-Danielle"]
    # "below" works too, and a move onto a team that isn't there is ignored.
    t = inp.teams.copy()
    t.loc[t["team"] == "17-James", "peck_move"] = "below 18 Black"
    assert pk.order_table(t)["team"].tolist()[:3] == ["18 Black", "17-James", "18-Danielle"]
    t.loc[t["team"] == "17-James", "peck_move"] = "above 99-Nobody"
    assert pk.order_table(t)["team"].tolist()[-1] == "17-James" and pk.order_table(t)["moved"].eq("").all()
    # Renaming the team it sits above keeps the move.
    rename_team(inp, "18-Danielle", "18-Dani")
    assert pk.order_table(inp.teams)["team"].tolist()[1:3] == ["17-James", "18-Dani"]
    # Undo: back by age and rank.
    t = inp.teams.copy()
    t.loc[t["team"] == "17-James", "peck_move"] = ""
    assert pk.order_table(t)["team"].tolist()[:3] == ["18 Black", "18-Dani", "18-Two"]


def test_every_place_in_the_pecking_order_counts_for_courts():
    """Nov 9, 2026 bug: with ~40 teams the 2-20 court-priority scale gave neighbors in the order the
    same number, so the top two teams at a time counted as tied for Court 1 and a coach staying
    put decided it. Every place now counts."""
    from types import SimpleNamespace as NS

    from scheduler.config import pecking_weights
    from scheduler.solver import _assign_courts
    ages = [18] * 6 + [17] * 6 + [16] * 8 + [15] * 8 + [14] * 6 + [13] * 6
    teams = [NS(name=f"T{i}", age=a, level=i, club="Girls" if i % 2 else "Boys", peck_move="")
             for i, a in enumerate(ages)]
    w = pecking_weights(teams)
    assert len(set(w.values())) == len(teams) and w["T0"] == len(teams) and w["T39"] == 1
    # A moved team gets its own place too.
    teams[13].peck_move = "above T5"
    w2 = pecking_weights(teams)
    assert w2["T4"] > w2["T13"] > w2["T5"] and len(set(w2.values())) == len(teams)
    # T0 and T1 (next to each other) at 7:20; T1's coach had T30 on Court 1 at 5:30. T0 still gets 1.
    d = date(2026, 11, 16)
    idx = {"T0": 0, "T1": 1, "T30": 2}
    booked = {(0, d, "7:20"), (1, d, "7:20"), (2, d, "5:30")}
    _, c, _ = _assign_courts(booked, [0, 1, 2], lambda d_, s_: [1, 2, 3, 4, 5, 6, 7],
                             {i: w[n] for n, i in idx.items()}, time_limit_s=5,
                             net_of={0: 1, 1: 1, 2: 1}, ages={0: 18, 1: 18, 2: 15},
                             boys_of={0: True, 1: False, 2: False},
                             coach_links=[((2, d, "5:30"), (1, d, "7:20"))])
    assert c[0, d, "7:20"] == 1 and c[1, d, "7:20"] == 7


def test_changes_by_day():
    """Nov 9, 2026: the changes since publish, one section per day, each day's changes by time.
    A move to another day shows on both days."""
    from scheduler.changes import changes_by_day, changes_day_text, diff
    from scheduler.prints import changes_print_html
    old = pd.DataFrame([
        {"kind": "Practice", "name": "16 Black", "date": date(2026, 11, 18), "slot": "5:30", "court": 7},
        {"kind": "Practice", "name": "14-Ann", "date": date(2026, 11, 18), "slot": "7:20", "court": 3},
        {"kind": "Practice", "name": "12-Kai", "date": date(2026, 11, 16), "slot": "3:45", "court": 4}])
    new = pd.DataFrame([
        {"kind": "Practice", "name": "16 Black", "date": date(2026, 11, 17), "slot": "5:30", "court": 3},
        {"kind": "Practice", "name": "14-Ann", "date": date(2026, 11, 18), "slot": "7:20", "court": 1},
        {"kind": "Practice", "name": "12-Kai", "date": date(2026, 11, 16), "slot": "5:30", "court": 2}])
    ch = diff(old, new, None, *NOV)
    days = changes_by_day(ch, {"16 Black": "S16-B"})
    assert [d for d, _ in days] == [date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)]
    d16, d17, d18 = (dict(((e[1], e[2]), e) for e in items) for _, items in days)
    assert d16["12-Kai", "Moved"][3] == "from 3:45–5:30 · Court 4 to 5:30–7:20 · Court 2"
    assert d17["S16-B (16 Black)", "Moved here"][3] == "Court 3 (was Wed 11/18 5:30–7:20 · Court 7)"
    assert d18["S16-B (16 Black)", "Moved away"][3] == "Court 7 → Tue 11/17 5:30–7:20 · Court 3"
    assert d18["14-Ann", "Court"][3] == "Court 3 → Court 1"
    assert [e[1] for e in days[2][1]] == ["S16-B (16 Black)", "14-Ann"]       # by time: 5:30, then 7:20
    txt = changes_day_text(ch, "Changes")
    assert "Wednesday Nov 18" in txt and txt.index("Monday Nov 16") < txt.index("Tuesday Nov 17")
    h = changes_print_html(ch, "Changes", "x", by_day=True)
    assert h.count('class="pc-week"') == 3 and "Moved here" in h
