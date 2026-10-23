"""Simulated club data, for testing before the real spreadsheet exists.

Produces the same tables the real input has, so everything downstream
(the solver, the app, the exports) is exercised exactly as it will be
with real data.
"""

from __future__ import annotations

import calendar
import random
from datetime import date

import pandas as pd

from .closures import default_closures
from .config import SLOT_KEYS, WEEKDAYS, default_priority
from .data import ScheduleInput

FIRST_NAMES = [
    "Alex", "Brooke", "Carlos", "Dana", "Eli", "Fiona", "Gabe", "Hana",
    "Isaac", "Jess", "Kai", "Lena", "Marco", "Nina", "Owen", "Priya",
    "Quinn", "Rosa", "Sam", "Tara", "Uma", "Vic", "Wes", "Yara", "Zoe",
]
COLORS = ["Navy", "Black", "Teal", "White", "Gold", "Red"]

LOCAL_TEMPLATES = [  # most locals run twice a week
    ("Little Spikers (8-10U)", "Tue, Thu", "3:45", "1-4"),
    ("Adult League", "Mon, Wed", "7:20", "1-5"),
    ("Skills Clinic", "Tue, Thu", "5:30", "6-7"),
    ("Beginner Academy", "Mon, Wed", "3:45", "1-3"),
    ("Middle School Clinic", "Fri", "3:45", "1-3"),
    ("Open Gym", "Fri", "7:20", "5-7"),
]


def month_range(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def simulate(
    n_teams: int = 10,
    n_coaches: int = 10,
    n_locals: int = 4,
    year: int = 2026,
    month: int = 11,
    seed: int = 7,
    no_dates_per_coach: tuple[int, int] = (0, 3),
) -> ScheduleInput:
    rng = random.Random(seed)
    start, end = month_range(year, month)

    coaches = rng.sample(FIRST_NAMES, k=min(n_coaches, len(FIRST_NAMES)))
    if n_coaches > len(FIRST_NAMES):
        coaches += [f"Coach {i}" for i in range(len(FIRST_NAMES), n_coaches)]

    # ---- teams: ages 12-18, spread across the range ------------------
    ages = sorted(12 + round(i * 6 / max(1, n_teams - 1)) for i in range(n_teams))
    team_rows, per_age = [], {}
    # Roughly 70% of coaches run a team; some run two.
    head_coaches = coaches[: max(1, round(len(coaches) * 0.7))]
    for i, age in enumerate(ages):
        k = per_age.get(age, 0)
        per_age[age] = k + 1
        name = f"{age} {COLORS[k % len(COLORS)]}" + (f" {k // len(COLORS) + 1}" if k >= len(COLORS) else "")
        coach = head_coaches[i % len(head_coaches)]
        team_rows.append({"team": name, "age": age, "coach": coach,
                          "priority": default_priority(age)})
    teams = pd.DataFrame(team_rows)

    # ---- coaches + preferences --------------------------------------
    coach_rows = []
    for c in coaches:
        pref_days = ", ".join(sorted(rng.sample(WEEKDAYS, 2), key=WEEKDAYS.index)) if rng.random() < 0.5 else ""
        pref_slots = rng.choice(["5:30", "7:20", "5:30, 7:20"]) if rng.random() < 0.4 else ""
        coach_rows.append({
            "coach": c,
            "email": f"{c.lower().replace(' ', '.')}@example.com",
            "preferred_days": pref_days,
            "preferred_slots": pref_slots,
        })
    coaches_df = pd.DataFrame(coach_rows)

    # ---- locals: fixed weekly programs, worked by some coaches -------
    local_rows = []
    for prog, days, slot, courts in LOCAL_TEMPLATES[:n_locals]:
        staff = rng.sample(coaches, k=min(len(coaches), rng.choice([1, 2])))
        local_rows.append({"program": prog, "days": days, "slot": slot,
                           "courts": courts, "coaches": "; ".join(staff)})
    locals_df = pd.DataFrame(local_rows, columns=["program", "days", "slot", "courts", "coaches"])

    # ---- closures ----------------------------------------------------
    closures = pd.DataFrame(
        [{"date": d, "reason": r} for d, r in default_closures(start, end)],
        columns=["date", "reason"],
    )

    # ---- no-dates: specific dates each coach can't make ---------------
    weekdays_in_range = [d for d in pd.date_range(start, end).date if d.weekday() < 5]
    nd_rows = []
    for c in coaches:
        k = rng.randint(*no_dates_per_coach)
        for d in rng.sample(weekdays_in_range, k=min(k, len(weekdays_in_range))):
            slot = "All" if rng.random() < 0.75 else rng.choice(SLOT_KEYS)
            nd_rows.append({"coach": c, "date": d, "slot": slot,
                            "note": rng.choice(["Work", "Family", "Tournament travel", "Appointment", ""])})
    no_dates = pd.DataFrame(nd_rows, columns=["coach", "date", "slot", "note"])
    if not no_dates.empty:
        no_dates = no_dates.sort_values(["date", "coach"]).reset_index(drop=True)

    return ScheduleInput(teams, coaches_df, no_dates, locals_df, closures, start, end)
