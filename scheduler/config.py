"""Club-wide constants and scoring weights.

Everything the club might want to tune lives here so the solver code
stays readable.
"""

COURTS = [1, 2, 3, 4, 5, 6, 7]

# Short key -> display label. Order matters (earliest first).
SLOTS = {
    "3:45": "3:45–5:30",
    "5:30": "5:30–7:20",
    "7:20": "7:20–9:10",
}
SLOT_KEYS = list(SLOTS.keys())

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]

PRACTICES_PER_WEEK = 2

# ---------------------------------------------------------------------------
# Soft-constraint weights (bigger = the solver tries harder to avoid it)
# ---------------------------------------------------------------------------
WEIGHTS = {
    # Missing one of a team's 2 weekly practices (multiplied by team priority).
    "missed_practice": 1000,
    # A practice that doesn't sit on the team's recurring weekly pattern.
    "off_pattern": 25,
    # A team's two practices on back-to-back days.
    "consecutive_days": 15,
    # A team's recurring pattern uses two different timeslots.
    "split_timeslot": 6,
    # A practice not on the team's home court.
    "court_change": 3,
    # Multiplier on the age/timeslot cost table below.
    "slot_fit": 4,
    # A practice on a day/slot the coach didn't mark as preferred.
    "coach_pref_day": 4,
    "coach_pref_slot": 4,
}


def slot_cost(age: int, slot: str) -> int:
    """How undesirable a timeslot is for a team of a given age (0 = ideal).

    - 5:30 is the default sweet spot for younger teams.
    - 7:20 suits older teams and gets worse the younger the team.
    - 3:45 is "only when really necessary" for everyone, but a 13s team
      at 3:45 still beats a 13s team at 7:20.
    """
    if slot == "5:30":
        return 0 if age <= 14 else 1
    if slot == "7:20":
        return max(0, 16 - age) * 3  # 12s: 12, 13s: 9, 14s: 6, 15s: 3, 16+: 0
    if slot == "3:45":
        return 7 + max(0, age - 14) * 2  # 13s: 7, 15s: 9, 16s: 11, 18s: 15
    raise ValueError(f"Unknown slot {slot}")


def default_priority(age: int) -> int:
    """Who wins when not everything fits: older teams first (1 = low, 3 = high)."""
    if age >= 16:
        return 3
    if age >= 14:
        return 2
    return 1
