"""Practices copied off the club's handwritten court sheets, imported as hand edits.

A CSV (or Excel) list with one practice a row: date, court, start time and team, the team written
the way the sheets write it (S14-B, 16-E, 14-Ca) or by its full name. Each row becomes an "Add"
hand edit, so the practice is pinned where the sheet has it and the scheduler fits around it.

Times can be the start ("17:30", "5:30", "9:00") or the sheet's label ("5:30-7:20", "9-11").
On a regular weekday, 9-11, 11-1 and 1-3 are daytime blocks (a day off school): the day keeps its
usual 3:45 / 5:30 / 7:20 times for everyone else.
"""

from __future__ import annotations

import io
import re
from collections import defaultdict
from datetime import date

import pandas as pd

from .config import COURTS, PRACTICES_PER_WEEK, SLOT_KEYS, WEEKDAY_KEYS, WEEKEND_KEYS, week_start
from .labels import label_map

# 24h start -> slot key.
_WEEKDAY_START = {(15, 45): "3:45", (17, 30): "5:30", (19, 20): "7:20"}
_DAYTIME_START = {(9, 0): "9AM", (11, 0): "11AM", (13, 0): "1PM"}  # weekday daytime blocks
_WEEKEND_START = {(9, 0): "9AM", (11, 0): "11AM", (13, 0): "1PM", (15, 0): "3PM", (17, 0): "5PM", (19, 0): "7PM"}


def _norm(s) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def slot_for(d: date, value) -> str:
    """The practice slot a written time means on day d (weekend blocks Sat/Sun, weekday times otherwise)."""
    txt = str(value or "").strip().upper().replace("–", "-")
    if not txt:
        raise ValueError("no time")
    if txt in WEEKDAY_KEYS + WEEKEND_KEYS:
        return txt
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?", txt)
    if not m:
        raise ValueError(f"can't read the time '{value}'")
    h, mi, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "PM" and h < 12:
        h += 12
    weekend = d.weekday() >= 5
    if ampm is None and h < 9 and not weekend:  # "5:30" written on a weekday is the evening
        h += 12
    if ampm is None and weekend and h < 8:  # "1-3", "5-7" on a weekend are afternoon blocks
        h += 12
    table = _WEEKEND_START if weekend else _WEEKDAY_START
    if (h, mi) in table:
        return table[(h, mi)]
    if not weekend and h < 12 and (h + 12, mi) in table:
        return table[(h + 12, mi)]
    if not weekend and (h, mi) in _DAYTIME_START:  # "9-11", "1-3" on a weekday: a daytime block
        return _DAYTIME_START[(h, mi)]
    raise ValueError(f"'{value}' isn't a {'weekend block' if weekend else 'weekday practice (or 9-11, 11-1, 1-3)'} time")


def read_rows(data: bytes, filename: str = "") -> pd.DataFrame:
    """The uploaded file as a table with lower-case column names."""
    if filename.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(data), dtype=str)
    else:
        df = pd.read_csv(io.BytesIO(data), dtype=str)
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df.fillna("")


def to_overrides(df: pd.DataFrame, teams, existing: pd.DataFrame | None = None) -> tuple[list[dict], list[str]]:
    """(hand-edit rows, problems). `teams` are the club's teams (objects with .name, .club and .per_week);
    `existing` the hand edits already there. Rows that can't be read, or whose team isn't known, are
    left out and listed as problems; so are repeats and practices already added by hand.
    A team's practices past its weekly number (counting ones already added by hand) go in as Extra,
    so the list is kept as written even when a team has 3 that week."""
    lm = label_map((t.name, t.club) for t in teams)
    lookup: dict[str, str] = {}
    for full, lab in lm.items():
        lookup.setdefault(_norm(full), full)
    exact = {lab: full for full, lab in lm.items()}  # 14-C and 14-Ca are different teams
    lower: dict[str, list[str]] = {}
    for full, lab in lm.items():
        lower.setdefault(_norm(lab), []).append(full)

    def team_of(written: str) -> str | None:
        w = str(written).strip()
        if w in exact:
            return exact[w]
        if _norm(w) in lookup:
            return lookup[_norm(w)]
        hits = lower.get(_norm(w), [])
        if len(hits) == 1:
            return hits[0]
        # A shorter way of writing one team's label ("16-D" for 16-D&D), when it can only mean that one.
        start = [full for full, lab in lm.items() if _norm(lab).startswith(_norm(w)) and "-" in w]
        return start[0] if len(start) == 1 else None

    need = {"date", "team"}
    missing = need - set(df.columns)
    if missing:
        return [], [f"The file needs these columns: {', '.join(sorted(missing))}."]
    time_col = "start" if "start" in df.columns else ("time" if "time" in df.columns else None)
    if time_col is None:
        return [], ["The file needs a start (or time) column."]

    already, placed = set(), defaultdict(int)  # hand Adds there now; (team, week) -> regular ones
    for _, r in (existing.fillna("").iterrows() if isinstance(existing, pd.DataFrame) else []):
        act = str(r.get("action", "")).strip().lower()
        if not act.startswith(("add", "pin", "move", "extra")):
            continue
        try:
            d = pd.to_datetime(str(r["date"]).strip()).date()
        except (ValueError, TypeError):
            continue
        already.add((str(r["team"]).strip(), d, str(r.get("time", "")).strip()))
        if not act.startswith("extra"):
            placed[str(r["team"]).strip(), week_start(d)] += 1

    rows, problems, seen = [], [], set()
    for i, r in df.iterrows():
        line = f"Row {i + 2}"
        if not str(r.get("team", "")).strip() and not str(r.get("date", "")).strip():
            continue
        try:
            d = pd.to_datetime(str(r["date"]).strip()).date()
        except (ValueError, TypeError):
            problems.append(f"{line}: can't read the date '{r['date']}'")
            continue
        team = team_of(r["team"])
        if team is None:
            problems.append(f"{line}: no team called '{r['team']}'")
            continue
        try:
            slot = slot_for(d, r[time_col])
        except ValueError as e:
            problems.append(f"{line} ({r['team']}): {e}")
            continue
        court = str(r.get("court", "")).strip()
        if court and (not court.isdigit() or int(court) not in COURTS):
            problems.append(f"{line} ({r['team']}): court '{court}' isn't a team court")
            continue
        key = (team, d, slot)
        if key in seen:
            problems.append(f"{line}: {r['team']} is already on {d:%a %b %-d} at {slot}, so it's added once")
            continue
        if key in already:
            problems.append(f"{line}: {r['team']} on {d:%a %b %-d} at {slot} was already added by hand, so skipped")
            continue
        seen.add(key)
        note = str(r.get("note", "")).strip()
        rows.append({"team": team, "action": "Add", "date": f"{d:%m/%d/%Y}", "days": "", "time": slot,
                     "court": court, "note": note or "from the handwritten sheet", "_d": d})
    per_week = {t.name: getattr(t, "per_week", PRACTICES_PER_WEEK) for t in teams}
    order = {k: i for i, k in enumerate(SLOT_KEYS)}
    for r in sorted(rows, key=lambda x: (x["_d"], order.get(x["time"], 99))):
        wk = (r["team"], week_start(r["_d"]))
        if placed[wk] >= per_week.get(r["team"], PRACTICES_PER_WEEK):
            r["action"] = "Extra"  # past the team's weekly number: kept on top of it
        else:
            placed[wk] += 1
    for r in rows:
        del r["_d"]
    return rows, problems
