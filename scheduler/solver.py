"""The auto-scheduler: a constraint-programming model (Google OR-Tools CP-SAT).

Stage 1 decides when every team practices (date + weekday timeslot or
weekend 2-hour block) and gives each team a recurring weekday pattern.
Stage 2 assigns courts, keeping each team on a home court.

Hard rules (never broken)
  - On-site locals block their courts; closures block whole days.
  - No more teams than open courts in any timeslot.
  - A head coach is never in two places at once (two teams, or a team + a
    local they work). Assistants can be, at a cost.
  - 'No' availability rows for a team or its head coach.
  - Age rules: only 14s & under at 3:45; 14s & under never at 7:20 / 7 PM.
  - No practices before a team's season start date.
  - Weekends: only open dates/blocks, only a team's weekend_only options,
    and never one team alone in a weekend block.
  - At most one practice per team per day (hand-placed extras can share a day) and two per week.

Soft preferences (weights in config.py)
  - 2 practices per week (shortfalls spread, older teams first).
  - Required weekends get used ("14th or 15th must be used").
  - Same weekday pattern each week; weekdays over weekends.
  - Ranked wishlists, not-preferred days/times, weekend preferences.
  - Coach double-ups (a coach's teams back-to-back the same day).
  - Non-consecutive days, same slot both days, home court, age fit.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import permutations

import pandas as pd
from ortools.sat.python import cp_model

from .config import (
    COURT_COST,
    COURT_RANK,
    COURTS,
    DAYS,
    PRACTICES_PER_WEEK,
    SLOT_KEYS,
    SLOTS,
    WEEKDAY_KEYS,
    WEEKDAYS,
    WEIGHTS,
    back_to_back,
    court_name,
    day_in_week,
    is_hs_boys,
    is_late_exception,
    pecking_weights,
    slot_cost,
    slots_overlap,
)
from .config import week_start as _week_start
from .data import CleanInput, ScheduleInput, clean, span_text, tournament_conflicts


@dataclass
class ScheduleResult:
    """A solved schedule: every booking, each team's usual week, what to look at, and summary numbers."""
    status: str
    assignments: pd.DataFrame          # every booking: practices + locals
    pattern: pd.DataFrame              # each team's recurring weekday template
    issues: pd.DataFrame               # things a human should look at
    metrics: dict
    warnings: list[str] = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    weekend_dates: list[date] = field(default_factory=list)
    tournaments: list = field(default_factory=list)   # data.Tournament objects, for display
    # One row per team per week: team, week_of, target, scheduled, short
    practice_counts: pd.DataFrame = field(default_factory=pd.DataFrame)
    # Practices that had to land on a no-date so the team still got 2 that week.
    broken: list = field(default_factory=list)

    @property
    def shortfalls(self) -> pd.DataFrame:
        pc = self.practice_counts
        if pc is None or pc.empty:
            return pd.DataFrame(columns=["team", "week_of", "target", "scheduled", "short"])
        return pc[pc["short"] > 0].sort_values(["week_of", "team"]).reset_index(drop=True)

    @property
    def ok(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE")


def week_start(d: date) -> date:
    """Weeks run Sunday-Saturday."""
    return _week_start(d)


def solve(
    inp: ScheduleInput,
    start: date,
    end: date,
    time_limit_s: float = 30.0,
    makeup_closures: bool = True,
    seed: int = 0,
    previous: pd.DataFrame | None = None,
    previous_pattern: pd.DataFrame | None = None,
    restore: pd.DataFrame | None = None,
) -> ScheduleResult:
    """previous: an earlier schedule's assignments (e.g. the published one).
    Practices stay where they were unless something needs them to move.

    restore: a published version's practices to bring back exactly. Each one
    is placed like a hand-placed practice; any that can't happen any more
    (team gone, club closed, court or coach taken...) is left out with a note,
    and the solver fills that week as usual."""
    data: CleanInput = clean(inp, start, end)
    t0 = time.time()
    teams = data.teams
    T = range(len(teams))

    # ------------------------------------------------------------ dates --
    all_days = list(pd.date_range(start, end).date)
    open_dates = [d for d in all_days if d not in data.closures and
                  (d.weekday() < 5 or d in data.weekends)]
    # Block days: open weekend days, plus weekdays listed as open days (school
    # breaks), which run in 2-hour blocks like a weekend.
    weekend_dates = [d for d in open_dates if d in data.weekends]

    def slots_on(d):
        return data.weekends[d].blocks if d in data.weekends else WEEKDAY_KEYS

    def locals_at(d, s):
        """Programs taking courts/coaches in slot s (incl. overlapping JumpStart times)."""
        return [lp for lp in data.locals if lp.blocks(d, s)]

    local_tourn = [tn for tn in data.tournaments if tn.kind == "local"]
    travel_tourn = [tn for tn in data.tournaments if tn.kind == "travel"]

    def courts_on(d, s):
        base = [c for c in data.weekends[d].courts if c in COURTS] if d in data.weekends else COURTS
        blocked = {c for lp in locals_at(d, s) if lp.on_site for c in lp.courts}
        blocked |= {c for tn in local_tourn if tn.blocks(d, s) for c in tn.courts}
        return [c for c in base if c not in blocked]

    # Travel tournaments: days a team is away, and the days right around them.
    away = defaultdict(set)                 # team name -> dates at a tournament
    rest_days = defaultdict(dict)           # team name -> {date: (tournament, 'before'|'after')}
    lead_in_weeks = defaultdict(set)        # team name -> week starts leading into a tournament
    for tn in travel_tourn:
        for name in tn.teams:
            d = tn.start
            while d <= tn.end:
                away[name].add(d)
                d += timedelta(days=1)
            rest_days[name].setdefault(tn.start - timedelta(days=1), (tn, "before"))
            rest_days[name].setdefault(tn.end + timedelta(days=1), (tn, "after"))
            lead_in_weeks[name].add(week_start(tn.start - timedelta(days=1)))

    weeks: dict[date, list[date]] = defaultdict(list)
    for d in open_dates:
        weeks[week_start(d)].append(d)

    # Open weeks (school breaks): every open weekday that week runs in 2-hour
    # blocks. Nothing is scheduled automatically then; coaches add the
    # practices they want by hand (those are placed like any hand-placed one).
    def is_break_week(ws):
        days_ = [day_in_week(ws, wd) for wd in WEEKDAYS]
        open_ = [d for d in days_ if start <= d <= end and d not in data.closures]
        return bool(open_) and all(d in data.weekends for d in open_)

    break_weeks = {ws for ws in weeks if is_break_week(ws)}

    def week_target(ws, per_week=PRACTICES_PER_WEEK):
        """Practices a team should get that week. A week cut off by the
        schedule's start or end is prorated: 3+ weekdays inside the range
        still count as a full week, 2 weekdays get 1 practice, and a single
        day just follows the usual pattern."""
        span = [day_in_week(ws, wd) for wd in WEEKDAYS]  # Mon-Fri of that Sun-Sat week
        inside = sum(start <= d <= end for d in span)
        if ws in break_weeks:
            return 0
        if not makeup_closures and any(d in data.closures for d in span):
            return 0
        return per_week if inside >= 3 else (min(1, per_week) if inside == 2 else 0)

    coach_local = defaultdict(set)  # (d, s) -> coaches working a local then
    coach_prog = defaultdict(set)   # ... a locals program (could get a sub)
    coach_tourn = defaultdict(set)  # ... a local tournament (busy all day)
    for d in open_dates:
        for s in slots_on(d):
            for lp in locals_at(d, s):
                coach_prog[d, s].update(lp.coaches)
            for tn in local_tourn:  # running a local tournament: busy until 7 PM
                if tn.blocks(d, s):
                    coach_tourn[d, s].update(tn.coaches)
            coach_local[d, s] = coach_prog[d, s] | coach_tourn[d, s]

    def programs_of(c, d, s):
        return [lp.name for lp in locals_at(d, s) if c in lp.coaches]

    rules_for = defaultdict(list)
    for r in data.rules:
        rules_for[r.who].append(r)

    m = cp_model.CpModel()
    obj = []

    # ------------------------------------------ manual practices (pins) --
    # Practices placed by hand are always scheduled. They override a team's
    # own rules (availability, age slots, season start...), but they still
    # need a free court and can't double-book a head coach.
    idx = {teams[t].name: t for t in T}
    pinned: dict[tuple, int | None] = {}
    problems = []
    for pn in data.pins:
        t, d, s = idx[pn.team], pn.day, pn.slot
        label = f"{pn.team} on {d:%a %-m/%-d} at {s}"
        if not (start <= d <= end):
            continue  # another run of dates: placed when those dates are built
        elif d in data.closures:
            problems.append(f"{label}: the club is closed ({data.closures[d] or 'closure'})")
        elif d.weekday() >= 5 and (d not in data.weekends or s not in data.weekends[d].blocks):
            problems.append(f"{label}: that weekend time isn't open (add it on the Weekends sheet)")
        elif s not in slots_on(d):
            problems.append(f"{label}: that time isn't open that day"
                            + (" (it runs in 2-hour blocks)" if d in data.weekends else ""))
        elif not courts_on(d, s):
            problems.append(f"{label}: every court is taken by programs or a tournament")
        elif pn.court is not None and pn.court not in courts_on(d, s):
            problems.append(f"{label}: Court {pn.court} is taken by a program or tournament")
        elif (t, d, s) in pinned:
            problems.append(f"{label}: that team already has a practice placed then")
        else:
            pinned[(t, d, s)] = pn.court
    by_slot = defaultdict(list)
    for (t, d, s), c in pinned.items():
        by_slot[d, s].append((t, c))
    for (d, s), lst in by_slot.items():
        if len(lst) > len(courts_on(d, s)):
            problems.append(f"{d:%a %-m/%-d} at {s}: more practices placed by hand than open courts")
        cs = [c for _, c in lst if c is not None]
        if len(cs) != len(set(cs)):
            problems.append(f"{d:%a %-m/%-d} at {s}: two practices placed on the same court")
        heads = [h for t, _ in lst for h in teams[t].heads]
        for h in {h for h in heads if heads.count(h) > 1}:
            problems.append(f"{d:%a %-m/%-d} at {s}: {h} would be coaching two teams at once")
    # Extras ("Extra" overrides) sit on top of the team's weekly number, so a
    # coach can add a 3rd practice without losing one of the usual two.
    extra_keys = {(idx[pn.team], pn.day, pn.slot) for pn in data.pins if pn.extra}
    extras = defaultdict(int)  # (t, week) -> extra practices placed by hand
    per_week = defaultdict(int)
    for (t, d, s) in pinned:
        if (t, d, s) in extra_keys:
            extras[t, week_start(d)] += 1
        else:
            per_week[t, week_start(d)] += 1
    for (t, ws), n in per_week.items():
        if n > teams[t].per_week:
            problems.append(f"{teams[t].name}: {n} practices placed by hand in the week of {ws:%b %-d} (it has "
                            f"{teams[t].per_week} a week); add the others as extra practices instead")
    if problems:
        raise ValueError("These manual changes can't all happen:\n- " + "\n- ".join(problems))

    restored, restore_notes = _restore_pins(restore, pinned, teams, idx, data, start, end, slots_on, courts_on,
                                            coach_tourn, rules_for, away, extras)

    # ---------------------------------------------- decision variables --
    # Teams whose 1st-choice wishlist asks for 3:45: no 3:45 cost for them.
    wants_345 = {t: any(w.choice == 1 and w.times and "3:45" in w.times
                        for w in data.wishes.get(teams[t].name, []))
                 for t in T}

    # late_ok teams whose 1st-choice wishlist asks for 7:20 / 7 PM: going
    # late is what they want, not an exception.
    wants_late = {t: any(w.choice == 1 and w.times and w.times & {"7:20", "7PM"}
                         for w in data.wishes.get(teams[t].name, []))
                  for t in T}

    def not_preferred_at(t, d, s):
        return any(r.kind == "not_preferred" and r.matches(d, s)
                   for who in [teams[t].name] + teams[t].staff for r in rules_for[who])

    def wished(t, d, s):
        """A 1st-choice wishlist slot the team doesn't also mark not preferred."""
        ws_ = data.wishes.get(teams[t].name, [])
        return any(w.choice == 1 and w.matches(d, s) for w in ws_) and not not_preferred_at(t, d, s)

    def unwanted(t, d, s):
        """A slot the team marked not preferred, or one outside a wishlist it gave."""
        ws_ = data.wishes.get(teams[t].name, [])
        return not_preferred_at(t, d, s) or (bool(ws_) and not any(w.choice == 1 and w.matches(d, s) for w in ws_))

    y = {}
    broken = {}  # (t, d, s) -> details, for practices that land on a no-date
    subs = {}    # (t, d, s) -> [(coach, [programs])]: possible if they get a sub
    for t in T:
        team = teams[t]
        for d in open_dates:
            # not started yet, or away at a travel tournament
            off_day = bool(team.start and d < team.start) or d in away[team.name]
            for s in slots_on(d):
                if (t, d, s) in pinned:
                    y[t, d, s] = m.NewBoolVar(f"y_{t}_{d}_{s}")
                    m.Add(y[t, d, s] == 1)
                else:
                    if off_day or not team.allowed(s) or not courts_on(d, s) or week_start(d) in break_weeks:
                        continue
                    if d.weekday() >= 5 and team.weekend_only and not any(w.matches(d, s) for w in team.weekend_only):
                        continue
                    team_nos = [r for r in rules_for[team.name] if r.kind == "no" and r.matches(d, s)]
                    if any(r.manual for r in team_nos):
                        continue  # removed by hand: never put back
                    if any(h in coach_tourn[d, s] for h in team.heads):
                        continue
                    y[t, d, s] = m.NewBoolVar(f"y_{t}_{d}_{s}")
                    # No-dates are kept unless it's the only way to get the
                    # team its 2 practices; then they're broken (and flagged).
                    head_out = [h for h in team.heads
                                if any(r.kind == "no" and r.matches(d, s) for r in rules_for[h])]
                    if team_nos or head_out:
                        free_asst = [a for a in team.assistants if a not in team.heads
                                     and a not in coach_local[d, s]
                                     and not any(r.kind == "no" and r.matches(d, s) for r in rules_for[a])]
                        busy_asst = [a for a in team.assistants if a not in team.heads and a not in free_asst]
                        bcost = 0
                        if head_out:
                            bcost += WEIGHTS["break_head_nodate"] if free_asst else WEIGHTS["break_head_and_asst"]
                        if team_nos:
                            bcost += WEIGHTS["break_team_nodate"]
                        broken[t, d, s] = {
                            "team": team.name, "date": d, "slot": s,
                            "head_out": head_out, "team_out": bool(team_nos), "free_asst": free_asst,
                            "busy_asst": busy_asst if head_out and not free_asst else [],
                            "note": "; ".join(r.note for r in team_nos + [r for h in head_out for r in rules_for[h]
                                                                          if r.kind == "no" and r.matches(d, s)]
                                              if r.note),
                        }
                        obj.append(bcost * y[t, d, s])

                # fixed costs of this particular practice
                cost = 0
                if d.weekday() >= 5:
                    cost += WEIGHTS["weekend"]
                    if team.weekend_pref and not any(w.matches(d, s) for w in team.weekend_pref):
                        cost += WEIGHTS["weekend_pref"]
                else:
                    if not (s == "3:45" and wants_345[t]):
                        cost += WEIGHTS["slot_fit"] * slot_cost(team.age, s)
                    cost += _wish_cost(data.wishes.get(team.name), DAYS[d.weekday()], s)
                cost += _not_preferred_cost([r for w_ in [team.name] + team.staff for r in rules_for[w_]
                                             if r.kind == "not_preferred" and r.matches(d, s)], s)
                need = [(c, programs_of(c, d, s)) for c in team.staff if c in coach_prog[d, s]]
                if need:
                    subs[t, d, s] = need
                    cost += WEIGHTS["locals_sub"] * len(need)
                if any(r.kind == "no" and r.matches(d, s) for a in team.assistants if a not in team.heads
                       for r in rules_for[a]):
                    cost += WEIGHTS["assistant_nodate"]
                if is_late_exception(team.age, s) and not wants_late[t]:
                    cost += WEIGHTS["late_exception"]
                if d in rest_days[team.name]:
                    tn, when = rest_days[team.name][d]
                    cost += WEIGHTS["tournament_rest"]
                    if when == "before" and s in ("7:20", "7PM", "5PM"):
                        cost += WEIGHTS["tournament_late_eve"]
                if cost:
                    obj.append(cost * y[t, d, s])

    def ys(t, d, s):
        return y.get((t, d, s), 0)

    def y_day(t, d):
        return sum(ys(t, d, s) for s in slots_on(d))

    multi_day = {(t, d) for (t, d, s) in pinned}  # days that can hold 2+ practices (hand-placed)
    day_on = {}

    def on_day(t, d):
        """1 if the team practices on d at all (a day with two practices counts once)."""
        if (t, d) not in multi_day:
            return y_day(t, d)
        if (t, d) not in day_on:
            vs = [y[t, d, s] for s in slots_on(d) if (t, d, s) in y]
            if not vs:
                return 0
            u = m.NewBoolVar(f"on_{t}_{d}")
            m.AddMaxEquality(u, vs)
            day_on[t, d] = u
        return day_on[t, d]

    # ---------------------------------------------------- hard rules --
    pair_vars = {}  # (d, s) -> teams over the court count (each one pairs 3 teams on 2 courts)
    for d in open_dates:
        for s in slots_on(d):
            vs = [y[t, d, s] for t in T if (t, d, s) in y]
            open_courts = courts_on(d, s)
            cap = len(open_courts)
            if len(vs) > cap:
                # Pairing: 3 teams can share 2 courts, at most one extra team
                # per 2 courts, and only as a last resort (WEIGHTS["pairing"]).
                ext = m.NewIntVar(0, min(cap // 2, len(vs) - cap), f"pair_{d}_{s}")
                m.Add(sum(vs) <= cap + ext)
                pair_vars[d, s] = ext
                obj.append(WEIGHTS["pairing"] * ext)
            # Court quality: the k-th team in a timeslot gets the k-th best open
            # court, so crowding a slot onto bad courts (Court 2) costs more
            # than moving a team to a time with a better court free.
            # On weekends this counts half: the club would rather fill one
            # block (4 teams 9-11) than split it (2 at 9-11, 2 at 11-1) when
            # there's room ("weekend_block").
            costs = sorted(COURT_COST[c] // (2 if d in data.weekends else 1) for c in open_courts)[:len(vs)]
            if len(vs) > 1 and any(costs):
                cum = [0]
                for c_ in costs:
                    cum.append(cum[-1] + c_)
                cum += [cum[-1]] * (len(vs) - len(costs))  # paired teams: the pairing cost covers them
                n = m.NewIntVar(0, len(vs), f"n_{d}_{s}")
                m.Add(n == sum(vs))
                cv = m.NewIntVar(0, cum[-1], f"cc_{d}_{s}")
                m.AddElement(n, cum, cv)
                obj.append(cv)

    heads_of, asst_of = defaultdict(list), defaultdict(list)
    for t in T:
        for h in teams[t].heads:
            heads_of[h].append(t)
        for a in teams[t].assistants:
            if a not in teams[t].heads:
                asst_of[a].append(t)

    asst_conflicts = {}
    for c in set(heads_of) | set(asst_of):
        for d in open_dates:
            for s in slots_on(d):
                H = [y[t, d, s] for t in heads_of[c] if (t, d, s) in y]
                A = [y[t, d, s] for t in asst_of[c] if (t, d, s) in y]
                busy = 1 if c in coach_tourn[d, s] else 0  # a program: see "locals_sub"
                if len(H) > 1:
                    m.AddAtMostOne(H)  # head coach: never two teams at once
                if A and (len(H) + len(A) + busy) > 1:
                    ov = m.NewIntVar(0, len(A), f"ov_{c}_{d}_{s}")
                    m.Add(ov >= sum(H) + sum(A) + busy - 1)
                    asst_conflicts[c, d, s] = ov
                    obj.append(WEIGHTS["assistant_conflict"] * ov)

    # One practice a day, as far as the solver's choices go. Practices placed
    # by hand can share a day with others at a different time: an Extra sits
    # beside whatever the team already has that day; a hand Add takes the
    # team's spot that day.
    for t in T:
        for d in open_dates:
            free = [y[t, d, s] for s in slots_on(d) if (t, d, s) in y and (t, d, s) not in pinned]
            held = any((t, d, s) in pinned and (t, d, s) not in extra_keys for s in slots_on(d))
            if held:
                for v in free:
                    m.Add(v == 0)
            elif len(free) > 1:
                m.AddAtMostOne(free)
        for ws, wdates in weeks.items():
            m.Add(sum(y_day(t, d) for d in wdates) <= teams[t].per_week + extras[t, ws])

    # Share of the period's weekdays each team is actually playing (season
    # start dates), used to weigh the weekly template.
    weekday_dates = [d for d in open_dates if d.weekday() < 5 and d not in data.weekends]
    active_frac = {t: (sum(1 for d in weekday_dates if not teams[t].start or d >= teams[t].start)
                       / len(weekday_dates)) if weekday_dates else 1.0
                   for t in T}

    # Weekday pattern: p[t, weekday, slot]; 2 different days.
    p = {(t, wd, s): m.NewBoolVar(f"p_{t}_{wd}_{s}") for t in T for wd in WEEKDAYS for s in WEEKDAY_KEYS
         if teams[t].allowed(s)}
    for t in T:
        # Soft so a team with no valid recurring slot can't make the model infeasible.
        n_usual = min(teams[t].per_week, len(WEEKDAYS))
        pat_short = m.NewIntVar(0, n_usual, f"patshort_{t}")
        m.Add(sum(v for (tt, _, _), v in p.items() if tt == t) + pat_short == n_usual)
        # A team that only starts at the very end of the range barely uses
        # the template this period, so a short template costs it less than
        # it would cost a team playing all month to lose its usual days.
        obj.append(max(300, round(3000 * active_frac[t])) * pat_short)
        for wd in WEEKDAYS:
            m.AddAtMostOne([p[t, wd, s] for s in WEEKDAY_KEYS if (t, wd, s) in p])

    # The template itself follows each team's wishlist, not-preferred days
    # and age/timeslot fit. Practices on real dates already carry these
    # costs; pricing the template too means a team with few dates in the
    # range (a late season start) still gets a usual week that matches
    # what it asked for, instead of whatever was left over.
    for (t, wd, s), v in p.items():
        team = teams[t]
        tc = 0
        if not (s == "3:45" and wants_345[t]):
            tc += WEIGHTS["slot_fit"] * slot_cost(team.age, s)
        tc += _wish_cost(data.wishes.get(team.name), wd, s)
        tc += _not_preferred_cost([r for who in [team.name] + team.staff for r in rules_for[who]
                                   if r.kind == "not_preferred" and not r.ranges and (not r.days or wd in r.days)
                                   and (not r.times or s in r.times)], s)
        if tc:
            obj.append(tc * v)

    # The template itself must be a valid week (it's what people read):
    # recurring 'no' rules and recurring locals rule slots out, head coaches
    # can't be in two places, and no more teams than courts.
    def recurring_local(lp, wd, s):
        """A program counts as part of the usual week if it runs on most of
        this weekday's dates in the period (so a program added for 11/2-12/18
        shapes November's template, but a one-week clinic doesn't)."""
        if lp.day != wd or not slots_overlap(lp.slot, s):
            return False
        if not lp.ranges:
            return True
        ds = [d for d in open_dates if DAYS[d.weekday()] == wd]
        return bool(ds) and sum(lp.runs_on(d) for d in ds) * 2 >= len(ds)

    def recurring_block(t, wd, s):
        team = teams[t]
        for who in [team.name] + team.heads:
            for r in rules_for[who]:
                if r.kind == "no" and not r.ranges and (not r.days or wd in r.days) and (not r.times or s in r.times):
                    return True
        for lp in data.locals:
            if recurring_local(lp, wd, s) and any(h in lp.coaches for h in team.heads):
                return True
        return False

    # Usual days set by hand are fixed in the template; the template's other
    # rules then apply to everyone else around them.
    forced = set()
    for name, entries in data.usual.items():
        t = idx[name]
        for wd, s in entries:
            if (t, wd, s) in p:
                forced.add((t, wd, s))
                m.Add(p[t, wd, s] == 1)
            else:
                data.warnings.append(f"Usual days for {name}: {s} isn't a time {teams[t].age}s can practice, "
                                     "so it was ignored")

    for (t, wd, s), v in p.items():
        if (t, wd, s) not in forced and recurring_block(t, wd, s):
            m.Add(v == 0)
    for wd in WEEKDAYS:
        for s in WEEKDAY_KEYS:
            cap = len([c for c in COURTS if not any(
                lp.on_site and recurring_local(lp, wd, s) and c in lp.courts
                for lp in data.locals)])
            n_forced = sum(1 for t in T if (t, wd, s) in forced)
            vs = [p[t, wd, s] for t in T if (t, wd, s) in p and (t, wd, s) not in forced]
            if len(vs) > max(0, cap - n_forced):
                m.Add(sum(vs) <= max(0, cap - n_forced))
    for t_list in [[t for t in T if h in teams[t].heads] for h in {h for tm in teams for h in tm.heads}]:
        if len(t_list) > 1:
            for wd in WEEKDAYS:
                for s in WEEKDAY_KEYS:
                    n_forced = sum(1 for t in t_list if (t, wd, s) in forced)
                    vs = [p[t, wd, s] for t in t_list if (t, wd, s) in p and (t, wd, s) not in forced]
                    if vs and n_forced >= 1:
                        for v in vs:
                            m.Add(v == 0)
                    elif len(vs) > 1:
                        m.AddAtMostOne(vs)

    # Weekend blocks: never one team alone; reward sharing blocks.
    block_used = {}
    for d in weekend_dates:
        for s in slots_on(d):
            vs = [y[t, d, s] for t in T if (t, d, s) in y]
            if not vs:
                continue
            if all((t, d, s) in pinned for t in T if (t, d, s) in y):
                continue  # only practices placed by hand (e.g. over a break): a coach's call, alone or not
            u = m.NewBoolVar(f"wb_{d}_{s}")
            m.Add(sum(vs) >= 2 * u)
            m.Add(sum(vs) <= len(vs) * u)
            block_used[d, s] = u
            obj.append(WEIGHTS["weekend_block"] * u)

    # Required weekends ("14th or 15th must be used").
    groups = defaultdict(list)
    for d in weekend_dates:
        wd = data.weekends[d]
        if wd.required:
            groups[wd.group or f"{d}"].append(d)
    group_unused = {}
    for g, gdates in groups.items():
        us = [u for (d, s), u in block_used.items() if d in gdates]
        miss = m.NewBoolVar(f"wreq_{g}")
        if us:
            m.Add(sum(us) >= 1 - miss)
        else:
            m.Add(miss == 1)
        group_unused[g] = (miss, gdates)
        obj.append(WEIGHTS["weekend_required"] * miss)

    # ----------------------------------------------- soft preferences --
    short_vars, off_vars, consec_vars = {}, {}, {}
    no_slot_weeks = []  # (t, ws): team has started but nothing is possible that week
    for t in T:
        team = teams[t]
        for ws, wdates in weeks.items():
            week_goal = week_target(ws, team.per_week)
            if not week_goal:
                continue
            if team.start and team.start > ws + timedelta(days=6):
                continue
            # A team whose season starts mid-week still gets the full two
            # practices, as long as at least two open days are left that week.
            days_left = sum(1 for d in wdates if not team.start or d >= team.start)
            start_cap = min(team.per_week, days_left)
            if not any((t, d, s) in y for d in wdates for s in slots_on(d)):
                if start_cap:
                    no_slot_weeks.append((t, ws, min(week_goal, start_cap)))
                continue
            target = min(week_goal, start_cap)
            cancelled = sum(data.cancels.get((team.name, ws + timedelta(days=i)), 0) for i in range(7))
            target = max(0, target - cancelled)
            if target == 0:
                continue
            w = WEIGHTS["missed_practice"] * team.priority
            if ws in lead_in_weeks[team.name]:  # get both practices in before a tournament
                w = int(w * 1.5)
            # short_k = k or more practices missed. Missing the 1st practice of
            # a week costs 4x missing the 2nd, so shortfalls get spread; a 3rd+
            # practice (a team that asked for more) is a bonus.
            terms = []
            for k in range(1, target + 1):
                sk = m.NewBoolVar(f"short{k}_{t}_{ws}")
                if terms:
                    m.Add(sk <= terms[-1])
                terms.append(sk)
                lost = target - k + 1  # which practice of the week this one is
                obj.append((WEIGHTS["missed_extra"] if lost > 2 else w if (lost == 2 or target == 1) else 4 * w) * sk)
            # Extra practices placed by hand sit on top: they don't count toward the target.
            m.Add(sum(terms) >= target - (sum(y_day(t, d) for d in wdates) - extras[t, ws]))
            short_vars[t, ws] = (terms, target)

        for d in open_dates:
            if d.weekday() >= 5 or d in data.weekends or (team.start and d < team.start):
                continue  # weekends and block days (breaks) aren't part of the usual week
            wd = DAYS[d.weekday()]
            for s in WEEKDAY_KEYS:
                if (t, wd, s) not in p:
                    continue
                v = ys(t, d, s)
                # Leaving a usual slot the team didn't ask for, for one it did,
                # is what coaches want, so it's nearly free. Teams already on
                # their requested slots stay just as steady.
                if (t, d, s) in y:
                    off = m.NewBoolVar(f"off_{t}_{d}_{s}")
                    m.Add(off >= v - p[t, wd, s])
                    off_vars[t, d, s] = off
                    obj.append((WEIGHTS["move_to_wish"] if wished(t, d, s) else WEIGHTS["off_pattern"]) * off)
                # Skipping a usual slot costs something only when the team could
                # normally practice there that date. A one-off block (a no-date,
                # a tournament or its rest day, a local) isn't the usual week's
                # fault: the makeup already pays the off-pattern cost, and
                # charging the template too pushes the usual week onto a day the
                # team then leaves almost every week.
                if (t, d, s) not in y or (t, d, s) in broken or d in rest_days[team.name]:
                    continue
                miss = m.NewBoolVar(f"miss_{t}_{d}_{s}")
                m.Add(miss >= p[t, wd, s] - v)
                obj.append((WEIGHTS["move_to_wish"] if unwanted(t, d, s) else WEIGHTS["off_pattern"] * 2) * miss)

        # Rotate 3:45: a usual week with both practices at 3:45 costs extra
        # (once per week in the range), so the early slot is shared around
        # the young teams. Skipped for teams that asked for 3:45.
        early = [p[t, wd, "3:45"] for wd in WEEKDAYS if (t, wd, "3:45") in p]
        if not wants_345[t] and len(early) > 1:
            both = m.NewBoolVar(f"both345_{t}")
            m.Add(sum(early) - 1 <= both)
            obj.append(WEIGHTS["second_345"] * len(weeks) * both)

        # Only one weekend practice a week when possible.
        for ws, wdates in weeks.items():
            wk = [y[t, d, s] for d in wdates if d.weekday() >= 5 for s in slots_on(d) if (t, d, s) in y]
            if len(wk) > 1:
                extra = m.NewBoolVar(f"wk2_{t}_{ws}")
                m.Add(sum(wk) - 1 <= extra)
                obj.append(WEIGHTS["second_weekend"] * extra)

        # Back-to-back weekdays the team asked for (a 1st-choice wish naming
        # both days, e.g. "Tue, Wed") cost nothing and aren't flagged.
        wished_pairs = {(w.days[i], w.days[j]) for w in data.wishes.get(team.name, []) if w.choice == 1
                        for i in range(len(w.days)) for j in range(len(w.days))}
        for d in open_dates:
            nxt = d + timedelta(days=1)
            if nxt in open_dates:  # back-to-back days, including Sat -> Sun
                if d.weekday() < 4 and (DAYS[d.weekday()], DAYS[nxt.weekday()]) in wished_pairs:
                    continue
                a, b = on_day(t, d), on_day(t, nxt)
                if isinstance(a, int) or isinstance(b, int):
                    continue
                cons = m.NewBoolVar(f"cons_{t}_{d}")
                m.Add(cons >= a + b - 1)
                consec_vars[t, d] = cons
                obj.append((WEIGHTS["consecutive_days"] + (WEIGHTS["sat_sun"] if d.weekday() == 5 else 0)) * cons)

        used = []
        for s in WEEKDAY_KEYS:
            ps = [p[t, wd, s] for wd in WEEKDAYS if (t, wd, s) in p]
            if not ps:
                continue
            u = m.NewBoolVar(f"slotused_{t}_{s}")
            for v in ps:
                m.Add(u >= v)
            used.append(u)
        obj.append(WEIGHTS["split_timeslot"] * sum(used))

    # Double-ups: a coach's teams back-to-back on the same day.
    pairs = set()
    for c in set(heads_of) | set(asst_of):
        ts = sorted(set(heads_of[c]) | set(asst_of[c]))
        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                pairs.add((ts[i], ts[j]))
    doubles = []
    for t1, t2 in sorted(pairs):
        for d in open_dates:
            sl = slots_on(d)
            for k in range(len(sl) - 1):
                s_a, s_b = sl[k], sl[k + 1]
                for a, b in ((t1, t2), (t2, t1)):
                    if (a, d, s_a) in y and (b, d, s_b) in y:
                        bv = m.NewBoolVar(f"dbl_{a}_{b}_{d}_{s_a}")
                        m.Add(bv <= y[a, d, s_a])
                        m.Add(bv <= y[b, d, s_b])
                        doubles.append(bv)
                        obj.append(-WEIGHTS["double_up"] * bv)

    # High school boys at 5:30 and 7:20 on the same courts (no net change).
    # Courts are picked in stage 2; here, only the net changes no court
    # arrangement could avoid are priced: e.g. 3 HS boys teams at 5:30 and
    # one at 7:20 with every other court full of girls teams means 2 changes.
    hs_boys = {t for t in T if is_hs_boys(teams[t].club, teams[t].age)}
    net_vars = []
    if hs_boys:
        for d in open_dates:
            if d.weekday() >= 5:
                continue
            ys_a = [(t, y[t, d, "5:30"]) for t in T if (t, d, "5:30") in y]
            ys_b = [(t, y[t, d, "7:20"]) for t in T if (t, d, "7:20") in y]
            if not any(t in hs_boys for t, _ in ys_a + ys_b) or not ys_a or not ys_b:
                continue
            B_a = sum(v for t, v in ys_a if t in hs_boys)
            B_b = sum(v for t, v in ys_b if t in hs_boys)
            N_a, N_b = sum(v for _, v in ys_a), sum(v for _, v in ys_b)
            cap_a, cap_b = len(courts_on(d, "5:30")), len(courts_on(d, "7:20"))
            nc = m.NewIntVar(0, len(COURTS), f"net_{d}")
            m.Add(nc >= B_a - B_b - (cap_b - N_b))
            m.Add(nc >= B_b - B_a - (cap_a - N_a))
            net_vars.append(nc)
            obj.append(WEIGHTS["net_change"] * nc)

    # ------------------------------------------- stability (previous) --
    prev_keys, prev_court = set(), {}
    if previous is not None and len(previous):
        pv = previous[previous["kind"] == "Practice"] if "kind" in previous else previous
        for r in pv.itertuples():
            if r.name in idx and start <= r.date <= end:
                k = (idx[r.name], r.date, r.slot)
                prev_keys.add(k)
                if pd.notna(getattr(r, "court", None)):
                    prev_court[k] = int(r.court)
        for k, v in y.items():
            if k in prev_keys:
                obj.append(-WEIGHTS["keep_previous"] * v)
            m.AddHint(v, 1 if k in prev_keys else 0)
    # A practice the solver puts on a day that already has a hand-placed
    # extra costs a little, so an extra doesn't pull a second practice onto
    # its day. One that was already there stays (adding an extra never bumps it).
    for (t, d, s), v in y.items():
        if (t, d, s) not in pinned and (t, d, s) not in prev_keys and any(
                (t, d, x) in extra_keys for x in slots_on(d) if x != s):
            obj.append(WEIGHTS["same_day_as_extra"] * v)
    if previous_pattern is not None and len(previous_pattern):
        pp = {(idx[r.team], r.day, r.slot) for r in previous_pattern.itertuples() if r.team in idx}
        for k, v in p.items():
            m.AddHint(v, 1 if k in pp else 0)

    m.Minimize(sum(obj))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = seed
    status = solver.Solve(m)
    status_name = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return ScheduleResult(status_name, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                              {"solve_seconds": round(time.time() - t0, 1)}, data.warnings, start, end,
                              weekend_dates)

    booked = [(t, d, s) for (t, d, s), v in y.items() if solver.Value(v)]
    pattern_slots = [(t, wd, s) for (t, wd, s), v in p.items() if solver.Value(v)]

    # ---------------------------------------------- stage 2: courts --
    pw = pecking_weights(teams)
    weights = {t: pw[teams[t].name] for t in T}
    fixed_home = {idx[n]: c for n, c in data.usual_court.items()}
    # A coach with back-to-back sessions stays on one court where possible:
    # two of their teams in a row, or a program they work then a team.
    booked_at = defaultdict(list)
    for (t, d, s) in booked:
        booked_at[d, s].append(t)
    coach_links, local_links = [], []
    for (d, s_a), ts_a in booked_at.items():
        for s_b in slots_on(d):
            if not back_to_back(s_a, s_b) or (d, s_b) not in booked_at:
                continue
            for t1 in ts_a:
                for t2 in booked_at[d, s_b]:
                    if t1 != t2 and set(teams[t1].staff) & set(teams[t2].staff):
                        coach_links.append(((t1, d, s_a), (t2, d, s_b)))
    for (t, d, s) in booked:
        for lp in data.locals:
            if not (lp.on_site and lp.runs_on(d) and set(lp.coaches) & set(teams[t].staff)):
                continue
            if back_to_back(lp.slot, s) or back_to_back(s, lp.slot):
                cs = [c for c in lp.courts if c in COURTS]
                if cs:
                    local_links.append(((t, d, s), cs))
    home_court, court_of, doubled = _assign_courts(
        booked, T, courts_on, weights, seed,
        fixed_courts={k: c for k, c in pinned.items() if c is not None},
        fixed_home=fixed_home, prev_court=prev_court, hs_boys=hs_boys,
        coach_links=coach_links, local_links=local_links)
    paired = _pair_groups(booked, court_of, doubled, courts_on, teams)
    elapsed = time.time() - t0

    rows = []
    for (t, d, s) in booked:
        team = teams[t]
        if (t, d, s) in off_vars:
            on_pattern = not solver.Value(off_vars[t, d, s])
        else:
            # A break's block days are their own schedule, not makeups.
            on_pattern = d.weekday() < 5
        rows.append({
            "date": d, "day": DAYS[d.weekday()], "slot": s, "slot_label": SLOTS[s],
            "court": court_of[t, d, s], "kind": "Practice", "name": team.name, "club": team.club,
            "age": team.age, "coach": "; ".join(team.staff), "head": "; ".join(team.heads),
            "on_pattern": on_pattern,
            "pinned": (t, d, s) in pinned and (t, d, s) not in restored,
            "extra": (t, d, s) in extra_keys,
            "paired": paired.get((t, d, s), ""),
        })
    # Locals show on every date they run (even weekend days not open for
    # practices), so the calendar fills in as soon as a program is added.
    for d in all_days:
        if d in data.closures:
            continue
        for lp in data.locals:
            s = lp.slot
            if not lp.on_site or not lp.active(d, s):
                continue
            for c in lp.courts:
                rows.append({
                    "date": d, "day": DAYS[d.weekday()], "slot": s, "slot_label": SLOTS[s],
                    "court": c, "kind": "Local", "name": lp.name, "club": "", "age": None,
                    "coach": "; ".join(lp.coaches), "head": "; ".join(lp.coaches), "on_pattern": True,
                    "side": lp.side,
                })
    assignments = pd.DataFrame(rows)
    if not assignments.empty:
        assignments["o"] = assignments["slot"].map(SLOT_KEYS.index)
        assignments = assignments.sort_values(["date", "o", "court"]).drop(columns="o").reset_index(drop=True)

    # Courts for the usual week, so it reads as a real week (one team per
    # court): each team keeps the court it actually gets most weeks on that
    # day and time, else its home court, else the best one left. Higher teams
    # in the pecking order choose first.
    usual_court_of = {}
    for (wd, s) in {(wd, s) for (_, wd, s) in pattern_slots}:
        free = [c for c in COURT_RANK if not any(
            lp.on_site and recurring_local(lp, wd, s) and c in lp.courts for lp in data.locals)]
        for t in sorted((t for (t, w, ss) in pattern_slots if (w, ss) == (wd, s)), key=lambda t: -weights[t]):
            seen = Counter(court_of[t, d, s] for (tt, d, ss) in booked
                           if tt == t and ss == s and DAYS[d.weekday()] == wd)
            prefs = [c for c, _ in seen.most_common()] + [home_court[t]] + COURT_RANK
            c = next((c for c in prefs if c in free), home_court[t])
            if c in free:
                free.remove(c)
            usual_court_of[t, wd, s] = c

    pattern = pd.DataFrame([
        {"team": teams[t].name, "club": teams[t].club, "age": teams[t].age,
         "coach": "; ".join(teams[t].staff), "head": "; ".join(teams[t].heads),
         "day": wd, "slot": s, "slot_label": SLOTS[s], "court": usual_court_of[t, wd, s],
         "home_court": home_court[t]}
        for (t, wd, s) in sorted(pattern_slots, key=lambda k: (k[0], WEEKDAYS.index(k[1])))
    ])

    issues = _issues(data, teams, solver, short_vars, off_vars, consec_vars, asst_conflicts,
                     group_unused, assignments, pattern_slots, rules_for, coach_local, heads_of, asst_of,
                     no_slot_weeks)
    wish_rows = _wish_issues(data, teams, pattern_slots, rules_for, week_start(start))
    if wish_rows:
        issues = pd.concat([issues, pd.DataFrame(wish_rows, columns=issues.columns)], ignore_index=True)

    practices = assignments[assignments["kind"] == "Practice"] if len(assignments) else assignments
    weekday_pr = (practices[practices["date"].map(lambda d: d.weekday() < 5 and d not in data.weekends)]
                  if len(practices) else practices)
    n_weekend = int(practices["date"].map(lambda d: d.weekday() >= 5).sum()) if len(practices) else 0
    wish_hits = wish_total = 0
    for _, r in weekday_pr.iterrows():
        ws_ = data.wishes.get(r["name"])
        if ws_:
            wish_total += 1
            wish_hits += any(w.choice == 1 and w.matches(r["date"], r["slot"]) for w in ws_)
    metrics = {
        "status": status_name,
        "solve_seconds": round(elapsed, 1),
        "teams": len(teams),
        "practices_scheduled": int(len(practices)),
        "practices_required": int(sum(tg for (_, tg) in short_vars.values()))
        + sum(goal for _, _, goal in no_slot_weeks),
        "missed_practices": int(sum(solver.Value(v) for (terms, _) in short_vars.values() for v in terms))
        + sum(goal for _, _, goal in no_slot_weeks),
        "weekend_practices": n_weekend,
        "on_pattern_pct": round(float(100 * weekday_pr["on_pattern"].mean()), 1) if len(weekday_pr) else None,
        "wishlist_pct": round(100 * wish_hits / wish_total, 1) if wish_total else None,
        "practices_at_3_45": int((practices["slot"] == "3:45").sum()) if len(practices) else 0,
        "double_ups": int(sum(solver.Value(v) for v in doubles)),
        "assistant_conflicts": int(sum(solver.Value(v) for v in asst_conflicts.values())),
        "paired_slots": len({(d, s, lab) for (_, d, s), lab in paired.items()}),
        "net_changes": _count_net_changes(booked, court_of, hs_boys),
        "objective": solver.ObjectiveValue(),
        "moved_from_previous": (len(prev_keys - set(booked)) if prev_keys else None),
    }
    shown_weekends = sorted(set(weekend_dates) | ({d for d in assignments["date"] if d.weekday() >= 5}
                                                  if len(assignments) else set()))

    # Practices each team got vs. its target, week by week (for the count
    # grid and the "short of practices" alerts).
    targets = {(teams[t].name, ws): tg for (t, ws), (_, tg) in short_vars.items()}
    targets.update({(teams[t].name, ws): g for t, ws, g in no_slot_weeks})
    got = practices.groupby(["name", practices["date"].map(week_start)]).size().to_dict() if len(practices) else {}
    practice_counts = pd.DataFrame([
        {"team": tm.name, "week_of": ws, "target": targets.get((tm.name, ws), 0),
         "scheduled": int(got.get((tm.name, ws), 0)),
         "short": max(0, targets.get((tm.name, ws), 0) - int(got.get((tm.name, ws), 0)))}
        for tm in teams for ws in sorted(weeks)
    ])
    broken_rows = [dict(info, slot_label=SLOTS[info["slot"]], who=_broken_who(info))
                   for k, info in sorted(broken.items(), key=lambda kv: (kv[1]["date"], kv[1]["team"]))
                   if k in set(booked)]
    for b in broken_rows:
        issues = pd.concat([pd.DataFrame([{
            "severity": "High", "team": b["team"], "week_of": week_start(b["date"]),
            "issue": f"No-date broken: {b['date']:%a %-m/%-d} {b['slot']}",
            "detail": f"{b['who']}; it was the only way to get 2 practices that week"
                      + (f" ({b['note']})" if b["note"] else "")}]), issues], ignore_index=True)
    for n in restore_notes:
        if n["kept"]:
            issues = pd.concat([pd.DataFrame([{
                "severity": "Medium", "team": n["team"], "week_of": week_start(n["date"]),
                "issue": f"Restored: {n['date']:%a %-m/%-d} {n['slot']}",
                "detail": f"Kept from the restored version even though {n['why']}"}]), issues], ignore_index=True)
    booked_set = set(booked)
    for (t, d, s), need in sorted(subs.items(), key=lambda kv: (kv[0][1], teams[kv[0][0]].name)):
        if (t, d, s) not in booked_set:
            continue
        issues = pd.concat([pd.DataFrame([{
            "severity": "Medium", "team": teams[t].name, "week_of": week_start(d),
            "issue": f"Needs a sub: {d:%a %-m/%-d} {s}",
            "detail": "Possible if " + " and ".join(f"{c} gets a sub for {' / '.join(ps)}" for c, ps in need)}]),
            issues], ignore_index=True)
    seen_pairs = set()
    for (_t, d, s), label in sorted(paired.items(), key=lambda kv: (kv[0][1], SLOT_KEYS.index(kv[0][2]), kv[1]),
                                   reverse=True):  # prepended one by one, so they end up in date order
        if (d, s, label) in seen_pairs:
            continue
        seen_pairs.add((d, s, label))
        names = label.split(": ", 1)[-1]
        issues = pd.concat([pd.DataFrame([{
            "severity": "High", "team": names, "week_of": week_start(d),
            "issue": f"Paired: {d:%a %-m/%-d} {s}",
            "detail": label.split(": ", 1)[0] + "; every court was taken, and this was the only way to keep these "
                      "practices without breaking a no-date"}]), issues], ignore_index=True)
    for c in tournament_conflicts(teams, data.tournaments):
        issues = pd.concat([pd.DataFrame([{
            "severity": "High", "team": ", ".join(c["teams"]), "week_of": week_start(c["start"]),
            "issue": f"Tournament conflict: {c['coach']} {span_text(c['start'], c['end'])}",
            "detail": (f"{c['coach']} is working {c['local']} (local) and coaching {', '.join(c['teams'])} at "
                       f"{c['travel']} (travel) at the same time") if c["kind"] == "local_travel" else
                      (f"{c['coach']} coaches {', '.join(c['teams'])} at {c['travel']} and "
                       f"{', '.join(c['teams2'])} at {c['travel2']} at the same time")}]), issues],
            ignore_index=True)
    warnings = list(data.warnings) + [n["text"] for n in restore_notes if not n["kept"]]
    return ScheduleResult(status_name, assignments, pattern, issues, metrics, warnings, start, end,
                          shown_weekends, data.tournaments, practice_counts, broken_rows)


def _restore_pins(restore, pinned, teams, idx, data, start, end, slots_on, courts_on, coach_busy, rules_for, away,
                  extras=None):
    """Adds a published version's practices to `pinned` (in place).

    Hand-placed practices win. A practice that can't happen now is dropped
    with a note; one that can but breaks a rule (a no-date, the team's age
    slots...) is kept, since restoring means bringing the version back as it
    was, and flagged so it's easy to move.
    Returns (keys restored, notes)."""
    restored: set[tuple] = set()
    notes: list[dict] = []
    if restore is None or not len(restore):
        return restored, notes
    rv = restore[restore["kind"] == "Practice"] if "kind" in restore else restore
    taken = defaultdict(set)          # (d, s) -> courts already placed
    count = defaultdict(int)          # (d, s) -> practices already placed
    heads_at = defaultdict(set)       # (d, s) -> head coaches already placed
    team_days, per_week = set(), defaultdict(int)   # team_days: (t, d, s) already placed
    for (t, d, s), c in pinned.items():
        count[d, s] += 1
        heads_at[d, s].update(teams[t].heads)
        team_days.add((t, d, s))
        per_week[t, week_start(d)] += 1
        if c is not None:
            taken[d, s].add(c)
    outside, gone = 0, set()

    def drop(name, d, s, why):
        notes.append({"kept": False, "team": name, "date": d, "slot": s,
                      "text": f"Not restored: {name} on {d:%a %-m/%-d} at {s}, because {why}"})

    # A version with extra practices (a 3rd that week) brings them back too.
    in_version = Counter((str(r.name), week_start(r.date)) for r in rv.itertuples())
    extras = extras if extras is not None else defaultdict(int)
    order = sorted(rv.itertuples(), key=lambda r: (r.date, SLOT_KEYS.index(r.slot) if r.slot in SLOT_KEYS else 99))
    for r in order:
        name, d, s = str(r.name), r.date, r.slot
        if name not in idx:
            gone.add(name)
            continue
        if not (start <= d <= end):
            outside += 1
            continue
        t, team = idx[name], teams[idx[name]]
        c = getattr(r, "court", None)
        c = None if c is None or pd.isna(c) else int(c)
        if d in data.closures:
            drop(name, d, s, f"the club is closed ({data.closures[d] or 'closure'})")
        elif d.weekday() >= 5 and (d not in data.weekends or s not in slots_on(d)):
            drop(name, d, s, "that weekend time isn't open")
        elif s not in slots_on(d):
            drop(name, d, s, "that time isn't open")
        elif (t, d, s) in team_days:
            drop(name, d, s, "the team already has a practice placed by hand then")
        elif (per_week[t, week_start(d)]
              >= max(team.per_week, in_version[name, week_start(d)]) + extras[t, week_start(d)]):
            drop(name, d, s, f"the team already has {per_week[t, week_start(d)]} practices placed that week")
        elif count[d, s] >= len(courts_on(d, s)):
            drop(name, d, s, "every court is taken then")
        elif busy := sorted(set(team.heads) & (heads_at[d, s] | coach_busy[d, s])):
            drop(name, d, s, f"{' and '.join(busy)} is coaching something else then")
        else:
            if c is not None and (c not in courts_on(d, s) or c in taken[d, s]):
                c = None  # its court is taken now: any free court will do
            pinned[t, d, s] = c
            restored.add((t, d, s))
            count[d, s] += 1
            heads_at[d, s].update(team.heads)
            team_days.add((t, d, s))
            per_week[t, week_start(d)] += 1
            if c is not None:
                taken[d, s].add(c)
            why = []
            if not team.allowed(s):
                why.append(f"{team.age}s don't normally use {s}")
            if team.start and d < team.start:
                why.append(f"the team's season starts {team.start:%-m/%-d}")
            if d in away[name]:
                why.append("the team is at a tournament")
            if any(rr.kind == "no" and rr.matches(d, s) for rr in rules_for[name]):
                why.append("it's a team no-date")
            out = [h for h in team.heads if any(rr.kind == "no" and rr.matches(d, s) for rr in rules_for[h])]
            if out:
                why.append(f"head coach {' and '.join(out)} can't make it")
            if why:
                notes.append({"kept": True, "team": name, "date": d, "slot": s, "why": "; ".join(why)})
    if outside:
        notes.append({"kept": False, "team": "", "date": start, "slot": "",
                      "text": f"{outside} practice{'s' if outside != 1 else ''} in the restored version fall outside "
                              f"this schedule's dates and weren't restored"})
    for name in sorted(gone):
        notes.append({"kept": False, "team": name, "date": start, "slot": "",
                      "text": f"Not restored: {name}'s practices, because {name} isn't on the Teams sheet any more"})
    return restored, notes


def _count_net_changes(booked, court_of, hs_boys) -> int:
    """Courts that switch between HS boys and anyone else from 5:30 to 7:20."""
    at = defaultdict(set)  # (d, c, slot) -> {is HS boys}
    for (t, d, s) in booked:
        if s in ("5:30", "7:20"):
            at[d, court_of[t, d, s], s].add(t in hs_boys)
    n = 0
    for (d, c, s), kinds in at.items():
        if s == "5:30" and (d, c, "7:20") in at and kinds != at[d, c, "7:20"] and True in kinds | at[d, c, "7:20"]:
            n += 1
    return n


def _wish_cost(wishes, wd: str, s: str) -> int:
    """Cost of a practice on weekday wd at slot s against a team's wishlist:
    a 1st-choice match is free, a 2nd-choice one costs a little; otherwise
    a day the wishlist doesn't name costs more than a time it doesn't."""
    if not wishes:
        return 0
    full = [w.choice for w in wishes if (not w.days or wd in w.days) and (not w.times or s in w.times)]
    if full:
        return 0 if min(full) == 1 else WEIGHTS["wish_second"]
    on_day = [w for w in wishes if not w.days or wd in w.days]
    if on_day:
        return WEIGHTS["wish_time_miss"]  # right day, wrong time
    cost = WEIGHTS["wish_day_miss"]
    if not any(not w.times or s in w.times for w in wishes):
        cost += WEIGHTS["wish_time_miss"]
    return cost


def _not_preferred_cost(hits, s: str) -> int:
    """Not-preferred rules matching a practice: a whole day marked not
    preferred costs more than a time."""
    cost = 0
    if any(not r.times for r in hits):
        cost += WEIGHTS["not_preferred_day"]
    if any(r.times for r in hits):
        cost += WEIGHTS["not_preferred"] + (WEIGHTS["not_preferred_345"] if s == "3:45" else 0)
    return cost


def _broken_who(b: dict) -> str:
    """Plain-language description of whose no-date a practice breaks."""
    parts = []
    if b["head_out"]:
        heads = " and ".join(b["head_out"])
        if b["free_asst"]:
            parts.append(f"head coach {heads} is out, so {' or '.join(b['free_asst'])} runs it")
        elif b["busy_asst"]:
            parts.append(f"head coach {heads} and assistant {' and '.join(b['busy_asst'])} are both out")
        else:
            parts.append(f"head coach {heads} is out and there's no assistant")
    if b["team_out"]:
        parts.append("the team marked this a no-date")
    return "; ".join(parts)


def _assign_courts(booked, T, courts_on, weights, seed=0, time_limit_s=10.0, fixed_courts=None, fixed_home=None,
                   prev_court=None, hs_boys=(), coach_links=(), local_links=()):
    """Stage 2: put each booked practice on an open court. Better courts go
    to older and 1s teams (court quality x team weight), teams stay on a
    home court where they can, HS boys at 5:30 and 7:20 share courts (no
    net change), and a coach with back-to-back sessions stays put.

    A timeslot with more teams than courts (pairing, only when stage 1 had
    to) puts two teams on some courts; each of those is later grouped with
    a neighboring court as "3 teams on 2 courts".
    Returns (home_court, court_of, doubled) where doubled is the set of
    (d, s, c) holding two teams."""
    m = cp_model.CpModel()
    x, home = {}, {}
    for t in T:
        for c in COURTS:
            home[t, c] = m.NewBoolVar(f"h_{t}_{c}")
        m.AddExactlyOne([home[t, c] for c in COURTS])
        if fixed_home and t in fixed_home:
            m.Add(home[t, fixed_home[t]] == 1)
    by_slot = defaultdict(list)
    n_at = Counter((d, s) for (_, d, s) in booked)
    changes = []
    for (t, d, s) in booked:
        opts = courts_on(d, s)
        for c in opts:
            x[t, d, s, c] = m.NewBoolVar(f"x_{t}_{d}_{s}_{c}")
            by_slot[d, s, c].append(x[t, d, s, c])
            ch = m.NewBoolVar(f"ch_{t}_{d}_{s}_{c}")
            m.Add(ch >= x[t, d, s, c] - home[t, c])
            changes.append(WEIGHTS["home_court"] * ch)
            if COURT_COST[c]:
                changes.append(COURT_COST[c] * weights[t] * x[t, d, s, c])
        m.AddExactlyOne([x[t, d, s, c] for c in opts])
        pc = (prev_court or {}).get((t, d, s))
        if pc is not None and (t, d, s, pc) in x:
            changes.append(-WEIGHTS["keep_court"] * x[t, d, s, pc])
            for c in opts:
                m.AddHint(x[t, d, s, c], 1 if c == pc else 0)
        if fixed_courts and (t, d, s) in fixed_courts and (t, d, s, fixed_courts[t, d, s]) in x:
            m.Add(x[t, d, s, fixed_courts[t, d, s]] == 1)
    dbl = {}
    for (d, s), n in n_at.items():
        cap = len(courts_on(d, s))
        extra = n - cap
        for c in courts_on(d, s):
            vs = by_slot.get((d, s, c), [])
            if extra > 0:
                dbl[d, s, c] = m.NewBoolVar(f"dbl_{d}_{s}_{c}")
                m.Add(sum(vs) <= 1 + dbl[d, s, c])
            elif len(vs) > 1:
                m.AddAtMostOne(vs)
        if extra > 0:
            m.Add(sum(dbl[d, s, c] for c in courts_on(d, s)) == extra)

    # HS boys at 5:30 and 7:20 on the same court: a court with HS boys in one
    # slot and anyone else in the other needs its net changed.
    if hs_boys:
        for d in sorted({d for (_, d, _) in booked if d.weekday() < 5}):
            for c in COURTS:
                a_b = [x[t, d, "5:30", c] for t in T if (t, d, "5:30", c) in x and t in hs_boys]
                a_o = [x[t, d, "5:30", c] for t in T if (t, d, "5:30", c) in x and t not in hs_boys]
                b_b = [x[t, d, "7:20", c] for t in T if (t, d, "7:20", c) in x and t in hs_boys]
                b_o = [x[t, d, "7:20", c] for t in T if (t, d, "7:20", c) in x and t not in hs_boys]
                for p_, q_ in ((a_b, b_o), (b_b, a_o)):
                    if p_ and q_:
                        mm = m.NewIntVar(0, 3, f"net_{d}_{c}_{len(changes)}")
                        m.Add(mm >= sum(p_) + sum(q_) - 1)
                        changes.append(WEIGHTS["net_change_court"] * mm)

    # A coach's back-to-back sessions on the same court.
    for (k1, k2) in coach_links:
        for c in COURTS:
            if (*k1, c) in x and (*k2, c) in x:
                same = m.NewBoolVar(f"stay_{len(changes)}")
                m.Add(same <= x[(*k1, c)])
                m.Add(same <= x[(*k2, c)])
                changes.append(-WEIGHTS["coach_stay"] * same)
    for k, cs in local_links:
        on = [x[(*k, c)] for c in cs if (*k, c) in x]
        if on:
            stay = m.NewBoolVar(f"lstay_{len(changes)}")
            m.Add(stay <= sum(on))
            changes.append(-WEIGHTS["coach_stay"] * stay)

    m.Minimize(sum(changes))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = seed
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError("Court assignment failed, which shouldn't happen if stage 1 respected capacity.")
    home_court = {t: next(c for c in COURTS if solver.Value(home[t, c])) for t in T}
    court_of = {(t, d, s): c for (t, d, s, c), v in x.items() if solver.Value(v)}
    doubled = {k for k, v in dbl.items() if solver.Value(v)}
    return home_court, court_of, doubled


def _pair_groups(booked, court_of, doubled, courts_on, teams) -> dict:
    """Groups each court holding two teams with its nearest single-team
    court: 3 teams on 2 courts. Returns {(t, d, s): label}."""
    out = {}
    by = defaultdict(lambda: defaultdict(list))  # (d, s) -> court -> [t]
    for (t, d, s) in booked:
        by[d, s][court_of[t, d, s]].append(t)
    for (d, s), courts in by.items():
        two = sorted(c for c in courts if (d, s, c) in doubled)
        if not two:
            continue
        free = sorted(c for c in courts if len(courts[c]) == 1)
        # Partner courts as close as possible overall (5 & 4, 7 & 6 beats 5 & 4, 7 & ... 2).
        best = min(permutations(free, len(two)), key=lambda ps: (sum(abs(p - c) for p, c in zip(ps, two)), ps),
                   default=None) if len(free) >= len(two) else None
        for i, c in enumerate(two):
            partner = best[i] if best else None
            group = [c] if partner is None else sorted([c, partner])
            names = [teams[t].name for g in group for t in courts[g]]
            label = (f"3 teams on {' & '.join(court_name(g) for g in group)}: " if len(group) == 2
                     else f"2 teams on {court_name(c)}: ") + ", ".join(names)
            for g in group:
                for t in courts[g]:
                    out[t, d, s] = label
    return out


def _issues(data, teams, solver, short_vars, off_vars, consec_vars, asst_conflicts, group_unused,
            assignments, pattern_slots, rules_for, coach_local, heads_of, asst_of,
            no_slot_weeks=()) -> pd.DataFrame:
    """Plain-language list of everything that isn't the 'ideal' schedule."""
    out = []
    usual = defaultdict(list)
    for (t, wd, sl) in pattern_slots:
        usual[t].append((wd, sl))

    def why(t, ws):
        team = teams[t]
        reasons = []
        for wd, sl in sorted(usual[t], key=lambda k: WEEKDAYS.index(k[0])):
            d = day_in_week(ws, wd)
            if team.start and d < team.start:
                continue
            if d in data.closures:
                reasons.append(f"usual {wd} {sl} falls on {data.closures[d] or 'a closure'} ({d:%-m/%-d})")
                continue
            tn_here = [tn for tn in data.tournaments if tn.covers(d) and
                       ((tn.kind == "travel" and team.name in tn.teams)
                        or (tn.kind == "local" and set(COURTS) <= set(tn.courts) and tn.blocks(d, sl)))]
            if tn_here:
                tn = tn_here[0]
                why = "away at" if tn.kind == "travel" else "courts taken by"
                reasons.append(f"{why} {tn.name} on usual {wd} {d:%-m/%-d}")
                continue
            near = [tn for tn in data.tournaments if tn.kind == "travel" and team.name in tn.teams and
                    d in (tn.start - timedelta(days=1), tn.end + timedelta(days=1))]
            if near:
                tn = near[0]
                reasons.append(f"usual {wd} {d:%-m/%-d} is the day {'before' if d < tn.start else 'after'} {tn.name}")
                continue
            for who in [team.name] + team.heads:
                hit = [r for r in rules_for[who] if r.kind == "no" and r.matches(d, sl)]
                if hit:
                    reasons.append(f"{who} unavailable on usual {wd} {d:%-m/%-d}"
                                   + (f" ({hit[0].note})" if hit[0].note else ""))
                    break
            else:
                busy = [h for h in team.heads if h in coach_local[d, sl]]
                if busy:
                    reasons.append(f"{busy[0]} working a local on usual {wd} {d:%-m/%-d}")
        return "; ".join(reasons)

    for (t, ws), (terms, target) in short_vars.items():
        n = sum(solver.Value(v) for v in terms)
        if n:
            r = why(t, ws)
            out.append({"severity": "High", "team": teams[t].name, "week_of": ws,
                        "issue": f"Only {target - n} of {target} practice{'' if target == 1 else 's'} this week",
                        "detail": (r + "; " if r else "") + "no open slot for a makeup"})

    for t, ws, _goal in no_slot_weeks:
        team = teams[t]
        nos = [r.describe() for who in [team.name] + team.heads for r in rules_for[who] if r.kind == "no"]
        out.append({"severity": "High", "team": team.name, "week_of": ws,
                    "issue": "No practice possible this week",
                    "detail": "every open slot is blocked" + (f" (no-dates: {'; '.join(nos)})" if nos else "")
                    + ("; weekend limited to its listed dates" if team.weekend_only else "")})

    for miss, gdates in group_unused.values():
        if solver.Value(miss):
            out.append({"severity": "High", "team": "(all)", "week_of": week_start(min(gdates)),
                        "issue": "Required weekend not used: " + " or ".join(f"{d:%a %-m/%-d}" for d in gdates),
                        "detail": "no two teams could share a block there"})

    def asked_for(t, d, sl):
        ws_ = data.wishes.get(teams[t].name, [])
        return any(w.choice == 1 and w.matches(d, sl) for w in ws_) and not any(
            r.kind == "not_preferred" and r.matches(d, sl) for who in [teams[t].name] + teams[t].staff
            for r in rules_for[who])

    for (t, d, sl), v in off_vars.items():
        if solver.Value(v):
            reason = why(t, week_start(d))
            if asked_for(t, d, sl) and not reason:
                out.append({"severity": "Low", "team": teams[t].name, "week_of": week_start(d),
                            "issue": f"Moved to a requested time: {d:%a %-m/%-d} {sl}",
                            "detail": "this slot was open this week and is on the team's wishlist"})
                continue
            out.append({"severity": "Medium", "team": teams[t].name, "week_of": week_start(d),
                        "issue": f"Makeup / moved: {d:%a %-m/%-d} {sl}",
                        "detail": reason or "differs from the usual weekly slot"})

    a = assignments
    for (c, d, sl), v in asst_conflicts.items():
        if solver.Value(v):
            on = [teams[t].name for t in heads_of[c] + asst_of[c]
                  if len(a) and ((a["name"] == teams[t].name) & (a["date"] == d) & (a["slot"] == sl)).any()]
            on += [lp.name for lp in data.locals if lp.blocks(d, sl) and c in lp.coaches]
            out.append({"severity": "Medium", "team": ", ".join(n for n in on if any(t.name == n for t in teams)),
                        "week_of": week_start(d),
                        "issue": f"Assistant {c} double-booked {d:%a %-m/%-d} {sl}",
                        "detail": f"{c} is on {' and '.join(on)} at the same time"})

    if len(a):
        for tn in data.tournaments:
            if tn.kind != "travel":
                continue
            for when, d in (("before", tn.start - timedelta(days=1)), ("after", tn.end + timedelta(days=1))):
                hit = a[(a["kind"] == "Practice") & (a["date"] == d) & a["name"].isin(tn.teams)]
                for _, r in hit.iterrows():
                    out.append({"severity": "Medium", "team": r["name"], "week_of": week_start(d),
                                "issue": f"Practice the day {when} {tn.name}: {d:%a %-m/%-d} {r['slot']}",
                                "detail": "no other open slot that week kept both practices"})

    for (t, d), v in consec_vars.items():
        if solver.Value(v) and d.weekday() != 5:
            out.append({"severity": "Low", "team": teams[t].name, "week_of": week_start(d),
                        "issue": f"Back-to-back days: {d:%a} & {(d + timedelta(days=1)):%a %-m/%-d}",
                        "detail": ""})

    # Read off the finished schedule, so hand-placed practices are caught too.
    if len(a):
        pr = a[a["kind"] == "Practice"]
        on_day = set(zip(pr["name"], pr["date"]))
        for name, d in sorted(on_day, key=lambda k: (k[1], k[0])):
            if d.weekday() == 5 and (name, d + timedelta(days=1)) in on_day:
                out.append({"severity": "High", "team": name, "week_of": week_start(d),
                            "issue": f"Saturday then Sunday: {d:%-m/%-d} & {d + timedelta(days=1):%-m/%-d}",
                            "detail": "the only way to keep 2 practices both weeks without breaking a head "
                                      "coach's no-date (or it was placed by hand)"})
        by_name = {tm.name: tm for tm in teams}
        for r in pr.itertuples():
            team = by_name.get(r.name)
            if team is None:
                continue
            for asst in team.assistants:
                if asst in team.heads:
                    continue
                hit = [rule for rule in rules_for[asst] if rule.kind == "no" and rule.matches(r.date, r.slot)]
                if hit:
                    out.append({"severity": "Medium", "team": r.name, "week_of": week_start(r.date),
                                "issue": f"Assistant no-date broken: {asst} on {r.date:%a %-m/%-d} {r.slot}",
                                "detail": f"{', '.join(team.heads)} runs practice without {asst}"
                                          + (f" ({hit[0].note})" if hit[0].note else "")})

    df = pd.DataFrame(out, columns=["severity", "team", "week_of", "issue", "detail"])
    if not df.empty:
        order = {"High": 0, "Medium": 1, "Low": 2}
        df = df.assign(o=df["severity"].map(order)).sort_values(["o", "week_of", "team"]).drop(columns="o")
        df = df.reset_index(drop=True)
    return df



def _wished_slot(wishes, wd: str, sl: str) -> bool:
    """Does any of these wishlist rows cover this weekday and timeslot?"""
    return any((not w.days or wd in w.days) and (not w.times or sl in w.times) for w in wishes)


def _wish_issues(data, teams, pattern_slots, rules_for, week_of) -> list[dict]:
    """One row per team whose usual week misses its 1st-choice wishlist, with
    what got in the way of each 1st-choice day/time."""
    usual = defaultdict(list)
    for (t, wd, sl) in pattern_slots:
        usual[t].append((wd, sl))
    by_slot = defaultdict(list)
    for (t, wd, sl) in pattern_slots:
        by_slot[wd, sl].append(t)

    def recurring(lp, wd, sl):
        return lp.day == wd and slots_overlap(lp.slot, sl) and not lp.ranges

    def blocker(t, wd, sl):
        team = teams[t]
        if not team.allowed(sl):
            return f"{team.age}s can't practice at {sl}"
        for who in [team.name] + team.heads:
            for r in rules_for[who]:
                if r.kind == "no" and not r.ranges and (not r.days or wd in r.days) and (not r.times or sl in r.times):
                    return f"{who} marked it a no" + (f" ({r.note})" if r.note else "")
        for h in team.heads:
            lp = next((lp for lp in data.locals if recurring(lp, wd, sl) and h in lp.coaches), None)
            if lp:
                return f"{h} coaches {lp.name} then"
            other = next((teams[o].name for o in by_slot[wd, sl] if o != t and h in teams[o].heads), None)
            if other:
                return f"{h} coaches {other} then"
        taken = len([o for o in by_slot[wd, sl] if o != t])
        taken += len({c for lp in data.locals if lp.on_site and recurring(lp, wd, sl) for c in lp.courts})
        if taken >= len(COURTS):
            return "every court is taken"
        for a in team.assistants:
            if a in team.heads:
                continue
            lp = next((lp for lp in data.locals if recurring(lp, wd, sl) and a in lp.coaches), None)
            if lp:
                return f"assistant {a} coaches {lp.name} then"
            other = next((teams[o].name for o in by_slot[wd, sl] if o != t and a in teams[o].staff), None)
            if other:
                return f"assistant {a} coaches {other} then"
        for who in [team.name] + team.staff:
            if any(r.kind == "not_preferred" and not r.ranges and (not r.days or wd in r.days)
                   and (not r.times or sl in r.times) for r in rules_for[who]):
                return f"{who} also marked it not preferred"
        return "traded off for other teams' requests"

    out = []
    for t, team in enumerate(teams):
        firsts = [w for w in data.wishes.get(team.name, []) if w.choice == 1]
        mine = sorted(usual.get(t, []), key=lambda k: WEEKDAYS.index(k[0]))
        if not firsts or not mine:
            continue
        # A 1st choice naming just one day (e.g. "Mon, then Wed/Thu") can
        # only cover one practice, so a 2nd-choice second day isn't a miss.
        first_days = {d for w in firsts for d in (w.days or WEEKDAYS)}
        seconds = [w for w in data.wishes.get(team.name, []) if w.choice == 2]
        misses = [(wd, sl) for wd, sl in mine if not _wished_slot(firsts, wd, sl)
                  and not (len(first_days) < 2 and _wished_slot(seconds, wd, sl))]
        if not misses:
            continue
        mine_days = {wd for wd, sl in mine if _wished_slot(firsts, wd, sl)}
        reasons = {}
        for w in firsts:
            for wd in (w.days or WEEKDAYS):
                if wd in mine_days:
                    continue
                for sl in sorted(w.times or set(WEEKDAY_KEYS), key=SLOT_KEYS.index):
                    if sl in WEEKDAY_KEYS and (wd, sl) not in mine:
                        reasons.setdefault(f"{wd} {sl}", blocker(t, wd, sl))
        asked = "; ".join(" ".join(x for x in (", ".join(w.days) if w.days else "any day",
                                                 ", ".join(sorted(w.times, key=SLOT_KEYS.index)) if w.times else "")
                                   if x) for w in firsts)
        detail = f"asked for {asked}"
        if reasons:
            detail += ". " + "; ".join(f"{k}: {v}" for k, v in list(reasons.items())[:4])
        out.append({"severity": "Low", "team": team.name, "week_of": week_of,
                    "issue": "Usual week isn't the 1st choice: " + ", ".join(f"{wd} {sl}" for wd, sl in misses),
                    "detail": detail})
    return out
