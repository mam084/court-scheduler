"""Published versions and what changed between two schedules.

A published version is a frozen copy of the practices (and weekly
template) that went out to coaches. Comparing the current schedule with it
lists every practice that moved, appeared, disappeared or changed court,
with the most likely reason taken from the current inputs.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone

import pandas as pd

from .config import COURTS, SLOT_KEYS, SLOTS, week_start
from .data import CleanInput

PRACTICE_COLS = ["name", "club", "age", "date", "day", "slot", "court", "coach", "head"]
PATTERN_COLS = ["team", "day", "slot", "court"]
MAX_VERSIONS = 12


def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else (None if isinstance(v, float) and pd.isna(v) else v)


def snapshot(result, label: str, note: str = "") -> dict:
    """A published version: the practices and template as they are now."""
    a = result.assignments
    pr = a[a["kind"] == "Practice"] if len(a) else a
    pr = pr[[c for c in PRACTICE_COLS if c in pr.columns]]
    pt = result.pattern[[c for c in PATTERN_COLS if c in result.pattern.columns]] if len(result.pattern) else result.pattern
    return {
        "id": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"),
        "label": label,
        "note": note,
        "at": datetime.now(timezone.utc).isoformat(),
        "start": result.start.isoformat() if result.start else None,
        "end": result.end.isoformat() if result.end else None,
        "practices": [{k: _iso(v) for k, v in r.items()} for r in pr.to_dict(orient="records")],
        "pattern": [{k: _iso(v) for k, v in r.items()} for r in pt.to_dict(orient="records")],
    }


def add_version(versions: list[dict], snap: dict) -> list[dict]:
    """Newest last; keeps the most recent MAX_VERSIONS."""
    return (list(versions) + [snap])[-MAX_VERSIONS:]


def practices_of(version: dict) -> pd.DataFrame:
    df = pd.DataFrame(version.get("practices", []), columns=PRACTICE_COLS)
    if len(df):
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["kind"] = "Practice"
    else:
        df["kind"] = []
    return df


def pattern_of(version: dict) -> pd.DataFrame:
    return pd.DataFrame(version.get("pattern", []), columns=PATTERN_COLS)


CLUB_TZ = "America/Los_Angeles"


def published_at(version: dict) -> datetime:
    """When a version was published, in the club's time zone."""
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(version["at"]).astimezone(ZoneInfo(CLUB_TZ))


# --------------------------------------------------------------- reasons --

def _why_gone(team, d: date, slot: str, data: CleanInput, start: date, end: date) -> str:
    """Best explanation for why a practice isn't on (team, d, slot) any more."""
    if not (start <= d <= end):
        return "outside this schedule's dates now"
    if d in data.closures:
        return f"club closed ({data.closures[d] or 'closure'})"
    if team is None:
        return "team no longer on the Teams sheet"
    if team.start and d < team.start:
        return f"season now starts {team.start:%-m/%-d}"
    for tn in data.tournaments:
        if not tn.covers(d):
            continue
        if tn.kind == "travel" and team.name in tn.teams:
            return f"away at {tn.name}"
        if tn.kind == "local" and set(COURTS) <= set(tn.courts):
            return f"courts taken by {tn.name}"
    for r in data.rules:
        if r.kind == "no" and r.manual and r.who == team.name and r.matches(d, slot):
            return "removed by hand"
    for r in data.rules:
        if r.kind == "no" and r.who == team.name and r.matches(d, slot):
            return "team no-date" + (f" ({r.note})" if r.note else "")
    for h in team.heads:
        for r in data.rules:
            if r.kind == "no" and r.who == h and r.matches(d, slot):
                return f"{h} unavailable" + (f" ({r.note})" if r.note else "")
    for lp in data.locals:
        if lp.blocks(d, slot) and set(lp.coaches) & set(team.heads):
            return f"{', '.join(set(lp.coaches) & set(team.heads))} working {lp.name}"
    for tn in data.tournaments:
        if tn.kind == "local" and tn.covers(d) and set(tn.coaches) & set(team.heads):
            return f"{', '.join(set(tn.coaches) & set(team.heads))} working {tn.name}"
    return ""


def _court_taken(d: date, slot: str, court: int, data: CleanInput) -> str:
    for lp in data.locals:
        if lp.on_site and lp.blocks(d, slot) and court in lp.courts:
            return f"Court {court} taken by {lp.name}"
    for tn in data.tournaments:
        if tn.kind == "local" and tn.covers(d) and court in tn.courts:
            return f"Court {court} taken by {tn.name}"
    return ""


def diff(old: pd.DataFrame, new: pd.DataFrame, data: CleanInput | None, start: date, end: date,
         pinned: set | None = None) -> pd.DataFrame:
    """Every practice that differs between two schedules.

    Returns columns: team, week_of, change (Moved / Added / Removed / Court),
    was, now, why, sort. A removed and an added practice for the same team in
    the same week are paired up as one move.
    """
    cols = ["team", "week_of", "change", "was", "now", "why"]
    old = old[old["kind"] == "Practice"] if "kind" in old.columns and len(old) else old
    new = new[new["kind"] == "Practice"] if "kind" in new.columns and len(new) else new
    # Only compare the dates both schedules cover.
    if len(old):
        old = old[(old["date"] >= start) & (old["date"] <= end)]
    teams = {t.name: t for t in data.teams} if data else {}
    pinned = pinned or set()

    def key(r):
        return (r["name"], r["date"], r["slot"])

    o = {key(r): r for r in old.to_dict(orient="records")} if len(old) else {}
    n = {key(r): r for r in new.to_dict(orient="records")} if len(new) else {}
    gone = sorted(set(o) - set(n), key=lambda k: (k[0], k[1], SLOT_KEYS.index(k[2])))
    came = sorted(set(n) - set(o), key=lambda k: (k[0], k[1], SLOT_KEYS.index(k[2])))

    def label(r, with_court=True):
        s = f"{r['date']:%a %-m/%-d} {SLOTS.get(r['slot'], r['slot'])}"
        return s + (f" · Court {int(r['court'])}" if with_court and pd.notna(r.get("court")) else "")

    rows = []
    came_by = defaultdict(list)
    for k in came:
        came_by[(k[0], week_start(k[1]))].append(k)
    for k in gone:
        team, d, s = k
        why = _why_gone(teams.get(team), d, s, data, start, end) if data else ""
        partner = came_by.get((team, week_start(d)))
        if partner:
            k2 = partner.pop(0)
            extra = " (placed by hand)" if k2 in pinned else ""
            rows.append({"team": team, "week_of": week_start(d), "change": "Moved", "was": label(o[k]),
                         "now": label(n[k2]),
                         "why": (why or "rearranged to fit other changes") + extra})
        else:
            rows.append({"team": team, "week_of": week_start(d), "change": "Removed", "was": label(o[k]),
                         "now": "", "why": why or "rearranged to fit other changes"})
    for lst in came_by.values():
        for k2 in lst:
            rows.append({"team": k2[0], "week_of": week_start(k2[1]), "change": "Added", "was": "",
                         "now": label(n[k2]),
                         "why": "placed by hand" if k2 in pinned else "added to reach 2 practices that week"})
    for k in sorted(set(o) & set(n)):
        a, b = o[k], n[k]
        if pd.notna(a.get("court")) and pd.notna(b.get("court")) and int(a["court"]) != int(b["court"]):
            why = _court_taken(k[1], k[2], int(a["court"]), data) if data else ""
            rows.append({"team": k[0], "week_of": week_start(k[1]), "change": "Court",
                         "was": label(a), "now": f"Court {int(b['court'])}",
                         "why": why or "courts rebalanced"})
    df = pd.DataFrame(rows, columns=cols)
    if len(df):
        order = {"Removed": 0, "Moved": 1, "Added": 2, "Court": 3}
        df = df.assign(o=df["change"].map(order)).sort_values(["week_of", "o", "team"]).drop(columns="o")
        df = df.reset_index(drop=True)
    return df


# ------------------------------------------------------------- fairness --

def burden(practices: pd.DataFrame, issues: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per team: how many of the less-wanted spots it got.

    Court 2 (the worst court), 3:45 practices, weekend practices, practices
    off the team's usual schedule (makeups / moves) and back-to-back days.
    """
    cols = ["team", "practices", "court_2", "at_3_45", "weekend", "off_usual", "back_to_back"]
    if practices is None or not len(practices):
        return pd.DataFrame(columns=cols)
    pr = practices[practices["kind"] == "Practice"] if "kind" in practices.columns else practices
    out = []
    for team, g in pr.groupby("name"):
        days = sorted(set(g["date"]))
        b2b = sum(1 for a, b in zip(days, days[1:]) if (b - a).days == 1)
        out.append({
            "team": team,
            "practices": len(g),
            "court_2": int((g["court"] == 2).sum()) if "court" in g else 0,
            "at_3_45": int((g["slot"] == "3:45").sum()),
            "weekend": int(g["date"].map(lambda d: d.weekday() >= 5).sum()),
            "off_usual": int((~g["on_pattern"].astype(bool)).sum()) if "on_pattern" in g else 0,
            "back_to_back": b2b,
        })
    return pd.DataFrame(out, columns=cols)
