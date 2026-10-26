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
COURT_RANK_COST = [0, 1, 2, 3, 4, 6, 12]
COURT_COST = {c: COURT_RANK_COST[i] for i, c in enumerate(COURT_RANK)}


def pecking_key(age: int, rank: int | None) -> tuple:
    """Sort key for the club's pecking order, top team first: older age
    groups first, then rank within the age (1 = top). A team with no rank
    sits between its age group's 1s and 2s."""
    return (-age, 1.5 if rank is None else rank)


def pecking_weights(teams) -> dict[str, int]:
    """How much each team 'deserves' the better courts, from the pecking
    order: the top team gets 20, the bottom team 2, everyone else spread in
    between (teams sharing a rank, e.g. 16-1 boys and girls, tie). The
    lowest team is the one that ends up on Court 2 when it's needed."""
    keys = sorted({pecking_key(t.age, t.level) for t in teams})
    if len(keys) <= 1:
        return {t.name: 10 for t in teams}
    pos = {k: i for i, k in enumerate(keys)}  # 0 = top
    n = len(keys) - 1
    return {t.name: 2 + round(18 * (n - pos[pecking_key(t.age, t.level)]) / n) for t in teams}


def team_court_weight(age: int, level: int | None) -> int:
    """Court weight for a team on its own (no club context)."""
    return 2 * max(0, age - 11) + max(0, 5 - level) if level else 2 * max(0, age - 11) + 2

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
    # The club's rule order, most important first (Oct 2026). Each step costs
    # roughly 2-4x the one below it, so one break of a higher rule outweighs
    # several of the next one down (e.g. two assistant no-dates still cost
    # less than one head coach's). Never alone on a weekend, head coaches never double-booked,
    # age/timeslot rules, one team per court and 2 practices a week max are
    # hard rules, above all of these.
    #
    # 1. Every team gets its practices. Per missed practice, x team priority;
    #    losing a team's 2nd practice of a week costs 4x more, so shortfalls
    #    get spread.
    "missed_practice": 150000,
    # (Required weekends are a note for coaches, not a rule.)
    "weekend_required": 0,
    # 2. No practice the day before / after a travel tournament; a late one
    #    the night before costs a bit more. The week leading into a
    #    tournament also weighs missed practices 1.5x.
    "tournament_rest": 60000,
    "tournament_late_eve": 2000,
    # 3-5. Breaking a no-date so a team still gets its 2 practices, worst
    #    first: the team's own, then the head coach's (worse when no
    #    assistant is free to run it), then an assistant's.
    "break_team_nodate": 30000,
    "break_head_and_asst": 20000,
    "break_head_nodate": 15000,
    "assistant_nodate": 8000,
    # 6. A coach (head or assistant) who works a locals program then: possible
    #    if they get a sub for the program, and flagged so. Below every
    #    no-date break, so a sub is asked for before a no-date is broken.
    #    An assistant on two teams at once costs the same.
    "locals_sub": 4000,
    "assistant_conflict": 4000,
    # 7. Weekday before weekend: a weekend practice only when the weekdays
    #    can't take it (two weekday practices, even doubled up, win).
    "weekend": 1500,
    # 8. Preferred days: the wishlist's days and days marked not preferred.
    "wish_day_miss": 600,
    "not_preferred_day": 550,
    # 9. Preferred times: the wishlist's times (a 2nd-choice match costs a
    #    little), times marked not preferred (3:45 a bit more: often a coach
    #    who can't get off work), weekend AM/PM preference.
    "wish_time_miss": 250,
    "not_preferred": 220,
    "not_preferred_345": 40,
    "weekend_pref": 220,
    "wish_second": 60,
    # 10. Saturday then Sunday (on top of "consecutive_days"; always flagged).
    "sat_sun": 100,
    # 11. A second weekend practice for a team in the same week.
    "second_weekend": 50,
    # 12. Back-to-back days (weekdays a team asked for, e.g. Tue/Wed, are free).
    "consecutive_days": 20,
    # 13. Everything else, roughly in this order:
    # age/timeslot fit (multiplier on the slot_cost table below: up to 14)
    "slot_fit": 2,
    # rotating 3:45: a team's second 3:45 in a week (not for teams that
    # asked for 3:45)
    "second_345": 12,
    # a late_ok young team (e.g. 14 Black) at 7:20 / 7 PM
    "late_exception": 11,
    # keeping the same days and times every week: a practice off the
    # team's usual week (and a usual slot it skips costs twice this)
    "off_pattern": 5,
    # the usual week using two different timeslots
    "split_timeslot": 6,
    # stability: reward for keeping a practice where the last published (or
    # last built) schedule had it, and its court
    "keep_previous": 15,
    "keep_court": 4,
    # each weekend 2-hour block opened (teams share blocks)
    "weekend_block": 7,
    # reward: a coach's two teams in back-to-back slots the same day
    # (e.g. girls at 5:30 then boys at 7:20)
    "double_up": 6,
    # courts: home court (stage 2), any court change
    "home_court": 5,
    "court_change": 2,
    # moving off the usual slot into a 1st-choice wishlist slot: small, so a
    # coach gets an open slot they asked for that week, but not zero, so
    # practices don't bounce around without a gain
    "move_to_wish": 3,
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
