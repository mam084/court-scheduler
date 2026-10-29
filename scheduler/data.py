"""Input data: the sheets the club fills in.

Canonical form is a set of pandas DataFrames, one per spreadsheet sheet
(and one editable table per sheet in the app).

Sheets / columns
----------------
Teams         team, club, age, head_coach, assistants, start_date, priority,
              weekend_only, weekend_pref, late_ok (Yes = 14s-and-under team that
              may practice at 7:20, though it's avoided), rank (pecking order
              within the age group: "16-1" or just 1 = the top 16s team, "16-3"
              the third; boys and girls teams can share a rank; blank = from
              the name: "Black" 1, "Green" 2. Older column name: level),
              practices (per week; blank = 2)
Availability  who, type, days, dates, times, note
Wishlist      team, choice, days, times, note
Locals        program, side (Girls/Boys), days, time, courts, coaches, on_site, dates,
              skip (single dates it doesn't run, e.g. "11/12; 11/26"). A one-off
              extra session is its own row with that one date.
Weekends      date, open, courts, group, required, note
Closures      date, reason
Coaches       coach, email, phone, note (roster: anyone not on a team yet,
              e.g. coaches who only work programs)
Overrides     team, action, date, days, time, court, note: manual edits the
              scheduler always keeps:
                Add     pin a practice on date + time (+ court)
                Extra   same, as an extra practice on top of the team's weekly
                        number (a 3rd practice that week)
                Remove  team can't practice on date (+ time); a makeup is
                        found that week if possible
                Cancel  same, but no makeup (the week's target drops by 1)
                Usual   fix the team's usual weekly days + time (+ court)
Privates      coach, date, time (start; 1-hour sessions), court, type (Team /
              Personal), team (team privates), player, note
Tournaments   name, type (Local / Travel), start_date, end_date, club, ages,
              teams, program, courts, coaches, note
                Local  = hosted here: blocks its courts (default all) until
                         7 PM, plus its coaches (listed, and its locals
                         program's)
                Travel = away: the matching teams (club + ages, or listed
                         teams) can't practice those days and avoid the
                         days right before and after

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
from datetime import date, datetime, timedelta

import pandas as pd

from .config import (
    ALL_COURTS,
    AM_BLOCKS,
    COURTS,
    DAYS,
    MAX_PRACTICES_PER_WEEK,
    PM_BLOCKS,
    PRACTICE_COURT,
    PRACTICES_PER_WEEK,
    PROGRAM_KEYS,
    SLOT_KEYS,
    WEEKDAY_KEYS,
    WEEKDAYS,
    WEEKEND,
    WEEKEND_KEYS,
    blocked_by_local_tournament,
    default_priority,
    slot_allowed_for_age,
    slots_overlap,
)

COLUMNS = {
    "Teams": ["team", "club", "age", "head_coach", "assistants", "start_date", "priority",
              "weekend_only", "weekend_pref", "late_ok", "rank", "practices"],
    "Availability": ["who", "type", "days", "dates", "times", "note"],
    "Wishlist": ["team", "choice", "days", "times", "note"],
    "Locals": ["program", "side", "days", "time", "courts", "coaches", "on_site", "dates", "skip"],
    "Coaches": ["coach", "email", "phone", "note"],
    "Overrides": ["team", "action", "date", "days", "time", "court", "note"],
    "Weekends": ["date", "open", "courts", "group", "required", "note"],
    "Closures": ["date", "reason"],
    "Tournaments": ["name", "type", "start_date", "end_date", "club", "ages", "teams", "program", "courts", "coaches",
                    "note"],
    "Privates": ["coach", "date", "time", "court", "type", "team", "player", "note"],
    "Calendars": ["kind", "name", "calendar_id"],
    # Coverage flags marked as sorted out (a coach double-booked at
    # tournaments or programs): the flag's id, how it was covered, by whom.
    "Resolved": ["flag", "note", "who", "at"],
}
ATTRS = {"Teams": "teams", "Availability": "availability", "Wishlist": "wishlist",
         "Locals": "locals", "Weekends": "weekends", "Closures": "closures", "Tournaments": "tournaments",
         "Coaches": "coaches", "Overrides": "overrides", "Privates": "privates",
         "Calendars": "calendars", "Resolved": "resolved"}


def empty(sheet: str) -> pd.DataFrame:
    """An empty table with a sheet's columns."""
    return pd.DataFrame(columns=COLUMNS[sheet])


def _migrate(sheet: str, df: pd.DataFrame) -> pd.DataFrame:
    """Older files and saved schedules call the Teams rank column 'level'."""
    if sheet == "Teams" and "level" in df.columns:
        df = df.copy()
        if "rank" not in df.columns:
            df["rank"] = df["level"]
        else:
            df["rank"] = [lv if blank(rk) else rk for rk, lv in zip(df["rank"], df["level"])]
        df = df.drop(columns="level")
    return df


@dataclass
class ScheduleInput:
    """The club's input tables, one DataFrame per spreadsheet sheet (see COLUMNS), as typed or imported.

    clean() turns these into the validated objects the solver uses.
    """
    teams: pd.DataFrame
    availability: pd.DataFrame
    wishlist: pd.DataFrame
    locals: pd.DataFrame
    weekends: pd.DataFrame
    closures: pd.DataFrame
    start: date | None = None
    end: date | None = None
    tournaments: pd.DataFrame = field(default_factory=lambda: empty("Tournaments"))
    coaches: pd.DataFrame = field(default_factory=lambda: empty("Coaches"))
    overrides: pd.DataFrame = field(default_factory=lambda: empty("Overrides"))
    privates: pd.DataFrame = field(default_factory=lambda: empty("Privates"))
    calendars: pd.DataFrame = field(default_factory=lambda: empty("Calendars"))
    resolved: pd.DataFrame = field(default_factory=lambda: empty("Resolved"))
    # Weekends not on the Open days list are open (the club's rule). False: only listed ones are
    # (kept for tests written around a closed-unless-listed weekend).
    weekends_open: bool = True
    # The big family holidays are closed without being listed (False: only the Closures list; tests).
    holidays_closed: bool = True

    def sheets(self) -> dict[str, pd.DataFrame]:
        out = {}
        for sheet, attr in ATTRS.items():
            df = getattr(self, attr, None)
            out[sheet] = df if isinstance(df, pd.DataFrame) else empty(sheet)
        return out

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

    def to_records(self) -> dict[str, list[dict]]:
        """Plain JSON-able form (all values as text) for saving."""
        out = {}
        for sheet, df in self.sheets().items():
            d = df.copy()
            for c in COLUMNS[sheet]:
                if c not in d.columns:
                    d[c] = ""
            d = d[COLUMNS[sheet]].astype(object)
            d = d.map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v))
                      else v.strftime("%m/%d/%Y") if isinstance(v, (date, datetime)) else str(v))
            out[sheet] = d.to_dict(orient="records")
        return out

    @classmethod
    def from_records(cls, records: dict[str, list[dict]]) -> ScheduleInput:
        frames = {}
        for sheet, cols in COLUMNS.items():
            df = _migrate(sheet, pd.DataFrame(records.get(sheet, [])))
            for c in cols:
                if c not in df.columns:
                    df[c] = ""
            frames[ATTRS[sheet]] = df[cols].astype(object) if len(df) else empty(sheet)
        return cls(**frames)

    @classmethod
    def from_excel(cls, file) -> ScheduleInput:
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
            df = _migrate(sheet, df)
            for c in cols:
                if c not in df.columns:
                    df[c] = None
            frames[sheet] = df[cols].dropna(how="all").reset_index(drop=True)
        return cls(**{ATTRS[s]: f for s, f in frames.items()})


# Tables that belong to the club rather than to one spreadsheet: importing a
# spreadsheet keeps them, and a new schedule starts with them.
KEPT_TABLES = {"Locals": "locals", "Tournaments": "tournaments", "Privates": "privates", "Calendars": "calendars",
               "Resolved": "resolved"}
_KEPT_LABEL = {"Locals": "programs", "Tournaments": "tournaments", "Privates": "privates",
               "Calendars": "calendar links", "Resolved": "resolved flags"}


def keep_programs(new: ScheduleInput, old: ScheduleInput | None, use_file: bool = False) -> list[str]:
    """Carries the locals programs and tournaments from `old` into `new` (in
    place), so importing a spreadsheet doesn't wipe them.

    use_file: take the spreadsheet's tables instead, but only where it has
    rows; an empty or missing sheet never wipes what's there.
    Returns one line per table saying where it came from."""
    notes = []
    for sheet, attr in KEPT_TABLES.items():
        mine = getattr(old, attr, None) if old is not None else None
        mine = mine.dropna(how="all") if isinstance(mine, pd.DataFrame) else empty(sheet)
        theirs = getattr(new, attr, None)
        theirs = theirs if isinstance(theirs, pd.DataFrame) else empty(sheet)
        label = _KEPT_LABEL[sheet]
        if len(mine) and not (use_file and len(theirs)):
            setattr(new, attr, mine.copy().reset_index(drop=True))
            notes.append(f"kept {len(mine)} {label} row{'s' if len(mine) != 1 else ''}")
        elif len(theirs):
            notes.append(f"{label} from the file")
    return notes


def _near_year(start: date):
    """Month -> year for dates written without one, read as the date nearest
    `start` (so '11/10' is last month and '1/2' is next year in December)."""
    def y(month: int) -> int:
        if month - start.month > 6:
            return start.year - 1
        if start.month - month > 6:
            return start.year + 1
        return start.year
    return y


def _ended_before(dates_value, start: date) -> bool:
    """A dated row whose every date is before `start` (an old period's).
    Undated rows (every week) and unreadable dates count as current."""
    try:
        spans = parse_date_ranges(dates_value, _near_year(start))
    except (ValueError, TypeError):
        return False
    return bool(spans) and all(b < start for _, b in spans)


def _norm_name(v) -> str:
    return re.sub(r"[\s_-]+", " ", str(v or "")).strip().lower()


def _tokens(v) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9&]+", str(v or "").lower()) if t]


def _team_key(v) -> tuple[str, frozenset] | None:
    """'15-1 Drea' / '15 Drea' / '15-Drea' -> ('15', {'drea'}); the age and the words after it (a rank
    number like the 1 in '15-1' left out)."""
    toks = _tokens(v)
    if not toks or not toks[0].isdigit():
        return None
    return toks[0], frozenset(t for t in toks[1:] if not t.isdigit())


class NameMatcher:
    """Matches the names people write on a form to the club's: teams ('15-1 Drea' -> 15-Drea,
    '16-4 hannah' -> the 16s ranked 4th) and coaches ('Madison-Leigh Ines' -> Madi, by first name, a
    nickname it starts with, or their email). Only a single clear match counts."""

    def __init__(self, teams: pd.DataFrame, roster: pd.DataFrame | None = None):
        teams = teams.dropna(how="all") if isinstance(teams, pd.DataFrame) else pd.DataFrame()
        self.teams = [str(t).strip() for t in teams.get("team", []) if not blank(t)]
        self.ranks = {}
        for _, r in teams.iterrows():
            rk = r.get("rank")
            if not blank(rk) and not blank(r.get("team")):
                self.ranks[_norm_name(rk)] = str(r["team"]).strip()
        coaches = []
        for col in ("head_coach", "assistants"):
            for v in teams.get(col, []):
                coaches += parse_names(v)
        self.emails = {}
        if isinstance(roster, pd.DataFrame):
            for _, r in roster.dropna(how="all").iterrows():
                if blank(r.get("coach")):
                    continue
                coaches.append(str(r["coach"]).strip())
                if not blank(r.get("email")):
                    self.emails[str(r["email"]).strip().lower()] = str(r["coach"]).strip()
        self.coaches = list(dict.fromkeys(c for c in coaches if c))

    def team(self, name) -> str | None:
        n = _norm_name(name)
        hit = [t for t in self.teams if _norm_name(t) == n]
        if len(hit) == 1:
            return hit[0]
        squash = re.sub(r"[^a-z0-9&]", "", n)
        hit = [t for t in self.teams if re.sub(r"[^a-z0-9&]", "", _norm_name(t)) == squash]
        if len(hit) == 1:
            return hit[0]
        key = _team_key(name)
        if key is None:
            return None
        age, words = key
        hit = [t for t in self.teams if (k := _team_key(t)) and k[0] == age and k[1] and k[1] <= words]
        if len(hit) == 1:
            return hit[0]
        m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\b", str(name))  # '16-4 hannah': the 16s ranked 4th
        if m and (t := self.ranks.get(f"{m.group(1)} {m.group(2)}")):
            return t
        return None

    def coach(self, name, email: str = "") -> str | None:
        n = _norm_name(name)
        hit = [c for c in self.coaches if _norm_name(c) == n]
        if len(hit) == 1:
            return hit[0]
        em = (email or "").strip().lower()
        if em in self.emails:
            return self.emails[em]
        toks = set(_tokens(name))
        if not toks:
            return None
        hit = [c for c in self.coaches if set(_tokens(c)) and set(_tokens(c)) <= toks]
        if len(hit) == 1:
            return hit[0]
        first = _tokens(name)[0]
        local = em.split("@")[0] if "@" in em else ""
        last = _tokens(name)[-1]

        def nick(c):  # 'Madi' or 'Madi Ines' for Madison-Leigh Ines; 'Drea' by drea@...
            ct = _tokens(c)
            starts = len(ct[0]) >= 3 and first.startswith(ct[0])
            if len(ct) == 1:
                return starts or ct[0] == local
            return (starts or ct[0] == local) and ct[-1] == last
        hit = [c for c in self.coaches if nick(c)]
        return hit[0] if len(set(hit)) == 1 else None

    def who(self, name, email: str = "") -> str | None:
        """An Availability 'who': a team, else a coach."""
        return self.team(name) or self.coach(name, email)


def match_file_names(new: ScheduleInput, old: ScheduleInput) -> list[str]:
    """Rewrites the names in a coaches' file (in place) to the club's: Availability 'who', Wishlist
    'team' and the Coaches sheet. Returns notes: what was matched to what, and what wasn't."""
    m = NameMatcher(old.teams, getattr(old, "coaches", None))
    if not m.teams and not m.coaches:
        return []
    file_roster = getattr(new, "coaches", None)
    emails = {}
    if isinstance(file_roster, pd.DataFrame):
        for _, r in file_roster.dropna(how="all").iterrows():
            if not blank(r.get("coach")) and not blank(r.get("email")):
                emails[_norm_name(r["coach"])] = str(r["email"]).strip()
    matched, unmatched = {}, []

    def fix(df: pd.DataFrame, col: str, how) -> pd.DataFrame:
        if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
            return df
        df = df.copy().astype(object)
        for i, v in df[col].items():
            if blank(v):
                continue
            got = how(v)
            if got is None:
                if str(v).strip() not in unmatched:
                    unmatched.append(str(v).strip())
            else:
                if str(v).strip() != got:
                    matched[str(v).strip()] = got
                df.at[i, col] = got
        return df

    new.availability = fix(new.availability, "who", lambda v: m.who(v, emails.get(_norm_name(v), "")))
    new.wishlist = fix(new.wishlist, "team", m.team)
    if isinstance(file_roster, pd.DataFrame) and not file_roster.empty:
        new.coaches = fix(file_roster, "coach", lambda v: m.coach(v, emails.get(_norm_name(v), "")))
    notes = []
    if matched:
        notes.append("matched names to the club's: " + ", ".join(f"{a} → {b}" for a, b in matched.items()))
    if unmatched:
        notes.append("couldn't match " + ", ".join(unmatched) + " to a team or coach (new coaches are added; "
                     "check the spelling of any that should already be here)")
    return notes


def merge_import(new: ScheduleInput, old: ScheduleInput | None, start: date,
                 use_file_programs: bool = False) -> list[str]:
    """Brings a coaches' spreadsheet into the club's data (in place, into
    `new`) without losing anything the club already has:

    - Teams and the coach roster: the club's stay exactly as they are (names,
      coaches, ranks); teams or coaches that are new in the file are added.
    - No-dates and wishlists: everyone in the file gets the file's, replacing
      their standing ones and anything from before `start`. A dated row for
      `start` or later that the file doesn't have is kept (a coach may have
      added it in the app). Everyone else keeps theirs, minus rows from
      before `start`.
    - Weekends / open days, closures, hand edits: the club's plus the file's.
    - Programs, tournaments, privates, calendar links: the club's (see
      keep_programs; use_file_programs takes the file's where it has rows).
    Returns one line per change."""
    if old is None:
        return keep_programs(new, None, use_file=use_file_programs)
    notes = match_file_names(new, old)
    notes += keep_programs(new, old, use_file=use_file_programs)

    def rows(df):
        return df.dropna(how="all").reset_index(drop=True) if isinstance(df, pd.DataFrame) else pd.DataFrame()

    # Teams and coaches: keep, add new.
    for sheet, key, label in (("Teams", "team", "team"), ("Coaches", "coach", "coach")):
        attr = ATTRS[sheet]
        mine, theirs = rows(getattr(old, attr, None)), rows(getattr(new, attr, None))
        if mine.empty:
            continue
        have = {_norm_name(v) for v in mine.get(key, [])}
        if sheet == "Coaches" and len(theirs):  # contact info for coaches already here: fill in what's missing
            mine = mine.copy().astype(object)
            byname = {_norm_name(r[key]): r for _, r in theirs.iterrows() if not blank(r.get(key))}
            filled = 0
            for i, r in mine.iterrows():
                f = byname.get(_norm_name(r.get(key)))
                for c in ("email", "phone"):
                    if f is not None and c in mine.columns and blank(r.get(c)) and not blank(f.get(c)):
                        mine.at[i, c] = f[c]
                        filled += c == "email"
            if filled:
                notes.append(f"added {filled} email{'s' if filled != 1 else ''} to the coach list")
        add = theirs[~theirs[key].map(_norm_name).isin(have)] if len(theirs) else theirs
        add = add[~add[key].map(blank)] if len(add) else add
        merged = pd.concat([mine, add], ignore_index=True) if len(add) else mine
        setattr(new, attr, merged.astype(object).reset_index(drop=True))
        if sheet == "Coaches" and len(add):
            on_team = {_norm_name(c) for col in ("head_coach", "assistants")
                       for v in rows(old.teams).get(col, []) for c in parse_names(v)}
            known = [str(x) for x in add[key] if _norm_name(x) in on_team]
            fresh = [str(x) for x in add[key] if _norm_name(x) not in on_team]
            if known:
                notes.append(f"contact info for {len(known)} coach{'es' if len(known) != 1 else ''} already on teams")
            notes.append(f"added {len(fresh)} new coach{'es' if len(fresh) != 1 else ''} ({', '.join(fresh)})"
                         if fresh else "no new coaches")
        elif len(add):
            notes.append(f"added {len(add)} new {label}{'s' if len(add) != 1 else ''} "
                         f"({', '.join(map(str, add[key]))})")
        else:
            notes.append(f"no new {label}s")

    # No-dates and wishlists: the file's for everyone in it.
    for sheet, who_col, date_col, label in (("Availability", "who", "dates", "no-dates"),
                                            ("Wishlist", "team", None, "wishlists")):
        attr = ATTRS[sheet]
        mine, theirs = rows(getattr(old, attr, None)), rows(getattr(new, attr, None))
        in_file = {_norm_name(v) for v in theirs.get(who_col, [])} if len(theirs) else set()
        keep = []
        for _, r in mine.iterrows():
            who = _norm_name(r.get(who_col))
            dated = date_col and not blank(r.get(date_col))
            if dated and _ended_before(r.get(date_col), start):
                continue                       # an old period's: gone
            if who in in_file and not dated:
                continue                       # their standing ones: the file's replace them
            keep.append(r)
        kept = pd.DataFrame(keep, columns=mine.columns) if keep else mine.iloc[0:0]
        merged = pd.concat([theirs, kept], ignore_index=True) if len(kept) else theirs
        if len(merged):
            cols = COLUMNS[sheet]
            merged = merged.reindex(columns=cols).astype(object)
            merged = merged.drop_duplicates(subset=cols).reset_index(drop=True)
        setattr(new, attr, merged if len(merged) else empty(sheet))
        if len(theirs):
            notes.append(f"{label} from the file for {len(in_file)} "
                         f"{'coach or team' if len(in_file) == 1 else 'coaches and teams'}")

    # Open days, closures, hand edits: both.
    for sheet in ("Weekends", "Closures", "Overrides"):
        attr = ATTRS[sheet]
        mine, theirs = rows(getattr(old, attr, None)), rows(getattr(new, attr, None))
        merged = pd.concat([mine, theirs], ignore_index=True) if len(theirs) else mine
        if len(merged):
            cols = COLUMNS[sheet]
            merged = merged.reindex(columns=cols).astype(object)
            merged = merged.drop_duplicates(subset=cols).reset_index(drop=True)
        setattr(new, attr, merged if len(merged) else empty(sheet))
    return notes


# ============================================================== parsing ==

def blank(v) -> bool:
    """Is a spreadsheet cell empty? Blank text, NaN, 'none', 'x' and '-' all count."""
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip().lower() in ("", "nan", "none", "x", "-")


def parse_list(value, sep=r"[;,]") -> list[str]:
    """'A; B, C' -> ['A', 'B', 'C'] (blank -> [])."""
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
    text = re.sub(r"\s+to\s+", "-", re.sub(r"\s*[-–]\s*", "-", str(value)), flags=re.I)  # 'Mon - Fri' -> 'Mon-Fri'
    for p in re.split(r"[;,/&.]|\s+and\s+|\s+or\s+|\s+", text):
        p = p.strip().lower()
        if not p:
            continue
        rng = re.match(r"^([a-z]{3})[a-z]*\s*(?:-|–|to)\s*([a-z]{3})[a-z]*$", p)
        if rng and rng.group(1).title() in DAYS and rng.group(2).title() in DAYS:  # 'Mon-Fri', 'tue-thu'
            i, j = DAYS.index(rng.group(1).title()), DAYS.index(rng.group(2).title())
            out.extend(DAYS[i:j + 1] if i <= j else DAYS[i:] + DAYS[:j + 1])
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
        span = _parse_span(tok)
        if span:
            out |= span
            continue
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
            if key not in WEEKDAY_KEYS and key not in PROGRAM_KEYS:
                raise ValueError(f"'{tok}' isn't a club timeslot ({', '.join(WEEKDAY_KEYS + PROGRAM_KEYS)})")
            out.add(key)
            continue
        # whole hour -> weekend block start
        h24 = h
        if ampm == "pm" and h < 12 or ampm is None and h < 9:
            h24 = h + 12
        if h24 not in _WEEKEND_HOUR:
            raise ValueError(f"'{tok}' isn't a weekend block start ({', '.join(WEEKEND_KEYS)})")
        out.add(_WEEKEND_HOUR[h24])
    return out or None


def _parse_span(tok: str) -> set[str] | None:
    """'3:45-7:20' -> {3:45, 5:30}; '9AM-1PM' -> {9AM, 11AM}; '3:45-9:10' -> all three.
    A span covers the slots starting from its start up to (not including) its end."""
    parts = re.split(r"\s*[-–]\s*", tok.strip())
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    a_tok, b_tok = parts
    if re.search(r"(am|pm)", b_tok.lower()) and not re.search(r"(am|pm)", a_tok.lower()) and ":" not in a_tok:
        a_tok = a_tok + ("am" if b_tok.lower().endswith("am") and int(re.match(r"\d+", a_tok).group()) >= 9 else "pm")
    try:
        a = parse_times(a_tok)
    except ValueError:
        return None
    if not a or len(a) != 1:
        return None
    a = next(iter(a))
    keys = WEEKDAY_KEYS if a in WEEKDAY_KEYS else PROGRAM_KEYS if a in PROGRAM_KEYS else WEEKEND_KEYS
    try:
        b = parse_times(b_tok)
        b = next(iter(b)) if b and len(b) == 1 else None
    except ValueError:
        b = None
    i = keys.index(a)
    j = keys.index(b) if b in keys else len(keys)
    return set(keys[i:max(j, i + 1)])


def parse_slot(value) -> str | None:
    """Single weekday/weekend slot (used for locals)."""
    ts = parse_times(value)
    if ts is None:
        return None
    return sorted(ts, key=SLOT_KEYS.index)[0]


def parse_courts(value) -> list[int]:
    """'1-3' -> [1,2,3]; '1, 4,5' -> [1,4,5]; 'All' -> courts 1-7; 'P' or
    'Practice' -> the practice court (programs only); blank/'TBD' -> []."""
    if blank(value) or str(value).strip().lower() in ("tbd", "?"):
        return []
    if str(value).strip().lower() == "all":
        return list(COURTS)
    out: list[int] = []
    for part in parse_list(value):
        if part.strip().lower().replace(" court", "").replace(" ct", "") in ("p", "pc", "practice", "prac"):
            out.append(PRACTICE_COURT)
        elif "-" in part:
            a, b = (int(x) for x in part.split("-"))
            out.extend(range(a, b + 1))
        else:
            out.append(int(float(part)))
    bad = [c for c in out if c not in ALL_COURTS]
    if bad:
        raise ValueError(f"Court(s) {bad} don't exist (courts are {COURTS[0]}-{COURTS[-1]}, "
                         "plus P for the practice court)")
    return sorted(set(out))


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _year(year, month: int) -> int:
    """`year` is a fixed year, or a function month -> year (for ranges that
    cross New Year, so '1/5' in a Dec 15 - Jan 15 schedule means next year)."""
    return year(month) if callable(year) else year


def _one_date(tok: str, year, month_hint: int | None = None) -> date:
    tok = tok.strip().lower().replace(",", "")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", tok)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", tok)
    if m:
        y = int(m.group(3)) if m.group(3) else _year(year, int(m.group(1)))
        y = y + 2000 if y < 100 else y
        return date(y, int(m.group(1)), int(m.group(2)))
    m = re.match(r"^([a-z]{3})[a-z]*\.?\s*(\d{1,2})(?:st|nd|rd|th)?$", tok)
    if m and m.group(1) in _MONTHS:
        mo = _MONTHS[m.group(1)]
        return date(_year(year, mo), mo, int(m.group(2)))
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?$", tok)
    if m and month_hint:
        return date(_year(year, month_hint), month_hint, int(m.group(1)))
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
            if b < a and b.month < a.month:  # e.g. 12/28-1/3
                b = b.replace(year=a.year + 1)
            if b < a:
                raise ValueError(f"Date range '{tok}' ends before it starts")
            out.append((a, b))
        else:
            out.append((a, a))
    return out


def to_date(value, year: int | None = None) -> date:
    """A cell -> date. Dates without a year use `year` (a number, or month -> year)."""
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return pd.Timestamp(value).date()
    s = re.sub(r"\s+00:00:00$", "", str(value).strip())
    try:
        return _one_date(s, year if year is not None else date.today().year)
    except ValueError:
        return pd.to_datetime(s).date()


def yes(v) -> bool:
    """Is a cell a yes ('Yes', 'Y', 'true', '1', 'required', 'on')?"""
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
    """'11/1 AM; Sat PM; 11/8 3PM' -> weekend options (a date or weekday, and AM / PM / a block)."""
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
    """A team as the solver sees it: club, age, coaches, season start, weekend limits, rank and practices a week."""
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
    level: int | None = None  # rank within the age group: 1 = top team, None = unknown
    per_week: int = PRACTICES_PER_WEEK  # practices a week this team wants

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
    manual: bool = False  # a practice removed by hand: never overridden

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
    """One wishlist row: a team's 1st or 2nd choice of days and times."""
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
    """One program (locals, JumpStart, a camp) on one weekday in one timeslot, with its courts and coaches."""
    name: str
    day: str
    slot: str
    courts: list[int]
    coaches: list[str]
    on_site: bool
    ranges: list[tuple[date, date]]
    side: str = ""  # "Girls", "Boys" or ""
    skip: set = field(default_factory=set)  # single dates it doesn't run (cancelled sessions)

    def runs_on(self, d: date) -> bool:
        if DAYS[d.weekday()] != self.day or d in self.skip:
            return False
        return not self.ranges or any(a <= d <= b for a, b in self.ranges)

    def active(self, d: date, slot: str) -> bool:
        """Running in exactly this slot (for display)."""
        return slot == self.slot and self.runs_on(d)

    def blocks(self, d: date, slot: str) -> bool:
        """Takes this slot's courts/coaches, including overlaps (a 3:30 or
        4:30 JumpStart blocks the 3:45 practice slot)."""
        return slots_overlap(slot, self.slot) and self.runs_on(d)


def program_side(value, name: str = "") -> str:
    """'Girls'/'G'/'Boys'/'B' -> side; blank -> guessed from the name ('G HS L', 'Boys 14 League')."""
    v = "" if blank(value) else str(value).strip().lower()
    if v.startswith("g"):
        return "Girls"
    if v.startswith("b"):
        return "Boys"
    if v:
        return ""
    n = name.strip().lower()
    if re.match(r"^g\b", n) or "girl" in n:
        return "Girls"
    if re.match(r"^b\b", n) or "boy" in n:
        return "Boys"
    return ""


def club_side(club: str) -> str:
    """'SDVBC' / girls club -> 'Girls'; 'Seaside' / boys club -> 'Boys'; else ''."""
    c = (club or "").strip().lower()
    if "sdvbc" in c or "girl" in c:
        return "Girls"
    if "seaside" in c or "boy" in c:
        return "Boys"
    return ""


def ranges_overlap(r1: list[tuple[date, date]], r2: list[tuple[date, date]]) -> bool:
    """Empty means 'every week'."""
    if not r1 or not r2:
        return True
    return any(a1 <= b2 and a2 <= b1 for a1, b1 in r1 for a2, b2 in r2)


def local_conflicts(programs: list[LocalProgram]) -> list[str]:
    """On-site programs sharing a court at the same day/time with overlapping dates."""
    out, seen = [], set()
    on = [lp for lp in programs if lp.on_site and lp.courts]
    for i, a in enumerate(on):
        for b in on[i + 1:]:
            if a.name == b.name or a.day != b.day or not slots_overlap(a.slot, b.slot) \
                    or not ranges_overlap(a.ranges, b.ranges):
                continue
            shared = sorted(set(a.courts) & set(b.courts))
            key = (a.name, b.name, a.day, a.slot)
            if shared and key not in seen:
                seen.add(key)
                out.append(f"Locals '{a.name}' and '{b.name}' both use court(s) "
                           f"{', '.join(map(str, shared))} on {a.day} at {a.slot}")
    return out


@dataclass
class WeekendDay:
    """An open day in 2-hour blocks (a weekend, or a weekday over a school break): its blocks and courts."""
    day: date
    blocks: list[str]
    courts: list[int]
    group: str | None
    required: bool
    note: str = ""
    auto: bool = False  # a weekend that's open because weekends are, not because it's on the Open days list


@dataclass
class Tournament:
    """A local tournament (hosted here: takes courts and coaches until 7 PM) or a travel one (teams away)."""
    name: str
    kind: str                 # "local" | "travel"
    start: date
    end: date
    teams: list[str]          # travel: teams attending
    courts: list[int]         # local: courts used (all day)
    coaches: list[str]        # local: coaches of its locals program
    club: str = ""
    ages: list[int] = field(default_factory=list)
    program: str = ""         # local: the locals program running it
    side: str = ""            # local: "Girls" / "Boys" (its program's side), for its colors

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end

    def blocks(self, d: date, slot: str) -> bool:
        """A local tournament takes its courts and coaches until 7 PM."""
        return self.kind == "local" and self.covers(d) and blocked_by_local_tournament(slot)

    @property
    def multi_day(self) -> bool:
        return self.end > self.start

    def label(self) -> str:
        return self.name


def tournament_conflicts(teams: list[Team], tournaments: list[Tournament]) -> list[dict]:
    """Coaches who can't be in two places at once at tournaments:

    - kind "local_travel": working a local tournament while coaching a team
      at a travel tournament on the same day(s): coach, local, travel, teams
      (the coach's teams going), start, end.
    - kind "two_travel": coaching teams at two different travel tournaments
      on the same day(s), e.g. one weekend: coach, travel, teams, travel2,
      teams2, start, end.
    Each has an "id" that stays the same while the clash does (for marking
    it resolved)."""
    staff = {t.name: t.staff for t in teams}
    out = []
    travel = [tn for tn in tournaments if tn.kind == "travel"]
    for lt in (tn for tn in tournaments if tn.kind == "local" and tn.coaches):
        for tt in travel:
            a, b = max(lt.start, tt.start), min(lt.end, tt.end)
            if a > b:
                continue
            for c in lt.coaches:
                going = [n for n in tt.teams if c in staff.get(n, [])]
                if going:
                    out.append({"kind": "local_travel", "coach": c, "local": lt.name, "travel": tt.name,
                                "teams": going, "start": a, "end": b,
                                "id": f"lt|{c}|{lt.name}|{tt.name}|{a.isoformat()}"})
    for i, t1 in enumerate(travel):
        for t2 in travel[i + 1:]:
            if t1.name == t2.name:
                continue
            a, b = max(t1.start, t2.start), min(t1.end, t2.end)
            if a > b:
                continue
            coaches = {c for n in t1.teams for c in staff.get(n, [])} & {c for n in t2.teams for c in staff.get(n, [])}
            for c in sorted(coaches):
                x, y = sorted([t1, t2], key=lambda t: t.name)
                out.append({"kind": "two_travel", "coach": c, "travel": x.name, "travel2": y.name,
                            "teams": [n for n in x.teams if c in staff.get(n, [])],
                            "teams2": [n for n in y.teams if c in staff.get(n, [])], "start": a, "end": b,
                            "id": f"tt|{c}|{x.name}|{y.name}|{a.isoformat()}"})
    return sorted(out, key=lambda f: (f["start"], f["coach"]))


def program_coach_conflicts(locals_: list[LocalProgram], start: date, end: date,
                            closures: dict | None = None) -> list[dict]:
    """Coaches listed on two programs that run at the same time (overlapping
    timeslots on the same date). One dict per coach and pair of programs:
    coach, programs (a, b), when ('Tue 5:30 / 5:30'), dates."""
    closures = closures or {}
    hits: dict[tuple, dict] = {}
    d = start
    while d <= end:
        if d not in closures:
            running = [lp for lp in locals_ if lp.runs_on(d)]
            for i, a in enumerate(running):
                for b in running[i + 1:]:
                    if a.name == b.name or not slots_overlap(a.slot, b.slot):
                        continue
                    for c in sorted(set(a.coaches) & set(b.coaches)):
                        x, y = sorted([(a.name, a.slot), (b.name, b.slot)])
                        k = (c, x, y)
                        h = hits.setdefault(k, {"coach": c, "programs": (x[0], y[0]),
                                                "slots": (x[1], y[1]), "dates": []})
                        if d not in h["dates"]:
                            h["dates"].append(d)
        d += timedelta(days=1)
    return sorted(hits.values(), key=lambda h: (h["dates"][0], h["coach"]))


def span_text(a: date, b: date) -> str:
    """'Sat 11/14' or 'Sat 11/14–Sun 11/15'."""
    return f"{a:%a %-m/%-d}" + ("" if a == b else f"–{b:%a %-m/%-d}")


def parse_ages(value, all_ages: list[int] | None = None) -> list[int]:
    """'15-16' / '15/16' / '15, 16' / '14' / '14U' / '14 & under' -> ages. Blank -> []."""
    if blank(value):
        return []
    s = str(value).lower()
    nums = [int(n) for n in re.findall(r"\d{1,2}", s)]
    if "under" in s or re.search(r"\d+\s*u\b", s):
        lo = min(all_ages) if all_ages else 10
        return list(range(lo, max(nums) + 1)) if nums else []
    out = set()
    for part in re.split(r"[;,/&]|\s+and\s+", s):
        m = re.match(r"\s*(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})", part)
        if m:
            out |= set(range(int(m.group(1)), int(m.group(2)) + 1))
        else:
            out |= {int(n) for n in re.findall(r"\d{1,2}", part)}
    return sorted(out)


def parse_rank(value, age: int | None = None) -> tuple[int | None, str | None]:
    """Pecking order within an age group -> (rank, problem).

    '16-3' / '16 - 3' / '16-3s' -> 3 · '3' / 3.0 -> 3 · '1s' -> 1 · 'Black' -> 1,
    'Green' -> 2. Excel often turns '12-1' into the date Dec 1 (or Jan 12):
    a date whose month or day is the team's age is read back as age-rank.
    problem is a short note when the age in the rank doesn't match the team."""
    if blank(value):
        return None, None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        m_, d_ = value.month, value.day
        if age in (m_, d_) and m_ != d_:
            return (d_ if m_ == age else m_), None
        return d_, (f"looks like Excel turned the rank into a date ({value:%b %-d}); "
                    f"read it as {m_}-{d_}" + (f", but this team is {age}s" if age and m_ != age else ""))
    text = str(value).strip().lower()
    m = re.match(r"^\s*(\d{4})-(\d{2})-(\d{2})", text)  # a date that came through as text
    if m:
        return parse_rank(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))), age)
    if "black" in text:
        return 1, None
    if "green" in text:
        return 2, None
    m = re.match(r"^\s*(\d{1,2})\s*[-–/ ]\s*(\d{1,2})", text)
    if m:
        a, r = int(m.group(1)), int(m.group(2))
        note = f"says {a}s but this team is {age}s" if age and a != age else None
        return r, note
    m = re.search(r"\d+", text)
    if m:
        return int(float(m.group())), None
    return None, f"couldn't read '{value}' as a rank"


def _team_level(name: str, value, age: int | None = None) -> tuple[int | None, str | None]:
    rank, note = parse_rank(value, age)
    if rank is not None or note:
        return rank, note
    low = name.lower()
    if low.endswith("black"):
        return 1, None
    if low.endswith("green"):
        return 2, None
    return None, None


@dataclass
class Pin:
    """A practice placed by hand: always scheduled, on this court if given."""
    team: str
    day: date
    slot: str
    court: int | None = None
    note: str = ""
    extra: bool = False  # on top of the team's weekly practices


@dataclass
class CleanInput:
    """The validated, parsed inputs for one run of dates: what the solver works from."""
    teams: list[Team]
    rules: list[Rule]
    wishes: dict[str, list[Wish]]
    locals: list[LocalProgram]
    weekends: dict[date, WeekendDay]
    closures: dict[date, str]
    warnings: list[str]
    tournaments: list[Tournament] = field(default_factory=list)
    roster: dict[str, dict] = field(default_factory=dict)  # coach -> {email, phone, note}
    pins: list[Pin] = field(default_factory=list)          # manual practices
    cancels: dict = field(default_factory=dict)              # (team, date) -> practices cancelled (no makeup)
    usual: dict = field(default_factory=dict)                # team -> [(weekday, slot)]
    usual_court: dict = field(default_factory=dict)          # team -> court

    @property
    def coaches(self) -> list[str]:
        out = ({c for t in self.teams for c in t.staff} | {c for lp in self.locals for c in lp.coaches}
               | set(self.roster))
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
    end = end or inp.end or start
    if end.year == start.year:
        year = start.year
    else:  # schedule crosses New Year: months before the start month are next year
        year = lambda month: start.year if month >= start.month else end.year  # noqa: E731
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
        rank_val = r.get("rank") if not blank(r.get("rank")) else r.get("level")
        level, rank_note = _team_level(name, rank_val, age)
        if rank_note:
            warnings.append(f"{row}: rank {rank_note}")
        per_week = PRACTICES_PER_WEEK
        if not blank(r.get("practices")):
            try:
                per_week = int(float(r["practices"]))
            except (TypeError, ValueError):
                errors.append(f"{row}: practices should be a number of practices a week, like 2 or 3")
                continue
            if not 1 <= per_week <= MAX_PRACTICES_PER_WEEK:
                errors.append(f"{row}: practices should be between 1 and {MAX_PRACTICES_PER_WEEK} a week")
                continue
        teams.append(Team(name, club, age, heads, assistants, team_start, priority, w_only, w_pref, late_ok,
                          level, per_week))

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
            times = parse_times(r["time"])
            if not times:
                raise ValueError("needs a time (e.g. 5:30, 3:45-7:20, or 9AM-1PM on weekends)")
            courts = parse_courts(r.get("courts"))
            on_site = blank(r.get("on_site")) or yes(r.get("on_site"))
            staff = parse_names(r.get("coaches"))
            ranges = parse_date_ranges(r.get("dates"), year)
            skip = {a + timedelta(days=k) for a, b in parse_date_ranges(r.get("skip"), year)
                    for k in range((b - a).days + 1)}
            # Any time on any day: a morning camp on weekdays (9AM-11AM), a 5:30 program on a Saturday...
            day_slots = [s for s in SLOT_KEYS if s in times]
            for day in days:
                for slot in day_slots:
                    locals_.append(LocalProgram(name, day, slot, courts, staff, on_site, ranges,
                                                program_side(r.get("side"), name), skip))
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
        # A team no-date whose note names one of its assistants ("Cierra can't
        # make Mondays") is probably that assistant's: as a team no-date it's
        # the hardest kind to break.
        if kind == "no" and who in team_names and rule.note:
            tm = next(t for t in teams if t.name == who)
            # The longest coach name the note starts with, so "Tyler Lyons
            # can't..." is Tyler Lyons, not Tyler.
            note_l = rule.note.strip().lower()
            named = max((c for c in people if re.match(re.escape(c.lower()) + r"(?![\w])", note_l)),
                        key=len, default=None)
            asst = named if named in tm.assistants and named not in tm.heads else None
            if asst:
                warnings.append(f"Availability row {i + 2}: '{who}' no-date says \"{rule.note}\". If that's "
                                f"assistant {asst}'s, put {asst} in the who column instead, so it's treated as "
                                f"an assistant's no-date (broken before a Sat->Sun or a head coach's)")
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
            # A weekend day, or a weekday (a school break's days run in
            # 2-hour blocks like a weekend). A range ("12/21-1/2") opens
            # every day in it.
            courts = parse_courts(r.get("courts")) if not blank(r.get("courts")) else list(COURTS)
            blocks = _parse_open(r.get("open"))
            for a, b in parse_date_ranges(r["date"], year):
                if (b - a).days > 62:
                    raise ValueError(f"{a:%-m/%-d}–{b:%-m/%-d} is more than two months")
                d = a
                while d <= b:
                    weekends[d] = WeekendDay(d, list(blocks), courts,
                                             None if blank(r.get("group")) else str(r["group"]).strip(),
                                             yes(r.get("required")), "" if blank(r.get("note")) else str(r["note"]))
                    d += timedelta(days=1)
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
    # The big family holidays are always closed (weekends too); the list adds any other closed days.
    from .closures import major_holidays
    for y in (range(start.year - 1, end.year + 2) if getattr(inp, "holidays_closed", True) else ()):
        for d, name in major_holidays(y).items():
            closures.setdefault(d, name)

    # Weekends are open (9 AM-9 PM, every court) unless they're closed; the Open days list only
    # changes a weekend's hours or courts, or opens weekdays in blocks (a school break).
    d = start - timedelta(days=400)
    while getattr(inp, "weekends_open", True) and d <= end + timedelta(days=400):
        if d.weekday() >= 5 and d not in weekends:
            weekends[d] = WeekendDay(d, list(WEEKEND_KEYS), list(COURTS), None, False, "", auto=True)
        d += timedelta(days=1)

    # Two on-site locals on one court at the same time is a data error.
    for msg in local_conflicts(locals_):
        errors.append(msg)

    # ---- tournaments --------------------------------------------------------
    tournaments: list[Tournament] = []
    tdf = getattr(inp, "tournaments", None)
    all_ages = sorted({t.age for t in teams})
    for i, r in (tdf.iterrows() if tdf is not None else []):
        if blank(r.get("name")):
            continue
        name = str(r["name"]).strip()
        row = f"Tournaments row {i + 2} ({name})"
        try:
            kind_raw = "" if blank(r.get("type")) else str(r["type"]).strip().lower()
            if kind_raw.startswith("loc") or kind_raw.startswith("home") or kind_raw.startswith("host"):
                kind = "local"
            elif kind_raw.startswith("trav") or kind_raw.startswith("away"):
                kind = "travel"
            else:
                raise ValueError("type should be Local or Travel")
            t_start = to_date(r["start_date"], year)
            t_end = t_start if blank(r.get("end_date")) else to_date(r["end_date"], year)
            if t_end < t_start:
                raise ValueError("end_date is before start_date")
            club = "" if blank(r.get("club")) else str(r["club"]).strip()
            ages = parse_ages(r.get("ages"), all_ages)
            listed = parse_list(r.get("teams"))
            unknown = [n for n in listed if n not in team_names]
            if unknown:
                warnings.append(f"{row}: team(s) {', '.join(unknown)} aren't on the Teams sheet")
            if listed:
                t_teams = [n for n in listed if n in team_names]
            elif kind == "travel":
                t_teams = [t.name for t in teams
                           if (not club or t.club.lower() == club.lower()) and (not ages or t.age in ages)]
            else:
                t_teams = []
            if kind == "travel" and not t_teams:
                warnings.append(f"{row}: no teams match club '{club or 'any'}' and ages "
                                f"'{r.get('ages') or 'any'}', so it doesn't affect anyone")
            courts = parse_courts(r.get("courts")) if not blank(r.get("courts")) else list(COURTS)
            prog = "" if blank(r.get("program")) else str(r["program"]).strip()
            coaches = sorted({c for lp in locals_ if prog and lp.name.lower() == prog.lower() for c in lp.coaches})
            if kind == "local":  # coaches working it besides its program's
                coaches = sorted(set(coaches) | set(parse_names(r.get("coaches"))))
            if prog and not any(lp.name.lower() == prog.lower() for lp in locals_):
                warnings.append(f"{row}: program '{prog}' isn't on the Locals sheet")
            side = next((lp.side for lp in locals_ if prog and lp.name.lower() == prog.lower() and lp.side), "")
            side = side or club_side(club) or program_side("", prog or name)
            tournaments.append(Tournament(name, kind, t_start, t_end, t_teams,
                                          courts if kind == "local" else [], coaches, club, ages,
                                          next((lp.name for lp in locals_ if lp.name.lower() == prog.lower()), prog),
                                          side if kind == "local" else ""))
        except (ValueError, TypeError, KeyError) as e:
            errors.append(f"{row}: {e}")

    if errors:
        raise ValueError("Please fix these in the input:\n- " + "\n- ".join(errors))
    # ---- coach roster (coaches who aren't on a team yet, plus contact info)
    roster: dict[str, dict] = {}
    cdf = getattr(inp, "coaches", None)
    for _, r in (cdf.iterrows() if isinstance(cdf, pd.DataFrame) else []):
        if blank(r.get("coach")):
            continue
        roster[str(r["coach"]).strip()] = {k: ("" if blank(r.get(k)) else str(r[k]).strip())
                                           for k in ("email", "phone", "note")}
    # ---- manual overrides ------------------------------------------------------
    pins: list[Pin] = []
    cancels: dict = {}
    usual: dict = {}
    usual_court: dict = {}
    odf = getattr(inp, "overrides", None)
    for i, r in (odf.iterrows() if isinstance(odf, pd.DataFrame) else []):
        if blank(r.get("team")):
            continue
        tname = str(r["team"]).strip()
        row = f"Manual change row {i + 2} ({tname})"
        if tname not in team_names:
            warnings.append(f"{row}: team isn't on the Teams sheet, so it's ignored")
            continue
        team = next(t for t in teams if t.name == tname)
        action = "" if blank(r.get("action")) else str(r["action"]).strip().lower()
        note = "" if blank(r.get("note")) else str(r["note"])
        try:
            times = parse_times(r.get("time"))
            court = None if blank(r.get("court")) or str(r.get("court")).strip().lower() == "any" \
                else (parse_courts(r["court"]) or [None])[0]
            if court == PRACTICE_COURT:
                raise ValueError("team practices can't use the practice court")
            if court is not None and court not in COURTS:
                raise ValueError(f"court {court} doesn't exist")
            if action.startswith(("add", "pin", "move", "extra")):
                d = to_date(r["date"], year)
                if not times or len(times) != 1:
                    raise ValueError("needs exactly one time")
                slot = next(iter(times))
                block = d.weekday() >= 5 or d in weekends  # weekends and break days run in 2-hour blocks
                if block != (slot in WEEKEND_KEYS):
                    raise ValueError(f"{d:%a %-m/%-d} needs a {'2-hour block' if block else 'weekday'} time"
                                     + (" (it's an open day)" if block and d.weekday() < 5 else ""))
                if slot in PROGRAM_KEYS:
                    raise ValueError("JumpStart times are for programs, not team practices")
                if not team.allowed(slot):
                    warnings.append(f"{row}: {team.age}s don't normally practice at {slot}; "
                                    "kept because it was set by hand")
                pins.append(Pin(tname, d, slot, court, note, extra=action.startswith("extra")))
            elif action.startswith("rem") or action.startswith("can") or action.startswith("del"):
                d = to_date(r["date"], year)
                rules.append(Rule(tname, "no", [], [(d, d)], times, note or "removed by hand", manual=True))
                if action.startswith("can"):
                    cancels[(tname, d)] = cancels.get((tname, d), 0) + 1
            elif action.startswith("usual") or action.startswith("fix"):
                days = [x for x in parse_days(r.get("days")) if x in WEEKDAYS]
                if not days or not times:
                    raise ValueError("needs weekdays and a time, e.g. Tue, Thu at 5:30")
                slot = sorted(times, key=SLOT_KEYS.index)[0]
                if slot not in WEEKDAY_KEYS:
                    raise ValueError("usual days need a weekday practice time (3:45, 5:30 or 7:20)")
                usual[tname] = [(x, slot) for x in days][:team.per_week]
                if court is not None:
                    usual_court[tname] = court
            else:
                raise ValueError("action should be Add, Extra, Remove, Cancel or Usual")
        except (ValueError, TypeError, KeyError) as e:
            errors.append(f"{row}: {e}")
    if errors:
        raise ValueError("Please fix these in the input:\n- " + "\n- ".join(errors))

    return CleanInput(teams, rules, wishes, locals_, weekends, closures, warnings, tournaments, roster,
                      pins, cancels, usual, usual_court)


def rename_team(inp: ScheduleInput, old: str, new: str) -> None:
    """Renames a team everywhere in the club's tables (in place): Teams, its no-dates and wishlist,
    hand edits, the tournaments it goes to, privates and its Google Calendar link."""
    o = _norm_name(old)

    def same(v) -> bool:
        return not blank(v) and _norm_name(v) == o

    for attr, col in (("teams", "team"), ("availability", "who"), ("wishlist", "team"), ("overrides", "team"),
                      ("privates", "team")):
        df = getattr(inp, attr, None)
        if isinstance(df, pd.DataFrame) and col in df.columns and len(df):
            df = df.copy().astype(object)
            df[col] = [new if same(v) else v for v in df[col]]
            setattr(inp, attr, df)
    t = getattr(inp, "tournaments", None)
    if isinstance(t, pd.DataFrame) and "teams" in t.columns and len(t):
        t = t.copy().astype(object)
        t["teams"] = [v if blank(v) else ", ".join(new if same(x) else x for x in parse_list(v)) for v in t["teams"]]
        inp.tournaments = t
    c = getattr(inp, "calendars", None)
    if isinstance(c, pd.DataFrame) and {"kind", "name"} <= set(c.columns) and len(c):
        c = c.copy().astype(object)
        c["name"] = [new if str(k).strip().lower() == "team" and same(n) else n for k, n in zip(c["kind"], c["name"])]
        inp.calendars = c
