"""App-level tests (Streamlit's AppTest) against a throwaway database."""

from datetime import date

import pandas as pd
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


def test_every_section_renders_and_publish_flow(db):
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Sections", 5)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    assert not at.exception
    views = at.radio(key="nav").options
    for v in views:
        at.radio(key="nav").set_value(v).run()
        assert not at.exception, (v, at.exception)
        assert at.session_state["nav"] == v   # stays on the chosen section
    # Publish, then the version is saved with the schedule.
    at.radio(key="nav").set_value("📢 Publish").run()
    next(b for b in at.button if "Publish this schedule" in b.label).click().run()
    assert not at.exception
    assert at.session_state["nav"] == "📢 Publish"
    assert len(db.get(pid)["payload"]["published"]) == 1
    # Rebuild: keeps the published schedule, nothing changed.
    next(b for b in at.button if "Auto-schedule" in b.label).click().run()
    assert not at.exception
    assert at.session_state["result"].metrics["moved_from_previous"] == 0
    assert any("No changes" in s.value for s in at.success)


def test_program_edits_stay_on_programs_and_can_wait_for_rebuild(db):
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Programs", 6)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("🗓️ Programs").run()
    at.toggle(key="prog_autobuild").set_value(False).run()
    before = at.session_state["result"]
    pick = next(s for s in at.selectbox if s.key and s.key.startswith("edit_pick_"))
    pick.set_value(pick.options[0]).run()
    assert at.session_state["nav"] == "🗓️ Programs"
    save = next(b for b in at.button if "Save changes" in b.label)
    save.click().run()
    assert not at.exception
    assert at.session_state["nav"] == "🗓️ Programs"
    assert at.session_state["result"] is before          # no rebuild yet
    assert at.session_state["pending_rebuild"] == "Programs changed"
    next(b for b in at.button if "Auto-schedule" in b.label).click().run()
    assert at.session_state["result"] is not before
    assert "pending_rebuild" not in at.session_state


def test_court_view_opens_on_first_week(db):
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "First week", 8)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    show = next(s for s in at.selectbox if s.label == "Show")
    assert show.value == "Week of Nov 1"
    assert "Weekly template" in show.options


def test_suggest_a_preference_adds_wish_and_not_preferred(db):
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Prefs", 9)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("✏️ Edit practices").run()
    team = at.selectbox(key="ed_team").value
    n_wish, n_avail = len(at.session_state["inp"].wishlist), len(at.session_state["inp"].availability)
    pk = f"pref_{team}"
    at.multiselect(key=f"{pk}_pd").set_value(["Tue"]).run()
    at.multiselect(key=f"{pk}_pt").set_value(["7:20"]).run()
    at.multiselect(key=f"{pk}_ad").set_value(["Tue"]).run()
    at.multiselect(key=f"{pk}_at").set_value(["3:45"]).run()
    next(b for b in at.button if b.label == "Save preference").click().run()
    assert not at.exception
    inp = at.session_state["inp"]
    assert len(inp.wishlist) == n_wish + 1 and len(inp.availability) == n_avail + 1
    w = inp.wishlist.iloc[-1]
    assert (w["team"], w["days"], w["times"], int(w["choice"])) == (team, "Tue", "7:20", 1)
    a = inp.availability.iloc[-1]
    assert (a["who"], a["type"], a["days"], a["times"]) == (team, "Not preferred", "Tue", "3:45")
    assert at.session_state["nav"] == "✏️ Edit practices"
    # Saved with the schedule, and removable again.
    assert len(db.get(pid)["payload"]["inputs"]["Wishlist"]) == n_wish + 1
    drop = at.multiselect(key=f"{pk}_drop")
    drop.set_value([("w", n_wish), ("a", n_avail)]).run()   # the two rows just added
    at.button(key=f"{pk}_dropbtn").click().run()
    assert not at.exception
    assert len(at.session_state["inp"].wishlist) == n_wish
    assert len(at.session_state["inp"].availability) == n_avail


def test_pecking_order_page_and_calendar_downloads(db):
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Pecking", 10)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("🏅 Pecking order").run()
    assert not at.exception
    teams = at.session_state["inp"].teams
    first = str(teams["team"].iloc[0])
    age = int(float(teams["age"].iloc[0]))
    assert any("Save ranks" in b.label for b in at.button)
    assert any(e.label.startswith("📥 Import a pecking order") for e in at.expander)
    # Import: apply the same flow the upload uses.
    from scheduler import pecking as pk
    rows = pd.DataFrame([{"rank": f"{age}-2", "team": first, "head": "", "assistants": "", "side": ""}])
    inp = at.session_state["inp"]
    inp.teams = pk.apply(inp.teams, pk.match_teams(rows, inp.teams))
    at.session_state["locals_ver"] = at.session_state["locals_ver"] + 1 if "locals_ver" in at.session_state else 1
    at.session_state["resolve"] = True
    at.run()
    assert not at.exception
    assert str(at.session_state["inp"].teams.loc[at.session_state["inp"].teams["team"] == first, "rank"].iloc[0]) \
        == f"{age}-2"
    saved = db.get(pid)["payload"]["inputs"]["Teams"]
    assert any(r["team"] == first and r["rank"] == f"{age}-2" for r in saved)
    # Calendar page shows the three downloads.
    at.radio(key="nav").set_value("📅 Calendar").run()
    assert not at.exception
    labels = [d.proto.label for d in at.get("download_button")]
    assert {"📆 Google Calendar (.ics)", "🖨️ Printable calendar", "📊 Calendar (Excel)"} <= set(labels)


def test_hand_placed_practice_can_be_moved_again_and_removed(db):
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Moves", 5)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("✏️ Edit practices").run()
    team = at.selectbox(key="ed_team").value
    at.radio(key="ed_mode").set_value("Move a practice").run()

    def practices():
        a = at.session_state["result"].assignments
        return a[(a["kind"] == "Practice") & (a["name"] == team)].sort_values("date")

    first = practices().iloc[0]
    d, old_slot = first["date"], first["slot"]
    pk = f"{team}_{d}_{old_slot}"
    move_btn = next(b for b in at.button if b.label == "Move it")
    assert move_btn.disabled  # nothing changed yet
    others = [s for s in ("3:45", "5:30", "7:20") if s != old_slot]
    for new_slot in others:  # move it, then move the hand-placed practice again
        at.selectbox(key=f"ed_ns_{pk}").set_value(new_slot).run()
        next(b for b in at.button if b.label == "Move it").click().run()
        assert not at.exception
        assert not at.error, [e.value for e in at.error]
        p = practices()
        assert ((p["date"] == d) & (p["slot"] == new_slot)).any()
        assert at.success  # "Moved ..." confirmation
        pk = f"{team}_{d}_{new_slot}"
    adds = at.session_state["inp"].overrides
    assert (adds["action"] == "Add").sum() <= 1  # each move replaced the previous hand placement
