"""The Google connection, emails and calendar matching (no network)."""

import base64
import email
import json
from datetime import date

import pandas as pd

from scheduler import google as g
from scheduler import mail
from scheduler.access import coach_emails, suggest_people
from scheduler.storage import Store

SECRETS = {"auth": {"redirect_uri": "https://club.streamlit.app/oauth2callback", "cookie_secret": "x" * 40,
                    "client_id": "cid.apps.googleusercontent.com", "client_secret": "sec"}}


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


class _Http:
    def __init__(self, email="matthew@sdvbc.org", refresh="rt-1"):
        self.email, self.refresh, self.posts = email, refresh, []

    def post(self, url, data=None, json=None):
        self.posts.append((url, data))
        if url == g.TOKEN_URL:
            return _Resp(200, {"access_token": "at", "refresh_token": self.refresh,
                               "scope": " ".join(g.SCOPES)})
        return _Resp(200, {})

    def get(self, url, headers=None):
        return _Resp(200, {"email": self.email, "name": "Matthew M"})


def test_config_and_redirect():
    cfg = g.client_config(SECRETS)
    assert cfg["client_id"].startswith("cid") and cfg["redirect_uri"] == "https://club.streamlit.app/"


def test_connect_round_trip_keeps_token_encrypted(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'g.db'}")
    cfg = g.client_config(SECRETS)
    url = g.start(store, cfg, "matthew@sdvbc.org", "sdvbc.org")
    state = dict(x.split("=", 1) for x in url.split("?", 1)[1].split("&"))["state"]
    assert "gmail.send" in url and "access_type=offline" in url and "hd=sdvbc.org" in url
    c = g.finish(store, cfg, "code", state, "matthew@sdvbc.org", lambda e: e.endswith("@sdvbc.org"), _Http(),
                 secrets=SECRETS)
    assert c["email"] == "matthew@sdvbc.org" and g.can_email(c) and g.can_calendar(c)
    raw = store.get_setting(g.KEY_CONN)
    assert "rt-1" not in raw and g.decrypt(cfg, json.loads(raw)["token"], SECRETS) == "rt-1"
    # a state can't be reused, and someone else's state isn't accepted
    try:
        g.finish(store, cfg, "code", state, "matthew@sdvbc.org", lambda e: True, _Http(), secrets=SECRETS)
        assert False
    except ValueError:
        pass
    url2 = g.start(store, cfg, "cliff@sdvbc.org")
    st2 = dict(x.split("=", 1) for x in url2.split("?", 1)[1].split("&"))["state"]
    try:
        g.finish(store, cfg, "code", st2, "matthew@sdvbc.org", lambda e: True, _Http(), secrets=SECRETS)
        assert False
    except ValueError as e:
        assert "someone else" in str(e)
    g.disconnect(store, cfg, _Http(), SECRETS)
    assert g.connection(store) is None


def test_outside_account_is_refused(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'g.db'}")
    cfg = g.client_config(SECRETS)
    url = g.start(store, cfg, "matthew@sdvbc.org")
    state = dict(x.split("=", 1) for x in url.split("?", 1)[1].split("&"))["state"]
    try:
        g.finish(store, cfg, "c", state, "matthew@sdvbc.org", lambda e: e.endswith("@sdvbc.org"),
                 _Http(email="me@gmail.com"), secrets=SECRETS)
        assert False
    except ValueError as e:
        assert "isn't a club account" in str(e)


def test_email_message_has_text_and_html():
    body = g.build_message("matthew@sdvbc.org", ["cliff@sdvbc.org"], "Hi", "plain", "<b>html</b>",
                           reply_to="kai@sdvbc.org")
    m = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
    assert m["To"] == "cliff@sdvbc.org" and m["Reply-To"] == "kai@sdvbc.org" and "Court Scheduler" in m["From"]
    assert {p.get_content_type() for p in m.walk()} >= {"text/plain", "text/html"}


def test_practices_email_sorted_by_team_then_date():
    pr = pd.DataFrame([
        {"name": "16 Black", "date": date(2026, 11, 5), "slot": "7:20", "court": 1},
        {"name": "14 Black", "date": date(2026, 11, 4), "slot": "5:30", "court": 3},
        {"name": "14 Black", "date": date(2026, 11, 2), "slot": "5:30", "court": 2},
        {"name": "16 Black", "date": date(2026, 11, 3), "slot": "7:20", "court": 6},
        {"name": "15 Green", "date": date(2026, 11, 3), "slot": "7:20", "court": 6}])
    rows = mail.coach_rows(pr, ["14 Black", "16 Black"])
    assert list(zip(rows["team"], rows["date"].map(lambda d: d.day))) == [
        ("14 Black", 2), ("14 Black", 4), ("16 Black", 3), ("16 Black", 5)]
    subj, text, html = mail.practices_email("Matthew", ["14 Black", "16 Black"], pr, "Nov 2026", "v1")
    assert text.index("14 Black") < text.index("Mon Nov 2") < text.index("Wed Nov 4") < text.index("16 Black")
    assert "15 Green" not in text and "Court 3" in html


def test_request_emails():
    req = {"id": "x", "who": "Kai", "email": "kai@sdvbc.org", "summary": "Move 14 Black to Thu", "note": "school"}
    s, t, h = mail.new_request(req, "https://club.streamlit.app/")
    assert "Kai" in s and "school" in t and "Requests" in h
    s, t, h = mail.decision(req, "denied", "Cliff", "no courts")
    assert s.startswith("Declined") and "no courts" in t


def test_calendar_matching_by_name():
    cals = [{"id": "a", "name": "14 Black Practices"}, {"id": "b", "name": "SDVBC 16-Ann"},
            {"id": "c", "name": "Seaside"}, {"id": "d", "name": "Matthew Mitchell"}]
    m = g.match_calendars([("Team", "14 Black"), ("Team", "16-Ann"), ("Club", "Seaside"), ("Team", "15 Green")], cals)
    assert m == {("Team", "14 Black"): "a", ("Team", "16-Ann"): "b", ("Club", "Seaside"): "c"}


def test_coach_emails_prefer_access_links():
    users = [{"email": "tyler@sdvbc.org", "coach": "Tyler"}]
    assert coach_emails(users, {"Tyler": {"email": "old@sdvbc.org"}, "Ann": {"email": "ann@sdvbc.org"}}) == \
        {"Tyler": "tyler@sdvbc.org", "Ann": "ann@sdvbc.org"}
    rows = {r["coach"]: r for r in suggest_people(["Tyler", "Tyler Lyons"], users)}
    assert rows["Tyler"]["status"] == "linked" and rows["Tyler Lyons"]["status"] == "check"
