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


def _save(store, name, seed, pid=None):
    """Saves a solved November as the club's schedule (or, with pid, as an
    old saved schedule from before there was one)."""
    from scheduler.storage import MAIN_ID
    inp = simulate(4, 2, 2, seed=seed, start=date(2026, 11, 1), end=date(2026, 11, 30))
    r = solve(inp, date(2026, 11, 1), date(2026, 11, 30), time_limit_s=10)
    pid = pid or MAIN_ID
    store.save(pid, name, make_payload(inp, date(2026, 11, 1), date(2026, 11, 30),
                                       {"time_limit": 10, "makeup": True}, r))
    return pid


def test_one_schedule_new_dates_and_switching_between_them(db):
    """The club has one schedule; new dates are added to it, both stay in
    the Dates menu, and each keeps its own built schedule."""
    from streamlit.testing.v1 import AppTest
    from scheduler.storage import MAIN_ID, all_periods
    pid = _save(db, "November", 1)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    assert not at.exception
    assert at.session_state["project_id"] == MAIN_ID
    nov_practices = len(at.session_state["result"].assignments)
    at.sidebar.date_input(key="new_from").set_value(date(2026, 12, 5))
    at.sidebar.date_input(key="new_to").set_value(date(2026, 12, 20))
    next(b for b in at.sidebar.button if b.label == "Start these dates").click().run()
    assert not at.exception, at.exception
    assert at.session_state["range_start"] == date(2026, 12, 5)
    assert at.session_state["result"] is not None and at.session_state["result"].start == date(2026, 12, 5)
    periods = all_periods(db.get(pid)["payload"])
    assert set(periods) == {"2026-11-01_2026-11-30", "2026-12-05_2026-12-20"}
    # Back to November: its schedule is still there; no other saved schedules appeared.
    menu = next(sb for sb in at.sidebar.selectbox if sb.label == "Dates")
    menu.set_value("2026-11-01_2026-11-30").run()
    assert not at.exception
    assert at.session_state["range_start"] == date(2026, 11, 1)
    assert len(at.session_state["result"].assignments) == nov_practices
    assert [p["id"] for p in db.list()] == [MAIN_ID]
    # A refresh opens the dates last worked on, with both still listed.
    at2 = AppTest.from_file("../app.py", default_timeout=180)
    at2.run()
    assert at2.session_state["range_start"] == date(2026, 11, 1)
    assert len(next(sb for sb in at2.sidebar.selectbox if sb.label == "Dates").options) == 2


def test_first_open_moves_an_old_saved_schedule_into_the_one_schedule(db):
    from streamlit.testing.v1 import AppTest
    from scheduler.storage import MAIN_ID
    _save(db, "Demo club", 2, pid="old1")
    _save(db, "November", 3, pid="old2")
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    assert not at.exception
    main = db.get(MAIN_ID)
    assert main is not None and main["payload"]["inputs"] == db.get("old2")["payload"]["inputs"]
    assert any(e.label == "Old saved schedules" for e in at.sidebar.expander)


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
    at.radio(key="nav").set_value("Publish").run()
    next(b for b in at.button if "Publish this schedule" in b.label).click().run()
    assert not at.exception
    assert at.session_state["nav"] == "Publish"
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
    at.radio(key="nav").set_value("Programs").run()
    at.toggle(key="prog_autobuild").set_value(False).run()
    before = at.session_state["result"]
    pick = next(s for s in at.selectbox if s.key and s.key.startswith("edit_pick_"))
    pick.set_value(pick.options[0]).run()
    assert at.session_state["nav"] == "Programs"
    save = next(b for b in at.button if "Save changes" in b.label)
    save.click().run()
    assert not at.exception
    assert at.session_state["nav"] == "Programs"
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
    at.radio(key="nav").set_value("Edit practices").run()
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
    assert at.session_state["nav"] == "Edit practices"
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
    at.radio(key="nav").set_value("Pecking order").run()
    assert not at.exception
    teams = at.session_state["inp"].teams
    first = str(teams["team"].iloc[0])
    age = int(float(teams["age"].iloc[0]))
    assert any("Save ranks" in b.label for b in at.button)
    assert any(e.label.startswith("Import a pecking order") for e in at.expander)
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
    at.radio(key="nav").set_value("Calendar").run()
    assert not at.exception
    labels = [d.proto.label for d in at.get("download_button")]
    assert {"Google Calendar (.ics)", "Printable calendar", "Calendar (Excel)"} <= set(labels)


def test_hand_placed_practice_can_be_moved_again_and_removed(db):
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Moves", 5)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("Edit practices").run()
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


def test_restore_published_version_button(db):
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Restore", 6)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("Publish").run()
    next(b for b in at.button if "Publish this schedule" in b.label).click().run()
    v1 = db.get(pid)["payload"]["published"][0]
    key = lambda df: set(zip(df["name"], df["date"].map(str), df["slot"], df["court"].map(int)))  # noqa: E731
    want = {(p["name"], p["date"], p["slot"], int(p["court"])) for p in v1["practices"]}
    # Rebuild from scratch so the schedule drifts away from v1.
    at.session_state["keep_prev"] = False
    next(b for b in at.button if "Auto-schedule" in b.label).click().run()
    at.session_state["keep_prev"] = True
    at.run()
    next(b for b in at.button if "Restore this version" in b.label).click().run()
    assert not at.exception
    a = at.session_state["result"].assignments
    assert key(a[a["kind"] == "Practice"]) == want
    assert any("Restored" in s.value for s in at.success)
    assert db.get(pid)["payload"]["settings"]["base_version"] == v1["id"]
    # Publishing something new clears it.
    next(b for b in at.button if "Publish this schedule" in b.label).click().run()
    assert db.get(pid)["payload"]["settings"]["base_version"] is None


def test_replacing_inputs_keeps_programs(db, tmp_path):
    import io
    from streamlit.testing.v1 import AppTest
    from scheduler.data import ScheduleInput
    pid = _save(db, "Keep programs", 7)
    before = ScheduleInput.from_records(db.get(pid)["payload"]["inputs"])
    assert len(before.locals)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    assert not at.exception
    assert any(c.label == "Also replace programs and tournaments" and c.value is False for c in at.checkbox)
    # What the replace button does, with a spreadsheet that has no Locals sheet.
    from scheduler.data import keep_programs
    new = ScheduleInput.from_excel(io.BytesIO(before.to_excel_bytes()))
    new.locals = new.locals.iloc[0:0]
    keep_programs(new, at.session_state["inp"])
    assert len(new.locals) == len(before.locals)


def _open(db, name, seed):
    from streamlit.testing.v1 import AppTest
    pid = _save(db, name, seed)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    assert not at.exception
    return at, pid


def test_book_a_private_and_it_saves(db):
    at, pid = _open(db, "Privates", 10)
    at.radio(key="nav").set_value("Privates").run()
    coach = at.selectbox(key="pv_coach").options[0]
    at.selectbox(key="pv_coach").set_value(coach).run()
    at.toggle(key="pv_is_team").set_value(True).run()
    next(b for b in at.button if b.label == "Book private").click().run()
    assert not at.exception
    pr = at.session_state["inp"].privates
    assert len(pr) == 1 and pr.iloc[0]["coach"] == coach and pr.iloc[0]["type"] == "Team"
    assert pr.iloc[0]["court"] == "P" and pr.iloc[0]["time"] == "4:00 PM"
    assert len(db.get(pid)["payload"]["inputs"]["Privates"]) == 1
    assert any("privates" in d.label.lower() for d in at.get("download_button"))


def test_add_extra_practice_and_three_a_week(db):
    at, _ = _open(db, "Extras", 11)
    at.radio(key="nav").set_value("Edit practices").run()
    team = at.selectbox(key="ed_team").value
    at.radio(key="ed_mode").set_value("Add a practice").run()
    at.date_input(key="ed_ad").set_value(date(2026, 11, 4)).run()   # a Wednesday
    next(b for b in at.button if b.label == "Add it").click().run()
    assert not at.exception, at.exception
    ov = at.session_state["inp"].overrides
    assert ov.iloc[-1]["action"] == "Extra" and ov.iloc[-1]["team"] == team
    assert at.session_state["result"].assignments["extra"].fillna(False).any()
    at.radio(key="ed_mode").set_value("Practices per week").run()
    at.number_input(key=f"ed_pw_{team}").set_value(3).run()
    next(b for b in at.button if b.label == "Save").click().run()
    assert not at.exception
    t = at.session_state["inp"].teams
    assert t.loc[t["team"] == team, "practices"].iloc[0] == "3"


def test_cancel_one_program_session(db):
    at, _ = _open(db, "One session", 12)
    at.radio(key="nav").set_value("Programs").run()
    prog = next(s for s in at.selectbox if s.key and s.key.startswith("one_prog_")).value
    d = next(s for s in at.selectbox if s.key and s.key.startswith("one_cancel_")).options[0]
    next(b for b in at.button if b.label == "Cancel this session").click().run()
    assert not at.exception, at.exception
    loc = at.session_state["inp"].locals
    assert loc[loc["program"] == prog]["skip"].astype(str).str.len().max() > 0
    a = at.session_state["result"].assignments
    assert not ((a["kind"] == "Local") & (a["name"] == prog) & (a["date"].map(lambda x: f"{x:%a %b %-d}") == d)).any()


# --------------------------------------------------------- roles (stage 1) --

def _as(db, monkeypatch, email, seed=20, pid=None):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("COURT_DEV_USER", email)
    pid = pid or _save(db, "Roles", seed)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    assert not at.exception, at.exception
    return at, pid


def _head_of_first_team(db, pid):
    from scheduler import clean
    from scheduler.storage import read_payload
    inp = read_payload(db.get(pid)["payload"])[0]
    t = clean(inp, date(2026, 11, 1), date(2026, 11, 30)).teams[0]
    return t.heads[0], t.name


def test_first_sign_in_seeds_admins_and_coach_sees_limited_app(db, monkeypatch):
    pid = _save(db, "Roles", 21)
    head, team = _head_of_first_team(db, pid)
    at, _ = _as(db, monkeypatch, "newcoach@sdvbc.org", pid=pid)
    assert db.get_user("matthew@sdvbc.org")["app_admin"] and db.get_user("cliff@sdvbc.org")["sched_admin"]
    views = at.radio(key="nav").options
    assert "Programs" not in views and "Inputs" not in views and "Access" not in views
    assert "Review" not in views                                     # admins only
    assert not any("Auto-schedule" in b.label for b in at.button)
    # Not linked yet: can't edit any team.
    at.radio(key="nav").set_value("Edit practices").run()
    assert any("isn't linked" in i.value for i in at.info)
    # Linked to a head coach: only their teams, preferences only.
    db.save_user("newcoach@sdvbc.org", "New", head)
    at, _ = _as(db, monkeypatch, "newcoach@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Edit practices").run()
    assert team in at.selectbox(key="ed_team").options
    assert at.radio(key="ed_mode").options == ["Suggest a preference"]


def test_coach_preference_saves_without_rebuild_or_overwriting(db, monkeypatch):
    from scheduler.storage import make_payload, read_payload, read_published
    pid = _save(db, "Roles", 22)
    head, team = _head_of_first_team(db, pid)
    db.save_user("coach@sdvbc.org", "Coach", head)
    at, _ = _as(db, monkeypatch, "coach@sdvbc.org", pid=pid)
    before = db.get(pid)["payload"]["result"]["assignments"]
    # Meanwhile an admin adds a closure in another session.
    rec = db.get(pid)
    i_, s_, e_, set_, r_ = read_payload(rec["payload"])
    i_.closures = pd.concat([i_.closures, pd.DataFrame([{"date": "11/20/2026", "reason": "Gym floor"}])],
                            ignore_index=True)
    db.save(pid, rec["name"], make_payload(i_, s_, e_, set_, r_, read_published(rec["payload"])))
    at.radio(key="nav").set_value("Edit practices").run()
    at.selectbox(key="ed_team").set_value(team).run()
    pk = f"pref_{team}"
    at.multiselect(key=f"{pk}_pd").set_value(["Thu"]).run()
    next(b for b in at.button if b.label == "Save preference").click().run()
    assert not at.exception, at.exception
    pay = db.get(pid)["payload"]
    assert any(w["team"] == team and w["days"] == "Thu" for w in pay["inputs"]["Wishlist"])
    assert any(c["reason"] == "Gym floor" for c in pay["inputs"]["Closures"])   # admin's edit kept
    assert pay["result"]["assignments"] == before                              # no rebuild by a coach
    assert team in pay["settings"]["pending_prefs"][-1]
    assert db.activity()[0]["action"] == "Preference"


def test_app_admin_can_view_as_and_manage_access(db, monkeypatch):
    at, pid = _as(db, monkeypatch, "matthew@sdvbc.org", seed=23)
    views = at.radio(key="nav").options
    assert "Access" in views and "Programs" not in views          # app admin only, not scheduling admin
    at.radio(key="nav").set_value("Access").run()
    assert not at.exception
    at.selectbox(key="view_as").set_value("Scheduling admin").run()
    views = at.radio(key="nav").options
    assert "Programs" in views and "Access" not in views   # View as shows exactly what that role sees
    assert any("Auto-schedule" in b.label for b in at.button)
    at.selectbox(key="view_as").set_value("Myself").run()
    assert "Access" in at.radio(key="nav").options


def test_scheduling_admin_has_everything_but_access(db, monkeypatch):
    at, _ = _as(db, monkeypatch, "cliff@sdvbc.org", seed=24)
    db.save_user("cliff@sdvbc.org", "Cliff", "", False, True)
    at, _ = _as(db, monkeypatch, "cliff@sdvbc.org", seed=24)
    views = at.radio(key="nav").options
    assert "Programs" in views and "Publish" in views and "Access" not in views


def test_outside_addresses_are_refused():
    from scheduler import access
    assert access.email_allowed("x@sdvbc.org") and not access.email_allowed("x@gmail.com")
    assert access.email_allowed("matthew@sdvbc.org")


def test_coach_adds_own_no_date_and_sees_only_their_things(db, monkeypatch):
    pid = _save(db, "Roles", 25)
    head, team = _head_of_first_team(db, pid)
    db.save_user("coach@sdvbc.org", "Coach", head)
    at, _ = _as(db, monkeypatch, "coach@sdvbc.org", pid=pid)
    assert "Access" not in at.radio(key="nav").options
    at.radio(key="nav").set_value("Court view").run()
    keys = {t.key for t in at.toggle}
    assert "hl_mine" in keys and "show_moved" not in keys
    at.toggle(key="hl_mine").set_value(True).run()
    assert not at.exception
    at.radio(key="nav").set_value("Coaches").run()
    assert at.selectbox(key=f"coach_pick_{head}").value == head      # opens on them
    nk = f"nd_{head}"
    at.multiselect(key=f"{nk}_days").set_value(["Fri"]).run()
    next(b for b in at.button if b.key == f"{nk}_add").click().run()
    assert not at.exception, at.exception
    av = db.get(pid)["payload"]["inputs"]["Availability"]
    assert any(r["who"] == head and r["type"] == "No" and r["days"] == "Fri" for r in av)


def test_program_tournament_dates_round_trip_through_the_form(db):
    """A program's local tournaments show in its form and survive a save."""
    from streamlit.testing.v1 import AppTest
    from scheduler.storage import read_payload
    inp = simulate(4, 2, 2, seed=26, start=date(2026, 11, 1), end=date(2026, 11, 30))
    prog = str(inp.locals.iloc[0]["program"])
    inp.tournaments = pd.concat([inp.tournaments, pd.DataFrame([
        {"name": "Local A", "type": "Local", "start_date": "11/07/2026", "end_date": "11/07/2026", "program": prog,
         "courts": "1, 2, 3, 4"},
        {"name": "Local B", "type": "Local", "start_date": "11/07/2026", "end_date": "11/07/2026", "program": prog,
         "courts": "5, 6, 7"}])], ignore_index=True)
    r = solve(inp, date(2026, 11, 1), date(2026, 11, 30), time_limit_s=10)
    pid = new_id()
    db.save(pid, "Prog T", make_payload(inp, date(2026, 11, 1), date(2026, 11, 30), {"time_limit": 10}, r))
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("Programs").run()
    pick = next(s for s in at.selectbox if s.key and s.key.startswith("edit_pick_"))
    pick.set_value(next(o for o in pick.options if o.startswith(prog))).run()
    next(b for b in at.button if b.label == "Save changes").click().run()
    assert not at.exception, at.exception
    t = at.session_state["inp"].tournaments
    mine = t[t["program"] == prog]
    assert sorted(mine["name"]) == ["Local A", "Local B"] and set(mine["start_date"]) == {"11/07/2026"}
    saved = read_payload(db.get(pid)["payload"])[0].tournaments
    assert len(saved[saved["program"] == prog]) == 2


def test_private_start_uses_12_hour_clock(db):
    at, _ = _open(db, "Privates 12h", 27)
    at.radio(key="nav").set_value("Privates").run()
    at.selectbox(key="pv_coach").set_value(at.selectbox(key="pv_coach").options[0]).run()
    at.selectbox(key="pv_hour").set_value(9).run()
    at.selectbox(key="pv_min").set_value(30).run()
    at.radio(key="pv_ampm").set_value("AM").run()
    next(b for b in at.button if b.label == "Book private").click().run()
    assert not at.exception, at.exception
    assert at.session_state["inp"].privates.iloc[-1]["time"] == "9:30 AM"


def test_import_adds_new_teams_and_replaces_no_dates(db):
    """Importing the coaches' spreadsheet keeps the club's teams and programs,
    adds the new teams, and brings in the file's no-dates and wishlists."""
    import io
    from streamlit.testing.v1 import AppTest
    from scheduler.data import ScheduleInput, merge_import
    _save(db, "November", 30)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    assert not at.exception
    club = at.session_state["inp"]
    club.locals.loc[0, "program"] = "Renamed local"
    club.teams.loc[0, "head_coach"] = "Edited Coach"
    first = club.teams.loc[0, "team"]
    coaches = simulate(3, 2, 1, seed=99, start=date(2026, 12, 5), end=date(2026, 12, 20))
    coaches.teams.loc[0, "team"] = first            # a team the club already has
    new = ScheduleInput.from_excel(io.BytesIO(coaches.to_excel_bytes()))
    merge_import(new, club, date(2026, 12, 5))
    assert "Renamed local" in set(new.locals["program"])
    assert new.teams.loc[new.teams["team"] == first, "head_coach"].iloc[0] == "Edited Coach"
    assert set(club.teams["team"]) <= set(new.teams["team"])
    assert set(coaches.teams["team"]) <= set(new.teams["team"])
    assert len(new.teams) == len(set(club.teams["team"]) | set(coaches.teams["team"]))   # no duplicates


def test_open_days_form_opens_a_break_in_blocks(db):
    from streamlit.testing.v1 import AppTest
    _save(db, "November", 31)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    at.radio(key="nav").set_value("Open days").run()
    assert not at.exception
    at.date_input(key="o_from").set_value(date(2026, 11, 23))
    at.date_input(key="o_to").set_value(date(2026, 11, 25))
    at.text_input(key="o_note").set_value("Thanksgiving week")
    next(b for b in at.button if b.label == "Open these days").click().run()
    assert not at.exception, at.exception
    pr = at.session_state["result"].assignments
    pr = pr[(pr["kind"] == "Practice") & pr["date"].isin([date(2026, 11, d) for d in (23, 24, 25)])]
    assert len(pr) and set(pr["slot"]) <= {"9AM", "11AM", "1PM", "3PM", "5PM", "7PM"}
    assert "11/23/2026-11/25/2026" in set(at.session_state["inp"].weekends["date"])


def test_coach_opens_on_the_published_dates_covering_today(db, monkeypatch):
    """A coach lands on the published dates that include today, not the
    dates an admin happens to be working on."""
    from scheduler.storage import MAIN_ID, current_period, make_payload, read_payload
    from datetime import timedelta
    today = date.today()
    pid = _save(db, "Main", 32)
    rec = db.get(pid)
    inp, s_, e_, settings, r = read_payload(rec["payload"])
    live = dict(current_period(rec["payload"]), start=(today - timedelta(days=3)).isoformat(),
                end=(today + timedelta(days=3)).isoformat(), published=[{"id": "v1", "at": "x"}])
    db.save(pid, "Main", make_payload(inp, today + timedelta(days=40), today + timedelta(days=50), settings, None,
                                      periods={"live": live}))
    at, _ = _as(db, monkeypatch, "somecoach@sdvbc.org", pid=pid)
    assert at.session_state["range_start"] == today - timedelta(days=3)


def _with_clash(db):
    """The club's schedule with one coach at two travel tournaments one weekend."""
    from scheduler.storage import MAIN_ID, make_payload, read_payload
    from scheduler import clean
    pid = _save(db, "Flags", 40)
    rec = db.get(pid)
    inp, s_, e_, settings, r = read_payload(rec["payload"])
    t = clean(inp, s_, e_).teams
    coach = t[0].heads[0]
    other = next(x for x in t[1:] if coach not in x.staff)
    inp.teams.loc[inp.teams["team"] == other.name, "assistants"] = coach
    inp.tournaments = pd.DataFrame([
        {"name": "Vegas Classic", "type": "Travel", "start_date": "11/14/2026", "end_date": "11/15/2026",
         "teams": t[0].name},
        {"name": "Desert Duel", "type": "Travel", "start_date": "11/14/2026", "end_date": "11/15/2026",
         "teams": other.name}])
    db.save(pid, "Flags", make_payload(inp, s_, e_, settings, r))
    return pid, coach


def test_coverage_flag_resolves_and_reopens(db):
    from streamlit.testing.v1 import AppTest
    from scheduler.storage import MAIN_ID
    pid, coach = _with_clash(db)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    assert not at.exception
    assert any("1 coverage flag to sort out" in e.label for e in at.expander)
    next(b for b in at.button if b.label == "Mark resolved").click().run()
    assert not at.exception, at.exception
    assert any("all sorted out" in e.label and "1 resolved" in e.label for e in at.expander)
    res = db.get(MAIN_ID)["payload"]["inputs"]["Resolved"]
    assert len(res) == 1 and res[0]["flag"].startswith(f"tt|{coach}|")
    at.toggle(key="flags_top_showdone").set_value(True).run()
    next(b for b in at.button if b.label == "Reopen").click().run()
    assert any("1 coverage flag to sort out" in e.label for e in at.expander)


def test_edit_a_tournament(db):
    from streamlit.testing.v1 import AppTest
    _with_clash(db)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    at.radio(key="nav").set_value("Tournaments").run()
    sel = next(sb for sb in at.selectbox if sb.label == "Tournament")
    sel.set_value(1).run()                           # Desert Duel
    name = next(ti for ti in at.text_input if ti.label == "Tournament name")
    assert name.value == "Desert Duel"
    name.set_value("Desert Duel 2").run()
    next(d for d in at.date_input if d.label == "Start date").set_value(date(2026, 11, 21)).run()
    next(d for d in at.date_input if d.label == "End date").set_value(date(2026, 11, 22)).run()
    next(b for b in at.button if b.label == "Save changes").click().run()
    assert not at.exception, at.exception
    t = at.session_state["inp"].tournaments
    assert list(t["name"]) == ["Vegas Classic", "Desert Duel 2"] and t.iloc[1]["start_date"] == "11/21/2026"
    assert not any("coverage flag to sort out" in e.label for e in at.expander)   # no longer the same weekend


def test_show_every_date_scheduled(db):
    from streamlit.testing.v1 import AppTest
    _save(db, "November", 41)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    at.sidebar.date_input(key="new_from").set_value(date(2026, 12, 1))
    at.sidebar.date_input(key="new_to").set_value(date(2026, 12, 14))
    next(b for b in at.sidebar.button if b.label == "Start these dates").click().run()
    assert not at.exception
    at.radio(key="nav").set_value("Court view").run()
    at.toggle(key="all_dates").set_value(True).run()
    assert not at.exception, at.exception
    show = next(sb for sb in at.selectbox if sb.label == "Show")
    assert "Week of Nov 1" in show.options and "Week of Dec 6" in show.options
    show.set_value("Week of Nov 1").run()
    assert not at.exception
    at.radio(key="nav").set_value("Calendar").run()
    assert not at.exception
    assert at.session_state["range_start"] == date(2026, 12, 1)    # still working on December


def test_new_dates_show_programs_before_auto_schedule(db):
    """Dates that haven't been built yet still show their programs on the
    court view and calendar."""
    from streamlit.testing.v1 import AppTest
    _save(db, "November", 42)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    at.sidebar.date_input(key="new_from").set_value(date(2026, 12, 6))
    at.sidebar.date_input(key="new_to").set_value(date(2026, 12, 20))
    next(b for b in at.sidebar.button if b.label == "Start these dates").click().run()
    at.session_state["result"] = None            # as if it hadn't been built
    at.radio(key="nav").set_value("Court view").run()
    assert not at.exception, at.exception
    assert any("Not auto-scheduled yet" in i.value for i in at.info)
    assert any("Winter" in m.value or "cs-grid" in m.value for m in at.markdown)
    show = next(sb for sb in at.selectbox if sb.label == "Show")
    assert "Week of Dec 6" in show.options and "Week of Dec 20" in show.options
    show.set_value("Week of Dec 13").run()
    assert not at.exception
    at.radio(key="nav").set_value("Calendar").run()
    assert not at.exception, at.exception
    assert any("cs-cal" in m.value or "<table" in m.value for m in at.markdown)
    assert at.session_state["result"] is None    # nothing was auto-scheduled
