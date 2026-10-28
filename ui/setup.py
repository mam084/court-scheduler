"""Getting in: the password or Google sign-in, the database connection and the club's Google account."""

from __future__ import annotations

import hashlib
import os
import re
import time as time_mod

import streamlit as st

from scheduler import access
from scheduler import google as gsvc
from scheduler.storage import Store, database_url
from ui.common import secret
from ui.state import S


def _all_secrets():
    try:
        st.secrets.get("_")
        return st.secrets
    except Exception:  # noqa: BLE001  (no secrets file)
        return {}


@st.cache_resource
def get_store(url: str | None, code_version: str) -> Store:
    """One database connection for the whole app. `code_version` changes
    whenever storage.py does, so an update never keeps using a connection
    object built from the old code (which lacked newer methods)."""
    return Store(url)


def _storage_version() -> str:
    from scheduler import storage as _storage_mod
    with open(_storage_mod.__file__, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()[:12]


def _me() -> access.Person | None:
    """The signed-in person, re-read from the database at most every 30 s."""
    SECRETS, store, _email, _name = S.SECRETS, S.store, S._email, S._name
    if _email is None:
        return None
    cached = st.session_state.get("_me")
    if cached and cached[0] == _email and time_mod.time() - cached[1] < 30:
        return cached[2]
    p = access.person_for(store, _email, _name, SECRETS)
    st.session_state._me = (_email, time_mod.time(), p)
    return p


def run():
    """Password or Google sign-in, the database, the signed-in person and the Google connection.
    Stops the page (st.stop) until someone is allowed in."""
    # ------------------------------------------------------------ password --


    SECRETS = _all_secrets()
    AUTH_ON = access.auth_configured(SECRETS)   # Google sign-in set up in secrets
    APP_PASSWORD = secret("APP_PASSWORD")
    if APP_PASSWORD and not AUTH_ON and not st.session_state.get("authed"):
        st.title("Court Scheduler")
        pw = st.text_input("Club password", type="password")
        if pw and pw == str(APP_PASSWORD):
            st.session_state.authed = True
            st.rerun()
        elif pw:
            st.error("That password isn't right.")
        st.stop()

    try:
        store = get_store(database_url(), _storage_version())
    except Exception as e:  # noqa: BLE001  (bad DATABASE_URL: explain instead of crashing)
        url = database_url() or ""
        st.error("**Couldn't connect to the database in `DATABASE_URL`.**")
        if re.search(r"@db\.[a-z0-9]+\.supabase\.co", url):
            st.markdown(
                "This is Supabase's **Direct connection** string, which Streamlit Cloud can't reach. "
                "In Supabase, click **Connect** → **Connection string** → change *Method* to "
                "**Session pooler**, and copy that one instead. It looks like "
                "`postgresql://postgres.<project>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`.")
        else:
            st.markdown("Check the address and password in the app's **Settings → Secrets**. "
                        "For Supabase, use the **Session pooler** connection string.")
        st.caption(f"Details: {type(e).__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}")
        st.stop()

    # --------------------------------------------------------------- sign-in --
    # With Google sign-in set up (see docs/SETUP_LOGINS.md), only club addresses
    # get in, and each person's role decides what they can change. Without it
    # (or locally), the app works as before: anyone who opens it can do
    # everything. COURT_DEV_USER=someone@sdvbc.org tries a role locally.
    if AUTH_ON:
        if not st.user.is_logged_in:
            st.title("Court Scheduler")
            st.markdown("Sign in with your club Google account to see the schedule.")
            st.button("Sign in with Google", type="primary", on_click=st.login)
            st.stop()
        _email, _name = str(st.user.get("email") or ""), str(st.user.get("name") or "")
        if not access.email_allowed(_email, SECRETS):
            st.title("Court Scheduler")
            st.error(f"{_email} isn't a club account. Sign in with your @{access.allowed_domains(SECRETS)[0]} address.")
            st.button("Sign out", on_click=st.logout)
            st.stop()
    elif os.environ.get("COURT_DEV_USER"):
        _email, _name = os.environ["COURT_DEV_USER"], ""
    else:
        _email = _name = None

    S.update(SECRETS=SECRETS, store=store, _email=_email, _name=_name)  # _me() reads them
    ME = _me()

    GCFG = gsvc.client_config(SECRETS)
    APP_LINK = gsvc.app_url(SECRETS)

    # Coming back from Google after pressing Connect: ?code=...&state=...
    if GCFG and "code" in st.query_params and "state" in st.query_params:
        _code, _state = st.query_params["code"], st.query_params["state"]
        st.query_params.clear()
        try:
            _c = gsvc.finish(store, GCFG, _code, _state, _email or "",
                             lambda e: access.email_allowed(e, SECRETS), secrets=SECRETS)
            st.session_state.pop("_gconn", None)
            store.log(_email or "", "Google connection", f"connected {_c['email']}")
            st.session_state.flash = (f"Connected {_c['email']}. The app can now use its Google Calendars"
                                      + (" and send email" if gsvc.can_email(_c) else "") + ".")
            st.session_state.nav = "Access"
        except ValueError as e:
            st.session_state.flash_warn = f"Google connection didn't work: {e}"
        st.rerun()
    if "error" in st.query_params and "state" in st.query_params:  # declined on Google's page
        st.query_params.clear()
        st.session_state.flash_warn = "Google connection cancelled."
        st.rerun()
    S.update(SECRETS=SECRETS, AUTH_ON=AUTH_ON, store=store, _email=_email, _name=_name, ME=ME, GCFG=GCFG,
             APP_LINK=APP_LINK)
