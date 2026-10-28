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


def test_new_schedule_from_locals_template_then_import_coaches(db):
    """New schedule: starts from the saved locals (no teams), the locals get
    edited, then the coaches' spreadsheet comes in. The edited locals and the
    tournaments stay; the old schedule's teams never show up."""
    import io
    from streamlit.testing.v1 import AppTest
    from scheduler.data import ScheduleInput
    src = _save(db, "Locals template", 30)
    src_inp = ScheduleInput.from_records(db.get(src)["payload"]["inputs"])
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    assert not at.exception
    assert at.sidebar.radio[0].value == "Locals template"
    at.sidebar.text_input[0].set_value("Dec 5 - 20")
    at.sidebar.date_input[0].set_value(date(2026, 12, 5))
    at.sidebar.date_input[1].set_value(date(2026, 12, 20))
    next(b for b in at.sidebar.button if b.label == "Create schedule").click().run()
    assert not at.exception, at.exception
    inp = at.session_state["inp"]
    assert inp.teams.dropna(how="all").empty and inp.privates.dropna(how="all").empty
    assert len(inp.locals) == len(src_inp.locals) and len(inp.tournaments) == len(src_inp.tournaments)
    assert at.session_state["nav"] == "Programs"
    for v in at.radio(key="nav").options:   # every section works with no teams yet
        at.radio(key="nav").set_value(v).run()
        assert not at.exception, (v, at.exception)
    # Edit a program here, then import a coaches' spreadsheet whose Locals differ.
    at.session_state["inp"].locals.loc[0, "program"] = "Renamed local"
    coaches = simulate(3, 2, 1, seed=99, start=date(2026, 12, 5), end=date(2026, 12, 20))
    from scheduler.data import keep_programs
    new = ScheduleInput.from_excel(io.BytesIO(coaches.to_excel_bytes()))
    keep_programs(new, at.session_state["inp"])
    assert "Renamed local" in set(new.locals["program"])
    assert set(new.teams["team"]) == set(coaches.teams["team"])
    assert not set(new.teams["team"]) - set(coaches.teams["team"])
