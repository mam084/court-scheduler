"""Google Calendar sync, against an in-memory stand-in for Google."""

from datetime import date

import pandas as pd

from scheduler import clean, simulate, solve
from scheduler import gcal
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
    mine = [e for e in evs if e.calendar_id == f"team-{team}" and "practice" in e.summary]
    assert len(mine) == (pr["name"] == team).sum()
    club = pr.iloc[0]["club"]
    assert len([e for e in evs if e.calendar_id == f"club-{club}" and "practice" in e.summary]) == \
        (pr["club"] == club).sum()
    loc = r.assignments[r.assignments["kind"] == "Local"]
    prog = loc.iloc[0]["name"]
    sessions = loc[loc["name"] == prog].groupby(["date", "slot"]).ngroups
    assert len([e for e in evs if e.calendar_id == f"prog-{prog}"]) == sessions
    travel = [e for e in evs if "travel tournament" in e.summary]
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
    evs2, _ = gcal.desired_events(pr, loc, data.tournaments, data.teams, links, S, E)
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
