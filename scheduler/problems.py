"""Finding out when something goes wrong, without waiting for someone to mention it.

record() writes the problem to the activity log (Access → Activity, as "Error")
and emails the app admins, at most once an hour for the same problem, so a
crash that repeats on every click doesn't flood anyone's inbox.
Recording never raises: a problem while reporting a problem is ignored.
"""
from __future__ import annotations

import hashlib
import json
import time
import traceback

SEEN = "problems_seen"     # settings key: {fingerprint: last time emailed}
QUIET = 60 * 60            # seconds before the same problem is emailed again


def describe(err) -> tuple[str, str]:
    """(one line, details) for an exception or a message."""
    if isinstance(err, BaseException):
        line = f"{type(err).__name__}: {err}"
        tb = traceback.extract_tb(err.__traceback__)
        ours = [f for f in tb if "/scheduler/" in f.filename or "/ui/" in f.filename or f.filename.endswith("app.py")]
        where = ours[-1] if ours else (tb[-1] if tb else None)
        at = f" (at {where.filename.rsplit('/', 2)[-2]}/{where.filename.rsplit('/', 1)[-1]}:{where.lineno})" \
            if where else ""
        return line[:300] + at, "".join(traceback.format_exception(err))[-4000:]
    return str(err)[:400], str(err)


def record(store, where: str, err, who: str = "", page: str = "", secrets=None, send=None) -> bool:
    """Log a problem and email the app admins if it's new this hour. Returns True if emailed.
    send(to, subject, text) overrides how the email goes out (the app passes its own sender)."""
    try:
        line, details = describe(err)
        store.log(who or "App", "Error", f"{where}{' · ' + page if page else ''}: {line}")
        fp = hashlib.sha1(f"{where}|{line}".encode()).hexdigest()[:16]
        try:
            seen = json.loads(store.get_setting(SEEN) or "{}")
        except ValueError:
            seen = {}
        now = time.time()
        seen = {k: v for k, v in seen.items() if now - v < QUIET}
        if fp in seen:
            store.set_setting(SEEN, json.dumps(seen))
            return False
        seen[fp] = now
        store.set_setting(SEEN, json.dumps(seen))
        to = admin_emails(store, secrets)
        if not to:
            return False
        subject = f"Court Scheduler problem: {where}"
        text = (f"Something went wrong in the Court Scheduler.\n\nWhere: {where}{' (' + page + ')' if page else ''}\n"
                f"Who: {who or 'unknown'}\nWhat: {line}\n\nIt's in Access → Activity too. "
                f"The same problem won't be emailed again for an hour.\n\nDetails:\n{details}")
        if send is not None:
            return send(to, subject, text) is None
        return _send_as_connected(store, secrets, to, subject, text)
    except Exception:  # noqa: BLE001
        return False


def admin_emails(store, secrets=None) -> list[str]:
    from .access import app_admins
    out = [u["email"] for u in store.users() if u.get("app_admin")]
    return list(dict.fromkeys(out + list(app_admins(secrets))))


def _send_as_connected(store, secrets, to, subject, text) -> bool:
    from . import google as gsvc
    cfg = gsvc.client_config(secrets) if secrets is not None else None
    conn = gsvc.connection(store)
    if not (cfg and gsvc.can_email(conn)):
        return False
    sess = gsvc.session(store, cfg, secrets)
    if sess is None:
        return False
    gsvc.send(sess, conn["email"], to, subject, text)
    return True
