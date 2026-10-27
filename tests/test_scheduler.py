"""Checks every hard rule on solved schedules, plus input parsing."""

import io
from collections import Counter
from datetime import date

import pandas as pd
import pytest

from scheduler import ScheduleInput, clean, simulate, solve
from scheduler.closures import default_closures
from scheduler.data import (
    COLUMNS, tournament_conflicts, parse_courts, parse_date_ranges, parse_days, parse_names, parse_slot, parse_times,
    parse_weekend_tokens,
)
from scheduler.config import slots_overlap
from scheduler.solver import week_start

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
    for (d, s), g in paired.groupby(["date", "slot"]):
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
    assert len(r2.assignments) == len(r.assignments) and r2.metrics["practices_scheduled"] == r.metrics["practices_scheduled"]
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
    assert ">Sun<" in grid and ">Sat<" in grid and "weekend not open" in grid
    cal = month_calendar_html(empty, date(2026, 11, 1), date(2026, 11, 30))
    assert ">Sun<" in cal and ">Sat<" in cal


def test_no_dates_broken_only_as_last_resort_head_coach_first():
    """When no-dates leave fewer than 2 days, the head coach's no-date is
    broken first on a day an assistant is free; hand removals never are."""
    teams = [{"team": "15-Kit", "age": 15, "head_coach": "Kit", "assistants": "Ash"}]
    avail = [{"who": "Kit", "type": "No", "dates": "11/9-11/12"},    # head out Mon-Thu
             {"who": "Ash", "type": "No", "dates": "11/9-11/10"},    # assistant out Mon-Tue
             {"who": "15-Kit", "type": "No", "dates": "11/13"}]      # team out Fri
    r = solve(_inp(teams, avail), *NOV, time_limit_s=10)
    wk = r.assignments[r.assignments["date"].between(date(2026, 11, 9), date(2026, 11, 13))]
    assert len(wk) == 2 and r.shortfalls.empty
    # Wed/Thu: only Kit's no-date is broken, and Ash is free to run it.
    assert set(wk["day"]) <= {"Wed", "Thu"}
    assert all(b["head_out"] == ["Kit"] and b["free_asst"] == ["Ash"] and not b["team_out"] for b in r.broken)
    assert len(r.broken) == 2


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


def test_restore_published_version():
    """Restoring brings a version's practices back exactly, even after the
    schedule was rebuilt differently; what can't happen now is noted."""
    from scheduler.changes import diff, practices_of, snapshot
    inp = simulate(6, 4, 4, 2026, 11, 7)
    r1 = solve(inp, *NOV, time_limit_s=20)
    v1 = snapshot(r1, "Nov v1")
    old = practices_of(v1)
    # A different schedule (no stability) so there's something to restore.
    r2 = solve(inp, *NOV, time_limit_s=20, seed=9)
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
    for wd, d in dd.items():
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


def test_breaking_a_no_date_beats_practicing_next_to_a_tournament():
    """Only Tue and Fri (the day before a Sat-Sun tournament) are free that
    week. A no-date on Wed is broken instead of practicing Friday."""
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
    assert [(b["date"], b["head_out"]) for b in r.broken] == [(date(2026, 11, 11), ["Ana"])]


def test_saturday_then_sunday_avoided():
    """Weekdays only fit 1 practice a week (Fri), so teams need a weekend
    practice every week. Sundays are open too, but taking one right after a
    Saturday practice is avoided: every week uses its Saturday instead."""
    teams = [{"team": f"16-W{i}", "age": 16} for i in range(4)]
    avail = [{"who": t["team"], "type": "No", "days": "Mon, Tue, Wed, Thu"} for t in teams]
    weekends = [{"date": d, "open": "9AM-9PM"} for d in ("11/7", "11/8", "11/14", "11/15", "11/21", "11/22", "11/28")]
    r = solve(_inp(teams, avail, weekends=weekends), *NOV, time_limit_s=15)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    for name, g in pr.groupby("name"):
        days = set(g["date"])
        assert not any(d.weekday() == 5 and d + pd.Timedelta(days=1) in days for d in days), name
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
    wish = [{"team": "17-Lin", "choice": 1, "days": "Tue, Thu", "times": "7:20"}]
    r = solve(_inp(teams, wishlist=wish), *NOV, time_limit_s=10)
    lin = r.pattern[r.pattern["team"] == "17-Lin"]
    assert set(zip(lin["day"], lin["slot"])) == {("Tue", "7:20"), ("Thu", "7:20")}


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
    assert not mine["issue"].str.contains("Back-to-back").any()  # asked for, so not flagged


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


def test_sat_sun_beats_breaking_an_assistant_no_date():
    """Assistants' no-dates rank above Saturday->Sunday (club order, Oct 2026)."""
    inp, r = _ladder_club("assistant")
    for team in ("16-Ann", "16-Bea"):
        assert _days(r, team) == {"Sat 11/7", "Sun 11/8", "Fri 11/13"}, _days(r, team)
    assert not r.issues["issue"].str.startswith("Assistant no-date broken").any()
    flags = r.issues[r.issues["issue"].str.startswith("Saturday then Sunday")]
    assert set(flags["team"]) == {"16-Ann", "16-Bea"}


def test_rule_order():
    """The club's order, most important first: each rule outweighs at least
    two breaks of the next one down."""
    from scheduler.config import WEIGHTS as W
    order = [W["missed_practice"], W["tournament_rest"], W["break_team_nodate"], W["break_head_and_asst"],
             W["break_head_nodate"], W["assistant_nodate"], W["locals_sub"], W["weekend"],
             W["wish_day_miss"], W["wish_time_miss"], W["sat_sun"] + W["consecutive_days"],
             W["second_weekend"], W["consecutive_days"]]
    for hi, lo in zip(order, order[1:]):
        assert hi > lo, (hi, lo)
    # Head coach vs assistant: two assistant no-dates still cost less.
    assert W["break_head_nodate"] > 1.8 * W["assistant_nodate"]
    # A sub is asked for before any no-date is broken; a weekend before a sub.
    assert W["assistant_nodate"] > W["locals_sub"] > W["weekend"]
    # Days before times, for both the wishlist and not-preferred.
    assert W["wish_day_miss"] > W["wish_time_miss"]
    assert W["not_preferred_day"] > W["not_preferred"] + W["not_preferred_345"]
    # Every "everything else" weight is below back-to-back days.
    for k in ("second_345", "late_exception", "off_pattern", "split_timeslot", "keep_previous", "keep_court",
              "weekend_block", "double_up", "home_court", "move_to_wish"):
        assert W[k] < W["consecutive_days"], k
    assert W["weekend_required"] == 0  # a note for coaches, not a rule


def test_wishlist_days_count_more_than_times():
    from scheduler.data import Wish
    from scheduler.solver import _wish_cost
    w = [Wish("16-A", 1, ["Tue", "Thu"], {"5:30"})]
    assert _wish_cost(w, "Tue", "5:30") == 0
    assert _wish_cost(w, "Tue", "7:20") < _wish_cost(w, "Mon", "5:30") < _wish_cost(w, "Mon", "7:20")


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
    # The lowest in the pecking order gets Court 2, the top team Court 1.
    teams = [{"team": f"16-T{i}", "age": 16, "rank": f"16-{i + 1}"} for i in range(7)]
    avail = [{"who": t["team"], "type": "No", "days": "Mon, Wed, Fri"} for t in teams] + \
            [{"who": t["team"], "type": "No", "times": "7:20"} for t in teams]
    r = solve(_inp(teams, avail), *NOV, time_limit_s=15)
    pr = r.assignments
    assert set(pr[pr["court"] == 2]["name"]) == {"16-T6"}
    assert set(pr[pr["court"] == 1]["name"]) == {"16-T0"}


def test_calendar_exports(tmp_path):
    import io as _io
    from openpyxl import load_workbook
    from scheduler.export import calendar_excel, calendar_ics, to_excel
    from scheduler.views import calendar_print_html
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
        hits = played[(played["day"] == p.day) & (played["slot"] == p.slot) & played["wk"].isin(full)]
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


def test_pairing_only_when_courts_run_out():
    # Every team can only make Mon/Wed 5:30. 7 teams fit; 9 need pairing.
    def club(n):
        teams = [{"team": f"16-C{i}", "age": 16} for i in range(n)]
        avail = [{"who": t["team"], "type": "No", "days": "Tue, Thu, Fri"} for t in teams]
        avail += [{"who": t["team"], "type": "No", "times": "7:20"} for t in teams]
        return _inp(teams, avail)
    r7 = solve(club(7), *NOV, time_limit_s=15)
    assert r7.metrics["paired_slots"] == 0
    r9 = solve(club(9), *NOV, time_limit_s=20)
    _check_hard_rules(club(9), r9)
    pr = r9.assignments[r9.assignments["kind"] == "Practice"]
    assert r9.metrics["paired_slots"] > 0
    assert not r9.broken                      # paired rather than breaking anyone's no-dates
    assert r9.shortfalls.empty
    assert (r9.issues["issue"].str.startswith("Paired:")).any()
    one = pr[pr["paired"] != ""].iloc[0]
    assert one["paired"].startswith("3 teams on Court")


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


def test_hs_boys_keep_their_courts_from_530_to_720():
    teams = [{"team": f"{a} {c}", "club": "Boys", "age": a, "head_coach": f"B{a}{c}"}
             for a in (15, 16, 17, 18) for c in ("Black", "Green")]
    teams += [{"team": f"{a}-G{i}", "club": "Girls", "age": a} for a in (15, 16, 17) for i in range(2)]
    r = solve(_inp(teams), *NOV, time_limit_s=20)
    a = r.assignments
    pr = a[a["kind"] == "Practice"]
    boys = {t["team"] for t in teams if t["club"] == "Boys"}
    changes = 0
    for (d, c), g in pr[pr["slot"].isin(["5:30", "7:20"])].groupby(["date", "court"]):
        kinds = {s: set(g[g["slot"] == s]["name"].isin(boys)) for s in ("5:30", "7:20")}
        if kinds["5:30"] and kinds["7:20"] and kinds["5:30"] != kinds["7:20"]:
            changes += 1
    assert changes == r.metrics["net_changes"]
    assert changes <= 2


def test_coach_stays_on_court_for_back_to_back_teams():
    # (Girls then HS boys would need a net change, which outranks this.)
    teams = [{"team": "16-Ana", "club": "Girls", "age": 16, "head_coach": "Ana"},
             {"team": "18-Ana", "club": "Girls", "age": 18, "head_coach": "Ana"}]
    wish = [{"team": "16-Ana", "choice": 1, "days": "Mon, Wed", "times": "5:30"},
            {"team": "18-Ana", "choice": 1, "days": "Mon, Wed", "times": "7:20"}]
    r = solve(_inp(teams, wishlist=wish), *NOV, time_limit_s=10)
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    for d, g in pr.groupby("date"):
        if set(g["slot"]) == {"5:30", "7:20"}:
            assert g["court"].nunique() == 1, g


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
    assert page.count('class="cs-week"') == 5   # Nov 1, 8, 15, 22, 29
    assert "break-after: page" in page
