"""Club-wide constants and scoring weights.

Everything the club might want to tune lives here so the solver code
stays readable.
"""

COURTS = [1, 2, 3, 4, 5, 6, 7]          # courts team practices can use
# The practice court: programs (locals, privates) can use it, team
# practices never do. Stored as court 8 so everything else stays numeric.
PRACTICE_COURT = 8
ALL_COURTS = COURTS + [PRACTICE_COURT]  # every court a program can book


def court_name(c) -> str:
    """'Court 3', or 'Practice court' for the practice court."""
    try:
        c = int(c)
    except (TypeError, ValueError):
        return str(c)
    return "Practice court" if c == PRACTICE_COURT else f"Court {c}"


def court_short(c) -> str:
    """Compact label: '3' or 'P'."""
    try:
        return "P" if int(c) == PRACTICE_COURT else str(int(c))
    except (TypeError, ValueError):
        return str(c)

# Court quality, best first. Court 2 is clearly the worst.
COURT_RANK = [1, 7, 6, 5, 3, 4, 2]
# Cost of using a court, by its rank (best = 0). The jump at the end makes
# the solver move a team to another time rather than put it on Court 2,
# unless that time is a lot worse for the team.
COURT_RANK_COST = [0, 1, 2, 3, 4, 6, 20]
COURT_COST = {c: COURT_RANK_COST[i] for i, c in enumerate(COURT_RANK)}


def team_court_weight(age: int, level: int | None) -> int:
    """How much a team 'deserves' the better courts: older first, 1s over 2s."""
    level_bonus = {1: 4, 2: 0}.get(level, 2)
    return 2 * max(0, age - 11) + level_bonus

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # index = date.weekday()
WEEKDAYS = DAYS[:5]
WEEKEND = DAYS[5:]
# The club's week runs Sunday through Saturday.
WEEK_ORDER = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def week_start(d):
    """Sunday that starts d's week."""
    from datetime import timedelta
    return d - timedelta(days=(d.weekday() + 1) % 7)


def day_in_week(ws, day: str):
    """Date of `day` ('Mon'...) in the week starting Sunday ws."""
    from datetime import timedelta
    return ws + timedelta(days=WEEK_ORDER.index(day))

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
# JumpStart (junior program) runs only in these early hour-long windows.
# They're for programs, not team practices, and they overlap the 3:45 slot:
# a court or coach in JumpStart can't also have a 3:45 practice.
PROGRAM_SLOTS = {
    "3:30": "3:30–4:30",
    "4:30": "4:30–5:30",
}
SLOT_OVERLAPS = {"3:30": {"3:45"}, "4:30": {"3:45"}}


def slots_overlap(a: str, b: str) -> bool:
    return a == b or b in SLOT_OVERLAPS.get(a, ()) or a in SLOT_OVERLAPS.get(b, ())


SLOTS = {"3:30": PROGRAM_SLOTS["3:30"], "3:45": WEEKDAY_SLOTS["3:45"], "4:30": PROGRAM_SLOTS["4:30"],
         "5:30": WEEKDAY_SLOTS["5:30"], "7:20": WEEKDAY_SLOTS["7:20"], **WEEKEND_SLOTS}
SLOT_KEYS = list(SLOTS)  # display order within a day
WEEKDAY_KEYS = list(WEEKDAY_SLOTS)        # team practice slots
PROGRAM_KEYS = list(PROGRAM_SLOTS)
WEEKEND_KEYS = list(WEEKEND_SLOTS)
AM_BLOCKS = {"9AM", "11AM"}
PM_BLOCKS = {"1PM", "3PM", "5PM", "7PM"}

# 24h (start, end) for calendar files.
SLOT_TIMES = {
    "3:30": ((15, 30), (16, 30)),
    "4:30": ((16, 30), (17, 30)),
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
    # Practicing on a weekend instead of a weekday. High on purpose: a
    # weekday practice wins unless it's genuinely unavailable.
    "weekend": 150,
    # Each weekend 2-hour block opened (encourages teams to share blocks).
    "weekend_block": 20,
    # Weekend practice that doesn't match the team's weekend preference.
    "weekend_pref": 12,
    # Travel tournaments: a practice the day before / after the tournament,
    # extra for a late practice the night before. The week leading into a
    # tournament also weighs missed practices 1.5x.
    # Above every no-date break below: breaking a no-date is better than
    # practicing the day before or after a tournament. Still below
    # "missed_practice", so a team keeps its 2 practices.
    "tournament_rest": 800,
    "tournament_late_eve": 60,
    # Keeping a team on its home court (stage 2).
    "home_court": 8,
    # A late_ok young team (e.g. 14 Black) practicing at 7:20 / 7 PM.
    "late_exception": 25,
    # A team's practices on back-to-back days, including Sat -> Sun
    # (practices should have a day in between when possible).
    "consecutive_days": 40,
    # Extra on top for Saturday then Sunday: back-to-back weekdays are fine,
    # back-to-back weekend days are really not.
    "sat_sun": 300,
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
    # Breaking a no-date so a team still gets its 2 practices. Only used
    # when there's no other way: these stay below "missed_practice", so a
    # team gets both practices before any no-date is kept. Cheapest when
    # the head coach is out but an assistant is free to run practice.
    "break_head_nodate": 250,        # head coach out, an assistant is free
    "break_head_and_asst": 400,      # head coach out, no assistant free either
    "break_team_nodate": 600,        # the team's own no-date (last resort)
    # Stability: reward for keeping a practice where the last published (or
    # last built) schedule had it, so a small change doesn't reshuffle
    # everyone. Same idea for keeping its court.
    "keep_previous": 40,
    "keep_court": 6,
    # A second weekend practice for a team in the same week.
    "second_weekend": 200,
    # Reward: a coach's two teams in back-to-back slots the same day
    # (e.g. girls at 5:30 then boys at 7:20).
    "double_up": 12,
    # Rotating 3:45: a team's second 3:45 practice in the same week, so the
    # early slot is shared around the young teams instead of one team
    # getting it every time. Skipped for teams that asked for 3:45.
    "second_345": 14,
    # Extra on top of "not_preferred" when the not-preferred time is 3:45
    # (e.g. a coach who can't get off work that early).
    "not_preferred_345": 40,
}


def slot_cost(age: int, slot: str) -> int:
    """How undesirable an allowed weekday timeslot is for an age (0 = ideal).
    14s & under: 5:30 ideal, 3:45 as needed. 15+: younger lean 5:30, older 7:20."""
    if slot == "3:45":
        # Still a last resort for everyone, but younger teams go early
        # first: 12s 5, 13s 6, 14s 7.
        return max(3, age - 7)
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
