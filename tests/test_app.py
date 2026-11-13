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


def _auto_schedule(at, a, b):
    """Pick dates under Auto-schedule in the sidebar and press it."""
    at.sidebar.date_input(key=next(w.key for w in at.sidebar.date_input if w.key.startswith("as_from"))).set_value(a)
    at.sidebar.date_input(key=next(w.key for w in at.sidebar.date_input if w.key.startswith("as_to"))).set_value(b)
    at.sidebar.button(key="as_go").click().run()
    return at


def test_one_schedule_is_one_calendar(db):
    """The club has one schedule and it's one calendar: an admin schedules some dates, and moving
    around the calendar works on whichever scheduled dates are being looked at (no Dates menu)."""
    from streamlit.testing.v1 import AppTest

    from scheduler.storage import MAIN_ID, all_periods
    pid = _save(db, "November", 1)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    assert not at.exception
    assert at.session_state["project_id"] == MAIN_ID
    assert not any(sb.label == "Dates" for sb in at.sidebar.selectbox)
    nov_practices = len(at.session_state["result"].assignments)
    _auto_schedule(at, date(2026, 12, 5), date(2026, 12, 20))
    assert not at.exception, at.exception
    assert at.session_state["range_start"] == date(2026, 12, 5)
    assert at.session_state["result"] is not None and at.session_state["result"].start == date(2026, 12, 5)
    assert at.session_state["view_day"] == date(2026, 12, 5)
    periods = all_periods(db.get(pid)["payload"])
    assert set(periods) == {"2026-11-01_2026-11-30", "2026-12-05_2026-12-20"}
    # Back a few weeks on the court view: November's schedule, still as it was.
    at.radio(key="nav").set_value("Court view").run()
    for _ in range(3):
        next(b for b in at.button if b.key == "cv_prev").click().run()
    assert not at.exception
    assert at.session_state["range_start"] == date(2026, 11, 1)
    assert len(at.session_state["result"].assignments) == nov_practices
    assert [p["id"] for p in db.list()] == [MAIN_ID]
    # A refresh opens on the calendar with both still there.
    at2 = AppTest.from_file("../app.py", default_timeout=180)
    at2.run()
    assert not at2.exception
    assert len(all_periods(db.get(pid)["payload"])) == 2


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
    # Rebuild: published dates are locked, so it asks first; then keeps the published schedule.
    at.sidebar.button(key="as_go").click().run()
    at.sidebar.button(key="as_replan").click().run()
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
    next(d for d in at.date_input if d.label == "Start date").set_value(date(2026, 11, 1))
    next(d for d in at.date_input if d.label == "End date").set_value(date(2026, 11, 30))
    save = next(b for b in at.button if "Save changes" in b.label)
    save.click().run()
    assert not at.exception
    assert at.session_state["nav"] == "Programs"
    assert at.session_state["result"] is before          # no rebuild yet
    assert at.session_state["pending_rebuild"] == "Programs changed"
    at.sidebar.button(key="as_go").click().run()
    assert at.session_state["result"] is not before
    assert "pending_rebuild" not in at.session_state


def test_court_view_opens_on_first_week(db):
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "First week", 8)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    assert not at.exception
    assert not any(s.label == "Show" for s in at.selectbox)   # no weekly template view (Nov 2026)
    assert any("Week of" in m.value for m in at.markdown)


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
    assert {"Export to personal calendar (.ics)", "Printable calendar · color", "Printable calendar · one page a month",
            "Calendar (Excel)"} <= set(labels)


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
    at.sidebar.button(key="as_go").click().run()
    at.session_state["keep_prev"] = True
    at.run()
    assert not at.exception, at.exception
    if not any("No changes" in s.value for s in at.success):   # the readable list of what changed
        assert any(c.value.startswith("Changes since") and "Week of" in c.value for c in at.code)
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
    assert at.radio(key="pv_type").value == "Player"     # a player private is the default
    at.radio(key="pv_type").set_value("Team").run()
    next(b for b in at.button if b.label == "Book private").click().run()
    assert not at.exception
    pr = at.session_state["inp"].privates
    assert len(pr) == 1 and pr.iloc[0]["coach"] == coach and pr.iloc[0]["type"] == "Team"
    assert pr.iloc[0]["court"] == "P" and pr.iloc[0]["time"] == "4:00 PM"
    assert len(db.get(pid)["payload"]["inputs"]["Privates"]) == 1
    assert any("privates" in d.label.lower() for d in at.get("download_button"))


def _free_time(at, team, day):
    """A weekday time the team doesn't practice at on `day`."""
    a = at.session_state["result"].assignments
    used = set(a[(a["name"] == team) & (a["date"] == day)]["slot"])
    return next(s for s in ("5:30", "7:20", "3:45") if s not in used)


def test_add_extra_practice_and_three_a_week(db):
    at, _ = _open(db, "Extras", 11)
    at.radio(key="nav").set_value("Edit practices").run()
    team = at.selectbox(key="ed_team").value
    at.radio(key="ed_mode").set_value("Add a practice").run()
    at.date_input(key="ed_ad").set_value(date(2026, 11, 4)).run()   # a Wednesday
    at.selectbox(key="ed_as").set_value(_free_time(at, team, date(2026, 11, 4))).run()
    next(b for b in at.button if b.label == "Add it").click().run()
    assert not at.exception, at.exception
    ov = at.session_state["inp"].overrides
    assert ov.iloc[-1]["action"] == "Extra" and ov.iloc[-1]["team"] == team
    assert at.session_state["result"].assignments["extra"].fillna(False).any()
    # The form empties once it's added, so it doesn't say the team already practices then (Nov 2026).
    assert at.date_input(key="ed_ad").value != date(2026, 11, 4)
    assert not any("already practices then" in e.value for e in at.error)
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
    assert not any(b.key == "as_go" for b in at.button)
    # Not linked yet: can't edit any team.
    at.radio(key="nav").set_value("Edit practices").run()
    assert any("isn't linked" in i.value for i in at.info)
    # Linked to a head coach: only their teams, preferences only.
    db.save_user("newcoach@sdvbc.org", "New", head)
    at, _ = _as(db, monkeypatch, "newcoach@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Edit practices").run()
    assert team in at.selectbox(key="ed_team").options
    assert at.radio(key="ed_mode").options == ["Ask to move a practice", "Ask for an extra practice",
                                               "Ask to cancel a practice"]   # no preferences (Nov 2026)


def test_coach_sees_no_every_team_prints(db, monkeypatch):
    """Every team's dates prints are for the scheduling admins (Nov 2026); coaches export their own calendar."""
    pid = _save(db, "Roles", 27)
    head, _ = _head_of_first_team(db, pid)
    db.save_user("coach@sdvbc.org", "Coach", head)
    at, _ = _as(db, monkeypatch, "coach@sdvbc.org", pid=pid)
    for page in ("Court view", "Calendar"):
        at.radio(key="nav").set_value(page).run()
        assert not at.exception, at.exception
        labels = [d.proto.label for d in at.get("download_button")]
        assert not any(lab.startswith("Every team's dates") for lab in labels), (page, labels)
    assert "Export to personal calendar (.ics)" in labels
    at.radio(key="nav").set_value("Coaches").run()
    assert "Export to personal calendar (.ics)" in [d.proto.label for d in at.get("download_button")]


def test_coach_cannot_set_preferences(db, monkeypatch):
    """Preferences are set by the scheduling admins only (Nov 2026); coaches only ask for changes."""
    pid = _save(db, "Roles", 22)
    head, team = _head_of_first_team(db, pid)
    db.save_user("coach@sdvbc.org", "Coach", head)
    at, _ = _as(db, monkeypatch, "coach@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Edit practices").run()
    at.selectbox(key="ed_team").set_value(team).run()
    assert "Suggest a preference" not in at.radio(key="ed_mode").options
    assert not any(m.key == f"pref_{team}_pd" for m in at.multiselect)
    assert not any(b.label == "Save preference" for b in at.button)


def test_app_admin_can_view_as_and_manage_access(db, monkeypatch):
    at, pid = _as(db, monkeypatch, "matthew@sdvbc.org", seed=23)
    views = at.radio(key="nav").options
    assert "Access" in views and "Programs" not in views          # app admin only, not scheduling admin
    at.radio(key="nav").set_value("Access").run()
    assert not at.exception
    at.selectbox(key="view_as").set_value("Scheduling admin").run()
    views = at.radio(key="nav").options
    assert "Programs" in views and "Access" not in views   # View as shows exactly what that role sees
    assert any(b.key == "as_go" for b in at.button)
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


def test_outside_guest_can_sign_in_only_when_allowed(db):
    from scheduler import access
    db.save_user("jane@gmail.com", "Jane", "")
    assert not access.email_allowed("jane@gmail.com", store=db)   # listed = emails only
    access.set_guest(db, "Jane@Gmail.com", True)
    assert access.email_allowed("jane@gmail.com", store=db)
    assert not access.email_allowed("other@gmail.com", store=db)
    assert not access.email_allowed("jane@gmail.com")              # Google connect stays club-only
    access.set_guest(db, "jane@gmail.com", False)
    assert not access.email_allowed("jane@gmail.com", store=db)
    assert db.get_setting(access.GUEST_KEY) is None


def test_access_tab_shows_outside_people(db, monkeypatch):
    from scheduler import access
    db.save_user("jane@gmail.com", "Jane", "")
    access.set_guest(db, "jane@gmail.com", True)
    db.save_user("bob@gmail.com", "Bob", "")
    at, _ = _as(db, monkeypatch, "matthew@sdvbc.org", seed=23)
    at.radio(key="nav").set_value("Access").run()
    assert not at.exception
    assert any("Outside (non-club)" in c.value for c in at.caption)


def test_outside_email_only_person_gets_publish_emails(db):
    from scheduler import access
    db.save_user("jane@gmail.com", "Jane", "Jane Doe")
    assert access.coach_emails(db.users())["Jane Doe"] == "jane@gmail.com"


def test_coach_cannot_add_no_dates_and_sees_only_their_things(db, monkeypatch):
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
    nk = f"nd_{head}"   # no-dates are set by the scheduling admins only (Nov 2026)
    assert not any(m.key == f"{nk}_days" for m in at.multiselect)
    assert not any(b.key == f"{nk}_add" for b in at.button)
    assert any("Tell a scheduling admin" in c.value for c in at.caption)


def test_admin_adds_a_coachs_no_date(db, monkeypatch):
    pid = _save(db, "Roles", 26)
    head, _ = _head_of_first_team(db, pid)
    db.save_user("cliff@sdvbc.org", "Cliff", "", False, True)
    at, _ = _as(db, monkeypatch, "cliff@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Coaches").run()
    at.selectbox(key="coach_pick_").set_value(head).run()
    nk = f"nd_{head}"
    at.multiselect(key=f"{nk}_days").set_value(["Fri"]).run()
    next(b for b in at.button if b.key == f"{nk}_add").click().run()
    assert not at.exception, at.exception
    av = at.session_state["inp"].availability
    assert any(r.who == head and r.type == "No" and r.days == "Fri" for r in av.itertuples())


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
    assert "11/23/2026-11/25/2026" in set(at.session_state["inp"].weekends["date"])
    # Thanksgiving week is all open days now: an open week, so nothing's scheduled automatically.
    pr = at.session_state["result"].assignments
    pr = pr[(pr["kind"] == "Practice") & pr["date"].isin([date(2026, 11, d) for d in (23, 24, 25)])]
    assert pr.empty
    # A coach's practice added by hand goes in a 2-hour block.
    team = at.session_state["inp"].teams["team"].iloc[0]
    at.radio(key="nav").set_value("Edit practices").run()
    at.selectbox(key="ed_team").set_value(team).run()
    at.radio(key="ed_mode").set_value("Add a practice").run()
    at.date_input(key="ed_ad").set_value(date(2026, 11, 3)).run()           # a regular Tuesday
    assert "5:30–7:20" in at.selectbox(key="ed_as").options
    at.selectbox(key="ed_as").set_value("7:20").run()
    at.date_input(key="ed_ad").set_value(date(2026, 11, 24)).run()
    # An open day offers only its 2-hour blocks, and says so (no 3:45 / 5:30 / 7:20 to pick by mistake).
    opts = at.selectbox(key="ed_as").options
    assert not any(o.startswith(("3:45", "5:30", "7:20")) for o in opts) and "11 AM–1 PM" in opts
    assert any("is an open day" in c.value for c in at.caption)
    at.selectbox(key="ed_as").set_value("11AM").run()
    next(b for b in at.button if b.label == "Add it").click().run()
    assert not at.exception, at.exception
    a = at.session_state["result"].assignments
    assert ((a["name"] == team) & (a["date"] == date(2026, 11, 24)) & (a["slot"] == "11AM")).any()


def test_coach_opens_on_the_published_dates_covering_today(db, monkeypatch):
    """A coach lands on the published dates that include today, not the
    dates an admin happens to be working on."""
    from datetime import timedelta

    from scheduler import clock
    from scheduler.storage import current_period, make_payload, read_payload
    today = clock.today()
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
    from scheduler import clean
    from scheduler.storage import make_payload, read_payload
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


def test_calendar_moves_across_scheduled_and_unscheduled_dates(db):
    from streamlit.testing.v1 import AppTest
    _save(db, "November", 41)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    _auto_schedule(at, date(2026, 12, 1), date(2026, 12, 14))
    assert not at.exception
    at.radio(key="nav").set_value("Calendar").run()
    assert not at.exception, at.exception
    assert any("December 2026" in m.value for m in at.markdown)
    next(b for b in at.button if b.key == "cal_prev").click().run()      # November
    assert not at.exception
    assert any("November 2026" in m.value for m in at.markdown)
    assert at.session_state["range_start"] == date(2026, 11, 1)
    for _ in range(3):                                                    # January: not scheduled
        next(b for b in at.button if b.key == "cal_next").click().run()
    assert not at.exception, at.exception
    assert any("Not scheduled yet" in c.value for c in at.caption)
    assert at.session_state["range_start"] == date(2026, 12, 1)          # stays on the last dates looked at


def test_dates_not_scheduled_show_programs_and_nothing_is_built(db):
    """Dates that haven't been scheduled still show their programs on the court view and calendar,
    and looking at them never schedules them."""
    from streamlit.testing.v1 import AppTest

    from scheduler.storage import all_periods
    pid = _save(db, "November", 42)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    at.radio(key="nav").set_value("Court view").run()
    at.date_input(key="cv_pick").set_value(date(2026, 12, 13)).run()
    assert not at.exception, at.exception
    assert any("Not scheduled yet" in c.value for c in at.caption)
    assert any("cs-grid" in m.value for m in at.markdown)
    at.radio(key="nav").set_value("Calendar").run()
    assert not at.exception, at.exception
    assert any("cs-cal" in m.value for m in at.markdown)
    assert set(all_periods(db.get(pid)["payload"])) == {"2026-11-01_2026-11-30"}   # nothing was auto-scheduled


def test_coach_request_then_another_admin_approves(db, monkeypatch):
    pid = _save(db, "Roles", 31)
    head, team = _head_of_first_team(db, pid)
    db.save_user("coach@sdvbc.org", "Coach", head)
    at, _ = _as(db, monkeypatch, "coach@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Edit practices").run()
    at.selectbox(key="ed_team").set_value(team).run()
    at.radio(key="ed_mode").set_value("Ask for an extra practice").run()
    at.date_input(key=f"rq_x_d_{team}").set_value(date(2026, 11, 4)).run()
    at.selectbox(key=f"rq_x_t_{team}").set_value(_free_time(at, team, date(2026, 11, 4))).run()
    next(b for b in at.button if b.key == f"rq_x_go_{team}").click().run()
    assert not at.exception, at.exception
    reqs = db.requests("pending")
    assert len(reqs) == 1 and reqs[0]["email"] == "coach@sdvbc.org" and reqs[0]["kind"] == "extra"
    at.radio(key="nav").set_value("Requests").run()
    assert any(team in m.value for m in at.markdown)
    # Cliff (scheduling admin) approves it: the extra practice is in the rebuilt schedule.
    db.save_user("cliff@sdvbc.org", "Cliff", "", False, True)
    at, _ = _as(db, monkeypatch, "cliff@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Requests").run()
    rid = reqs[0]["id"]
    next(b for b in at.button if b.key == f"rq_ok_{rid}").click().run()
    assert not at.exception, at.exception
    assert db.get_request(rid)["status"] == "approved"
    a = at.session_state["result"].assignments
    assert ((a["name"] == team) & (a["date"] == date(2026, 11, 4)) & (a["extra"] == True)).any()  # noqa: E712


def test_nobody_approves_their_own_request(db, monkeypatch):
    pid = _save(db, "Roles", 32)
    head, team = _head_of_first_team(db, pid)
    db.save_user("cliff@sdvbc.org", "Cliff", head, False, True)
    rid = db.add_request("cliff@sdvbc.org", "Cliff", "extra", team, "", "Extra practice", {"rows": []})
    at, _ = _as(db, monkeypatch, "cliff@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Requests").run()
    assert next(b for b in at.button if b.key == f"rq_ok_{rid}").disabled


def test_coach_requests_a_private(db, monkeypatch):
    pid = _save(db, "Roles", 33)
    head, team = _head_of_first_team(db, pid)
    db.save_user("coach@sdvbc.org", "Coach", head)
    at, _ = _as(db, monkeypatch, "coach@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Privates").run()
    next(b for b in at.button if b.key == "pv_request").click().run()
    assert not at.exception, at.exception
    r = db.requests("pending")[0]
    assert r["kind"] == "private" and r["detail"]["private"]["coach"] == head
    # A scheduling admin sees it on that week's courts, then approves it.
    db.save_user("cliff@sdvbc.org", "Cliff", "", False, True)
    at, _ = _as(db, monkeypatch, "cliff@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Requests").run()
    assert not at.exception, at.exception
    assert any("Requested private" in m.value for m in at.markdown)
    next(b for b in at.button if b.key == f"rq_ok_{r['id']}").click().run()
    assert not at.exception, at.exception
    assert (at.session_state["inp"].privates["coach"] == head).any()


def test_add_coaches_from_schedule(db, monkeypatch):
    at, pid = _as(db, monkeypatch, "matthew@sdvbc.org", seed=34)
    at.radio(key="nav").set_value("Access").run()
    next(b for b in at.button if b.key == "acc_sugg_go").click().run()
    assert not at.exception, at.exception
    linked = [u for u in db.users() if u["coach"]]
    assert len(linked) >= 4 and all(u["email"].endswith("@sdvbc.org") for u in linked)
    assert all(u["email"].split("@")[0] == u["coach"].split()[0].lower() for u in linked)


def test_publish_email_needs_a_connection(db, monkeypatch):
    at, pid = _as(db, monkeypatch, "cliff@sdvbc.org", seed=35)
    db.save_user("cliff@sdvbc.org", "Cliff", "", False, True)
    at, _ = _as(db, monkeypatch, "cliff@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Publish").run()
    at.toggle(key="pub_mail").set_value(True).run()
    next(b for b in at.button if b.label == "Publish this schedule").click().run()
    assert not at.exception, at.exception
    assert any("email isn't connected" in s.value or "Not sent" in s.value for s in at.success)


def test_every_section_has_a_tab_module():
    """The section menu and the tab modules stay in step."""
    from ui.main import SECTIONS
    from ui.nav import ALL_VIEWS
    assert set(SECTIONS) == set(ALL_VIEWS)
    assert all(callable(m.render) for m in SECTIONS.values())


def test_page_values_are_kept_per_visitor(db):
    """S lives in each visitor's own session state: two people's pages never mix
    (one person's admin rights or dates can't leak into another's page)."""
    from streamlit.testing.v1 import AppTest
    _save(db, "November", 43)
    admin = AppTest.from_file("../app.py", default_timeout=180)
    admin.run()
    coach = AppTest.from_file("../app.py", default_timeout=180)
    coach.run()
    assert admin.session_state["_page_state"] is not coach.session_state["_page_state"]
    assert admin.session_state["_page_state"].CAN is True


def test_two_admins_on_different_dates_keep_each_others_work(db):
    """Saving skips re-reading the schedule only when nobody else saved; when
    someone did, their dates are kept."""
    from streamlit.testing.v1 import AppTest

    from scheduler.storage import MAIN_ID, all_periods
    _save(db, "November", 44)
    a = AppTest.from_file("../app.py", default_timeout=180)
    a.run()
    b = AppTest.from_file("../app.py", default_timeout=180)
    b.run()
    # B schedules December (and saves it).
    _auto_schedule(b, date(2026, 12, 1), date(2026, 12, 14))
    assert "2026-12-01_2026-12-14" in all_periods(db.get(MAIN_ID)["payload"])
    # A, still on November, changes something: December must survive A's save.
    a.session_state["makeup"] = False
    a.run()
    assert not a.exception
    periods = all_periods(db.get(MAIN_ID)["payload"])
    assert {"2026-11-01_2026-11-30", "2026-12-01_2026-12-14"} <= set(periods)
    assert db.get(MAIN_ID)["payload"]["settings"]["makeup"] is False
    # A's next change (nobody else saved in between) is saved too.
    a.session_state["time_limit"] = 15
    a.run()
    assert db.get(MAIN_ID)["payload"]["settings"]["time_limit"] == 15
    assert "2026-12-01_2026-12-14" in all_periods(db.get(MAIN_ID)["payload"])


def test_clicking_around_does_not_query_the_database(db):
    """Switching sections makes no database round trips (each one is network
    time on Supabase); a change saves with a handful."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from streamlit.testing.v1 import AppTest
    _save(db, "November", 45)
    n = {"q": 0}

    def count(*_a):
        n["q"] += 1
    event.listen(Engine, "before_cursor_execute", count)
    try:
        at = AppTest.from_file("../app.py", default_timeout=180)
        at.run()
        for tab in ("Calendar", "Court view", "Edit practices", "Review"):
            n["q"] = 0
            at.radio(key="nav").set_value(tab).run()
            assert not at.exception
            assert n["q"] == 0, (tab, n["q"])
        n["q"] = 0
        at.session_state["makeup"] = False
        at.run()
        assert 0 < n["q"] <= 6
    finally:
        event.remove(Engine, "before_cursor_execute", count)


# ------------------------------------------------- batch: Oct 28, 2026 --

def test_scheduling_over_scheduled_dates_trims_them_and_keeps_practices(db):
    """Auto-scheduling dates that overlap scheduled ones takes those days out of the old run (the
    rest of it stays) and starts from the practices already there."""
    from streamlit.testing.v1 import AppTest

    from scheduler.storage import all_periods
    pid = _save(db, "November", 46)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    before = at.session_state["result"].assignments
    before = before[(before["kind"] == "Practice") & before["date"].map(lambda d: d >= date(2026, 11, 15))]
    _auto_schedule(at, date(2026, 11, 15), date(2026, 12, 5))
    assert not at.exception, at.exception
    periods = all_periods(db.get(pid)["payload"])
    assert set(periods) == {"2026-11-01_2026-11-14", "2026-11-15_2026-12-05"}
    nov = periods["2026-11-01_2026-11-14"]["result"]["assignments"]
    assert nov and all(x["date"] <= "2026-11-14" for x in nov)
    now = at.session_state["result"].assignments
    keep = {(r.name, r.date, r.slot) for r in now.itertuples() if r.kind == "Practice"}
    kept = sum((r.name, r.date, r.slot) in keep for r in before.itertuples())
    assert kept >= 0.8 * len(before)
    # The middle of a scheduled run can't be cut out.
    _auto_schedule(at, date(2026, 11, 20), date(2026, 11, 25))
    assert any("in the middle of" in e.value for e in at.sidebar.error)


def test_request_shows_where_it_goes_and_never_bumps_a_team(db, monkeypatch):
    """A coach can't pick a taken court or a time the team already practices; the admin sees the
    request on the court view, and approving leaves the team already there in place."""
    pid = _save(db, "Roles", 47)
    head, team = _head_of_first_team(db, pid)
    db.save_user("coach@sdvbc.org", "Coach", head)
    at, _ = _as(db, monkeypatch, "coach@sdvbc.org", pid=pid)
    a = at.session_state["result"].assignments
    mine = a[(a["kind"] == "Practice") & (a["name"] == team)]
    # A 5:30 another team holds, whose head coach isn't this team's (so the extra can go next to it).
    pr = a[(a["kind"] == "Practice") & (a["slot"] == "5:30") & (a["name"] != team)]
    pr = pr[pr["head"].map(lambda h: head not in str(h)) & pr["date"].map(lambda d: d.weekday() < 5)]
    pr = pr[~pr["date"].isin(set(mine["date"]))]
    assert len(pr)
    day = pr.iloc[0]["date"]
    taken = pr[pr["date"] == day]
    other, court = taken.iloc[0]["name"], int(taken.iloc[0]["court"])
    at.radio(key="nav").set_value("Edit practices").run()
    at.selectbox(key="ed_team").set_value(team).run()
    at.radio(key="ed_mode").set_value("Ask for an extra practice").run()
    # A time the team already practices: can't send.
    own = mine.iloc[0]
    at.date_input(key=f"rq_x_d_{team}").set_value(own["date"]).run()
    at.selectbox(key=f"rq_x_t_{team}").set_value(own["slot"]).run()
    assert any("already practices then" in e.value for e in at.error)
    assert next(b for b in at.button if b.key == f"rq_x_go_{team}").disabled
    # A free time: the taken court isn't offered.
    at.date_input(key=f"rq_x_d_{team}").set_value(day).run()
    at.selectbox(key=f"rq_x_t_{team}").set_value("5:30").run()
    if ((mine["date"] == day) & (mine["slot"] == "5:30")).any():
        return
    assert court not in at.selectbox(key=f"rq_x_c_{team}").options
    next(b for b in at.button if b.key == f"rq_x_go_{team}").click().run()
    assert not at.exception, at.exception
    req = db.requests("pending")[0]
    db.save_user("cliff@sdvbc.org", "Cliff", "", False, True)
    at, _ = _as(db, monkeypatch, "cliff@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Requests").run()
    assert any("requested" in m.value and "cs-grid" in m.value for m in at.markdown)
    put = at.selectbox(key=f"rq_court_{req['id']}")
    assert court not in put.options
    put_on = put.value
    next(b for b in at.button if b.key == f"rq_ok_{req['id']}").click().run()
    assert not at.exception, at.exception
    assert db.get_request(req["id"])["status"] == "approved"
    a2 = at.session_state["result"].assignments
    got = a2[(a2["name"] == team) & (a2["date"] == day) & (a2["slot"] == "5:30")]
    assert len(got) == 1 and int(got.iloc[0]["court"]) == put_on
    still = a2[(a2["name"] == other) & (a2["date"] == day) & (a2["slot"] == "5:30")]
    assert len(still) == 1 and int(still.iloc[0]["court"]) == court

def test_program_needs_start_and_end_date(db):
    from streamlit.testing.v1 import AppTest
    _save(db, "Programs", 48)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    at.radio(key="nav").set_value("Programs").run()
    assert not any(c.label == "Every week" for c in at.checkbox)
    n = len(at.session_state["inp"].locals)
    form_inputs = [t for t in at.text_input if t.label == "Program name"]
    form_inputs[-1].set_value("G 12 Clinic").run()
    next(m for m in reversed(at.multiselect) if m.label == "Days").set_value(["Tue"]).run()
    next(m for m in reversed(at.multiselect) if m.label == "Time").set_value(["5:30"]).run()
    next(b for b in at.button if b.label == "Add program").click().run()
    assert any("start date and an end date" in e.value for e in at.error)
    assert len(at.session_state["inp"].locals) == n


def test_local_tournaments_wear_their_programs_colors():
    from scheduler.data import Tournament
    from scheduler.views import SIDE_COLORS, _tourn_chip
    g = Tournament("G 14 League Tournament", "local", date(2026, 11, 14), date(2026, 11, 14), [], [1, 2], [],
                   side="Girls")
    b = Tournament("Boys Bash", "local", date(2026, 11, 15), date(2026, 11, 15), [], [1, 2], [], side="Boys")
    assert SIDE_COLORS["Girls"][0] in _tourn_chip(g) and SIDE_COLORS["Boys"][0] in _tourn_chip(b)


def test_hand_edits_on_published_dates_move_nobody_else(db):
    """Once dates are published, moving or adding a practice by hand leaves every other team's
    practices exactly as they were; a taken court is refused."""
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Published", 49)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("Publish").run()
    next(b for b in at.button if "Publish this schedule" in b.label).click().run()
    assert not at.exception, at.exception
    a = at.session_state["result"].assignments
    pr = a[a["kind"] == "Practice"]
    day = date(2026, 11, 4)
    # A team not already practicing then (the solver doesn't always place teams the same way).
    busy = set(pr[(pr["date"] == day) & (pr["slot"] == "5:30")]["name"])
    team = next(t for t in pr["name"] if t not in busy)
    others = {(r.name, r.date, r.slot, int(r.court)) for r in pr.itertuples() if r.name != team}
    taken = pr[(pr["date"] == day) & (pr["slot"] == "5:30") & (pr["name"] != team)]
    at.radio(key="nav").set_value("Edit practices").run()
    at.selectbox(key="ed_team").set_value(team).run()
    at.radio(key="ed_mode").set_value("Add a practice").run()
    at.date_input(key="ed_ad").set_value(day).run()
    at.selectbox(key="ed_as").set_value("5:30").run()
    if len(taken):  # a taken court is refused
        at.selectbox(key="ed_ac").set_value(int(taken.iloc[0]["court"])).run()
        next(b for b in at.button if b.label == "Add it").click().run()
        assert any("is taken then" in e.value for e in at.error)
        at.selectbox(key="ed_ac").set_value("Any").run()
    next(b for b in at.button if b.label == "Add it").click().run()
    assert not at.exception, at.exception
    a2 = at.session_state["result"].assignments
    pr2 = a2[a2["kind"] == "Practice"]
    assert ((pr2["name"] == team) & (pr2["date"] == day) & (pr2["slot"] == "5:30")).any()
    assert {(r.name, r.date, r.slot, int(r.court)) for r in pr2.itertuples() if r.name != team} == others


# ------------------------------------------------- batch: Oct 28 (night) --

def test_opening_a_weekend_rebuilds_the_dates_it_is_in(db):
    """Opening a Saturday that's in other scheduled dates (not the ones being worked on) rebuilds
    those dates, so the day shows as open with practices instead of 'weekend not open'."""
    from streamlit.testing.v1 import AppTest

    from scheduler.storage import all_periods
    _save(db, "November", 50)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    _auto_schedule(at, date(2026, 12, 6), date(2026, 12, 20))
    at.radio(key="nav").set_value("Court view").run()
    at.date_input(key="cv_pick").set_value(date(2026, 11, 10)).run()      # back to November
    assert at.session_state["range_start"] == date(2026, 11, 1)
    at.radio(key="nav").set_value("Open days").run()
    at.date_input(key="o_from").set_value(date(2026, 12, 19))
    at.date_input(key="o_to").set_value(date(2026, 12, 19))
    next(b for b in at.button if b.label == "Open these days").click().run()
    assert not at.exception, at.exception
    assert any("Rebuilt Dec 6–20" in s.value for s in at.success)
    dec = all_periods(db.get(at.session_state["project_id"])["payload"])["2026-12-06_2026-12-20"]
    assert "2026-12-19" in dec["result"]["weekend_dates"]
    at.radio(key="nav").set_value("Court view").run()
    at.date_input(key="cv_pick").set_value(date(2026, 12, 19)).run()
    assert not at.exception
    grid = next(m.value for m in at.markdown if "cs-grid" in m.value and "Dec 19" in m.value)
    assert "cs-wkcell" in grid          # Saturday is open (Sunday Dec 20 still isn't)


def test_days_opened_after_building_are_flagged(db):
    from streamlit.testing.v1 import AppTest
    _save(db, "November", 51)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    inp = at.session_state["inp"]
    inp.weekends = pd.concat([inp.weekends, pd.DataFrame([{"date": "11/16/2026", "open": "9AM-1PM"}])],
                             ignore_index=True)
    at.radio(key="nav").set_value("Court view").run()
    at.date_input(key="cv_pick").set_value(date(2026, 11, 16)).run()
    assert not at.exception, at.exception
    assert any("opened after" in w.value and "Mon Nov 16" in w.value for w in at.warning)


def test_programs_take_any_time_on_any_day():
    """A morning camp on weekdays that aren't a break, and a 5:30 program on a Saturday."""
    from scheduler import clean
    inp = simulate(4, 2, 2, seed=52, start=date(2026, 11, 1), end=date(2026, 11, 30))
    inp.locals = pd.concat([inp.locals, pd.DataFrame([
        {"program": "Morning Camp", "side": "Girls", "days": "Mon, Tue", "time": "9AM-11AM", "courts": "1-2",
         "on_site": "Yes", "dates": "11/02/2026-11/10/2026"},
        {"program": "Sat Clinic", "side": "Boys", "days": "Sat", "time": "5:30", "courts": "3",
         "on_site": "Yes", "dates": "11/07/2026-11/07/2026"}])], ignore_index=True)
    data = clean(inp, date(2026, 11, 1), date(2026, 11, 30))
    camp = [lp for lp in data.locals if lp.name == "Morning Camp"]
    assert {lp.day for lp in camp} == {"Mon", "Tue"} and {lp.slot for lp in camp} == {"9AM"}
    assert [lp.slot for lp in data.locals if lp.name == "Sat Clinic"] == ["5:30"]
    r = solve(inp, date(2026, 11, 1), date(2026, 11, 30), time_limit_s=10)
    a = r.assignments
    assert ((a["name"] == "Morning Camp") & (a["date"] == date(2026, 11, 2)) & (a["slot"] == "9AM")).any()


def test_program_on_dates_not_scheduled_yet(db):
    """Adding a program on dates nobody has scheduled saves it without rebuilding anything, and it
    shows on the calendar there."""
    from streamlit.testing.v1 import AppTest
    _save(db, "November", 53)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    before = at.session_state["result"]
    at.radio(key="nav").set_value("Programs").run()
    next(t for t in reversed(at.text_input) if t.label == "Program name").set_value("Winter Camp")
    next(m for m in reversed(at.multiselect) if m.label == "Days").set_value(["Mon", "Tue", "Wed"])
    next(m for m in reversed(at.multiselect) if m.label == "Time").set_value(["9AM"])
    next(m for m in reversed(at.multiselect) if m.label == "Courts used").set_value([1, 2])
    next(d for d in reversed(at.date_input) if d.label == "Start date").set_value(date(2026, 12, 21))
    next(d for d in reversed(at.date_input) if d.label == "End date").set_value(date(2026, 12, 23))
    next(b for b in at.button if b.label == "Add program").click().run()
    assert not at.exception, at.exception
    assert any("nothing needed rebuilding" in s.value for s in at.success)
    assert at.session_state["result"] is before
    at.radio(key="nav").set_value("Calendar").run()
    at.date_input(key="cal_pick").set_value(date(2026, 12, 21)).run()
    assert any("Winter Camp" in m.value for m in at.markdown)


def test_clear_requests(db, monkeypatch):
    pid = _save(db, "Roles", 54)
    r1 = db.add_request("a@sdvbc.org", "A", "extra", "t", "", "Extra one", {"rows": []})
    r2 = db.add_request("b@sdvbc.org", "B", "extra", "t", "", "Extra two", {"rows": []})
    db.decide_request(r2, "denied", "cliff@sdvbc.org")
    db.save_user("cliff@sdvbc.org", "Cliff", "", False, True)
    at, _ = _as(db, monkeypatch, "cliff@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Requests").run()
    next(b for b in at.button if b.key == f"rq_rm_{r1}").click().run()     # changed my mind: no email
    assert not at.exception
    assert db.get_request(r1) is None
    at.checkbox(key="rq_clear_sure").check().run()
    next(b for b in at.button if b.key == "rq_clear").click().run()
    assert db.requests() == []


def test_rename_a_team_everywhere(db):
    """Renaming a team changes it in the inputs, the built schedule, other scheduled dates, published
    versions and waiting requests, without rebuilding."""
    import json

    from streamlit.testing.v1 import AppTest

    from scheduler.storage import MAIN_ID, all_periods
    _save(db, "November", 55)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    at.radio(key="nav").set_value("Publish").run()
    next(b for b in at.button if "Publish this schedule" in b.label).click().run()
    _auto_schedule(at, date(2026, 12, 6), date(2026, 12, 20))
    at.radio(key="nav").set_value("Court view").run()
    at.date_input(key="cv_pick").set_value(date(2026, 11, 10)).run()   # back on November
    a = at.session_state["result"].assignments
    old = a[a["kind"] == "Practice"]["name"].iloc[0]
    rid = db.add_request("c@sdvbc.org", "C", "extra", old, "", f"Extra for {old}",
                         {"rows": [{"team": old, "action": "Extra", "date": "11/20/2026", "time": "5:30"}]})
    before = at.session_state["result"]
    at.radio(key="nav").set_value("Pecking order").run()
    at.selectbox(key="rn_old").set_value(old)
    at.text_input(key="rn_new").set_value("16-Danielle&Darci").run()
    at.button(key="rn_go").click().run()
    assert not at.exception, at.exception
    assert at.session_state["result"] is before                      # nothing rebuilt
    payload = db.get(MAIN_ID)["payload"]
    blob = json.dumps(payload)
    assert "16-Danielle&Darci" in blob and f'"{old}"' not in blob
    assert "16-Danielle&Darci" in set(all_periods(payload)["2026-12-06_2026-12-20"]["result"]["assignments"][i]["name"]
                                      for i in range(len(all_periods(payload)["2026-12-06_2026-12-20"]["result"]
                                                         ["assignments"])))
    req = db.get_request(rid)
    assert req["team"] == "16-Danielle&Darci" and req["detail"]["rows"][0]["team"] == "16-Danielle&Darci"


def test_moves_wait_for_update_schedule(db):
    """Moving practices puts them on the schedule right away without rebuilding; Update schedule then
    rebuilds once, keeping them where they were put."""
    from streamlit.testing.v1 import AppTest
    _save(db, "Staged", 56)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    at.radio(key="nav").set_value("Edit practices").run()
    team = at.selectbox(key="ed_team").value
    at.radio(key="ed_mode").set_value("Move a practice").run()
    a = at.session_state["result"].assignments
    first = a[(a["kind"] == "Practice") & (a["name"] == team)].sort_values("date").iloc[0]
    d, old_slot = first["date"], first["slot"]
    new_slot = next(s for s in ("5:30", "7:20", "3:45") if s != old_slot
                    and not ((a["name"] == team) & (a["date"] == d) & (a["slot"] == s)).any())
    metrics = dict(at.session_state["result"].metrics)
    at.selectbox(key=f"ed_ns_{team}_{d}_{old_slot}").set_value(new_slot).run()
    next(b for b in at.button if b.label == "Move it").click().run()
    assert not at.exception, at.exception
    r = at.session_state["result"]
    assert r.metrics == metrics                                    # not rebuilt
    p = r.assignments[(r.assignments["name"] == team) & (r.assignments["date"] == d)]
    assert list(p["slot"]) == [new_slot]
    assert at.session_state["staged"]["what"]
    at.button(key="upd_go").click().run()
    assert not at.exception, at.exception
    r = at.session_state["result"]
    p = r.assignments[(r.assignments["name"] == team) & (r.assignments["date"] == d)]
    assert list(p["slot"]) == [new_slot]
    assert "staged" not in at.session_state


def test_hand_placed_practice_next_to_a_tournament_is_flagged(db):
    from streamlit.testing.v1 import AppTest

    from scheduler.storage import MAIN_ID
    inp = simulate(4, 2, 2, seed=57, start=date(2026, 12, 6), end=date(2026, 12, 20))
    team = inp.teams["team"].iloc[0]
    inp.tournaments = pd.DataFrame([{"name": "SoCal Cup", "type": "Travel", "start_date": "12/11/2026",
                                     "end_date": "12/13/2026", "teams": team}])
    inp.overrides = pd.DataFrame([{"team": team, "action": "Extra", "date": "12/14/2026", "time": "5:30",
                                   "note": "by hand"}])
    r = solve(inp, date(2026, 12, 6), date(2026, 12, 20), time_limit_s=10)
    db.save(MAIN_ID, "Dec", make_payload(inp, date(2026, 12, 6), date(2026, 12, 20), {"time_limit": 10}, r))
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    assert not at.exception, at.exception
    assert any("the day after SoCal Cup" in w.value and team in w.value for w in at.warning)


def test_add_a_makeup_from_edit_practices(db):
    at, _ = _open(db, "Makeups", 58)
    at.radio(key="nav").set_value("Edit practices").run()
    team = at.selectbox(key="ed_team").value
    at.radio(key="ed_mode").set_value("Makeup practice").run()
    at.date_input(key=f"ed_mk_d_{team}").set_value(date(2026, 11, 3)).run()
    at.button(key=f"ed_mk_go_{team}").click().run()
    assert not at.exception, at.exception
    pc = at.session_state["result"].practice_counts
    row = pc[(pc["team"] == team) & (pc["week_of"] == date(2026, 11, 1))].iloc[0]
    assert row["target"] == 3


def test_off_preference_highlight_is_for_the_app_admin_only(db, monkeypatch):
    pid = _save(db, "Prefs", 59)
    at, _ = _as(db, monkeypatch, "matthew@sdvbc.org", pid=pid)          # app admin
    at.radio(key="nav").set_value("Court view").run()
    at.toggle(key="show_offpref").set_value(True).run()
    assert not at.exception, at.exception
    at.radio(key="nav").set_value("Calendar").run()
    at.toggle(key="cal_offpref").set_value(True).run()
    assert not at.exception, at.exception
    assert any("cs-offpref" in m.value or "cs-cal" in m.value for m in at.markdown)
    head, _team = _head_of_first_team(db, pid)
    db.save_user("coach@sdvbc.org", "Coach", head)
    at, _ = _as(db, monkeypatch, "coach@sdvbc.org", pid=pid)            # a coach doesn't get it
    at.radio(key="nav").set_value("Court view").run()
    assert not any(t.key == "show_offpref" for t in at.toggle)


def test_off_preference_reasons():
    from types import SimpleNamespace

    from scheduler import clean
    from ui import common
    from ui.state import S  # noqa: F401
    inp = simulate(4, 2, 2, seed=60, start=date(2026, 11, 1), end=date(2026, 11, 30))
    team = inp.teams["team"].iloc[0]
    inp.availability = pd.DataFrame([{"who": team, "type": "Not preferred", "days": "Mon"}])
    inp.wishlist = pd.DataFrame([{"team": team, "choice": 1, "days": "Tue, Thu", "times": "5:30"}])
    data = clean(inp, date(2026, 11, 1), date(2026, 11, 30))
    a = pd.DataFrame([
        {"kind": "Practice", "name": team, "date": date(2026, 11, 2), "slot": "5:30"},   # Mon: rather not
        {"kind": "Practice", "name": team, "date": date(2026, 11, 3), "slot": "5:30"},   # Tue 5:30: preferred
        {"kind": "Practice", "name": team, "date": date(2026, 11, 5), "slot": "7:20"}])  # Thu 7:20: wrong time
    common.S = SimpleNamespace(cleaned=data)
    try:
        out = list(common.off_preference(a)["off_pref"])
    finally:
        from ui.state import S as real
        common.S = real
    assert "rather not" in out[0] and out[1] == "" and "another time" in out[2]


def _import_list(at, rows):
    """Upload a practice list under Edit practices and press Add."""
    csv = "date,court,start,team\n" + "".join(f"{d},{c},{s},{t}\n" for d, c, s, t in rows)
    at.radio(key="nav").set_value("Edit practices").run()
    at.get("file_uploader")[0].set_value(("list.csv", csv.encode(), "text/csv")).run()
    assert not at.exception, at.exception
    at.button(key="ed_import_go").click().run()
    assert not at.exception, at.exception
    return at


def test_imported_week_not_yet_scheduled_has_only_the_list(db):
    at, _ = _open(db, "Import new week", 21)
    teams = list(at.session_state["inp"].teams["team"])
    t0, t1 = teams[0], teams[1]
    _import_list(at, [("2026-12-07", 1, "17:30", t0), ("2026-12-09", 1, "17:30", t0),
                      ("2026-12-11", 1, "17:30", t0), ("2026-12-08", 2, "19:20", t1)])
    r = at.session_state["result"]
    assert r is not None and r.ok and (r.start, r.end) == (date(2026, 12, 6), date(2026, 12, 12))
    p = r.assignments[r.assignments["kind"] == "Practice"]
    assert sorted(zip(p["name"], p["date"])) == sorted([(t0, date(2026, 12, 7)), (t0, date(2026, 12, 9)),
                                                        (t0, date(2026, 12, 11)), (t1, date(2026, 12, 8))])
    ov = at.session_state["inp"].overrides
    assert (ov["action"] == "Hand only").sum() == 1
    assert any("only" in str(m.value) for m in at.success) or "Scheduled" in str(at.session_state.get("flash", ""))


def test_imported_week_already_scheduled_drops_the_rest(db):
    at, _ = _open(db, "Import old week", 22)
    before = at.session_state["result"].assignments
    wk = lambda a: a[(a["kind"] == "Practice") & a["date"].map(lambda d: date(2026, 11, 15) <= d <= date(2026, 11, 21))]  # noqa: E731
    assert len(wk(before)) > 2
    t0 = at.session_state["inp"].teams["team"].iloc[0]
    _import_list(at, [("2026-11-16", 3, "17:30", t0), ("2026-11-18", 3, "17:30", t0)])
    r = at.session_state["result"]
    assert r.ok and (r.start, r.end) == (date(2026, 11, 1), date(2026, 11, 30))
    got = wk(r.assignments)
    assert sorted(zip(got["name"], got["date"], got["court"])) == [(t0, date(2026, 11, 16), 3), (t0, date(2026, 11, 18), 3)]
    others = r.assignments[(r.assignments["kind"] == "Practice") & (r.assignments["date"] >= date(2026, 11, 22))]
    assert len(others)  # the other weeks are still scheduled


def test_flags_with_the_same_id_show_once(db):
    """Two local tournaments with the same name on the same day, worked by a coach who's at a travel
    tournament then: one flag, not a StreamlitDuplicateElementKey crash (Nov 2026)."""
    from streamlit.testing.v1 import AppTest

    from scheduler import clean
    from scheduler.storage import make_payload, read_payload
    pid = _save(db, "Dup flags", 40)
    inp, s_, e_, settings, r = read_payload(db.get(pid)["payload"])
    t = clean(inp, s_, e_).teams[0]
    coach = t.heads[0]
    local = {"name": "Boys Local", "type": "Local", "start_date": "11/14/2026", "end_date": "11/14/2026",
             "coaches": coach}
    inp.tournaments = pd.DataFrame([
        {"name": "Vegas Classic", "type": "Travel", "start_date": "11/14/2026", "end_date": "11/15/2026",
         "teams": t.name}, dict(local, courts="1-3"), dict(local, courts="4-6")])
    db.save(pid, "Dup flags", make_payload(inp, s_, e_, settings, r))
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    assert not at.exception, at.exception
    assert any("1 coverage flag to sort out" in e.label for e in at.expander)


def test_refresh_keeps_the_dates_being_looked_at(db):
    """Nov 2026: the day being looked at rides in the page address, so a refresh (a new session)
    comes back to it instead of jumping to the first scheduled dates."""
    from streamlit.testing.v1 import AppTest
    _save(db, "November", 1)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    _auto_schedule(at, date(2026, 12, 6), date(2026, 12, 19))
    assert not at.exception, at.exception
    assert at.query_params["day"] in ("2026-12-06", ["2026-12-06"])
    again = AppTest.from_file("../app.py", default_timeout=180)
    again.query_params["day"] = "2026-12-06"
    again.run()
    assert not again.exception, again.exception
    assert again.session_state["view_day"] == date(2026, 12, 6)
    assert again.session_state["range_start"] == date(2026, 12, 6)


def test_publish_lists_changes_readably(db):
    """Nov 8, 2026: the Publish page prints what changed since a version, by week and team."""
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Changes list", 6)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("Publish").run()
    next(b for b in at.button if "Publish this schedule" in b.label).click().run()
    # Pretend the published version had the first practice on another court.
    pub = at.session_state["published"]
    p0 = pub[-1]["practices"][0]
    p0["court"] = 2 if int(p0["court"]) != 2 else 3
    at.run()
    assert not at.exception, at.exception
    txt = next(c.value for c in at.code if c.value.startswith("Changes since"))
    assert "Court:" in txt and "→ Court" in txt and "Week of" not in txt      # by day by default
    assert any(b.label == "🖨 Print changes" for b in at.get("download_button"))
    at.radio(key="pub_list_by").set_value("By week and team").run()
    txt = next(c.value for c in at.code if c.value.startswith("Changes since"))
    assert "Week of" in txt


def test_move_a_team_in_the_pecking_order(db):
    """Nov 8, 2026: the Pecking order section can put a team above an older team."""
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Peck move", 6)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    teams = at.session_state["inp"].teams
    ages = dict(zip(teams["team"], teams["age"].astype(float)))
    young = min(ages, key=ages.get)
    old = max(ages, key=ages.get)
    from ui.nav import TAB_PECK
    at.radio(key="nav").set_value(TAB_PECK).run()
    at.selectbox(key="pm_team").set_value(young).run()
    at.selectbox(key="pm_side").set_value("above").run()
    at.selectbox(key="pm_target").set_value(old).run()
    at.button(key="pm_go").click().run()
    assert not at.exception, at.exception
    t = at.session_state["inp"].teams
    assert t.set_index("team").loc[young, "peck_move"] == f"above {old}"


def test_warnings_can_be_dismissed_and_brought_back(db):
    """An admin dismisses warning lines (one, or a whole warning); they stay dismissed after a
    refresh and can be brought back (Nov 2026)."""
    from streamlit.testing.v1 import AppTest

    from scheduler.storage import MAIN_ID, all_periods
    inp = simulate(4, 2, 2, seed=1, start=date(2026, 11, 1), end=date(2026, 11, 30))
    r = solve(inp, date(2026, 11, 1), date(2026, 11, 30), time_limit_s=10)
    r.warnings = ["First made-up note", "Second made-up note"]
    db.save(MAIN_ID, "November", make_payload(inp, date(2026, 11, 1), date(2026, 11, 30),
                                              {"time_limit": 10, "makeup": True}, r))
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    assert not at.exception, at.exception
    pick = next(w for w in at.multiselect if w.key.startswith("wd_pick_input"))
    pick.set_value(["First made-up note"]).run()
    next(b for b in at.button if b.key.startswith("wd_some_input")).click().run()
    assert not at.exception, at.exception
    assert at.session_state["dismissed"] == ["input|First made-up note"]
    assert not any("First made-up note" in w.value for w in at.warning)
    assert any("Second made-up note" in w.value for w in at.warning)
    # Saved with these dates: still dismissed after a refresh.
    settings = next(iter(all_periods(db.get(MAIN_ID)["payload"]).values()))["settings"]
    assert settings.get("dismissed") == ["input|First made-up note"]
    at2 = AppTest.from_file("../app.py", default_timeout=180)
    at2.run()
    assert not any("First made-up note" in w.value for w in at2.warning)
    at2.button(key="wd_back_all").click().run()
    assert not at2.exception, at2.exception
    assert any("First made-up note" in w.value for w in at2.warning)


def test_requests_are_grouped_by_coach(db, monkeypatch):
    """The admin's Requests list groups waiting requests by coach, longest waiting first (Nov 2026)."""
    import time
    pid = _save(db, "Roles", 34)
    head, team = _head_of_first_team(db, pid)
    db.save_user("cliff@sdvbc.org", "Cliff", "", False, True)
    a1 = db.add_request("ann@sdvbc.org", "Ann", "cancel", team, "", "Ann cancel 1", {"rows": []})
    time.sleep(0.01)
    b1 = db.add_request("bo@sdvbc.org", "Bo", "cancel", team, "", "Bo cancel", {"rows": []})
    time.sleep(0.01)
    a2 = db.add_request("ann@sdvbc.org", "Ann", "cancel", team, "", "Ann cancel 2", {"rows": []})
    at, _ = _as(db, monkeypatch, "cliff@sdvbc.org", pid=pid)
    at.radio(key="nav").set_value("Requests").run()
    assert not at.exception, at.exception
    heads = [m.value for m in at.markdown if m.value.startswith("#### ")]
    assert heads == ["#### Ann", "#### Bo"]
    order = [b.key for b in at.button if b.key and b.key.startswith("rq_ok_")]
    assert order == [f"rq_ok_{a1}", f"rq_ok_{a2}", f"rq_ok_{b1}"]       # Ann's two together


def test_published_dates_with_no_build_come_back(db):
    """A restore or change that couldn't be scheduled left published dates with no build: they
    stay on the calendar and the page brings back their published version."""
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Lost build", 6)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("Publish").run()
    next(b for b in at.button if "Publish this schedule" in b.label).click().run()
    rec = db.get(pid)
    v1 = rec["payload"]["published"][0]
    want = {(p["name"], p["date"], p["slot"]) for p in v1["practices"]}
    payload = dict(rec["payload"], result=None)  # what the failed restore left behind
    db.save(pid, rec["name"], payload)
    at2 = AppTest.from_file("../app.py", default_timeout=180)
    at2.query_params["project"] = pid
    at2.run()
    assert not at2.exception, at2.exception
    r = at2.session_state["result"]
    assert r is not None and r.ok
    a = r.assignments[r.assignments["kind"] == "Practice"]
    assert set(zip(a["name"], a["date"].map(str), a["slot"])) == want


def test_unpublished_copy_inside_published_dates_is_taken_off(db):
    """Auto-schedule pressed while published dates had dropped off the calendar left a second,
    unpublished run inside them: everything showed twice. The copy is taken off; the published
    dates and their versions stay, and nothing shows twice."""
    from datetime import date as _d

    from streamlit.testing.v1 import AppTest

    from scheduler.storage import period_key
    pid = _save(db, "Twice", 6)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("Publish").run()
    next(b for b in at.button if "Publish this schedule" in b.label).click().run()
    rec = db.get(pid)
    payload = dict(rec["payload"])
    inner = {"start": "2026-11-01", "end": "2026-11-14", "settings": {}, "result": payload["result"], "published": []}
    payload["periods"] = dict(payload.get("periods") or {}, **{period_key(_d(2026, 11, 1), _d(2026, 11, 14)): inner})
    db.save(pid, rec["name"], payload)
    at2 = AppTest.from_file("../app.py", default_timeout=180)
    at2.query_params["project"] = pid
    at2.run()
    assert not at2.exception, at2.exception
    assert at2.session_state["range_start"] == _d(2026, 11, 1) and at2.session_state["range_end"] == _d(2026, 11, 30)
    assert at2.session_state["published"]
    stored = db.get(pid)["payload"]
    assert period_key(_d(2026, 11, 1), _d(2026, 11, 14)) not in (stored.get("periods") or {})
    assert stored["published"]


def test_rebuilding_published_dates_asks_first(db):
    """Published dates are locked: Rebuild these dates asks before re-planning them."""
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Locked", 6)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("Publish").run()
    next(b for b in at.button if "Publish this schedule" in b.label).click().run()
    a = at.session_state["result"].assignments
    before = set(map(tuple, a[a["kind"] == "Practice"][["name", "date", "slot", "court"]].values))
    at.button(key="as_go").click().run()
    assert not at.exception, at.exception
    assert any("locked" in w.value for w in at.warning)
    a2 = at.session_state["result"].assignments
    assert set(map(tuple, a2[a2["kind"] == "Practice"][["name", "date", "slot", "court"]].values)) == before
    at.button(key="as_replan").click().run()
    assert not at.exception, at.exception
    assert not any("locked" in w.value for w in at.warning)


def test_update_schedule_that_cant_build_keeps_the_schedule(db):
    """Staged hand edits that can't all happen: Update schedule takes them back out and the schedule
    stays (it used to wipe the whole calendar)."""
    from streamlit.testing.v1 import AppTest
    pid = _save(db, "Keep", 6)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.query_params["project"] = pid
    at.run()
    at.radio(key="nav").set_value("Publish").run()
    next(b for b in at.button if "Publish this schedule" in b.label).click().run()
    res = at.session_state["result"]
    a = res.assignments
    p = a[a["kind"] == "Practice"]
    team = p.iloc[0]["name"]
    days = sorted(set(p["date"]))[:3]
    before_ov = at.session_state["inp"].overrides.copy() if "inp" in at.session_state else None
    bad = [{"team": team, "action": "Add", "date": f"{d:%m/%d/%Y}", "days": "", "time": "7:20", "court": "",
            "note": ""} for d in days]  # 3 by hand in a week: too many
    inp = at.session_state["inp"]
    inp.overrides = pd.concat([inp.overrides, pd.DataFrame(bad)], ignore_index=True)
    at.session_state["staged"] = {"rows": bad, "pins": [], "what": ["test"], "backup": before_ov,
                                  "base_result": res}
    at.run()
    at.button(key="upd_go").click().run()
    assert not at.exception, at.exception
    r = at.session_state["result"]
    assert r is not None and r.ok
    assert len(r.assignments) == len(a)
    assert len(at.session_state["inp"].overrides) == len(before_ov)


def _move_first_practice(at):
    """Move the team's first practice to another time (a change that saves)."""
    at.radio(key="nav").set_value("Edit practices").run()
    team = at.selectbox(key="ed_team").value
    at.radio(key="ed_mode").set_value("Move a practice").run()
    a = at.session_state["result"].assignments
    first = a[(a["kind"] == "Practice") & (a["name"] == team)].sort_values("date").iloc[0]
    d, old_slot = first["date"], first["slot"]
    new_slot = next(s for s in ("5:30", "7:20", "3:45") if s != old_slot
                    and not ((a["name"] == team) & (a["date"] == d) & (a["slot"] == s)).any())
    at.selectbox(key=f"ed_ns_{team}_{d}_{old_slot}").set_value(new_slot).run()
    next(b for b in at.button if b.label == "Move it").click().run()


def test_a_second_admin_doesnt_undo_the_firsts_change(db):
    """Two admins have the schedule open. The other one changes the club's tables; this page's next
    save is stopped (their change stays) until it gets the latest schedule."""
    from streamlit.testing.v1 import AppTest

    from scheduler.storage import MAIN_ID
    _save(db, "Main", 56)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    assert not at.exception, at.exception
    # The other admin changes a team (its assistant coach).
    rec = db.get(MAIN_ID)
    payload = rec["payload"]
    payload["inputs"]["Teams"][0]["assistants"] = "Changed"
    db.save(MAIN_ID, rec["name"], payload)

    _move_first_practice(at)
    assert not at.exception, at.exception
    assert at.session_state["save_conflict"]
    assert any("wasn't saved" in e.value for e in at.error)
    assert db.get(MAIN_ID)["payload"]["inputs"]["Teams"][0]["assistants"] == "Changed"

    next(b for b in at.button if b.label == "Get the latest schedule").click().run()
    assert not at.exception, at.exception
    assert "save_conflict" not in at.session_state
    _move_first_practice(at)
    assert "save_conflict" not in at.session_state
    assert db.get(MAIN_ID)["payload"]["inputs"]["Teams"][0]["assistants"] == "Changed"


def test_other_admins_other_dates_dont_block_a_save(db):
    """Another admin working on different dates isn't a conflict: both changes are kept."""
    from streamlit.testing.v1 import AppTest

    from scheduler.storage import MAIN_ID
    _save(db, "Main", 56)
    at = AppTest.from_file("../app.py", default_timeout=180)
    at.run()
    rec = db.get(MAIN_ID)
    payload = rec["payload"]
    payload.setdefault("periods", {})["2027-01-04_2027-01-15"] = {
        "start": "2027-01-04", "end": "2027-01-15", "settings": {}, "result": None, "published": []}
    db.save(MAIN_ID, rec["name"], payload)
    before = at.session_state["result"].assignments
    _move_first_practice(at)
    assert not at.exception, at.exception
    assert "save_conflict" not in at.session_state
    saved = db.get(MAIN_ID)["payload"]
    assert "2027-01-04_2027-01-15" in saved["periods"]
    assert saved["result"] is not None and len(saved["result"]["assignments"]) == len(before)


def test_backups_and_restore(db):
    """Saving keeps a copy of the schedule from before; restoring brings it back and is itself undoable."""
    from scheduler.storage import MAIN_ID
    _save(db, "Main", 56)
    first = db.get(MAIN_ID)
    p2 = dict(first["payload"], settings={"time_limit": 99})
    db.save(MAIN_ID, "Main", p2, expect=str(first["updated_at"]))
    with pytest.raises(Exception, match="main") as e:   # by name: other tests reload the scheduler package
        db.save(MAIN_ID, "Main", first["payload"], expect=str(first["updated_at"]))
    assert type(e.value).__name__ == "StaleSave"
    bk = db.backups(MAIN_ID)
    assert len(bk) == 1 and db.backup_payload(bk[0]["id"]) == first["payload"]
    assert db.restore_backup(MAIN_ID, bk[0]["id"])
    assert db.get(MAIN_ID)["payload"] == first["payload"]
    assert len(db.backups(MAIN_ID)) == 2                 # the schedule it replaced was kept too


def test_old_backups_thin_out(db, monkeypatch):
    """The newest copies are kept, then one per day."""
    from datetime import UTC, datetime, timedelta

    storage = db.save.__func__.__globals__   # the module db's Store came from (other tests reload it)
    bt = storage["backups"]
    monkeypatch.setitem(storage, "BACKUP_KEEP_RECENT", 3)
    t0 = datetime(2026, 11, 1, 18, tzinfo=UTC)
    with db.engine.begin() as c:
        for i in range(12):   # 4 copies a day for 3 days
            c.execute(bt.insert().values(project="main", saved_at=t0 + timedelta(hours=2 * i + (i // 4) * 16),
                                         name="M", payload=storage["_pack"]("{}")))
    db.save("main", "M", {"v": 0})
    rec = db.get("main")
    with db.engine.begin() as c:
        c.execute(storage["projects"].update().values(updated_at=datetime.now(UTC) - timedelta(hours=1)))
    db.save("main", "M", {"v": 1})   # makes a 13th copy and thins out
    left = db.backups("main")
    assert rec is not None and 3 < len(left) < 13
