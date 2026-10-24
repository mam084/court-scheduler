"""App-level tests (Streamlit's AppTest) against a throwaway database."""

from datetime import date

import pytest

from scheduler import simulate, solve
from scheduler.storage import Store, make_payload, new_id


@pytest.fixture()
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    return Store(url)


def _save(store, name, seed):
    inp = simulate(4, 2, 2, seed=seed, start=date(2026, 11, 1), end=date(2026, 11, 30))
    r = solve(inp, date(2026, 11, 1), date(2026, 11, 30), time_limit_s=10)
    pid = new_id()
    store.save(pid, name, make_payload(inp, date(2026, 11, 1), date(2026, 11, 30),
                                       {"time_limit": 10, "makeup": True}, r))
    return pid


def test_open_schedule_stays_open_through_autosaves(db):
    from streamlit.testing.v1 import AppTest
    # Several schedules with the same name: the menu must never jump between them.
    ids = [_save(db, "November", seed) for seed in (1, 2, 3)]
    target = ids[1]
    at = AppTest.from_file("../app.py", default_timeout=120)
    at.query_params["project"] = target
    at.run()
    assert not at.exception
    assert at.session_state["project_id"] == target
    at.session_state["makeup"] = False   # a change -> autosave -> "last updated" order changes
    at.run()
    for _ in range(2):
        at.run()
    assert at.session_state["project_id"] == target
    assert db.get(target)["payload"]["settings"]["makeup"] is False


def test_link_reopens_schedule_with_its_programs(db):
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Nov test", 4)
    n_locals = len(db.get(pid)["payload"]["inputs"]["Locals"])
    at = AppTest.from_file("../app.py", default_timeout=120)
    at.query_params["project"] = pid
    at.run()
    assert not at.exception
    assert len(at.session_state["inp"].locals) == n_locals
    a = at.session_state["result"].assignments
    assert (a["kind"] == "Local").any()
