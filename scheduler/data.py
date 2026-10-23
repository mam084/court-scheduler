"""Input data: the five tables the club fills in.

The canonical form is a set of pandas DataFrames, which map one-to-one to
sheets in the input spreadsheet and to the editable tables in the app.

Sheets / columns
----------------
Teams     : team, age, coach, priority (optional, 1-3)
Coaches   : coach, email, preferred_days ("Mon, Wed"), preferred_slots ("5:30")
NoDates   : coach, date, slot ("All" or 3:45 / 5:30 / 7:20), note
Locals    : program, day, slot, courts ("1-3" or "1,2,5"), coaches ("Ana; Ben")
Closures  : date, reason
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .config import COURTS, SLOT_KEYS, WEEKDAYS, default_priority

COLUMNS = {
    "Teams": ["team", "age", "coach", "priority"],
    "Coaches": ["coach", "email", "preferred_days", "preferred_slots"],
    "NoDates": ["coach", "date", "slot", "note"],
    "Locals": ["program", "day", "slot", "courts", "coaches"],
    "Closures": ["date", "reason"],
}


@dataclass
class ScheduleInput:
    teams: pd.DataFrame
    coaches: pd.DataFrame
    no_dates: pd.DataFrame
    locals: pd.DataFrame
    closures: pd.DataFrame
    start: date | None = None
    end: date | None = None

    # ---------------------------------------------------------------- IO --
    def to_excel_bytes(self) -> bytes:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            for sheet, df in self.sheets().items():
                df.to_excel(xw, sheet_name=sheet, index=False)
        return buf.getvalue()

    def sheets(self) -> dict[str, pd.DataFrame]:
        return {
            "Teams": self.teams,
            "Coaches": self.coaches,
            "NoDates": self.no_dates,
            "Locals": self.locals,
            "Closures": self.closures,
        }

    @classmethod
    def from_excel(cls, file) -> "ScheduleInput":
        xl = pd.read_excel(file, sheet_name=None)
        frames = {}
        for sheet, cols in COLUMNS.items():
            df = xl.get(sheet, pd.DataFrame(columns=cols))
            df.columns = [str(c).strip().lower() for c in df.columns]
            for c in cols:
                if c not in df.columns:
                    df[c] = None
            frames[sheet] = df[cols].dropna(how="all").reset_index(drop=True)
        return cls(
            teams=frames["Teams"],
            coaches=frames["Coaches"],
            no_dates=frames["NoDates"],
            locals=frames["Locals"],
            closures=frames["Closures"],
        )


# ------------------------------------------------------------- parsing --

def parse_list(value, sep_pattern=r"[;,]") -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    return [p.strip() for p in re.split(sep_pattern, str(value)) if p.strip()]


def parse_days(value) -> list[str]:
    out = []
    for p in parse_list(value):
        key = p[:3].title()
        if key in WEEKDAYS:
            out.append(key)
    return out


def parse_slot(value) -> str | None:
    """'5:30', '5:30-7:20', '17:30', '530' -> '5:30'. 'All'/blank -> None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip().lower()
    if s in ("", "all", "any", "all day"):
        return None
    m = re.match(r"(\d{1,2}):?(\d{2})", s)
    if not m:
        raise ValueError(f"Can't read timeslot '{value}'")
    h, mnt = int(m.group(1)), m.group(2)
    if h > 12:
        h -= 12
    key = f"{h}:{mnt}"
    if key not in SLOT_KEYS:
        raise ValueError(f"'{value}' isn't one of the club timeslots {SLOT_KEYS}")
    return key


def parse_slots(value) -> list[str]:
    return [s for s in (parse_slot(p) for p in parse_list(value)) if s]


def parse_courts(value) -> list[int]:
    """'1-3' -> [1,2,3]; '1, 4,5' -> [1,4,5]; 'All' -> every court."""
    if str(value).strip().lower() == "all":
        return list(COURTS)
    out: list[int] = []
    for part in parse_list(value):
        if "-" in part:
            a, b = (int(x) for x in part.split("-"))
            out.extend(range(a, b + 1))
        else:
            out.append(int(float(part)))
    bad = [c for c in out if c not in COURTS]
    if bad:
        raise ValueError(f"Court(s) {bad} don't exist (courts are {COURTS[0]}-{COURTS[-1]})")
    return sorted(set(out))


def to_date(value) -> date:
    return pd.to_datetime(value).date()


# ---------------------------------------------------------- normalised --

@dataclass
class Team:
    name: str
    age: int
    coach: str
    priority: int


@dataclass
class Coach:
    name: str
    email: str = ""
    preferred_days: list[str] = field(default_factory=list)
    preferred_slots: list[str] = field(default_factory=list)


@dataclass
class LocalProgram:
    name: str
    day: str
    slot: str
    courts: list[int]
    coaches: list[str]


@dataclass
class CleanInput:
    teams: list[Team]
    coaches: dict[str, Coach]
    # (coach, date) -> set of blocked slots (all slots if blocked all day)
    no_dates: dict[tuple[str, date], set[str]]
    locals: list[LocalProgram]
    closures: dict[date, str]
    warnings: list[str]


def clean(inp: ScheduleInput) -> CleanInput:
    """Validate and normalise. Raises ValueError with a readable message
    for problems the solver can't work around; collects softer issues as
    warnings."""
    errors: list[str] = []
    warnings: list[str] = []

    coaches: dict[str, Coach] = {}
    for i, r in inp.coaches.iterrows():
        name = str(r["coach"]).strip() if pd.notna(r["coach"]) else ""
        if not name:
            continue
        try:
            coaches[name] = Coach(
                name=name,
                email="" if pd.isna(r.get("email")) else str(r["email"]).strip(),
                preferred_days=parse_days(r.get("preferred_days")),
                preferred_slots=parse_slots(r.get("preferred_slots")),
            )
        except ValueError as e:
            errors.append(f"Coaches row {i + 2} ({name}): {e}")

    teams: list[Team] = []
    seen = set()
    for i, r in inp.teams.iterrows():
        name = str(r["team"]).strip() if pd.notna(r["team"]) else ""
        if not name:
            continue
        if name in seen:
            errors.append(f"Teams: '{name}' is listed twice")
            continue
        seen.add(name)
        try:
            age = int(float(r["age"]))
        except (TypeError, ValueError):
            errors.append(f"Teams row {i + 2} ({name}): age '{r['age']}' isn't a number")
            continue
        coach = str(r["coach"]).strip() if pd.notna(r["coach"]) else ""
        if not coach:
            errors.append(f"Teams row {i + 2} ({name}): no coach")
            continue
        if coach not in coaches:
            warnings.append(f"Coach '{coach}' (team {name}) has no row on the Coaches sheet; assuming no preferences")
            coaches[coach] = Coach(name=coach)
        pr = r.get("priority")
        priority = default_priority(age) if pd.isna(pr) else int(float(pr))
        teams.append(Team(name, age, coach, max(1, min(3, priority))))

    no_dates: dict[tuple[str, date], set[str]] = {}
    for i, r in inp.no_dates.iterrows():
        if pd.isna(r["coach"]) or pd.isna(r["date"]):
            continue
        coach = str(r["coach"]).strip()
        try:
            d = to_date(r["date"])
            slot = parse_slot(r.get("slot"))
        except (ValueError, TypeError) as e:
            errors.append(f"NoDates row {i + 2}: {e}")
            continue
        if coach not in coaches:
            warnings.append(f"NoDates row {i + 2}: coach '{coach}' isn't on the Coaches sheet")
        blocked = set(SLOT_KEYS) if slot is None else {slot}
        no_dates.setdefault((coach, d), set()).update(blocked)

    locals_: list[LocalProgram] = []
    for i, r in inp.locals.iterrows():
        if pd.isna(r["program"]):
            continue
        try:
            day = parse_days(r["day"])
            if len(day) != 1:
                raise ValueError(f"day '{r['day']}' should be one weekday (Mon-Fri)")
            slot = parse_slot(r["slot"])
            if slot is None:
                raise ValueError("needs a timeslot")
            locals_.append(LocalProgram(
                name=str(r["program"]).strip(),
                day=day[0],
                slot=slot,
                courts=parse_courts(r["courts"]),
                coaches=parse_list(r.get("coaches")),
            ))
        except (ValueError, TypeError) as e:
            errors.append(f"Locals row {i + 2} ({r['program']}): {e}")

    # Two locals on the same court at the same time is a data error.
    used: dict[tuple[str, str, int], str] = {}
    for lp in locals_:
        for c in lp.courts:
            key = (lp.day, lp.slot, c)
            if key in used:
                errors.append(f"Locals '{used[key]}' and '{lp.name}' both use court {c} {lp.day} {lp.slot}")
            used[key] = lp.name

    closures: dict[date, str] = {}
    for i, r in inp.closures.iterrows():
        if pd.isna(r["date"]):
            continue
        try:
            closures[to_date(r["date"])] = "" if pd.isna(r.get("reason")) else str(r["reason"])
        except (ValueError, TypeError) as e:
            errors.append(f"Closures row {i + 2}: {e}")

    if errors:
        raise ValueError("Please fix these in the input:\n- " + "\n- ".join(errors))
    if not teams:
        raise ValueError("There are no teams to schedule.")

    return CleanInput(teams, coaches, no_dates, locals_, closures, warnings)
