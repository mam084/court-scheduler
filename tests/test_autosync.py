"""Automatic Google Calendar updates and problem reports."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from scheduler import autosync, problems, simulate, solve
from scheduler.storage import MAIN_ID, Store, make_payload
from tests.test_gcal import FakeGoogle

S, E = date(2026, 11, 1), date(2026, 11, 30)


@pytest.fixture()
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'a.db'}")


def _setup(store, published=True):
    inp = simulate(6, 4, 4, seed=7, start=S, end=E)
    r = solve(inp, S, E, time_limit_s=10)
    teams = sorted(set(r.assignments.loc[r.assignments["kind"] == "Practice", "name"]))
    clubs = sorted({c for c in inp.teams["club"]})
    inp.calendars = pd.DataFrame([{"kind": "Team", "name": t, "calendar_id": f"team-{t}"} for t in teams]
                                 + [{"kind": "Club", "name": c, "calendar_id": f"club-{c}"} for c in clubs])
    pub = [{"id": "v1", "label": "First", "at": "2026-10-30T12:00:00+00:00", "practices": []}] if published else []
    store.save(MAIN_ID, "Main", make_payload(inp, S, E, {}, r, pub))
    return r, teams


@pytest.fixture()
def google(monkeypatch):
    g = FakeGoogle()
    monkeypatch.setattr(autosync, "client_for", lambda store, secrets: g)
    return g


def test_off_by_default_and_sync_now_still_works(store, google):
    r, teams = _setup(store)
    assert autosync.settings(store) == {"on": False, "extras": False}
    assert autosync.sync(store, {}).skipped == "automatic updates are off"
    assert google.writes == 0
    out = autosync.sync(store, {}, force=True)
    n = (r.assignments["kind"] == "Practice").sum()
    assert out.added == 2 * n and not out.errors        # each practice on its team and its club calendar
    assert autosync.sync(store, {}, force=True).summary() == "already up to date"


def test_unpublished_dates_are_never_sent(store, google):
    _setup(store, published=False)
    autosync.save_settings(store, True, False)
    out = autosync.sync(store, {})
    assert google.writes == 0 and out.skipped == "nothing published to sync"


def test_a_change_after_publishing_reaches_the_calendar(store, google):
    from scheduler.storage import read_payload
    r, teams = _setup(store)
    autosync.save_settings(store, True, False)
    autosync.sync(store, {})
    rec = store.get(MAIN_ID)
    inp, s_, e_, settings, res = read_payload(rec["payload"])
    a = res.assignments
    i = a.index[a["kind"] == "Practice"][0]
    team, old_slot = a.at[i, "name"], a.at[i, "slot"]
    a.at[i, "slot"] = "7:20" if old_slot != "7:20" else "5:30"
    store.save(MAIN_ID, "Main", make_payload(inp, s_, e_, settings, res, rec["payload"]["published"]))
    out = autosync.sync(store, {}, teams={team})
    assert (out.added, out.removed) == (2, 2)            # moved on its team calendar and its club calendar
    assert autosync.last_run(store)["teams"] == [team]


def test_only_the_teams_calendars_are_read_after_an_approval(store, google):
    r, teams = _setup(store)
    autosync.save_settings(store, True, False)
    read = []
    orig = google.app_events
    google.app_events = lambda cid, s, e: read.append(cid) or orig(cid, s, e)
    autosync.sync(store, {}, teams={teams[0]})
    assert f"team-{teams[0]}" in read and not any(c.startswith("team-") and c != f"team-{teams[0]}" for c in read)
    assert sum(c.startswith("club-") for c in read) == 1


def test_evening_job_once_a_day_from_5pm(store, google):
    pt = ZoneInfo("America/Los_Angeles")
    assert not autosync.evening_due(store, datetime(2026, 11, 12, 16, 10, tzinfo=pt))
    assert autosync.evening_due(store, datetime(2026, 11, 12, 17, 5, tzinfo=pt))
    _setup(store)
    autosync.save_settings(store, True, False)
    autosync.sync(store, {}, who="Evening update")
    now = autosync.clock.now()
    if now.hour >= 17:
        assert not autosync.evening_due(store, now)


def test_problems_are_logged_and_emailed_once_an_hour(store):
    store.save_user("boss@sdvbc.org", app_admin=True)
    sent = []
    send = lambda to, subject, text: sent.append((to, subject)) and None  # noqa: E731
    try:
        raise ValueError("boom")
    except ValueError as e:
        assert problems.record(store, "Saving the schedule", e, who="matthew", send=send)
        assert not problems.record(store, "Saving the schedule", e, who="matthew", send=send)
    assert len(sent) == 1 and "boss@sdvbc.org" in sent[0][0]
    log = [x for x in store.activity(10) if x["action"] == "Error"]
    assert len(log) == 2 and "ValueError: boom" in log[0]["detail"]
