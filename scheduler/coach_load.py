"""Coach schedule check: flags coaches whose week is rough on them.

Looks at every session a coach has (their teams' practices, as head or assistant, and the
on-site programs they work) and flags what makes a coach's schedule "bad":

- High:   two sessions at once; a late session (7:20 / 7 PM) the night before their tournament
- Medium: a break between two sessions the same day (a 3:45 program, then a team at 7:20);
          4+ days in a row; a team practice Friday at 7:20
- Low:    changing courts between back-to-back sessions; 3+ late nights (7:20 / 7 PM) in a week;
          5+ days in a week

Each coach gets a score (High 3, Medium 2, Low 1 per flag); a coach at BAD_SCORE or more for a
week is highlighted as having a bad schedule that week.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

import pandas as pd

from .config import SLOT_KEYS, SLOT_TIMES, back_to_back, court_short, week_start

LATE = {"7:20", "7PM"}
POINTS = {"High": 3, "Medium": 2, "Low": 1}
BAD_SCORE = 4          # a week at this score or more is a "bad" week for the coach
DAYS_IN_A_ROW = 4
LATE_NIGHTS = 3
DAYS_A_WEEK = 5

COLUMNS = ["coach", "week_of", "date", "severity", "issue", "detail"]


def _names(s) -> list[str]:
    return [x.strip() for x in str(s or "").split(";") if x.strip() and x.strip().lower() != "nan"]


def sessions(assignments: pd.DataFrame) -> pd.DataFrame:
    """One row per coach per session (date, slot, what, courts), practices and on-site programs."""
    if assignments is None or not len(assignments):
        return pd.DataFrame(columns=["coach", "date", "slot", "kind", "name", "courts"])
    a = assignments[assignments["kind"].isin(["Practice", "Local"])]
    rows = defaultdict(set)
    for r in a.itertuples():
        for c in _names(getattr(r, "coach", "")):
            rows[c, r.date, r.slot, r.kind, r.name].add(r.court)
    out = pd.DataFrame([{"coach": c, "date": d, "slot": s, "kind": k, "name": n,
                         "courts": frozenset(x for x in cs if pd.notna(x))}
                        for (c, d, s, k, n), cs in rows.items()])
    if len(out):
        out["o"] = out["slot"].map(lambda s: SLOT_KEYS.index(s) if s in SLOT_KEYS else 99)
        out = out.sort_values(["coach", "date", "o"]).drop(columns="o").reset_index(drop=True)
    return out


def _ct(cs) -> str:
    return "/".join(court_short(c) for c in sorted(cs)) or "?"


def coach_flags(assignments: pd.DataFrame, tournaments=()) -> pd.DataFrame:
    """Every flag for every coach: coach, week_of, date, severity, issue, detail."""
    ses = sessions(assignments)
    if not len(ses):
        return pd.DataFrame(columns=COLUMNS)
    pr = assignments[assignments["kind"] == "Practice"]
    staff = defaultdict(set)
    for n, c in zip(pr["name"], pr["coach"]):
        staff[n].update(_names(c))
    eve = defaultdict(list)  # (coach, date) -> tournaments the next day
    for tn in tournaments or ():
        who = set(tn.coaches) if tn.kind == "local" else {c for n in tn.teams for c in staff.get(n, ())}
        for c in who:
            eve[c, tn.start - timedelta(days=1)].append(tn.name)

    out = []

    def flag(c, d, sev, issue, detail):
        out.append({"coach": c, "week_of": week_start(d), "date": d, "severity": sev, "issue": issue,
                    "detail": detail})

    for c, g in ses.groupby("coach"):
        for d, day in g.groupby("date"):
            day = day.reset_index(drop=True)
            slots = list(day["slot"])
            # two at once
            for i in range(len(day)):
                for j in range(i + 1, len(day)):
                    a_, b_ = day.loc[i], day.loc[j]
                    if a_["slot"] in SLOT_TIMES and b_["slot"] in SLOT_TIMES:
                        (a0, a1), (b0, b1) = SLOT_TIMES[a_["slot"]], SLOT_TIMES[b_["slot"]]
                        if a0 < b1 and b0 < a1:
                            flag(c, d, "High", "Two places at once",
                                 f"{a_['name']} ({a_['slot']}) and {b_['name']} ({b_['slot']})")
            # breaks and court changes between consecutive sessions
            timed = day[day["slot"].isin(SLOT_TIMES)].sort_values(
                "slot", key=lambda s: s.map(lambda x: SLOT_TIMES[x][0]))
            for (_, a_), (_, b_) in zip(timed.iterrows(), list(timed.iterrows())[1:]):
                if SLOT_TIMES[a_["slot"]][1] < SLOT_TIMES[b_["slot"]][0]:
                    flag(c, d, "Medium", "Break between sessions",
                         f"{a_['name']} at {a_['slot']}, then {b_['name']} at {b_['slot']}")
                elif back_to_back(a_["slot"], b_["slot"]) and a_["courts"] and b_["courts"] \
                        and not (a_["courts"] & b_["courts"]):
                    flag(c, d, "Low", "Changes courts",
                         f"{a_['name']} on Ct {_ct(a_['courts'])}, then {b_['name']} on Ct {_ct(b_['courts'])}")
            late = [s for s in slots if s in LATE]
            if late and eve.get((c, d)):
                flag(c, d, "High", "Late the night before a tournament",
                     f"{', '.join(day[day['slot'].isin(LATE)]['name'])} late; {', '.join(eve[c, d])} the next day")
            fri = day[(day["slot"] == "7:20") & (day["kind"] == "Practice")]
            if len(fri) and d.weekday() == 4:  # team practices only: programs set their own times
                flag(c, d, "Medium", "Friday 7:20", ", ".join(fri["name"]))

        days = sorted(set(g["date"]))
        # days in a row
        run = [days[0]] if days else []
        for d in days[1:] + [None]:
            if d is not None and d - run[-1] == timedelta(days=1):
                run.append(d)
                continue
            if len(run) >= DAYS_IN_A_ROW:
                flag(c, run[0], "Medium", f"{len(run)} days in a row", f"{run[0]:%a %-m/%-d} – {run[-1]:%a %-m/%-d}")
            run = [d] if d is not None else []
        # per week
        g = g.assign(week_of=g["date"].map(week_start))
        for ws, wk in g.groupby("week_of"):
            n_late = wk[wk["slot"].isin(LATE)]["date"].nunique()
            if n_late >= LATE_NIGHTS:
                flag(c, ws, "Low", f"{n_late} late nights", "7:20 / 7 PM on " + ", ".join(
                    f"{d:%a}" for d in sorted(set(wk[wk["slot"].isin(LATE)]["date"]))))
            n_days = wk["date"].nunique()
            if n_days >= DAYS_A_WEEK:
                flag(c, ws, "Low", f"{n_days} days this week", ", ".join(f"{d:%a}" for d in sorted(set(wk["date"]))))
    df = pd.DataFrame(out, columns=COLUMNS)
    if len(df):
        df["o"] = df["severity"].map({"High": 0, "Medium": 1, "Low": 2})
        df = df.sort_values(["coach", "date", "o"]).drop(columns="o").reset_index(drop=True)
    return df


def coach_scores(flags: pd.DataFrame) -> pd.DataFrame:
    """One row per coach per week with flags: coach, week_of, score, bad, high, medium, low, issues."""
    cols = ["coach", "week_of", "score", "bad", "high", "medium", "low", "issues"]
    if flags is None or not len(flags):
        return pd.DataFrame(columns=cols)
    rows = []
    for (c, ws), g in flags.groupby(["coach", "week_of"]):
        sc = int(g["severity"].map(POINTS).sum())
        rows.append({"coach": c, "week_of": ws, "score": sc, "bad": sc >= BAD_SCORE,
                     "high": int((g["severity"] == "High").sum()), "medium": int((g["severity"] == "Medium").sum()),
                     "low": int((g["severity"] == "Low").sum()),
                     "issues": "; ".join(dict.fromkeys(g["issue"]))})
    return pd.DataFrame(rows, columns=cols).sort_values(["score", "coach"], ascending=[False, True]) \
        .reset_index(drop=True)


def bad_coaches(flags: pd.DataFrame) -> pd.DataFrame:
    """Coaches with at least one bad week, worst first: coach, bad_weeks, worst, total, issues."""
    sc = coach_scores(flags)
    sc = sc[sc["bad"]] if len(sc) else sc
    if not len(sc):
        return pd.DataFrame(columns=["coach", "bad_weeks", "worst", "total", "issues"])
    g = sc.groupby("coach")
    out = pd.DataFrame({"bad_weeks": g.size(), "worst": g["score"].max(), "total": g["score"].sum(),
                        "issues": g["issues"].agg(lambda s: "; ".join(dict.fromkeys("; ".join(s).split("; "))))})
    return out.reset_index().sort_values(["worst", "total", "coach"], ascending=[False, False, True]) \
        .reset_index(drop=True)


def team_back_to_back(assignments: pd.DataFrame) -> list[tuple[str, object, object]]:
    """Every time a team practices two days in a row: [(team, first day, second day)], in date order.
    Read off the finished schedule, so practices placed by hand count too."""
    if assignments is None or not len(assignments) or "kind" not in assignments:
        return []
    pr = assignments[assignments["kind"] == "Practice"]
    on = set(zip(pr["name"], pr["date"]))
    return sorted(((n, d, d + timedelta(days=1)) for n, d in on if (n, d + timedelta(days=1)) in on),
                  key=lambda k: (k[1], k[0]))


def back_to_back_days(assignments: pd.DataFrame) -> set:
    """{(team, date)} for every practice that has one the day before or after."""
    out = set()
    for n, d1, d2 in team_back_to_back(assignments):
        out.update({(n, d1), (n, d2)})
    return out
