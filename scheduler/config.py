"""Club-wide constants and scoring weights.

Everything the club might want to tune lives here so the solver code
stays readable.
"""

COURTS = [1, 2, 3, 4, 5, 6, 7]

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS = DAYS[:5]
WEEKEND = DAYS[5:]

# Weekday timeslots: short key -> display label (earliest first).
WEEKDAY_SLOTS = {
    "3:45": "3:45–5:30",
    "5:30": "5:30–7:20",
    "7:20": "7:20–9:10",
}
# Weekend 2-hour blocks (9 AM – 9 PM).
WEEKEND_SLOTS = {
    "9AM": "9–11 AM",
    "11AM": "11 AM–1 PM",
    "1PM": "1–3 PM",
    "3PM": "3–5 PM",
    "5PM": "5–7 PM",
    "7PM": "7–9 PM",
}
SLOTS = {**WEEKDAY_SLOTS, **WEEKEND_SLOTS}
SLOT_KEYS = list(SLOTS)  # display order within a day
WEEKDAY_KEYS = list(WEEKDAY_SLOTS)
WEEKEND_KEYS = list(WEEKEND_SLOTS)
AM_BLOCKS = {"9AM", "11AM"}
PM_BLOCKS = {"1PM", "3PM", "5PM", "7PM"}

# 24h (start, end) for calendar files.
SLOT_TIMES = {
    "3:45": ((15, 45), (17, 30)),
    "5:30": ((17, 30), (19, 20)),
    "7:20": ((19, 20), (21, 10)),
    "9AM": ((9, 0), (11, 0)),
    "11AM": ((11, 0), (13, 0)),
    "1PM": ((13, 0), (15, 0)),
    "3PM": ((15, 0), (17, 0)),
    "5PM": ((17, 0), (19, 0)),
    "7PM": ((19, 0), (21, 0)),
}

PRACTICES_PER_WEEK = 2

# ---------------------------------------------------------------------------
# Hard age rules
# ---------------------------------------------------------------------------
YOUNG_MAX_AGE = 14            # "14s & under"
YOUNG_ONLY_SLOTS = {"3:45"}   # only 14s & under can practice at 3:45
NOT_FOR_YOUNG = {"7:20", "7PM"}  # 14s & under can't practice this late


def slot_allowed_for_age(age: int, slot: str, late_ok: bool = False) -> bool:
    """late_ok: a 14s-and-under team that's an exception to the no-7:20 rule."""
    if slot in YOUNG_ONLY_SLOTS and age > YOUNG_MAX_AGE:
        return False
    if slot in NOT_FOR_YOUNG and age <= YOUNG_MAX_AGE and not late_ok:
        return False
    return True


def is_late_exception(age: int, slot: str) -> bool:
    """A young team practicing late (allowed only for late_ok teams, at a cost)."""
    return slot in NOT_FOR_YOUNG and age <= YOUNG_MAX_AGE


# ---------------------------------------------------------------------------
# Soft-constraint weights (bigger = the solver tries harder to avoid it)
# ---------------------------------------------------------------------------
WEIGHTS = {
    # Missing one of a team's weekly practices (x team priority). Losing a
    # team's 2nd practice of a week costs 4x more, so shortfalls get spread.
    "missed_practice": 1000,
    # A required weekend (e.g. "14th or 15th must be used") left unused.
    "weekend_required": 800,
    # Assistant coach double-booked (other team or a local). Head coaches
    # can never be double-booked; assistants can, at a cost.
    "assistant_conflict": 60,
    # A weekday practice that doesn't sit on the team's recurring pattern.
    "off_pattern": 25,
    # Practicing on a weekend instead of a weekday.
    "weekend": 30,
    # Each weekend 2-hour block opened (encourages teams to share blocks).
    "weekend_block": 20,
    # Weekend practice that doesn't match the team's weekend preference.
    "weekend_pref": 12,
    # A late_ok young team (e.g. 14 Black) practicing at 7:20 / 7 PM.
    "late_exception": 25,
    # A team's practices on back-to-back days.
    "consecutive_days": 15,
    # Wishlist: practice matching only the 2nd choice / matching neither.
    "wish_second": 6,
    "wish_miss": 16,
    # A day/time the team marked "not preferred".
    "not_preferred": 10,
    # A team's recurring pattern uses two different timeslots.
    "split_timeslot": 6,
    # A practice not on the team's home court.
    "court_change": 3,
    # Multiplier on the age/timeslot cost table below.
    "slot_fit": 2,
    # Reward: a coach's two teams in back-to-back slots the same day
    # (e.g. girls at 5:30 then boys at 7:20).
    "double_up": 12,
}


def slot_cost(age: int, slot: str) -> int:
    """How undesirable an allowed weekday timeslot is for an age (0 = ideal).
    14s & under: 5:30 ideal, 3:45 as needed. 15+: younger lean 5:30, older 7:20."""
    if slot == "3:45":
        return 6
    if slot == "5:30":
        return 0 if age <= 15 else 1
    if slot == "7:20":
        return max(0, 17 - age)  # 15s: 2, 16s: 1, 17+: 0
    return 0


def default_priority(age: int) -> int:
    """Who wins when not everything fits: older teams first (1 = low, 3 = high)."""
    if age >= 16:
        return 3
    if age >= 14:
        return 2
    return 1
