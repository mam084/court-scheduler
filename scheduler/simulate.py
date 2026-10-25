"""Simulated club data in the real template's shape, for testing and demos.

Mirrors the real club: a girls side (teams named "<age>-<head coach>") and a
boys side ("<age> Black" for 1s, "<age> Green" for 2s), coaches who run a
girls team and assist or run a boys team, twice-weekly locals, recurring
and date-specific no-dates, ranked wishlists, and a few open weekends.
"""

from __future__ import annotations

import calendar
import random
from datetime import date, timedelta

import pandas as pd

from .closures import default_closures
from .config import WEEKDAYS
from .data import COLUMNS, ScheduleInput

FIRST_NAMES = [
    "Alex", "Brooke", "Carlos", "Dana", "Eli", "Fiona", "Gabe", "Hana", "Isaac", "Jess",
    "Kai", "Lena", "Marco", "Nina", "Owen", "Priya", "Quinn", "Rosa", "Sam", "Tara",
    "Uma", "Vic", "Wes", "Yara", "Zoe", "Ivy", "Jude", "Noel", "Remy", "Sky",
]

LOCAL_TEMPLATES = [  # program, days, time, courts
    ("Girls HS League", "Tue, Thu", "5:30", "6-7"),
    ("Boys 14 League", "Mon, Fri", "5:30", "6-7"),
    ("Boys HS League", "Mon, Fri", "7:20", "6-7"),
    ("Girls 14 League", "Wed, Fri", "3:45", "1-3"),
    ("Girls 12 League", "Tue, Thu", "3:45", "1-2"),
    ("Girls 14 Training", "Mon, Thu", "3:45", "4"),
]

WISH_OPTIONS = [
    ("Tue, Thu", "5:30"), ("Tue, Thu", "7:20"), ("Mon, Wed", "5:30"), ("Mon, Wed", "7:20"),
    ("Mon, Wed, Fri", "5:30"), ("Wed, Fri", "5:30"), ("", "5:30"), ("", "7:20"),
]


def month_range(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def simulate(
    n_girls: int = 6,
    n_boys: int = 4,
    n_locals: int = 4,
    year: int = 2026,
    month: int = 11,
    seed: int = 7,
    start: date | None = None,
    end: date | None = None,
) -> ScheduleInput:
    """Simulated club for a month, or for any start/end date range."""
    rng = random.Random(seed)
    if start is None or end is None:
        start, end = month_range(year, month)
    names = rng.sample(FIRST_NAMES, k=len(FIRST_NAMES))
    pool = iter(names + [f"Coach{i}" for i in range(1, 200)])

    # ---- girls teams: "<age>-<head>" ---------------------------------
    team_rows = []
    girls_heads = []
    ages_g = sorted(12 + round(i * 6 / max(1, n_girls - 1)) for i in range(n_girls)) if n_girls else []
    for i, age in enumerate(ages_g):
        head = next(pool)
        girls_heads.append(head)
        rank = ages_g[:i + 1].count(age)  # pecking order within the age group: 16-1, 16-2, ...
        team_rows.append({"team": f"{age}-{head}", "club": "Girls", "age": age, "head_coach": head,
                          "assistants": next(pool) if rng.random() < 0.7 else "", "rank": f"{age}-{rank}"})

    # ---- boys teams: "<age> Black" / "<age> Green" --------------------
    ages_b = sorted(13 + round(i * 5 / max(1, n_boys - 1)) for i in range(n_boys)) if n_boys else []
    used = set()
    for age in ages_b:
        color = "Black" if f"{age} Black" not in used else "Green"
        used.add(f"{age} {color}")
        # Many boys coaches also run or assist a girls team (makes double-ups possible).
        head = rng.choice(girls_heads) if girls_heads and rng.random() < 0.5 else next(pool)
        team_rows.append({"team": f"{age} {color}", "club": "Boys", "age": age, "head_coach": head,
                          "assistants": next(pool), "rank": f"{age}-{1 if color == 'Black' else 2}"})
    teams = pd.DataFrame(team_rows)
    for c in COLUMNS["Teams"]:
        if c not in teams:
            teams[c] = ""
    staff = sorted({p for col in ("head_coach", "assistants") for v in teams[col] for p in str(v).split(";") if p.strip()})

    # Older girls start later, like the real season.
    teams.loc[(teams["club"] == "Girls") & (teams["age"] >= 16), "start_date"] = (start + timedelta(days=13)).strftime("%m/%d/%Y")
    teams.loc[teams["age"] <= 14, "weekend_pref"] = "AM"
    teams = teams[COLUMNS["Teams"]]

    # ---- availability: recurring and date-specific no-dates ----------
    weekdays = [d for d in pd.date_range(start, end).date if d.weekday() < 5]
    avail = []
    for _, t in teams.iterrows():
        if rng.random() < 0.3:
            avail.append({"who": t["team"], "type": "No", "days": rng.choice(WEEKDAYS), "dates": "",
                          "times": "", "note": "Coach works late"})
        if rng.random() < 0.5:
            d = rng.choice(weekdays)
            span = rng.choice([0, 0, 2, 4])
            e = min(end, d + timedelta(days=span))
            avail.append({"who": t["team"], "type": "No", "days": "",
                          "dates": f"{d:%-m/%-d}" + ("" if e == d else f"-{e:%-m/%-d}"),
                          "times": "", "note": rng.choice(["Tournament travel", "Family", "Work", ""])})
        if rng.random() < 0.25:
            avail.append({"who": t["team"], "type": "Not preferred", "days": rng.choice(WEEKDAYS),
                          "dates": "", "times": "", "note": ""})
    availability = pd.DataFrame(avail, columns=COLUMNS["Availability"])

    # ---- wishlists ----------------------------------------------------
    wish = []
    for _, t in teams.iterrows():
        if rng.random() < 0.8:
            opts = [o for o in WISH_OPTIONS if not (t["age"] <= 14 and o[1] == "7:20")]
            days, time_ = rng.choice(opts)
            wish.append({"team": t["team"], "choice": 1, "days": days, "times": time_, "note": ""})
    wishlist = pd.DataFrame(wish, columns=COLUMNS["Wishlist"])

    # ---- locals: twice a week, worked by some coaches ----------------
    loc = []
    for prog, days, time_, courts in LOCAL_TEMPLATES[:n_locals]:
        who = rng.sample(staff, k=min(len(staff), rng.choice([1, 2])))
        loc.append({"program": prog, "side": "Girls" if prog.startswith("Girls") else "Boys" if prog.startswith("Boys") else "",
                    "days": days, "time": time_, "courts": courts,
                    "coaches": "; ".join(who), "on_site": "Yes", "dates": ""})
    locals_df = pd.DataFrame(loc, columns=COLUMNS["Locals"])

    # ---- weekends: a few open dates, one required --------------------
    weekend_days = [d for d in pd.date_range(start, end).date if d.weekday() >= 5]
    picks = sorted(rng.sample(weekend_days, k=min(3, len(weekend_days))))
    wk = [{"date": d.strftime("%m/%d/%Y"), "open": "9AM-9PM", "courts": "All",
           "group": "A" if i == len(picks) - 1 else "", "required": "Yes" if i == len(picks) - 1 else "No",
           "note": ""} for i, d in enumerate(picks)]
    weekends = pd.DataFrame(wk, columns=COLUMNS["Weekends"])

    closures = pd.DataFrame([{"date": d.strftime("%m/%d/%Y"), "reason": r} for d, r in default_closures(start, end)],
                            columns=COLUMNS["Closures"])

    # ---- one travel tournament for the older boys, on the 2nd weekend --------
    sats = [d for d in pd.date_range(start, end).date if d.weekday() == 5]
    tourn = []
    if len(sats) >= 2 and n_boys:
        sat = sats[1]
        tourn.append({"name": "Boys Travel Tournament", "type": "Travel", "start_date": sat.strftime("%m/%d/%Y"),
                      "end_date": (sat + timedelta(days=1)).strftime("%m/%d/%Y"), "club": "Boys", "ages": "15-18",
                      "teams": "", "program": "", "courts": "", "note": ""})
    tournaments = pd.DataFrame(tourn, columns=COLUMNS["Tournaments"])

    return ScheduleInput(teams, availability, wishlist, locals_df, weekends, closures, start, end, tournaments)
