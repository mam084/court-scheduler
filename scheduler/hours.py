"""Practices with their own start and end times (Nov 18, 2026).

Team practices normally fill a timeslot (3:45-5:30, a 9-11 block). A scheduling admin can give one
practice its own hours instead, like 9-11:30 or 8:30-11. The practice keeps its timeslot (that's how
the schedule knows it), but:

- its court is held from team practices in every other timeslot its hours run into (9-11:30 on
  Court 3 means no team gets Court 3 at 11-1), while
- privates only steer clear of its actual hours (a private on Court 3 at 11:30 is fine).

Everything that shows a time (court view, printouts, coach emails, Google Calendar) shows its hours.
The Hours table: team, date, time (its timeslot), court, start, end, note.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from .config import SLOT_TIMES


@dataclass
class Hours:
    team: str
    day: date
    slot: str
    court: int | None
    start: int  # minutes after midnight
    end: int


def slot_span(slot: str) -> tuple[int, int] | None:
    """A timeslot's (start, end) in minutes; None for one without set times."""
    if slot not in SLOT_TIMES:
        return None
    (sh, sm), (eh, em) = SLOT_TIMES[slot]
    return sh * 60 + sm, eh * 60 + em


def parse_minutes(value) -> int:
    """'9', '9:00', '11:30', '11:30 AM', '2 PM', '14:00' -> minutes after midnight. Without AM/PM a
    bare hour 1-7 is afternoon, 8-11 morning."""
    import re
    v = str(value).strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?$", v)
    if not m:
        raise ValueError(f"'{value}' isn't a time (like 11:30 AM)")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3) or ""
    if ap.startswith("p") and h < 12:
        h += 12
    elif ap.startswith("a") and h == 12:
        h = 0
    elif not ap and 1 <= h <= 7:
        h += 12
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError(f"'{value}' isn't a time (like 11:30 AM)")
    return h * 60 + mi


def clock(m: int, ampm: bool = True) -> str:
    """690 -> '11:30 AM' (ampm=False: '11:30')."""
    h, mi = divmod(int(m), 60)
    out = f"{(h - 1) % 12 + 1}" + (f":{mi:02d}" if mi else "")
    return out + (f" {'PM' if h >= 12 else 'AM'}" if ampm else "")


def label(start: int, end: int) -> str:
    """Like the timeslot labels: '9–11:30 AM', '11:30 AM–1 PM', '3:45–6 PM'."""
    same = (start >= 720) == (end >= 720)
    return f"{clock(start, ampm=not same)}–{clock(end)}"


def short_label(start: int, end: int) -> str:
    """As the paper sheets write times: '9-11:30', '3:45-6'."""
    return f"{clock(start, False)}-{clock(end, False)}"


def from_table(df: pd.DataFrame | None) -> dict[tuple[str, date, str], Hours]:
    """The Hours table -> {(team, date, timeslot): Hours}. Rows that don't read are skipped."""
    out: dict = {}
    if df is None or not len(df):
        return out
    for r in df.fillna("").to_dict("records"):
        try:
            team, slot = str(r.get("team", "")).strip(), str(r.get("time", "")).strip()
            d = pd.to_datetime(str(r.get("date", "")).strip()).date()
            s, e = parse_minutes(r.get("start")), parse_minutes(r.get("end"))
        except (ValueError, TypeError):
            continue
        if not team or slot not in SLOT_TIMES or e <= s:
            continue
        ct = str(r.get("court", "")).strip()
        out[team, d, slot] = Hours(team, d, slot, int(float(ct)) if ct.replace(".", "").isdigit() else None, s, e)
    return out


def row(team: str, d: date, slot: str, court, start: int, end: int, note: str = "") -> dict:
    """One Hours table row."""
    return {"team": team, "date": f"{d:%m/%d/%Y}", "time": slot,
            "court": "" if court is None or (isinstance(court, float) and pd.isna(court)) else str(int(court)),
            "start": clock(start), "end": clock(end), "note": note}


def without(df: pd.DataFrame, team: str, d: date, slot: str) -> pd.DataFrame:
    """The Hours table minus this practice's row."""
    if df is None or not len(df):
        return df
    ds = pd.to_datetime(df["date"].astype(str), errors="coerce").dt.date
    hit = (df["team"].astype(str).str.strip() == team) & (ds == d) & (df["time"].astype(str).str.strip() == slot)
    return df[~hit].reset_index(drop=True)


def held_courts(hours: dict, d: date, slot: str) -> set[int]:
    """Courts held from team practices in timeslot `slot` on day d by practices running into it from
    another timeslot (their own hours overlap it)."""
    span = slot_span(slot)
    if span is None:
        return set()
    return {h.court for h in hours.values() if h.day == d and h.slot != slot and h.court is not None
            and h.start < span[1] and span[0] < h.end}


def with_hours(assignments: pd.DataFrame, hours: dict) -> pd.DataFrame:
    """The bookings with columns `t0`/`t1` (a practice's own hours in minutes, else empty) and its
    `slot_label` showing them. A practice's hours apply while it's still at that date and timeslot
    (moved away, it goes back to its timeslot's times)."""
    if assignments is None or not len(assignments):
        return assignments
    a = assignments.copy()
    from .config import SLOTS
    t0, t1, lab = [], [], []
    base_lab = a["slot_label"] if "slot_label" in a.columns else a["slot"].map(lambda s: SLOTS.get(s, s))
    for k, n, d, s, bl in zip(a["kind"], a["name"], a["date"], a["slot"], base_lab):
        h = hours.get((n, d, s)) if k == "Practice" and hours else None
        if h is None:
            t0.append(None)
            t1.append(None)
            lab.append(SLOTS.get(s, bl) if k == "Practice" else bl)  # (hours since cleared: back to normal)
        else:
            t0.append(h.start)
            t1.append(h.end)
            lab.append(label(h.start, h.end))
    a["t0"], a["t1"], a["slot_label"] = pd.Series(t0, index=a.index, dtype=object), \
        pd.Series(t1, index=a.index, dtype=object), lab
    return a


def span_of(r) -> tuple[int, int] | None:
    """A booking's (start, end) in minutes: its own hours if it has them, else its timeslot's."""
    get = r.get if hasattr(r, "get") else (lambda k, default=None: getattr(r, k, default))
    t0, t1 = get("t0", None), get("t1", None)
    if t0 is not None and t1 is not None and not (isinstance(t0, float) and pd.isna(t0)):
        return int(t0), int(t1)
    return slot_span(str(get("slot", "")))
