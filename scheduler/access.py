"""Who can do what.

Three roles, which can be combined:
  - App admin: manages who has access and their roles, and can "view as"
    anyone to test what they see.
  - Scheduling admin: edits the schedule, programs, tournaments and inputs,
    rebuilds and publishes, and (stage 2) approves coaches' change requests.
  - Coach (everyone signed in): sees the whole schedule, and changes their
    own teams' preferences right away. Other changes will be requests.

Sign-in is with Google (Streamlit's built-in login, set up in secrets).
Only addresses on the club's domain(s) can get in. When sign-in isn't set
up yet, the app runs as before: everyone who opens it can do everything.

The app admins listed in ADMIN_EMAILS (secret, or the default below) are
always app admins, so nobody can lock the club out by editing roles.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_DOMAINS = ["sdvbc.org"]
DEFAULT_APP_ADMINS = ["matthew@sdvbc.org"]
# Seeded the first time sign-in runs; editable afterwards in the Access tab.
DEFAULT_SCHED_ADMINS = ["cliff@sdvbc.org"]


@dataclass
class Person:
    email: str
    name: str = ""
    coach: str = ""            # the coach name used on the schedule
    app_admin: bool = False
    sched_admin: bool = False

    @property
    def label(self) -> str:
        return self.name or self.coach or self.email

    @property
    def roles(self) -> list[str]:
        out = []
        if self.app_admin:
            out.append("App admin")
        if self.sched_admin:
            out.append("Scheduling admin")
        out.append("Coach")
        return out


@dataclass
class Viewer:
    """Who the app is acting for right now: the signed-in person, or the
    role an app admin picked in "View as". None everywhere = sign-in off."""
    person: Person | None              # None: sign-in isn't set up (everyone can do everything)
    acting_as: Person | None = None    # set when an app admin is viewing as someone else

    @property
    def who(self) -> Person | None:
        return self.acting_as or self.person

    @property
    def open_mode(self) -> bool:
        return self.person is None

    @property
    def can_schedule(self) -> bool:
        return self.open_mode or bool(self.who and self.who.sched_admin)

    @property
    def can_manage_access(self) -> bool:
        # Only the real person counts: viewing as a coach still lets an app
        # admin get back to the Access tab.
        return self.open_mode or bool(self.person and self.person.app_admin)

    @property
    def coach_name(self) -> str:
        return self.who.coach if self.who else ""

    def my_teams(self, teams) -> list[str]:
        """Teams the current person coaches (head or assistant)."""
        c = self.coach_name
        return [t.name for t in teams if c and c in t.staff]

    def can_edit_prefs(self, team: str, teams) -> bool:
        return self.can_schedule or team in self.my_teams(teams)

    @property
    def log_name(self) -> str:
        if self.person is None:
            return "(no sign-in)"
        if self.acting_as is not None:
            return f"{self.person.email} (as {self.acting_as.label})"
        return self.person.email


def _env_list(name: str, default: list[str], secrets=None) -> list[str]:
    raw = None
    try:
        raw = (secrets or {}).get(name)
    except Exception:  # noqa: BLE001
        raw = None
    raw = raw or os.environ.get(name)
    if not raw:
        return list(default)
    if isinstance(raw, (list, tuple)):
        return [str(x).strip().lower() for x in raw if str(x).strip()]
    return [x.strip().lower() for x in str(raw).replace(";", ",").split(",") if x.strip()]


def allowed_domains(secrets=None) -> list[str]:
    return _env_list("ALLOWED_DOMAINS", DEFAULT_DOMAINS, secrets)


def app_admins(secrets=None) -> list[str]:
    return _env_list("ADMIN_EMAILS", DEFAULT_APP_ADMINS, secrets)


def email_allowed(email: str, secrets=None) -> bool:
    email = (email or "").strip().lower()
    if email in app_admins(secrets):
        return True
    return "@" in email and email.rsplit("@", 1)[1] in allowed_domains(secrets)


def person_for(store, email: str, name: str = "", secrets=None) -> Person:
    """The signed-in person's record, created on first sign-in. The first
    time anyone signs in, the default admins are seeded."""
    email = email.strip().lower()
    if not store.users():
        for e in app_admins(secrets):
            store.save_user(e, app_admin=True)
        for e in DEFAULT_SCHED_ADMINS:
            if not store.get_user(e):
                store.save_user(e, sched_admin=True)
    rec = store.get_user(email)
    if rec is None:
        store.save_user(email, name=name)
        store.log(email, "First sign-in", "added as a coach (not linked to a coach name yet)")
        rec = store.get_user(email)
    elif name and not rec.get("name"):
        store.save_user(email, name=name, coach=rec["coach"], app_admin=rec["app_admin"],
                        sched_admin=rec["sched_admin"])
        rec = store.get_user(email)
    p = Person(rec["email"], rec.get("name") or name, rec.get("coach") or "", bool(rec["app_admin"]),
               bool(rec["sched_admin"]))
    if email in app_admins(secrets):
        p.app_admin = True   # can't be locked out
    return p


def auth_configured(secrets) -> bool:
    """Google sign-in is set up in secrets ([auth] with a client id)."""
    try:
        a = secrets.get("auth")
        return bool(a and (a.get("client_id") or any(isinstance(v, dict) and v.get("client_id")
                                                     for v in a.values())))
    except Exception:  # noqa: BLE001
        return False
