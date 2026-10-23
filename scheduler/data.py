"""Input data: the sheets the club fills in.

Canonical form is a set of pandas DataFrames, one per spreadsheet sheet
(and one editable table per sheet in the app).

Sheets / columns
----------------
Teams         team, club, age, head_coach, assistants, start_date, priority,
              weekend_only, weekend_pref, late_ok (Yes = 14s-and-under team that
              may practice at 7:20, though it's avoided)
Availability  who, type, days, dates, times, note
Wishlist      team, choice, days, times, note
Locals        program, days, time, courts, coaches, on_site, dates
Weekends      date, open, courts, group, required, note
Closures      date, reason

Field formats (all forgiving):
  days   "Mon, Wed" · "Tue/Thu" · "Weekdays" · "Sat" · blank = any day
  dates  "11/2" · "11/10-11/16" · "11/10-16" · "Nov 15-16" · "11/2; 11/10-16"
  times  "3:45" · "5:30, 7:20" · "345pm" · weekend "9AM" / "3PM" / "AM" / "PM"
         blank or "All" = any time
  weekend_only / weekend_pref  tokens like "11/1 AM; 11/8 3PM" or "Sat AM; Sun PM"
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from .config import (
    AM_BLOCKS, COURTS, DAYS, PM_BLOCKS, SLOT_KEYS, WEEKDAY_KEYS, WEEKDAYS, WEEKEND,
    WEEKEND_KEYS, default_priority, slot_allowed_for_age,
)

COLUMNS = {
    "Teams": ["team", "club", "age", "head_coach", "assistants", "start_date", "priority",
              "weekend_only", "weekend_pref", "late_ok"],
    "Availability": ["who", "type", "days", "dates", "times", "note"],
    "Wishlist": ["team", "choice", "days", "times", "note"],
    "Locals": ["program", "days", "time", "courts", "coaches", "on_site", "dates"],
    "Weekends": ["date", "open", "courts", "group", "required", "note"],
    "Closures": ["date", "reason"],
}
ATTRS = {"Teams": "teams", "Availability": "availability", "Wishlist": "wishlist",
         "Locals": "locals", "Weekends": "weekends", "Closures": "closures"}


def empty(sheet: str) -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS[sheet])


@dataclass
class ScheduleInput:
    teams: pd.DataFrame
    availability: pd.DataFrame
    wishlist: pd.DataFrame
    locals: pd.DataFrame
    weekends: pd.DataFrame
    closures: pd.DataFrame
    start: date | None = None
    end: date | None = None

    def sheets(self) -> dict[str, pd.DataFrame]:
        return {sheet: getattr(self, attr) for sheet, attr in ATTRS.items()}

    def to_excel_bytes(self) -> bytes:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            for sheet, df in self.sheets().items():
                out = df.copy()
                for c in out.columns:  # keep dates readable as text
                    out[c] = out[c].map(lambda v: v.strftime("%m/%d/%Y") if isinstance(v, (date, datetime)) else v)
                out.to_excel(xw, sheet_name=sheet, index=False)
                ws = xw.sheets[sheet]
                for i, col in enumerate(out.columns, start=1):
                    width = max([len(str(col))] + [len(str(v)) for v in out[col].fillna("")]) + 2
                    ws.column_dimensions[ws.cell(1, i).column_letter].width = min(48, width)
        return buf.getvalue()

    @classmethod
    def from_excel(cls, file) -> "ScheduleInput":
        xl = pd.read_excel(file, sheet_name=None, dtype=str)
        frames = {}
        lower = {k.strip().lower(): v for k, v in xl.items()}
        for sheet, cols in COLUMNS.items():
            df = lower.get(sheet.lower())
            if df is None:
                frames[sheet] = empty(sheet)
                continue
            df = df.copy()
            df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
            for c in cols:
                if c not in df.columns:
                    df[c] = None
            frames[sheet] = df[cols].dropna(how="all").reset_index(drop=True)
        return cls(**{ATTRS[s]: f for s, f in frames.items()})


# ============================================================== parsing ==

def blank(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip().lower() in ("", "nan", "none", "x", "-")


def parse_list(value, sep=r"[;,]") -> list[str]:
    if blank(value):
        return []
    return [p.strip() for p in re.split(sep, str(value)) if p.strip()]


def parse_names(value) -> list[str]:
    """'Tyler, Alex' / 'Jordan & Casey' / 'Ana; Ben' -> names. '?' -> none."""
    return [n for n in parse_list(value, r"[;,&/]|\s+and\s+") if n not in ("?",)]


_DAY_WORDS = {"weekdays": WEEKDAYS, "weekday": WEEKDAYS, "weekends": WEEKEND, "weekend": WEEKEND,
              "any": [], "anyday": [], "anydays": [], "all": []}


def parse_days(value) -> list[str]:
    """'Tue, Thu' / 'Tue/Thu' / 'Tuesdays and Thursdays' / 'Weekdays' -> day keys.
    Blank / 'any' -> [] (meaning every day)."""
    if blank(value):
        return []
    out: list[str] = []
    for p in re.split(r"[;,/&.]|\s+and\s+|\s+or\s+|\s+", str(value)):
        p = p.strip().lower()
        if not p:
            continue
        if p in _DAY_WORDS:
            out.extend(_DAY_WORDS[p])
            continue
        key = p[:3].title()
        if key in DAYS:
            out.append(key)
    return list(dict.fromkeys(out))


_WEEKEND_HOUR = {9: "9AM", 11: "11AM", 13: "1PM", 15: "3PM", 17: "5PM", 19: "7PM"}


def parse_times(value) -> set[str] | None:
    """Any mix of weekday slots and weekend blocks -> set of slot keys.
    Blank / 'All' -> None (any time)."""
    if blank(value) or str(value).strip().lower() in ("all", "any", "all day", "anytime", "anytimes"):
        return None
    out: set[str] = set()
    for tok in parse_list(value, r"[;,/]|\s+or\s+|\s+and\s+"):
        t = tok.lower().replace(" ", "").replace(".", "")
        if t in ("am", "morning"):
            out |= AM_BLOCKS
            continue
        if t in ("pm", "afternoon", "evening"):
            out |= PM_BLOCKS
            continue
        m = (re.match(r"^(\d{1,2}):(\d{2})(am|pm)?", t)          # 5:30 / 5:30pm
             or re.match(r"^(\d{1,2})(\d{2})(am|pm)?$", t)          # 530pm / 1730
             or re.match(r"^(\d{1,2})()(am|pm)?", t))                # 5pm / 9 / 9am
        if not m:
            raise ValueError(f"Can't read time '{tok}'")
        h, mins, ampm = int(m.group(1)), m.group(2), m.group(3)
        if mins and mins != "00":  # weekday slot like 3:45 / 530pm / 17:30
            h12 = h - 12 if h > 12 else h
            key = f"{h12}:{mins}"
            if key not in WEEKDAY_KEYS:
                raise ValueError(f"'{tok}' isn't a club timeslot ({', '.join(WEEKDAY_KEYS)})")
            out.add(key)
            continue
        # whole hour -> weekend block start
        h24 = h
        if ampm == "pm" and h < 12:
            h24 = h + 12
        elif ampm is None and h < 9:
            h24 = h + 12
        if h24 not in _WEEKEND_HOUR:
            raise ValueError(f"'{tok}' isn't a weekend block start ({', '.join(WEEKEND_KEYS)})")
        out.add(_WEEKEND_HOUR[h24])
    return out or None


def parse_slot(value) -> str | None:
    """Single weekday/weekend slot (used for locals)."""
    ts = parse_times(value)
    if ts is None:
        return None
    return sorted(ts, key=SLOT_KEYS.index)[0]


def parse_courts(value) -> list[int]:
    """'1-3' -> [1,2,3]; '1, 4,5' -> [1,4,5]; 'All' -> every court; blank/'TBD' -> []."""
    if blank(value) or str(value).strip().lower() in ("tbd", "?"):
        return []
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


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _one_date(tok: str, year: int, month_hint: int | None = None) -> date:
    tok = tok.strip().lower().replace(",", "")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", tok)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", tok)
    if m:
        y = int(m.group(3)) if m.group(3) else year
        y = y + 2000 if y < 100 else y
        return date(y, int(m.group(1)), int(m.group(2)))
    m = re.match(r"^([a-z]{3})[a-z]*\.?\s*(\d{1,2})(?:st|nd|rd|th)?$", tok)
    if m and m.group(1) in _MONTHS:
        return date(year, _MONTHS[m.group(1)], int(m.group(2)))
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?$", tok)
    if m and month_hint:
        return date(year, month_hint, int(m.group(1)))
    raise ValueError(f"Can't read date '{tok}'")


def parse_date_ranges(value, year: int) -> list[tuple[date, date]]:
    """'11/2; 11/10-11/16; Nov 15-16; 2026-11-26' -> [(start, end), ...]."""
    if blank(value):
        return []
    if isinstance(value, (date, datetime, pd.Timestamp)):
        d = pd.Timestamp(value).date()
        return [(d, d)]
    out = []
    for tok in parse_list(value):
        tok = re.sub(r"\s+00:00:00$", "", tok)
        if re.match(r"^\d{4}-\d{2}-\d{2}$", tok):
            d = _one_date(tok, year)
            out.append((d, d))
            continue
        parts = re.split(r"\s*(?:-|–|to)\s*", tok, maxsplit=1)
        a = _one_date(parts[0], year)
        if len(parts) == 2:
            b = _one_date(parts[1], year, month_hint=a.month)
            if b < a:
                raise ValueError(f"Date range '{tok}' ends before it starts")
            out.append((a, b))
        else:
            out.append((a, a))
    return out


def to_date(value, year: int | None = None) -> date:
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return pd.Timestamp(value).date()
    s = re.sub(r"\s+00:00:00$", "", str(value).strip())
    try:
        return _one_date(s, year or date.today().year)
    except ValueError:
        return pd.to_datetime(s).date()


def yes(v) -> bool:
    return not blank(v) and str(v).strip().lower() in ("y", "yes", "true", "1", "required", "on")


# ========================================================== normalised ==

@dataclass
class WeekendToken:
    """One weekend option, e.g. '11/8 3PM' or 'Sat AM'. Unset parts match anything."""
    day: date | None = None
    weekday: str | None = None
    times: set[str] | None = None

    def matches(self, d: date, slot: str) -> bool:
        if self.day and self.day != d:
            return False
        if self.weekday and DAYS[d.weekday()] != self.weekday:
            return False
        if self.times and slot not in self.times:
            return False
        return True


def parse_weekend_tokens(value, year: int) -> list[WeekendToken]:
    out = []
    for tok in parse_list(value, r"[;,]|\s+or\s+|\s+&\s+"):
        wt = WeekendToken()
        rest = []
        for word in tok.split():
            w = word.strip().lower().rstrip(".")
            if w[:3].title() in WEEKEND and w.rstrip("s")[:3].isalpha():
                wt.weekday = w[:3].title()
            elif re.match(r"^\d{1,2}/\d{1,2}", w):
                wt.day = _one_date(w, year)
            else:
                rest.append(word)
        if rest:
            wt.times = parse_times(" ".join(rest))
        out.append(wt)
    return out


@dataclass
class Team:
    name: str
    club: str
    age: int
    heads: list[str]
    assistants: list[str]
    start: date | None
    priority: int
    weekend_only: list[WeekendToken] = field(default_factory=list)
    weekend_pref: list[WeekendToken] = field(default_factory=list)
    late_ok: bool = False

    def allowed(self, slot: str) -> bool:
        return slot_allowed_for_age(self.age, slot, self.late_ok)

    @property
    def staff(self) -> list[str]:
        return self.heads + [a for a in self.assistants if a not in self.heads]


@dataclass
class Rule:
    """An availability row: 'no' (hard for the team / head coach) or
    'not preferred' (soft)."""
    who: str
    kind: str  # "no" | "not_preferred"
    days: list[str]
    ranges: list[tuple[date, date]]
    times: set[str] | None
    note: str = ""

    def matches(self, d: date, slot: str) -> bool:
        if self.days and DAYS[d.weekday()] not in self.days:
            return False
        if self.ranges and not any(a <= d <= b for a, b in self.ranges):
            return False
        if self.times and slot not in self.times:
            return False
        return True

    def describe(self) -> str:
        bits = []
        if self.days:
            bits.append("/".join(self.days))
        if self.ranges:
            bits.append(", ".join(f"{a:%-m/%-d}" + ("" if a == b else f"–{b:%-m/%-d}") for a, b in self.ranges))
        if self.times:
            bits.append(", ".join(sorted(self.times, key=SLOT_KEYS.index)))
        return " ".join(bits) or "always"


@dataclass
class Wish:
    team: str
    choice: int
    days: list[str]
    times: set[str] | None

    def matches(self, d: date, slot: str) -> bool:
        if self.days and DAYS[d.weekday()] not in self.days:
            return False
        if self.times and slot not in self.times:
            return False
        return True


@dataclass
class LocalProgram:
    name: str
    day: str
    slot: str
    courts: list[int]
    coaches: list[str]
    on_site: bool
    ranges: list[tuple[date, date]]

    def active(self, d: date, slot: str) -> bool:
        if DAYS[d.weekday()] != self.day or slot != self.slot:
            return False
        return not self.ranges or any(a <= d <= b for a, b in self.ranges)


@dataclass
class WeekendDay:
    day: date
    blocks: list[str]
    courts: list[int]
    group: str | None
    required: bool
    note: str = ""


@dataclass
class CleanInput:
    teams: list[Team]
    rules: list[Rule]
    wishes: dict[str, list[Wish]]
    locals: list[LocalProgram]
    weekends: dict[date, WeekendDay]
    closures: dict[date, str]
    warnings: list[str]

    @property
    def coaches(self) -> list[str]:
        out = {c for t in self.teams for c in t.staff} | {c for lp in self.locals for c in lp.coaches}
        return sorted(out)


def _team_name_head(name: str) -> list[str]:
    """Girls team names carry the head coach: '18-Jordan' -> ['Jordan']."""
    m = re.match(r"^\s*\d{1,2}\s*[-–]\s*(.+)$", name)
    return parse_names(m.group(1)) if m else []


def _parse_open(value) -> list[str]:
    """'9AM-9PM' / '1PM-5PM' / blank (all day) -> weekend block keys."""
    if blank(value) or str(value).strip().lower() in ("all", "all day"):
        return list(WEEKEND_KEYS)
    parts = re.split(r"\s*(?:-|–|to)\s*", str(value).strip(), maxsplit=1)
    if len(parts) != 2:
        ts = parse_times(value)
        return [k for k in WEEKEND_KEYS if ts and k in ts]

    def hour(s):
        m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s.strip().lower())
        if not m:
            raise ValueError(f"Can't read hours '{value}' (use e.g. 9AM-9PM)")
        h = int(m.group(1))
        if m.group(3) == "pm" and h < 12:
            h += 12
        return h

    a, b = hour(parts[0]), hour(parts[1])
    hours = {9: "9AM", 11: "11AM", 13: "1PM", 15: "3PM", 17: "5PM", 19: "7PM"}
    return [k for h, k in hours.items() if a <= h and h + 2 <= b]


def clean(inp: ScheduleInput, start: date | None = None, end: date | None = None) -> CleanInput:
    """Validate and normalise. Raises ValueError listing every problem the
    solver can't work around; softer issues come back as warnings."""
    start = start or inp.start or date.today()
    year = start.year
    errors: list[str] = []
    warnings: list[str] = []

    # ---- teams ------------------------------------------------------------
    teams: list[Team] = []
    seen = set()
    for i, r in inp.teams.iterrows():
        name = "" if blank(r["team"]) else str(r["team"]).strip()
        if not name:
            continue
        row = f"Teams row {i + 2} ({name})"
        if name in seen:
            errors.append(f"Teams: '{name}' is listed twice")
            continue
        seen.add(name)
        try:
            age = int(float(r["age"])) if not blank(r["age"]) else int(re.match(r"\s*(\d{1,2})", name).group(1))
        except (TypeError, ValueError, AttributeError):
            errors.append(f"{row}: needs an age")
            continue
        heads = parse_names(r.get("head_coach")) or _team_name_head(name)
        assistants = [a for a in parse_names(r.get("assistants")) if a not in heads]
        if not heads:
            errors.append(f"{row}: no head coach")
            continue
        try:
            team_start = None if blank(r.get("start_date")) else to_date(r["start_date"], year)
            pr = r.get("priority")
            priority = default_priority(age) if blank(pr) else max(1, min(3, int(float(pr))))
            w_only = parse_weekend_tokens(r.get("weekend_only"), year)
            w_pref = parse_weekend_tokens(r.get("weekend_pref"), year)
        except (ValueError, TypeError) as e:
            errors.append(f"{row}: {e}")
            continue
        club = "" if blank(r.get("club")) else str(r["club"]).strip()
        late_ok = yes(r.get("late_ok"))
        if late_ok and age > 14:
            warnings.append(f"{row}: late_ok only matters for 14s and under, so it's ignored")
        teams.append(Team(name, club, age, heads, assistants, team_start, priority, w_only, w_pref, late_ok))

    team_names = {t.name for t in teams}
    people = {c for t in teams for c in t.staff}

    # ---- locals -----------------------------------------------------------
    locals_: list[LocalProgram] = []
    for i, r in inp.locals.iterrows():
        if blank(r["program"]):
            continue
        name = str(r["program"]).strip()
        try:
            days = parse_days(r["days"])
            if not days:
                raise ValueError(f"days '{r['days']}' should be like 'Tue, Thu'")
            slot = parse_slot(r["time"])
            if slot is None:
                raise ValueError("needs a time")
            courts = parse_courts(r.get("courts"))
            on_site = blank(r.get("on_site")) or yes(r.get("on_site"))
            staff = parse_names(r.get("coaches"))
            ranges = parse_date_ranges(r.get("dates"), year)
            for day in days:
                if (day in WEEKEND) != (slot not in WEEKDAY_KEYS):
                    raise ValueError(f"{day} needs a {'weekend time like 5PM' if day in WEEKEND else 'weekday time like 5:30'}"
                                     " (put weekday and weekend times on separate rows)")
                locals_.append(LocalProgram(name, day, slot, courts, staff, on_site, ranges))
            if on_site and not courts:
                warnings.append(f"Locals '{name}': courts not set yet, so it isn't blocking any courts "
                                f"(its coaches are still blocked)")
        except (ValueError, TypeError) as e:
            errors.append(f"Locals row {i + 2} ({name}): {e}")
    people |= {c for lp in locals_ for c in lp.coaches}

    # ---- availability -----------------------------------------------------
    rules: list[Rule] = []
    for i, r in inp.availability.iterrows():
        if blank(r["who"]):
            continue
        who = str(r["who"]).strip()
        kind_raw = "no" if blank(r.get("type")) else str(r["type"]).strip().lower()
        kind = "not_preferred" if kind_raw.startswith("not") or "pref" in kind_raw else "no"
        try:
            rule = Rule(who, kind, parse_days(r.get("days")), parse_date_ranges(r.get("dates"), year),
                        parse_times(r.get("times")), "" if blank(r.get("note")) else str(r["note"]))
        except (ValueError, TypeError) as e:
            errors.append(f"Availability row {i + 2} ({who}): {e}")
            continue
        if who not in team_names and who not in people:
            warnings.append(f"Availability row {i + 2}: '{who}' isn't a team or coach name, so it's ignored")
            continue
        rules.append(rule)

    # ---- wishlist ---------------------------------------------------------
    wishes: dict[str, list[Wish]] = {}
    for i, r in inp.wishlist.iterrows():
        if blank(r["team"]):
            continue
        tname = str(r["team"]).strip()
        if tname not in team_names:
            warnings.append(f"Wishlist row {i + 2}: team '{tname}' isn't on the Teams sheet")
            continue
        try:
            choice = 1 if blank(r.get("choice")) else int(float(r["choice"]))
            w = Wish(tname, choice, parse_days(r.get("days")), parse_times(r.get("times")))
        except (ValueError, TypeError) as e:
            errors.append(f"Wishlist row {i + 2} ({tname}): {e}")
            continue
        team = next(t for t in teams if t.name == tname)
        if w.times:
            bad = sorted((s for s in w.times if not team.allowed(s)), key=SLOT_KEYS.index)
            if bad:
                warnings.append(f"Wishlist for {tname} includes {', '.join(bad)}, which {team.age}s can't use")
                w.times -= set(bad)
                if not w.times:
                    continue
        wishes.setdefault(tname, []).append(w)

    # ---- weekends ---------------------------------------------------------
    weekends: dict[date, WeekendDay] = {}
    for i, r in inp.weekends.iterrows():
        if blank(r["date"]):
            continue
        try:
            d = to_date(r["date"], year)
            if d.weekday() < 5:
                raise ValueError(f"{d:%a %-m/%-d} isn't a weekend day")
            courts = parse_courts(r.get("courts")) if not blank(r.get("courts")) else list(COURTS)
            weekends[d] = WeekendDay(d, _parse_open(r.get("open")), courts,
                                     None if blank(r.get("group")) else str(r["group"]).strip(),
                                     yes(r.get("required")), "" if blank(r.get("note")) else str(r["note"]))
        except (ValueError, TypeError) as e:
            errors.append(f"Weekends row {i + 2}: {e}")

    # ---- closures ---------------------------------------------------------
    closures: dict[date, str] = {}
    for i, r in inp.closures.iterrows():
        if blank(r["date"]):
            continue
        try:
            closures[to_date(r["date"], year)] = "" if blank(r.get("reason")) else str(r["reason"])
        except (ValueError, TypeError) as e:
            errors.append(f"Closures row {i + 2}: {e}")

    # Two on-site locals on one court at the same time is a data error.
    used: dict[tuple, str] = {}
    for lp in locals_:
        if not lp.on_site:
            continue
        for c in lp.courts:
            key = (lp.day, lp.slot, c, tuple(lp.ranges))
            if key in used and used[key] != lp.name:
                errors.append(f"Locals '{used[key]}' and '{lp.name}' both use court {c} {lp.day} {lp.slot}")
            used[key] = lp.name

    if errors:
        raise ValueError("Please fix these in the input:\n- " + "\n- ".join(errors))
    if not teams:
        raise ValueError("There are no teams to schedule.")
    return CleanInput(teams, rules, wishes, locals_, weekends, closures, warnings)
