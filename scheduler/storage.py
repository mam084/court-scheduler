"""Saved schedules.

Each saved schedule ("project") keeps its input tables, date range,
settings and last solved schedule, so closing the tab or refreshing doesn't
lose anything.

Where it's stored
-----------------
- DATABASE_URL set (Streamlit secret or environment variable): a Postgres
  database, e.g. a free Supabase or Neon project. Permanent.
- Otherwise: a SQLite file next to the app (storage/court_scheduler.db).
  Fine on your own computer; on Streamlit Community Cloud it survives
  refreshes but is wiped whenever the app restarts or redeploys.
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    text,
    update,
)

from .data import ScheduleInput, clean
from .solver import ScheduleResult

PAYLOAD_VERSION = 1
DEFAULT_DB = Path(__file__).resolve().parent.parent / "storage" / "court_scheduler.db"

_meta = MetaData()
projects = Table(
    "court_scheduler_projects", _meta,
    Column("id", String(32), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("payload", Text, nullable=False),
)
# Who can sign in and what they can do (see access.py).
users = Table(
    "court_scheduler_users", _meta,
    Column("email", String(200), primary_key=True),
    Column("name", String(200), nullable=False, default=""),
    Column("coach", String(200), nullable=False, default=""),       # the coach name on the schedule
    Column("app_admin", Boolean, nullable=False, default=False),     # manages access and settings
    Column("sched_admin", Boolean, nullable=False, default=False),   # approves changes, edits the schedule
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
# Who did what: role changes, preference changes, (later) approvals.
activity = Table(
    "court_scheduler_log", _meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("who", String(200), nullable=False),
    Column("action", String(100), nullable=False),
    Column("detail", Text, nullable=False, default=""),
    Column("project", String(32), nullable=False, default=""),
)
# Small app settings: the connected Google account (its token encrypted),
# sign-in handshakes in progress.
settings_kv = Table(
    "court_scheduler_settings", _meta,
    Column("key", String(100), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
# Coaches' change requests (move / add / cancel a practice, book a private).
requests_t = Table(
    "court_scheduler_requests", _meta,
    Column("id", String(32), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("email", String(200), nullable=False),          # who asked (their sign-in)
    Column("who", String(200), nullable=False, default=""),  # their name / coach name
    Column("kind", String(30), nullable=False),            # move | extra | cancel | private
    Column("team", String(200), nullable=False, default=""),
    Column("period", String(40), nullable=False, default=""),
    Column("summary", Text, nullable=False, default=""),
    Column("detail", Text, nullable=False, default="{}"),  # JSON: what to do when approved
    Column("note", Text, nullable=False, default=""),
    Column("status", String(20), nullable=False, default="pending"),  # pending | approved | denied | withdrawn
    Column("decided_by", String(200), nullable=False, default=""),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("decision_note", Text, nullable=False, default=""),
)


def _normalize_url(url: str) -> str:
    """Supabase/Neon hand out postgres:// or postgresql:// URLs. Use the
    psycopg2 driver we ship (newer SQLAlchemy defaults to psycopg 3)."""
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


class Store:
    """The database: the club's schedule, people and roles, the activity log, settings and change requests.

    Postgres when DATABASE_URL is set (permanent), else a local SQLite file.
    """
    def __init__(self, url: str | None = None):
        if url:
            self.url = _normalize_url(url)
        else:
            DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
            self.url = f"sqlite:///{DEFAULT_DB}"
        self.engine = create_engine(self.url, pool_pre_ping=True)
        _meta.create_all(self.engine)
        self._lock_down()

    def _lock_down(self):
        """On Postgres (e.g. Supabase), turn on row-level security for our
        table with no policies. Supabase's public REST API then can't read
        it, while this app (connecting as the table's owner) still can."""
        if not self.permanent:
            return
        for tbl in (projects, users, activity, settings_kv, requests_t):
            try:
                with self.engine.begin() as c:
                    c.execute(text(f"ALTER TABLE {tbl.name} ENABLE ROW LEVEL SECURITY"))
            except Exception:  # noqa: BLE001  (not owner / not supported: carry on)
                pass

    @property
    def permanent(self) -> bool:
        return not self.url.startswith("sqlite")

    @property
    def kind(self) -> str:
        return "database" if self.permanent else "local file"

    def _ensure(self):
        """Recreate the tables if a local SQLite file was wiped while the app
        was running. The tables are created when the Store starts, so on
        Postgres this does nothing: checking them on every call cost several
        database round trips per click."""
        if not self.permanent and not os.path.exists(self.engine.url.database or ""):
            _meta.create_all(self.engine)

    # ------------------------------------------------------------- CRUD --
    def list(self) -> list[dict]:
        self._ensure()
        with self.engine.connect() as c:
            rows = c.execute(select(projects.c.id, projects.c.name, projects.c.updated_at)
                             .order_by(projects.c.updated_at.desc())).all()
        return [{"id": r.id, "name": r.name, "updated_at": r.updated_at} for r in rows]

    def get(self, pid: str) -> dict | None:
        self._ensure()
        with self.engine.connect() as c:
            r = c.execute(select(projects).where(projects.c.id == pid)).first()
        if r is None:
            return None
        return {"id": r.id, "name": r.name, "updated_at": r.updated_at, "payload": json.loads(r.payload)}

    def updated_at(self, pid: str):
        """When a saved schedule last changed (a tiny query, to skip re-reading it if nobody else saved)."""
        self._ensure()
        with self.engine.connect() as c:
            return c.execute(select(projects.c.updated_at).where(projects.c.id == pid)).scalar()

    def save(self, pid: str, name: str, payload: dict) -> None:
        self._ensure()
        body = json.dumps(payload, default=str)
        now = datetime.now(UTC)
        with self.engine.begin() as c:
            exists = c.execute(select(projects.c.id).where(projects.c.id == pid)).first()
            if exists:
                c.execute(update(projects).where(projects.c.id == pid)
                          .values(name=name, payload=body, updated_at=now))
            else:
                c.execute(insert(projects).values(id=pid, name=name, payload=body, updated_at=now))

    def rename(self, pid: str, name: str) -> None:
        with self.engine.begin() as c:
            c.execute(update(projects).where(projects.c.id == pid).values(name=name))

    def delete(self, pid: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(projects).where(projects.c.id == pid))


    # ------------------------------------------------------------ users --
    def users(self) -> list[dict]:
        self._ensure()
        with self.engine.connect() as c:
            rows = c.execute(select(users).order_by(users.c.email)).all()
        return [dict(r._mapping) for r in rows]

    def get_user(self, email: str) -> dict | None:
        self._ensure()
        with self.engine.connect() as c:
            r = c.execute(select(users).where(users.c.email == email.lower())).first()
        return dict(r._mapping) if r else None

    def save_user(self, email: str, name: str = "", coach: str = "", app_admin: bool = False,
                  sched_admin: bool = False) -> None:
        self._ensure()
        email = email.strip().lower()
        vals = dict(name=name or "", coach=coach or "", app_admin=bool(app_admin), sched_admin=bool(sched_admin),
                    updated_at=datetime.now(UTC))
        with self.engine.begin() as c:
            if c.execute(select(users.c.email).where(users.c.email == email)).first():
                c.execute(update(users).where(users.c.email == email).values(**vals))
            else:
                c.execute(insert(users).values(email=email, **vals))

    def delete_user(self, email: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(users).where(users.c.email == email.strip().lower()))

    # -------------------------------------------------------------- log --
    def log(self, who: str, action: str, detail: str = "", project: str = "") -> None:
        self._ensure()
        with self.engine.begin() as c:
            c.execute(insert(activity).values(at=datetime.now(UTC), who=who or "", action=action,
                                              detail=detail or "", project=project or ""))

    def activity(self, limit: int = 200, project: str | None = None) -> list[dict]:
        self._ensure()
        q = select(activity).order_by(activity.c.at.desc(), activity.c.id.desc()).limit(limit)
        if project:
            q = q.where(activity.c.project.in_([project, ""]))
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(q).all()]


    # --------------------------------------------------------- settings --
    def get_setting(self, key: str) -> str | None:
        self._ensure()
        with self.engine.connect() as c:
            r = c.execute(select(settings_kv.c.value).where(settings_kv.c.key == key)).first()
        return r.value if r else None

    def set_setting(self, key: str, value: str | None) -> None:
        """Saves a setting (None deletes it)."""
        self._ensure()
        with self.engine.begin() as c:
            c.execute(delete(settings_kv).where(settings_kv.c.key == key))
            if value is not None:
                c.execute(insert(settings_kv).values(key=key, value=value, updated_at=datetime.now(UTC)))

    # --------------------------------------------------------- requests --
    def add_request(self, email: str, who: str, kind: str, team: str, period: str, summary: str,
                    detail: dict, note: str = "") -> str:
        self._ensure()
        rid = new_id()
        with self.engine.begin() as c:
            c.execute(insert(requests_t).values(
                id=rid, created_at=datetime.now(UTC), email=email.strip().lower(), who=who or "",
                kind=kind, team=team or "", period=period or "", summary=summary or "",
                detail=json.dumps(detail, default=str), note=note or "", status="pending", decided_by="",
                decision_note=""))
        return rid

    def requests(self, status: str | None = None, email: str | None = None, limit: int = 300) -> list[dict]:
        self._ensure()
        q = select(requests_t).order_by(requests_t.c.created_at.desc()).limit(limit)
        if status:
            q = q.where(requests_t.c.status == status)
        if email:
            q = q.where(requests_t.c.email == email.strip().lower())
        with self.engine.connect() as c:
            rows = [dict(r._mapping) for r in c.execute(q).all()]
        for r in rows:
            r["detail"] = json.loads(r["detail"] or "{}")
        return rows

    def get_request(self, rid: str) -> dict | None:
        self._ensure()
        with self.engine.connect() as c:
            r = c.execute(select(requests_t).where(requests_t.c.id == rid)).first()
        if r is None:
            return None
        out = dict(r._mapping)
        out["detail"] = json.loads(out["detail"] or "{}")
        return out

    def rename_team_in_requests(self, old: str, new: str) -> int:
        """Pending requests for a renamed team: their team and the rows they'd apply."""
        n = 0
        for r in self.requests("pending"):
            d = r["detail"] or {}
            rows = [dict(x, team=new) if x.get("team") == old else x for x in d.get("rows") or []]
            rp = d.get("replace_pin")
            if rp and rp[0] == old:
                rp = [new] + list(rp[1:])
            if r["team"] != old and rows == (d.get("rows") or []) and rp == d.get("replace_pin"):
                continue
            with self.engine.begin() as c:
                c.execute(update(requests_t).where(requests_t.c.id == r["id"]).values(
                    team=new if r["team"] == old else r["team"],
                    summary=r["summary"].replace(old, new),
                    detail=json.dumps(dict(d, rows=rows, replace_pin=rp), default=str)))
            n += 1
        return n

    def decide_request(self, rid: str, status: str, by: str, note: str = "") -> bool:
        """Approve / deny / withdraw a pending request. False if it wasn't pending any more
        (someone else got to it first)."""
        self._ensure()
        with self.engine.begin() as c:
            res = c.execute(update(requests_t).where((requests_t.c.id == rid) & (requests_t.c.status == "pending"))
                            .values(status=status, decided_by=by or "", decided_at=datetime.now(UTC),
                                    decision_note=note or ""))
        return bool(res.rowcount)

    def delete_requests(self, ids: list[str] | None = None, decided: bool = False,
                        email: str | None = None) -> int:
        """Removes requests for good (no emails): the ones in `ids`, and/or with decided=True every one
        that isn't pending (only `email`'s, if given). Returns how many were removed."""
        self._ensure()
        n = 0
        with self.engine.begin() as c:
            if ids:
                n += c.execute(delete(requests_t).where(requests_t.c.id.in_(list(ids)))).rowcount or 0
            if decided:
                q = delete(requests_t).where(requests_t.c.status != "pending")
                if email:
                    q = q.where(requests_t.c.email == email.strip().lower())
                n += c.execute(q).rowcount or 0
        return n


def new_id() -> str:
    """A random, URL-safe id."""
    return _secrets.token_urlsafe(9)  # unguessable, URL-safe


# ------------------------------------------------------------ payloads --

def _frame_out(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    d = df.copy()
    for c in d.columns:
        d[c] = d[c].map(lambda v: v.isoformat() if isinstance(v, (date, datetime)) else
                        (None if (isinstance(v, float) and pd.isna(v)) else v))
    return d.to_dict(orient="records")


def _frame_in(rows: list[dict], date_cols=("date", "week_of")) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for c in date_cols:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c]).dt.date
    return df


def result_to_dict(r: ScheduleResult | None) -> dict | None:
    """A solved schedule as JSON-ready data (None if there isn't a good one)."""
    if r is None or not r.ok:
        return None
    return {
        "status": r.status,
        "assignments": _frame_out(r.assignments),
        "pattern": _frame_out(r.pattern),
        "issues": _frame_out(r.issues),
        "metrics": {k: (float(v) if hasattr(v, "item") else v) for k, v in r.metrics.items()},
        "warnings": list(r.warnings),
        "start": r.start.isoformat() if r.start else None,
        "end": r.end.isoformat() if r.end else None,
        "weekend_dates": [d.isoformat() for d in r.weekend_dates],
        "practice_counts": _frame_out(r.practice_counts),
        "broken": [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in b.items()} for b in r.broken],
    }


def result_from_dict(d: dict | None, inp: ScheduleInput) -> ScheduleResult | None:
    """A solved schedule back from saved data; tournaments are re-read from the inputs."""
    if not d:
        return None
    start = date.fromisoformat(d["start"]) if d.get("start") else None
    end = date.fromisoformat(d["end"]) if d.get("end") else None
    issues = _frame_in(d.get("issues", []))
    if issues.empty:
        issues = pd.DataFrame(columns=["severity", "team", "week_of", "issue", "detail"])
    try:
        tournaments = clean(inp, start, end).tournaments  # rebuilt from the inputs
    except ValueError:
        tournaments = []
    return ScheduleResult(
        status=d["status"],
        assignments=_frame_in(d.get("assignments", [])),
        pattern=_frame_in(d.get("pattern", [])),
        issues=issues,
        metrics=d.get("metrics", {}),
        warnings=d.get("warnings", []),
        start=start, end=end,
        weekend_dates=[date.fromisoformat(x) for x in d.get("weekend_dates", [])],
        tournaments=tournaments,
        practice_counts=_frame_in(d.get("practice_counts", [])),
        broken=[dict(b, date=date.fromisoformat(b["date"])) for b in d.get("broken", [])],
    )


def make_payload(inp: ScheduleInput, start: date, end: date, settings: dict,
                 result: ScheduleResult | None, published: list | None = None,
                 periods: dict | None = None) -> dict:
    """The saved schedule: the club's input tables, plus the dates being
    worked on (start/end, settings, solved schedule, published versions).
    `periods` holds the other dates already scheduled, each in the same
    shape ({"start", "end", "settings", "result", "published"})."""
    out = {
        "version": PAYLOAD_VERSION,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "settings": settings,
        "inputs": inp.to_records(),
        "result": result_to_dict(result),
        "published": list(published or []),
    }
    if periods:
        out["periods"] = {k: v for k, v in periods.items() if k != period_key(start, end)}
    return out


# ------------------------------------------------------------- periods --
# The club has one schedule. Each run of dates scheduled at a time (Nov 3 –
# Dec 4, then Dec 5 – 20...) is a period: its own solved schedule and
# published versions, all sharing the club's teams, coaches and programs.
MAIN_ID = "main"
MAIN_NAME = "Practice schedule"


def period_key(start: date, end: date) -> str:
    """The key for a run of dates: '2026-12-05_2026-12-20'."""
    return f"{start.isoformat()}_{end.isoformat()}"


def current_period(payload: dict) -> dict:
    """The period stored at the top of a payload, in the periods' shape."""
    return {"start": payload["start"], "end": payload["end"], "settings": dict(payload.get("settings") or {}),
            "result": payload.get("result"), "published": list(payload.get("published") or [])}


def all_periods(payload: dict) -> dict[str, dict]:
    """Every period in a payload (the current one included), by key."""
    out = dict(payload.get("periods") or {})
    cur = current_period(payload)
    out[period_key(date.fromisoformat(cur["start"]), date.fromisoformat(cur["end"]))] = cur
    return out


def read_period(entry: dict, inp: ScheduleInput) -> tuple[date, date, dict, ScheduleResult | None, list]:
    """A stored run of dates -> (start, end, settings, solved schedule, published versions)."""
    s_, e_ = date.fromisoformat(entry["start"]), date.fromisoformat(entry["end"])
    return s_, e_, dict(entry.get("settings") or {}), result_from_dict(entry.get("result"), inp), \
        list(entry.get("published") or [])


def ensure_main(store: Store, default: tuple[date, date] | None = None) -> dict:
    """The club's one schedule, created the first time from the most recent
    saved schedule that has teams (older saved schedules stay in the
    database, and their dates can be brought in), or empty."""
    rec = store.get(MAIN_ID)
    if rec is not None:
        return rec
    best = None
    for p in store.list():  # newest first
        r = store.get(p["id"])
        if r is None:
            continue
        has_teams = bool((r["payload"].get("inputs") or {}).get("Teams"))
        if best is None or (has_teams and "demo" not in p["name"].lower() and not best[1]):
            best = (r, has_teams and "demo" not in p["name"].lower())
            if best[1]:
                break
    if best is not None:
        payload = dict(best[0]["payload"])
        payload.pop("periods", None)
    else:
        s_, e_ = default or (date.today(), date.today())
        payload = make_payload(ScheduleInput.from_records({}), s_, e_, {"time_limit": 60, "makeup": True}, None)
    store.save(MAIN_ID, MAIN_NAME, payload)
    return store.get(MAIN_ID)


def read_published(payload: dict) -> list[dict]:
    """Published versions saved with a schedule (oldest first)."""
    return list(payload.get("published") or [])


def read_payload(payload: dict) -> tuple[ScheduleInput, date, date, dict, ScheduleResult | None]:
    """The saved schedule -> (inputs, start, end, settings, solved schedule) for the dates on top."""
    inp = ScheduleInput.from_records(payload.get("inputs", {}))
    start = date.fromisoformat(payload["start"])
    end = date.fromisoformat(payload["end"])
    inp.start, inp.end = start, end
    result = result_from_dict(payload.get("result"), inp)
    return inp, start, end, payload.get("settings", {}), result


def database_url() -> str | None:
    """DATABASE_URL from Streamlit secrets or the environment."""
    try:
        import streamlit as st
        url = st.secrets.get("DATABASE_URL") or st.secrets.get("database_url")
        if url:
            return str(url)
    except Exception:  # noqa: BLE001  (no secrets file locally)
        pass
    return os.environ.get("DATABASE_URL")


def period_bookings(entry: dict) -> tuple[pd.DataFrame, list[date]]:
    """A stored period's bookings and open weekend/break days, without
    rebuilding the whole result (quick, for showing every date at once)."""
    r = entry.get("result") or {}
    a = _frame_in(r.get("assignments", []))
    return a, [date.fromisoformat(x) for x in r.get("weekend_dates", [])]


def rename_team_in_period(entry: dict, old: str, new: str) -> dict:
    """A stored run of dates (its build and published versions) with a team renamed."""
    def fix(rows, col):
        return [dict(r, **{col: new}) if r.get(col) == old else r for r in (rows or [])]
    out = dict(entry)
    r = entry.get("result")
    if r:
        out["result"] = dict(r, assignments=fix(r.get("assignments"), "name"), pattern=fix(r.get("pattern"), "team"),
                             practice_counts=fix(r.get("practice_counts"), "team"), issues=fix(r.get("issues"), "team"),
                             broken=fix(r.get("broken"), "team"))
    out["published"] = [dict(v, practices=fix(v.get("practices"), "name"), pattern=fix(v.get("pattern"), "team"))
                        for v in entry.get("published") or []]
    return out
