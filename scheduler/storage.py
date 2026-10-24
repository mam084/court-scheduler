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
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import Column, DateTime, MetaData, String, Table, Text, create_engine, delete, insert, select, update

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


def _normalize_url(url: str) -> str:
    """Supabase/Neon hand out postgres:// or postgresql:// URLs. Use the
    psycopg2 driver we ship (newer SQLAlchemy defaults to psycopg 3)."""
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


class Store:
    def __init__(self, url: str | None = None):
        if url:
            self.url = _normalize_url(url)
        else:
            DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
            self.url = f"sqlite:///{DEFAULT_DB}"
        self.engine = create_engine(self.url, pool_pre_ping=True)
        _meta.create_all(self.engine)

    @property
    def permanent(self) -> bool:
        return not self.url.startswith("sqlite")

    @property
    def kind(self) -> str:
        return "database" if self.permanent else "local file"

    # ------------------------------------------------------------- CRUD --
    def list(self) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(projects.c.id, projects.c.name, projects.c.updated_at)
                             .order_by(projects.c.updated_at.desc())).all()
        return [{"id": r.id, "name": r.name, "updated_at": r.updated_at} for r in rows]

    def get(self, pid: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(select(projects).where(projects.c.id == pid)).first()
        if r is None:
            return None
        return {"id": r.id, "name": r.name, "updated_at": r.updated_at, "payload": json.loads(r.payload)}

    def save(self, pid: str, name: str, payload: dict) -> None:
        body = json.dumps(payload, default=str)
        now = datetime.now(timezone.utc)
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


def new_id() -> str:
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
    }


def result_from_dict(d: dict | None, inp: ScheduleInput) -> ScheduleResult | None:
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
    )


def make_payload(inp: ScheduleInput, start: date, end: date, settings: dict,
                 result: ScheduleResult | None) -> dict:
    return {
        "version": PAYLOAD_VERSION,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "settings": settings,
        "inputs": inp.to_records(),
        "result": result_to_dict(result),
    }


def read_payload(payload: dict) -> tuple[ScheduleInput, date, date, dict, ScheduleResult | None]:
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
