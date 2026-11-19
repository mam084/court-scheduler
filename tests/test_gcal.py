"""Google Calendar sync, against an in-memory stand-in for Google."""

from datetime import date

import pandas as pd

from scheduler import clean, gcal, simulate, solve
from scheduler.data import COLUMNS

S, E = date(2026, 11, 1), date(2026, 11, 30)


class FakeGoogle(gcal.CalendarClient):
    def __init__(self):
        self.cals = {}          # calendar id -> {event id: event}
        self.robot = "robot@test.iam.gserviceaccount.com"
        self.writes = 0

    def app_events(self, cid, start, end):
        return [e for e in self.cals.get(cid, {}).values()
                if e.get("extendedProperties", {}).get("private", {}).get(gcal.TAG) == "1"]

    def other_events(self, cid, start, end):
        return [e for e in self.cals.get(cid, {}).values()
                if e.get("extendedProperties", {}).get("private", {}).get(gcal.TAG) != "1"]

    def create(self, cid, body):
        self.cals.setdefault(cid, {})[body["id"]] = body
        self.writes += 1

    def update(self, cid, eid, body):
        self.cals[cid][eid] = body
        self.writes += 1

    def delete(self, cid, eid):
        self.cals[cid].pop(eid, None)
        self.writes += 1


def _club():
    inp = simulate(6, 4, 4, seed=7, start=S, end=E)
    r = solve(inp, S, E, time_limit_s=10)
    data = clean(inp, S, E)
    links = {("Club", c): f"club-{c}" for c in {t.club for t in data.teams}}
    links.update({("Team", t.name): f"team-{t.name}" for t in data.teams})
    links.update({("Program", lp.name): f"prog-{lp.name}" for lp in data.locals})
    return inp, r, data, links


def _events(r, data, links, test=""):
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    loc = r.assignments[r.assignments["kind"] == "Local"]
    return gcal.desired_events(pr, loc, data.tournaments, data.teams, links, S, E, test,
                               programs_side={lp.name: lp.side for lp in data.locals})


def test_events_go_to_team_club_and_program_calendars():
    inp, r, data, links = _club()
    evs, missing = _events(r, data, links)
    assert not missing
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    team = pr.iloc[0]["name"]
    mine = [e for e in evs if e.calendar_id == f"team-{team}" and "|practice|" in e.key]
    assert len(mine) == (pr["name"] == team).sum()
    club = pr.iloc[0]["club"]
    assert len([e for e in evs if e.calendar_id == f"club-{club}" and "|practice|" in e.key]) == \
        (pr["club"] == club).sum()
    loc = r.assignments[r.assignments["kind"] == "Local"]
    prog = loc.iloc[0]["name"]
    sessions = loc[loc["name"] == prog].groupby(["date", "slot"]).ngroups
    assert len([e for e in evs if e.calendar_id == f"prog-{prog}"]) == sessions
    travel = [e for e in evs if "Travel Tournament" in e.summary]
    assert travel and all(e.all_day for e in travel)
    assert {e.calendar_id for e in travel} >= {f"team-{n}" for n in data.tournaments[0].teams}


def test_sync_adds_updates_removes_and_never_touches_hand_made_events():
    inp, r, data, links = _club()
    g = FakeGoogle()
    cid = next(iter(links.values()))
    g.cals[cid] = {"handmade": {"id": "handmade", "summary": "Team dinner"}}
    evs, _ = _events(r, data, links)
    p = gcal.plan(evs, g, S, E, list(links.values()))
    assert len(p.create) == len(evs) and not p.update and not p.delete
    gcal.apply(p, g)
    # Re-running changes nothing.
    assert gcal.plan(evs, g, S, E, list(links.values())).n_changes == 0
    # A practice moves court, one disappears: one update, one removal.
    pr = r.assignments[r.assignments["kind"] == "Practice"].copy()
    pr.loc[pr.index[0], "court"] = 2 if pr.iloc[0]["court"] != 2 else 3
    pr = pr.drop(pr.index[1])
    loc = r.assignments[r.assignments["kind"] == "Local"]
    evs2, _ = gcal.desired_events(pr, loc, data.tournaments, data.teams, links, S, E,
                                  programs_side={lp.name: lp.side for lp in data.locals})
    p2 = gcal.plan(evs2, g, S, E, list(links.values()))
    assert len(p2.update) == 2 and len(p2.delete) == 2 and not p2.create   # team + club calendar each
    gcal.apply(p2, g)
    assert g.cals[cid]["handmade"]["summary"] == "Team dinner"            # untouched


def test_test_mode_sends_everything_to_one_calendar():
    inp, r, data, links = _club()
    evs, _ = _events(r, data, links, test="test-cal")
    assert {e.calendar_id for e in evs} == {"test-cal"}
    assert all(e.summary.startswith("[") for e in evs)
    assert len({e.event_id for e in evs}) == len(evs)                     # nothing collapses together
    g = FakeGoogle()
    gcal.apply(gcal.plan(evs, g, S, E, ["test-cal"]), g)
    assert set(g.cals) == {"test-cal"}


def test_event_bodies_and_links():
    inp, r, data, links = _club()
    evs, _ = _events(r, data, links)
    b = next(e for e in evs if not e.all_day).body()
    assert b["start"]["timeZone"] == "America/Los_Angeles" and b["id"].startswith("cs")
    assert set(b["id"][2:]) <= set("0123456789abcdef")
    assert b["extendedProperties"]["private"][gcal.TAG] == "1"
    t = next(e for e in evs if e.all_day).body()
    assert "date" in t["start"] and t["end"]["date"] > t["start"]["date"]
    rows = gcal.link_rows(["16 Black"], ["Seaside"], ["G HS L"],
                          pd.DataFrame([{"kind": "Team", "name": "16 Black", "calendar_id": "abc@group"}],
                                       columns=COLUMNS["Calendars"]))
    assert gcal.links_from(rows) == {("Team", "16 Black"): "abc@group"}
    assert gcal.service_account_info({"GOOGLE_SERVICE_ACCOUNT": '{"client_email": "x@y"}'})["client_email"] == "x@y"


class _Resp:
    def __init__(self, code, data=None, text=""):
        self.status_code, self._data, self.text = code, data or {}, text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.replies.pop(0)


def test_rest_client_paths(monkeypatch):
    monkeypatch.setattr(gcal.time, "sleep", lambda s: None)
    s = _Session([_Resp(429), _Resp(200, {"items": [{"id": "a", "status": "confirmed"},
                                                     {"id": "b", "status": "cancelled"}]})])
    c = gcal.CalendarClient({"client_email": "r@x"}, session=s)
    got = c.app_events("team16@group.calendar.google.com", S, E)
    assert [e["id"] for e in got] == ["a"]                                  # retried after 429, skips cancelled
    assert "team16%40group.calendar.google.com" in s.calls[-1][1]
    assert s.calls[-1][2]["params"]["privateExtendedProperty"] == "court_scheduler=1"
    s = _Session([_Resp(409), _Resp(200)])
    c = gcal.CalendarClient({"client_email": "r@x"}, session=s)
    c.create("cal", {"id": "csabc"})
    assert [m for m, _, _ in s.calls] == ["POST", "PUT"] and s.calls[1][1].endswith("/events/csabc")
    s = _Session([_Resp(404)])
    import pytest
    with pytest.raises(LookupError):
        gcal.CalendarClient({"client_email": "r@x"}, session=s).calendar_name("nope")


def test_practices_only_leaves_programs_and_tournaments_alone():
    """With programs and tournaments switched off, a sync sends only practices and never removes the
    program or tournament events already on the calendars."""
    inp, r, data, links = _club()
    g = FakeGoogle()
    full, _ = _events(r, data, links)
    cals = sorted(set(links.values()))
    gcal.apply(gcal.plan(full, g, S, E, cals), g)     # an earlier sync that sent everything
    before = {cid: set(ev) for cid, ev in g.cals.items()}
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    only, _ = gcal.desired_events(pr, pr.iloc[0:0], [], data.teams,
                                  {k: v for k, v in links.items() if k[0] != "Program"}, S, E, "")
    assert only and all(e.key.split("|")[1] == "practice" for e in only)
    p = gcal.plan(only, g, S, E, sorted({e.calendar_id for e in only}), leave=("program", "tournament"))
    assert not p.delete and not p.create and not p.update
    if any(e.key.split("|")[1] == "tournament" for e in full):   # without leave=, they'd be removed
        assert gcal.plan(only, g, S, E, sorted({e.calendar_id for e in only})).delete
    gcal.apply(p, g)
    assert {cid: set(ev) for cid, ev in g.cals.items()} == before


def test_boys_practices_go_on_the_seaside_calendar_and_move_off_sdvbc():
    """Boys teams go on the Seaside club calendar even when their club column says SDVBC (or is
    blank); a sync after the fix moves events put on SDVBC by mistake over to Seaside (Nov 2026)."""
    inp, r, data, _ = _club()
    for t in data.teams:                        # every team typed as SDVBC, like the bug
        t.club = "SDVBC"
    pr = r.assignments[r.assignments["kind"] == "Practice"].assign(club="SDVBC")
    links = {("Club", "SDVBC"): "sdvbc", ("Club", "Seaside"): "seaside"}
    evs, _ = gcal.desired_events(pr, None, [], data.teams, links, S, E)
    boys = {t.name for t in data.teams if gcal.team_is_boys(t.club, t.name)}
    assert boys and len(boys) < len(data.teams)
    assert {e.calendar_id for e in evs if e.key.split("|")[-1] in boys} == {"seaside"}
    assert {e.calendar_id for e in evs if e.key.split("|")[-1] not in boys} == {"sdvbc"}
    # Events already on SDVBC for boys teams (the old way) come off it on the next sync.
    g = FakeGoogle()
    wrong = [gcal.Event("sdvbc", "Club: SDVBC", f"Club: SDVBC|practice|x|{n}", f"{n} practice", "",
                        evs[0].start, evs[0].end) for n in sorted(boys)[:2]]
    for e in wrong:
        g.create("sdvbc", e.body())
    p = gcal.plan(evs, g, S, E, calendars=["sdvbc", "seaside"])
    assert {d[1] for d in p.delete} == {e.event_id for e in wrong}
    assert any(e.calendar_id == "seaside" for e in p.create)
    # Both club rows are always offered for linking.
    rows = gcal.link_rows([], [], [], None)
    assert list(rows["name"][:2]) == ["SDVBC", "Seaside"]


def test_titles_capitalized_without_the_word_practice():
    """Practice titles are the team and court, every word capitalized (Nov 2026)."""
    inp, r, data, _ = _club()
    pr = r.assignments[r.assignments["kind"] == "Practice"]
    links = {("Team", t.name): f"cal-{t.name}" for t in data.teams}
    evs, _ = gcal.desired_events(pr, None, [], data.teams, links, S, E)
    assert evs and all("practice" not in e.summary.lower() for e in evs)
    assert all(" · Court " in e.summary or "Practice Court" in e.summary for e in evs)
    assert gcal.cap("14 black · court TBA") == "14 Black · Court TBA" and gcal.cap("SDVBC 14-CA") == "SDVBC 14-CA"


def test_program_over_its_whole_run():
    """A spring program from late December to March goes on its calendar in one go, under its (full)
    name, without the club's big holidays or its cancelled sessions (Nov 2026)."""
    from scheduler.data import LocalProgram
    lp = LocalProgram("Girls 14U Spring League", "Tue", "5:30", [1, 2], ["Ann"], True,
                      [(date(2026, 12, 22), date(2027, 3, 9))], "Girls", {date(2027, 1, 5)}, False, "G14 SL")
    other = LocalProgram("JS", "Tue", "3:30", [3], [], True, [(date(2026, 12, 1), date(2027, 3, 1))])
    rows = gcal.program_sessions([lp, other], {date(2027, 2, 16): "closed"}, date(2026, 12, 22),
                                 date(2027, 3, 9), {"Girls 14U Spring League"})
    days = sorted(set(rows["date"]))
    assert days[0] == date(2026, 12, 22) and days[-1] == date(2027, 3, 9)
    assert date(2027, 1, 5) not in days and date(2027, 2, 16) not in days and set(rows["name"]) == {"Girls 14U Spring League"}
    assert gcal.program_span([lp, other], {"Girls 14U Spring League"}) == (date(2026, 12, 22), date(2027, 3, 9))
    links = {("Program", "Girls 14U Spring League"): "g14"}
    evs, _ = gcal.desired_events(None, rows, [], [], links, date(2026, 12, 22), date(2027, 3, 9))
    assert len(evs) == len(days) and all(e.calendar_id == "g14" for e in evs)
    assert evs[0].summary == "Girls 14U Spring League"   # no courts on program events (Nov 2026)


def test_program_shorthand_only_on_printouts():
    """A program's shorthand is what the printouts write; its full name everywhere else (Nov 2026)."""
    from scheduler import labels
    from scheduler.data import LocalProgram
    from scheduler.paper import paper_weeks_html
    lp = LocalProgram("Girls 14U Spring League", "Tue", "5:30", [1], [], True, [(S, E)], short="G14 SL")
    labels.set_program_shorts([lp])
    try:
        a = pd.DataFrame([{"date": date(2026, 11, 3), "slot": "5:30", "court": 1, "kind": "Local",
                           "name": "Girls 14U Spring League", "club": "", "coach": ""}])
        html_ = paper_weeks_html(a, date(2026, 11, 1), date(2026, 11, 7))
        assert "G14 SL" in html_ and "Girls 14U Spring League" not in html_
        evs, _ = gcal.desired_events(None, a, [], [], {("Program", "Girls 14U Spring League"): "g"}, S, E)
        assert evs[0].summary == "Girls 14U Spring League"
    finally:
        labels.set_program_shorts([])


def test_add_only_sends_just_whats_missing_and_skips_times_taken_by_hand():
    from dataclasses import replace
    inp, r, data, links = _club()
    g = FakeGoogle()
    evs, _ = _events(r, data, links)
    team_evs = [e for e in evs if e.calendar_id.startswith("team-")]
    first, second, third = team_evs[0], team_evs[1], team_evs[2]
    gcal.apply(gcal.plan([first, second], g, S, E, [first.calendar_id, second.calendar_id]), g)
    # The schedule changed for `second` since it was sent; a hand-made event sits where `third` would go.
    changed = replace(second, summary=second.summary + " (moved)")
    g.cals.setdefault(third.calendar_id, {})["hand1"] = {"id": "hand1", "summary": "Practice (by hand)",
                                                           "start": {"dateTime": third.start.isoformat() + "-08:00"}}
    want = [first, changed, third] + [e for e in team_evs[3:6]]
    p = gcal.plan(want, g, S, E, sorted({e.calendar_id for e in want}), add_only=True, skip_taken=True)
    assert not p.update and not p.delete                         # nothing already there changes
    assert p.kept == 1 and p.unchanged >= 1
    assert third in p.skipped and third not in p.create          # not doubled up on the hand-made one
    assert {e.event_id for e in p.create} == {e.event_id for e in team_evs[3:6]} - {third.event_id}
    p2 = gcal.plan(want, g, S, E, sorted({e.calendar_id for e in want}), add_only=True)
    assert third in p2.create                                    # skipping is its own switch


def test_programs_link_by_their_printout_shorthand():
    inp, r, data, links = _club()
    lp = data.locals[0]
    lp.short = "BJS"
    links = {k: v for k, v in links.items() if k != ("Program", lp.name)}
    links["Program", "BJS"] = "prog-bjs"
    evs, missing = _events(r, data, gcal.with_program_shorthand(links, data.locals))
    assert f"Program: {lp.name}" not in missing
    assert any(e.calendar_id == "prog-bjs" for e in evs)
    rows = gcal.link_rows([], [], ["BJS"], None)
    assert ("Program", "BJS") in set(zip(rows["kind"], rows["name"]))


def test_same_named_local_tournaments_on_a_day_are_one_event():
    from scheduler.data import Tournament
    d = date(2026, 11, 21)
    a = Tournament("Winter Classic", "local", d, d, [], [1, 2], ["Ann"], program="Girls 12s", side="Girls")
    b = Tournament("winter  classic", "local", d, d, [], [5, 6], ["Bo"], program="Boys 14s", side="Boys")
    c = Tournament("Winter Classic", "local", date(2026, 12, 5), date(2026, 12, 5), [], [1], [], program="Girls 12s")
    links = {("Club", gcal.GIRLS_CLUB): "girls", ("Club", gcal.BOYS_CLUB): "boys",
             ("Program", "Girls 12s"): "g12", ("Program", "Boys 14s"): "b14"}
    evs, _ = gcal.desired_events(None, None, [a, b, c], [], links, S, date(2026, 12, 31),
                                 programs_side={"Girls 12s": "Girls", "Boys 14s": "Boys"})
    nov = [e for e in evs if e.start == d]
    assert sorted(e.calendar_id for e in nov) == ["b14", "boys", "g12", "girls"]  # one event per calendar
    assert len({e.summary for e in nov}) == 1 and not any("Court" in e.description for e in nov)
    assert all("Ann" in e.description and "Bo" in e.description for e in nov)
    assert len([e for e in evs if e.start == date(2026, 12, 5)]) == 2               # another day stays its own


def test_programs_go_on_their_sides_club_calendars():
    from scheduler.data import LocalProgram
    d = date(2026, 11, 4)
    lps = [LocalProgram("Girls Locals", "Wed", "5:30", [1], [], True, [(S, E)], "Girls", row=0),
           LocalProgram("Boys Locals", "Wed", "5:30", [2], [], True, [(S, E)], "Boys", row=1),
           LocalProgram("Jump Start", "Wed", "3:30", [3], [], True, [(S, E)], "Both", row=2),
           LocalProgram("Open Gym", "Wed", "7:20", [4], [], True, [(S, E)], "", row=3)]
    rows = gcal.program_sessions(lps, {}, d, d)
    links = {("Club", gcal.GIRLS_CLUB): "sd", ("Club", gcal.BOYS_CLUB): "ss",
             **{("Program", lp.name): lp.name for lp in lps}}
    evs, _ = gcal.desired_events(None, rows, [], [], links, d, d, locals_=lps)
    on = {name: sorted(e.calendar_id for e in evs if e.summary.startswith(name)) for name in
          ("Girls Locals", "Boys Locals", "Jump Start", "Open Gym")}
    assert on == {"Girls Locals": ["Girls Locals", "sd"], "Boys Locals": ["Boys Locals", "ss"],
                  "Jump Start": ["Jump Start", "sd", "ss"], "Open Gym": ["Open Gym"]}


def test_add_and_fix_updates_but_never_removes():
    from dataclasses import replace
    inp, r, data, links = _club()
    g = FakeGoogle()
    evs, _ = _events(r, data, links)
    ev = [e for e in evs if e.calendar_id.startswith("team-")][:3]
    gcal.apply(gcal.plan(ev, g, S, E, [e.calendar_id for e in ev]), g)
    want = [replace(ev[0], summary="Changed"), ev[1]]                       # ev[2] isn't wanted any more
    p = gcal.plan(want, g, S, E, sorted({e.calendar_id for e in ev}), no_delete=True)
    assert len(p.update) == 1 and not p.delete and p.kept == 1


def test_program_events_have_no_courts():
    from scheduler.data import LocalProgram
    d = date(2026, 11, 4)
    lp = LocalProgram("Girls Locals", "Wed", "5:30", [1, 2], ["Ann"], True, [(S, E)], "Girls", row=0)
    evs, _ = gcal.desired_events(None, gcal.program_sessions([lp], {}, d, d), [], [], {("Program", lp.name): "g"},
                                 d, d, locals_=[lp])
    assert [e.summary for e in evs] == ["Girls Locals"] and "Court" not in evs[0].description


def test_local_tournaments_go_only_to_their_own_programs_and_side():
    """Nov 19, 2026: three girls 14s programs (L14, L14T, Dave's Invite L14D) with "SDVBC" local tournaments
    on two dates, Dave's Invite only playing one. Sent together: each program calendar gets only its own
    dates, and they go on the SDVBC calendar only (not Seaside), even when the program sides are passed
    empty (the whole-run send did that)."""
    from scheduler.data import LocalProgram, Tournament
    j, f = date(2027, 1, 23), date(2027, 2, 13)
    rng = [(date(2027, 1, 5), date(2027, 3, 30))]
    lps = [LocalProgram(n, "Tue", "5:30", [6], [], True, rng, row=i, side="Girls", short=s)
           for i, (n, s) in enumerate([("Girls 14s Locals", "L14"), ("Girls 14s Tue", "L14T"),
                                       ("Dave's Invite", "L14D")])]
    tns = [Tournament("SDVBC", "local", j, j, [], [1, 2], [], program="Girls 14s Locals", side="Girls"),
           Tournament("SDVBC", "local", j, j, [], [3, 4], [], program="Dave's Invite", side="Girls"),
           Tournament("SDVBC", "local", f, f, [], [1, 2], [], program="Girls 14s Locals", side="Girls"),
           Tournament("SDVBC", "local", f, f, [], [3, 4], [], program="Girls 14s Tue", side="Girls")]
    links = gcal.with_program_shorthand({("Program", "L14"): "c14", ("Program", "L14T"): "c14t",
                                         ("Program", "L14D"): "cdave", ("Club", "SDVBC"): "girls",
                                         ("Club", "Seaside"): "boys"}, lps)
    for sides in ({lp.name: lp.side for lp in lps}, {}):
        evs, _ = gcal.desired_events(None, None, tns, [], links, date(2027, 1, 1), date(2027, 3, 31), "", "",
                                     sides, lps)
        on = lambda cid: sorted(e.start for e in evs if e.calendar_id == cid)  # noqa: E731
        assert on("cdave") == [j] and on("c14") == [j, f] and on("c14t") == [f]
        assert on("girls") == [j, f] and on("boys") == []
    assert gcal.program_span(lps[2:], {"Dave's Invite"},
                             tns + [Tournament("SDVBC", "local", date(2027, 5, 15), date(2027, 5, 15), [], [1], [],
                                               program="Dave's Invite")]) == (date(2027, 1, 5), date(2027, 5, 15))


def test_merged_tournament_keeps_each_programs_own_days():
    """Nov 19, 2026: the 14s locals' "SDVBC" runs Sat-Sun, Dave's Invite's "SDVBC" only the Saturday (no
    last day). The club calendar gets one Sat-Sun event; Dave's Invite's calendar only Saturday; and the
    court view's Sunday has only the locals' courts."""
    from scheduler.data import Tournament, merge_local_tournaments
    sat, sun = date(2027, 1, 23), date(2027, 1, 24)
    tns = [Tournament("SDVBC", "local", sat, sun, [], [1, 2, 3], [], program="Girls 14s Locals", side="Girls"),
           Tournament("SDVBC", "local", sat, sat, [], [5, 6], [], program="Dave's Invite", side="Girls")]
    links = {("Program", "Girls 14s Locals"): "c14", ("Program", "Dave's Invite"): "cdave",
             ("Club", "SDVBC"): "girls", ("Club", "Seaside"): "boys"}
    evs, _ = gcal.desired_events(None, None, tns, [], links, date(2027, 1, 1), date(2027, 1, 31))
    span = {e.calendar_id: (e.start, e.end) for e in evs}
    assert span["cdave"] == (sat, sun)            # all-day: Saturday only (end is exclusive)
    assert span["c14"] == (sat, date(2027, 1, 25)) and span["girls"] == (sat, date(2027, 1, 25))
    assert "boys" not in span and len(evs) == 3
    days = merge_local_tournaments(tns, by_day=True)
    assert [(t.start, t.courts, t.programs) for t in days] == [
        (sat, [1, 2, 3, 5, 6], ["Girls 14s Locals", "Dave's Invite"]), (sun, [1, 2, 3], ["Girls 14s Locals"])]
