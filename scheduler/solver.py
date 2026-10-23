"""The auto-scheduler: a constraint-programming model (Google OR-Tools CP-SAT).

How it thinks
-------------
For every open date in the period, every timeslot and every court, the
model decides whether each team practices there. On top of that, each team
gets a *recurring weekly pattern* (2 day/slot pairs plus a home court), and
the model is rewarded for keeping actual practices on that pattern, so the
result is "the same weekly schedule wherever possible, with makeups only
when a coach's no-date or a closure forces one".

Hard rules (never broken)
  - Locals programs block their courts.
  - One team per court per timeslot.
  - A coach is never in two places at once (two teams, or a team + a local).
  - Nothing on a coach's no-dates, nothing on closure days.
  - A team practices at most once a day and at most twice a week.

Soft preferences (traded off by the weights in config.py)
  - Every team gets its 2 practices each full week (heaviest; older teams win ties).
  - Practices stay on the team's weekly pattern and home court.
  - Non-consecutive days; same timeslot both days.
  - Younger teams earlier, older teams later, 3:45 only when needed.
  - Coaches' preferred days and timeslots.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from ortools.sat.python import cp_model

from .config import COURTS, PRACTICES_PER_WEEK, SLOT_KEYS, SLOTS, WEEKDAYS, WEIGHTS, slot_cost
from .data import CleanInput, ScheduleInput, clean


@dataclass
class ScheduleResult:
    status: str
    assignments: pd.DataFrame          # every booking: practices + locals
    pattern: pd.DataFrame              # each team's recurring weekly template
    issues: pd.DataFrame               # things a human should look at
    metrics: dict
    warnings: list[str] = field(default_factory=list)
    start: date | None = None
    end: date | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE")


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def solve(
    inp: ScheduleInput,
    start: date,
    end: date,
    time_limit_s: float = 20.0,
    makeup_closures: bool = True,
    seed: int = 0,
) -> ScheduleResult:
    data: CleanInput = clean(inp)
    t0 = time.time()

    # ------------------------------------------------------------ dates --
    all_weekdays = [d for d in pd.date_range(start, end).date if d.weekday() < 5]
    dates = [d for d in all_weekdays if d not in data.closures]
    weeks: dict[date, list[date]] = defaultdict(list)
    for d in dates:
        weeks[week_start(d)].append(d)

    # A "full" week is one where the 2-practice target applies. Weeks cut
    # off by the start/end of the period only follow the pattern (the rest
    # of that week belongs to the neighbouring month).
    full_week = {}
    for ws in weeks:
        span = [ws + timedelta(days=i) for i in range(5)]
        inside = all(start <= d <= end for d in span)
        has_closure = any(d in data.closures for d in span)
        full_week[ws] = inside and (makeup_closures or not has_closure)

    # Courts blocked by locals on a weekday/slot, and who's working them.
    local_courts: dict[tuple[str, str], set[int]] = defaultdict(set)
    local_coaches: dict[tuple[str, str], set[str]] = defaultdict(set)
    for lp in data.locals:
        local_courts[(lp.day, lp.slot)].update(lp.courts)
        local_coaches[(lp.day, lp.slot)].update(lp.coaches)

    teams = data.teams
    T = range(len(teams))

    # =====================================================================
    # Stage 1: which day and timeslot each team practices.
    # Courts are interchangeable here, so we only need "no more teams than
    # free courts" per timeslot. This keeps the model ~7x smaller.
    # =====================================================================
    m = cp_model.CpModel()
    obj = []

    def free_courts(d, s):
        return len(COURTS) - len(local_courts[(WEEKDAYS[d.weekday()], s)])

    y = {}  # (t, d, s) -> team t practices on date d in slot s
    for t in T:
        team = teams[t]
        for d in dates:
            wd = WEEKDAYS[d.weekday()]
            blocked_slots = data.no_dates.get((team.coach, d), set())
            for s in SLOT_KEYS:
                if s in blocked_slots or team.coach in local_coaches[(wd, s)]:
                    continue  # coach unavailable or working a local then
                if free_courts(d, s) <= 0:
                    continue
                y[t, d, s] = m.NewBoolVar(f"y_{t}_{d}_{s}")

    def ys(t, d, s):
        return y.get((t, d, s), 0)

    def y_day(t, d):
        return sum(ys(t, d, s) for s in SLOT_KEYS)

    # Recurring pattern: p[t, weekday, slot].
    p = {(t, wd, s): m.NewBoolVar(f"p_{t}_{wd}_{s}") for t in T for wd in WEEKDAYS for s in SLOT_KEYS}

    # ---------------------------------------------------- hard rules --
    # No more teams than free courts.
    for d in dates:
        for s in SLOT_KEYS:
            vs = [y[t, d, s] for t in T if (t, d, s) in y]
            if len(vs) > free_courts(d, s):
                m.Add(sum(vs) <= free_courts(d, s))

    # A coach can't be in two places at once.
    coach_teams = defaultdict(list)
    for t in T:
        coach_teams[teams[t].coach].append(t)
    for coach, ts in coach_teams.items():
        if len(ts) < 2:
            continue
        for d in dates:
            for s in SLOT_KEYS:
                vs = [y[t, d, s] for t in ts if (t, d, s) in y]
                if len(vs) > 1:
                    m.AddAtMostOne(vs)

    # Team: at most one practice per day, at most two per week.
    for t in T:
        for d in dates:
            vs = [y[t, d, s] for s in SLOT_KEYS if (t, d, s) in y]
            if len(vs) > 1:
                m.AddAtMostOne(vs)
        for ws, wdates in weeks.items():
            m.Add(sum(y_day(t, d) for d in wdates) <= PRACTICES_PER_WEEK)

    # Pattern shape: 2 different days, one slot each.
    for t in T:
        m.Add(sum(p[t, wd, s] for wd in WEEKDAYS for s in SLOT_KEYS) == PRACTICES_PER_WEEK)
        for wd in WEEKDAYS:
            m.AddAtMostOne([p[t, wd, s] for s in SLOT_KEYS])

    # ----------------------------------------------- soft preferences --
    short_vars, off_vars, consec_vars = {}, {}, {}

    for t in T:
        team = teams[t]
        coach = data.coaches.get(team.coach)

        # 2 practices per full week. Losing a team's *second* practice of a
        # week costs much more than losing its first, so shortfalls get
        # spread (one practice each for many teams, not zero for a few).
        # Older teams are weighted higher via priority.
        for ws, wdates in weeks.items():
            if not full_week[ws]:
                continue
            s1 = m.NewBoolVar(f"short1_{t}_{ws}")
            s2 = m.NewBoolVar(f"short2_{t}_{ws}")
            m.Add(s1 + s2 >= PRACTICES_PER_WEEK - sum(y_day(t, d) for d in wdates))
            m.Add(s2 <= s1)
            short_vars[t, ws] = (s1, s2)
            w = WEIGHTS["missed_practice"] * team.priority
            obj += [w * s1, 4 * w * s2]

        for d in dates:
            wd = WEEKDAYS[d.weekday()]
            for s in SLOT_KEYS:
                v = ys(t, d, s)
                # Off pattern: practicing at a day/slot that isn't the template.
                if (t, d, s) in y:
                    off = m.NewBoolVar(f"off_{t}_{d}_{s}")
                    m.Add(off >= v - p[t, wd, s])
                    off_vars[t, d, s] = off
                    obj.append(WEIGHTS["off_pattern"] * off)
                    # Age-appropriate timeslot + coach preferences.
                    cost = WEIGHTS["slot_fit"] * slot_cost(team.age, s)
                    if coach and coach.preferred_days and wd not in coach.preferred_days:
                        cost += WEIGHTS["coach_pref_day"]
                    if coach and coach.preferred_slots and s not in coach.preferred_slots:
                        cost += WEIGHTS["coach_pref_slot"]
                    if cost:
                        obj.append(cost * v)
                # Pattern miss: template says practice, but none today.
                miss = m.NewBoolVar(f"miss_{t}_{d}_{s}")
                m.Add(miss >= p[t, wd, s] - v)
                obj.append(WEIGHTS["off_pattern"] * 2 * miss)

        # Back-to-back days within a week.
        for d in dates:
            nxt = d + timedelta(days=1)
            if nxt in dates and week_start(nxt) == week_start(d):
                cons = m.NewBoolVar(f"cons_{t}_{d}")
                m.Add(cons >= y_day(t, d) + y_day(t, nxt) - 1)
                consec_vars[t, d] = cons
                obj.append(WEIGHTS["consecutive_days"] * cons)

        # Same timeslot both days of the pattern.
        used = []
        for s in SLOT_KEYS:
            u = m.NewBoolVar(f"slotused_{t}_{s}")
            for wd in WEEKDAYS:
                m.Add(u >= p[t, wd, s])
            used.append(u)
        obj.append(WEIGHTS["split_timeslot"] * sum(used))

    m.Minimize(sum(obj))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = seed
    status = solver.Solve(m)
    status_name = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return ScheduleResult(status_name, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                              {"solve_seconds": round(time.time() - t0, 1)}, data.warnings, start, end)

    booked = [(t, d, s) for (t, d, s), v in y.items() if solver.Value(v)]
    pattern_slots = [(t, wd, s) for (t, wd, s), v in p.items() if solver.Value(v)]

    # =====================================================================
    # Stage 2: courts. Give each team a home court and keep it there as
    # often as possible.
    # =====================================================================
    home_court, court_of = _assign_courts(booked, T, local_courts, seed)
    elapsed = time.time() - t0

    # ------------------------------------------------- read solution --
    rows = []
    for (t, d, s) in booked:
        team = teams[t]
        rows.append({
            "date": d, "day": WEEKDAYS[d.weekday()], "slot": s, "slot_label": SLOTS[s],
            "court": court_of[t, d, s], "kind": "Practice", "name": team.name, "age": team.age,
            "coach": team.coach,
            "on_pattern": not solver.Value(off_vars[t, d, s]),
        })
    for d in dates:
        wd = WEEKDAYS[d.weekday()]
        for lp in data.locals:
            if lp.day == wd:
                for c in lp.courts:
                    rows.append({
                        "date": d, "day": wd, "slot": lp.slot, "slot_label": SLOTS[lp.slot],
                        "court": c, "kind": "Local", "name": lp.name, "age": None,
                        "coach": "; ".join(lp.coaches), "on_pattern": True,
                    })
    assignments = pd.DataFrame(rows)
    if not assignments.empty:
        assignments["slot_order"] = assignments["slot"].map(SLOT_KEYS.index)
        assignments = assignments.sort_values(["date", "slot_order", "court"]).drop(columns="slot_order")
        assignments = assignments.reset_index(drop=True)

    pattern_rows = []
    for (t, wd, s) in sorted(pattern_slots, key=lambda k: (k[0], WEEKDAYS.index(k[1]))):
        pattern_rows.append({"team": teams[t].name, "age": teams[t].age, "coach": teams[t].coach,
                             "day": wd, "slot": s, "slot_label": SLOTS[s], "court": home_court[t]})
    pattern = pd.DataFrame(pattern_rows)

    issues = _issues(data, teams, solver, short_vars, off_vars, consec_vars, assignments, weeks, pattern_slots)

    practices = assignments[assignments["kind"] == "Practice"] if not assignments.empty else assignments
    required = sum(PRACTICES_PER_WEEK for t in T for ws in weeks if full_week[ws])
    in_full = practices[practices["date"].map(lambda d: full_week[week_start(d)])] if len(practices) else practices
    metrics = {
        "status": status_name,
        "solve_seconds": round(elapsed, 1),
        "teams": len(teams),
        "practices_scheduled": int(len(practices)),
        "full_week_practices_required": int(required),
        "full_week_practices_scheduled": int(len(in_full)),
        "on_pattern_pct": round(float(100 * practices["on_pattern"].mean()), 1) if len(practices) else 0.0,
        "practices_at_3_45": int((practices["slot"] == "3:45").sum()) if len(practices) else 0,
        "back_to_back_weeks": int(sum(solver.Value(v) for v in consec_vars.values())),
        "objective": solver.ObjectiveValue(),
    }
    return ScheduleResult(status_name, assignments, pattern, issues, metrics, data.warnings, start, end)


def _assign_courts(booked, T, local_courts, seed=0, time_limit_s=10.0):
    """Stage 2: put each booked practice on a free court, keeping teams on
    one home court as much as possible. Returns (home_court, court_of)."""
    m = cp_model.CpModel()
    x, home = {}, {}
    for t in T:
        for c in COURTS:
            home[t, c] = m.NewBoolVar(f"h_{t}_{c}")
        m.AddExactlyOne([home[t, c] for c in COURTS])
    by_slot = defaultdict(list)
    changes = []
    for (t, d, s) in booked:
        wd = WEEKDAYS[d.weekday()]
        opts = [c for c in COURTS if c not in local_courts[(wd, s)]]
        for c in opts:
            x[t, d, s, c] = m.NewBoolVar(f"x_{t}_{d}_{s}_{c}")
            by_slot[d, s, c].append(x[t, d, s, c])
            # moved off home court
            ch = m.NewBoolVar(f"ch_{t}_{d}_{s}_{c}")
            m.Add(ch >= x[t, d, s, c] - home[t, c])
            changes.append(ch)
        m.AddExactlyOne([x[t, d, s, c] for c in opts])
    for vs in by_slot.values():
        if len(vs) > 1:
            m.AddAtMostOne(vs)
    m.Minimize(sum(changes))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = seed
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError("Court assignment failed – this shouldn't happen if stage 1 respected capacity.")
    home_court = {t: next(c for c in COURTS if solver.Value(home[t, c])) for t in T}
    court_of = {(t, d, s): c for (t, d, s, c), v in x.items() if solver.Value(v)}
    return home_court, court_of


def _issues(data, teams, solver, short_vars, off_vars, consec_vars, assignments, weeks, pattern_slots) -> pd.DataFrame:
    """Plain-language list of everything that isn't the 'ideal' schedule."""
    out = []

    usual = defaultdict(list)  # team index -> [(weekday, slot)]
    for (t, wd, sl) in pattern_slots:
        usual[t].append((wd, sl))

    def why(t, ws):
        """Which of the team's usual practices that week couldn't happen, and why."""
        team = teams[t]
        reasons = []
        for wd, sl in sorted(usual[t], key=lambda k: WEEKDAYS.index(k[0])):
            d = ws + timedelta(days=WEEKDAYS.index(wd))
            if d in data.closures:
                reasons.append(f"usual {wd} {sl} falls on {data.closures[d] or 'a closure'} ({d:%-m/%-d})")
            elif sl in data.no_dates.get((team.coach, d), set()):
                reasons.append(f"{team.coach} unavailable on usual {wd} {d:%-m/%-d}")
        return "; ".join(reasons)

    for (t, ws), (s1, s2) in short_vars.items():
        n = solver.Value(s1) + solver.Value(s2)
        if n:
            team = teams[t]
            out.append({"severity": "High", "team": team.name, "week_of": ws,
                        "issue": f"Only {PRACTICES_PER_WEEK - n} practice(s) this week",
                        "detail": (why(t, ws) + "; " if why(t, ws) else "") + "no open court for a makeup"})

    for (t, d, sl), v in off_vars.items():
        if solver.Value(v):
            team = teams[t]
            out.append({"severity": "Medium", "team": team.name, "week_of": week_start(d),
                        "issue": f"Makeup / moved: {d:%a %-m/%-d} {sl}",
                        "detail": why(t, week_start(d)) or "differs from the usual weekly slot"})

    for (t, d), v in consec_vars.items():
        if solver.Value(v):
            team = teams[t]
            out.append({"severity": "Low", "team": team.name, "week_of": week_start(d),
                        "issue": f"Back-to-back days: {d:%a} & {(d + timedelta(days=1)):%a %-m/%-d}",
                        "detail": ""})

    if not assignments.empty:
        early = assignments[(assignments["kind"] == "Practice") & (assignments["slot"] == "3:45")]
        for _, r in early.iterrows():
            out.append({"severity": "Low", "team": r["name"], "week_of": week_start(r["date"]),
                        "issue": f"3:45 practice {r['date']:%a %-m/%-d}", "detail": "used because later slots were full"})

    df = pd.DataFrame(out, columns=["severity", "team", "week_of", "issue", "detail"])
    if not df.empty:
        order = {"High": 0, "Medium": 1, "Low": 2}
        df = df.sort_values(["severity", "week_of", "team"], key=lambda c: c.map(order) if c.name == "severity" else c)
        df = df.reset_index(drop=True)
    return df
