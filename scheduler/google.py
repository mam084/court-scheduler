"""The app's connection to a club Google account (no key files).

An admin presses Connect in the Access tab, signs in with Google and
approves calendar and email access. Google hands back a refresh token,
which is stored encrypted in the database. From then on the app acts as
that account to:
  - edit the club's Google Calendars (any calendar the account can change)
  - send emails (change requests to admins, decisions back to coaches,
    each coach's practices when a schedule is published)

It reuses the Google client that sign-in already uses (the [auth] secrets);
the only extra setup is the app's own address as one more redirect URI, and
the Calendar and Gmail APIs turned on in that Google Cloud project (see
docs/SETUP_GOOGLE.md). The connection can be removed in the app, or by the
account itself at myaccount.google.com → Security → Third-party connections.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets as _secrets
import time
import urllib.parse
from datetime import UTC, datetime
from email.message import EmailMessage

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
CAL_LIST = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
SCOPES = ["openid", "email", "profile",
          "https://www.googleapis.com/auth/calendar",
          "https://www.googleapis.com/auth/gmail.send"]

KEY_CONN = "google_connection"       # settings key: the connected account
STATE_PREFIX = "google_state:"       # settings key prefix: a Connect in progress
STATE_TTL = 15 * 60                  # seconds a Connect click stays valid


# ----------------------------------------------------------- config --

def client_config(secrets) -> dict | None:
    """client_id / client_secret / redirect base from the sign-in ([auth]) secrets."""
    try:
        a = secrets.get("auth")
    except Exception:  # noqa: BLE001
        return None
    if not a:
        return None
    a = {k: a[k] for k in a}
    cid, csec = a.get("client_id"), a.get("client_secret")
    if not (cid and csec):  # [auth.google] style
        for v in a.values():
            if isinstance(v, dict) or hasattr(v, "get"):
                try:
                    if v.get("client_id"):
                        cid, csec = v.get("client_id"), v.get("client_secret")
                        break
                except Exception:  # noqa: BLE001
                    continue
    if not (cid and csec):
        return None
    return {"client_id": str(cid), "client_secret": str(csec),
            "redirect_uri": redirect_uri(secrets, str(a.get("redirect_uri") or "")),
            "cookie_secret": str(a.get("cookie_secret") or "")}


def redirect_uri(secrets, signin_redirect: str = "") -> str:
    """Where Google sends people back after Connect: the app's own address
    (https://<app>.streamlit.app/), unless GOOGLE_CONNECT_REDIRECT says otherwise."""
    try:
        over = secrets.get("GOOGLE_CONNECT_REDIRECT")
    except Exception:  # noqa: BLE001
        over = None
    if over:
        return str(over)
    p = urllib.parse.urlsplit(signin_redirect)
    return f"{p.scheme}://{p.netloc}/" if p.netloc else ""


def app_url(secrets) -> str:
    """The app's address, as registered with Google for sign-in redirects."""
    cfg = client_config(secrets)
    return cfg["redirect_uri"] if cfg else ""


# ------------------------------------------------------- encryption --

def _fernet(cfg: dict, secrets=None):
    from cryptography.fernet import Fernet
    seed = ""
    try:
        seed = str(secrets.get("TOKEN_KEY") or "") if secrets is not None else ""
    except Exception:  # noqa: BLE001
        seed = ""
    seed = seed or cfg.get("cookie_secret") or cfg["client_secret"]
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("court-scheduler|" + seed).encode()).digest()))


def encrypt(cfg: dict, text: str, secrets=None) -> str:
    """Encrypts a token before it's stored in the database."""
    return _fernet(cfg, secrets).encrypt(text.encode()).decode()


def decrypt(cfg: dict, token: str, secrets=None) -> str:
    """Decrypts a stored token."""
    return _fernet(cfg, secrets).decrypt(token.encode()).decode()


# ---------------------------------------------------------- connect --

def start(store, cfg: dict, email: str, hd: str = "") -> str:
    """The Google sign-in address for Connect. Remembers the attempt (who
    started it) for STATE_TTL seconds, so only that person's return counts."""
    state = _secrets.token_urlsafe(24)
    store.set_setting(STATE_PREFIX + state, json.dumps({"email": email.lower(), "at": time.time()}))
    q = {"client_id": cfg["client_id"], "redirect_uri": cfg["redirect_uri"], "response_type": "code",
         "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
         "include_granted_scopes": "true", "state": state, "login_hint": email}
    if hd:
        q["hd"] = hd
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def finish(store, cfg: dict, code: str, state: str, email: str, allowed, http=None, secrets=None) -> dict:
    """Completes Connect after Google sends the person back with ?code=&state=.
    Saves the connection and returns it ({email, name, scopes, by, at}).
    Raises ValueError with a plain reason when it can't."""
    raw = store.get_setting(STATE_PREFIX + state)
    store.set_setting(STATE_PREFIX + state, None)  # one use only
    if not raw:
        raise ValueError("that Connect link expired or was already used. Press Connect again.")
    st_ = json.loads(raw)
    if time.time() - st_.get("at", 0) > STATE_TTL:
        raise ValueError("that Connect link expired. Press Connect again.")
    if email and st_.get("email") != email.lower():
        raise ValueError("Connect was started by someone else. Press Connect again.")
    http = http or _http()
    r = http.post(TOKEN_URL, data={"code": code, "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                                   "redirect_uri": cfg["redirect_uri"], "grant_type": "authorization_code"})
    if r.status_code != 200:
        raise ValueError(f"Google didn't accept the sign-in ({_err(r)}).")
    tok = r.json()
    refresh = tok.get("refresh_token")
    if not refresh:
        raise ValueError("Google didn't hand over lasting access. Remove the app at "
                         "myaccount.google.com → Security → Third-party connections, then Connect again.")
    granted = set((tok.get("scope") or "").split())
    who = http.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok['access_token']}"})
    info = who.json() if who.status_code == 200 else {}
    acct = str(info.get("email") or "").lower()
    if not acct:
        raise ValueError("couldn't tell which Google account was used.")
    if not allowed(acct):
        raise ValueError(f"{acct} isn't a club account.")
    conn = {"email": acct, "name": info.get("name") or "", "scopes": sorted(granted),
            "by": email or acct, "at": datetime.now(UTC).isoformat(),
            "token": encrypt(cfg, refresh, secrets)}
    store.set_setting(KEY_CONN, json.dumps(conn))
    return {k: v for k, v in conn.items() if k != "token"}


def connection(store) -> dict | None:
    """The connected account ({email, name, scopes, by, at}) without its token, or None."""
    raw = store.get_setting(KEY_CONN)
    if not raw:
        return None
    c = json.loads(raw)
    c.pop("token", None)
    return c


def can_email(conn: dict | None) -> bool:
    """Does this Google connection allow sending email?"""
    return bool(conn) and any(s.endswith("gmail.send") for s in conn.get("scopes", []))


def can_calendar(conn: dict | None) -> bool:
    """Does this Google connection allow writing to calendars?"""
    return bool(conn) and any(s.endswith("/auth/calendar") for s in conn.get("scopes", []))


def disconnect(store, cfg: dict | None, http=None, secrets=None) -> None:
    """Forgets the connection and asks Google to cancel its access."""
    raw = store.get_setting(KEY_CONN)
    store.set_setting(KEY_CONN, None)
    if raw and cfg:
        try:
            tok = decrypt(cfg, json.loads(raw)["token"], secrets)
            (http or _http()).post(REVOKE_URL, data={"token": tok})
        except Exception:  # noqa: BLE001  (already gone on Google's side: fine)
            pass


def session(store, cfg: dict, secrets=None):
    """An authorized HTTP session acting as the connected account (None if not connected)."""
    raw = store.get_setting(KEY_CONN)
    if not raw or not cfg:
        return None
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.credentials import Credentials
    c = json.loads(raw)
    creds = Credentials(None, refresh_token=decrypt(cfg, c["token"], secrets), token_uri=TOKEN_URL,
                        client_id=cfg["client_id"], client_secret=cfg["client_secret"], scopes=c.get("scopes"))
    return AuthorizedSession(creds)


def _http():
    import requests
    return requests.Session()


def _err(r) -> str:
    try:
        j = r.json()
        return j.get("error_description") or j.get("error") or str(r.status_code)
    except Exception:  # noqa: BLE001
        return str(r.status_code)


# ------------------------------------------------------------ email --

def build_message(sender: str, to: list[str], subject: str, text: str, html: str | None = None,
                  cc: list[str] | None = None, reply_to: str | None = None,
                  sender_name: str = "Court Scheduler") -> dict:
    """The Gmail API body for one email."""
    m = EmailMessage()
    m["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    m["To"] = ", ".join(to)
    if cc:
        m["Cc"] = ", ".join(cc)
    if reply_to:
        m["Reply-To"] = reply_to
    m["Subject"] = subject
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
    return {"raw": base64.urlsafe_b64encode(m.as_bytes()).decode()}


def send(sess, sender: str, to: list[str], subject: str, text: str, html: str | None = None,
         cc: list[str] | None = None, reply_to: str | None = None) -> None:
    """Sends one email as the connected account. Raises on failure."""
    to = [t for t in dict.fromkeys(x.strip().lower() for x in to) if t]
    if not to:
        return
    r = sess.post(GMAIL_SEND, json=build_message(sender, to, subject, text, html, cc, reply_to))
    if r.status_code >= 300:
        raise RuntimeError(f"Gmail said {r.status_code}: {_err(r)}")


# --------------------------------------------------------- calendars --

def calendars(sess) -> list[dict]:
    """Calendars the connected account can change: [{id, name, primary}]."""
    out, token = [], None
    while True:
        params = {"minAccessRole": "writer", "maxResults": 250}
        if token:
            params["pageToken"] = token
        r = sess.get(CAL_LIST, params=params)
        r.raise_for_status()
        j = r.json()
        out += [{"id": c["id"], "name": c.get("summaryOverride") or c.get("summary") or c["id"],
                 "primary": bool(c.get("primary"))} for c in j.get("items", [])]
        token = j.get("nextPageToken")
        if not token:
            return sorted(out, key=lambda c: (not c["primary"], c["name"].lower()))


def _norm(s: str) -> str:
    import re
    s = str(s).lower()
    plain = re.sub(r"[^a-z0-9]", "", s)
    s = re.sub(r"\b(calendar|cal|team|practices?|schedule|sdvbc|seaside|volleyball|vbc)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s) or plain


def match_calendars(targets: list[tuple[str, str]], cals: list[dict]) -> dict[tuple[str, str], str]:
    """Guesses which Google calendar is each team's / club's / program's by
    name ("14 Black Practices" -> team 14 Black). Only exact matches after
    dropping filler words and punctuation, and only when exactly one fits."""
    by = {}
    for c in cals:
        by.setdefault(_norm(c["name"]), []).append(c["id"])
    out = {}
    for kind, name in targets:
        hits = by.get(_norm(name), [])
        if len(hits) == 1:
            out[kind, name] = hits[0]
    return out
