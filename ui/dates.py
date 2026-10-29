"""The calendar: one schedule over every date, looked at a week or a month at a time.

Dates are scheduled a run at a time: a scheduling admin picks the dates and
presses Auto-schedule, and each run keeps its own build and published
versions (that's what the "periods" in storage are). Nobody has to pick a run
to look around, though. The page follows the day being looked at (the "view
day"): the run that holds it quietly becomes the one the page works on
(switch_period), and dates nobody has scheduled yet show just their programs,
tournaments and open days.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from scheduler import ScheduleInput, solve
from scheduler.data import empty
from scheduler.solver import ScheduleResult, week_start
from scheduler.storage import _frame_in, period_bookings, period_key
from scheduler.views import range_label
from ui.state import S

VIEW_DAY = "view_day"


# ---------------------------------------------------------------- runs --

def active_key() -> str:
    """The run of dates the page is working on."""
    return period_key(st.session_state.range_start, st.session_state.range_end)


def _active_built() -> bool:
    r = st.session_state.get("result")
    return (r is not None and r.ok
            and st.session_state.get("result_period") == (st.session_state.range_start,
                                                          st.session_state.range_end))


def runs() -> list[tuple[str, date, date]]:
    """Every run of dates that's been scheduled (built), the one being worked on included,
    in date order: (key, start, end)."""
    out = []
    if _active_built():
        out.append((active_key(), st.session_state.range_start, st.session_state.range_end))
    for k, ent in st.session_state.get("periods", {}).items():
        if ent.get("result") and k != active_key():
            out.append((k, date.fromisoformat(ent["start"]), date.fromisoformat(ent["end"])))
    return sorted(out, key=lambda r: (r[1], r[2]))


def run_at(d: date) -> tuple[str, date, date] | None:
    """The scheduled run holding day d (the latest-starting one if two overlap)."""
    hits = [r for r in runs() if r[1] <= d <= r[2]]
    return hits[-1] if hits else None


def is_published(key: str) -> bool:
    if key == active_key():
        return bool(st.session_state.get("published"))
    return bool((st.session_state.get("periods", {}).get(key) or {}).get("published"))


def season() -> tuple[date, date] | None:
    """First and last scheduled day, or None if nothing's scheduled yet."""
    rs = runs()
    return (min(r[1] for r in rs), max(r[2] for r in rs)) if rs else None


# ---------------------------------------------------------- view day --

def initial_day() -> date:
    """Where the calendar opens: today if it's scheduled, else the next scheduled dates
    (or the last ones, if they're all past), else the dates on top."""
    today = date.today()
    rs = runs()
    if any(s_ <= today <= e_ for _, s_, e_ in rs):
        return today
    ahead = [s_ for _, s_, _ in rs if s_ > today]
    if ahead:
        return min(ahead)
    if rs:
        return max(s_ for _, s_, _ in rs)
    return st.session_state.range_start


def view_day() -> date:
    d = st.session_state.get(VIEW_DAY)
    if d is None:
        d = st.session_state[VIEW_DAY] = initial_day()
    return d


def set_day(d: date):
    """Look at day d (a button callback, or before the page follows it)."""
    st.session_state[VIEW_DAY] = d


def follow():
    """Make the run holding the view day the one being worked on. Called each page run, before the
    schedule is built; skipped while a rebuild is on its way (a change being applied to its dates)."""
    from ui.session import switch_period
    d = view_day()
    if st.session_state.get("resolve") or st.session_state.get("rebuild_queue"):
        return
    hit = run_at(d)
    if hit and hit[0] != active_key():
        if S.CAN:
            from ui.session import refresh_periods
            refresh_periods()
            hit = run_at(d)
        if hit and hit[0] != active_key():
            switch_period(hit[0])


# ------------------------------------------------------- the schedule --

def preview(a: date, b: date) -> ScheduleResult | None:
    """Dates nobody has scheduled: the programs, tournaments, open days and closures on their own.
    Kept for a few windows, rebuilt only when one of those inputs changes."""
    inp = S.inp
    from scheduler.data import ATTRS as _AT
    base = ScheduleInput(**{at: empty(sh) for sh, at in _AT.items()})
    for attr in ("locals", "weekends", "closures", "tournaments"):
        setattr(base, attr, getattr(inp, attr))
    sig = hashlib.sha1(json.dumps([base.to_records(), str(a), str(b)], sort_keys=True,
                                  default=str).encode()).hexdigest()
    cache = st.session_state.setdefault("_previews", {})
    if sig in cache:
        return cache[sig]
    try:
        r_ = solve(base, a, b, time_limit_s=5)
    except ValueError:
        r_ = None
    r_ = r_ if r_ is not None and r_.ok else None
    if len(cache) > 16:
        cache.pop(next(iter(cache)))
    cache[sig] = r_
    return r_


def _built_parts() -> list[tuple]:
    """(start, end, assignments, open block days, practice counts) for every scheduled run;
    the one being worked on is its live build."""
    out = []
    r = st.session_state.get("result")
    if r is not None and r.ok and _active_built():
        out.append((st.session_state.range_start, st.session_state.range_end, r.assignments,
                    list(r.weekend_dates), r.practice_counts))
    cache = st.session_state.setdefault("_season_cache", {})
    for k, ent in st.session_state.get("periods", {}).items():
        res = ent.get("result")
        if not res or k == active_key():
            continue
        sig = (k, id(res), len(res.get("assignments", [])))
        if sig not in cache:
            a_, wd_ = period_bookings(ent)
            cache[sig] = (a_, wd_, _frame_in(res.get("practice_counts", [])))
        a_, wd_, pc_ = cache[sig]
        out.append((date.fromisoformat(ent["start"]), date.fromisoformat(ent["end"]), a_, wd_, pc_))
    return out


def _has(obj, name: str) -> bool:
    try:
        getattr(obj, name)
        return True
    except AttributeError:
        return False


def _days(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def open_now(a: date, b: date, listed_only: bool = False) -> set[date]:
    """Days from a to b open in 2-hour blocks now (weekends, and the Open days list), minus closures.
    listed_only: just the days on the Open days list."""
    cleaned = getattr(S, "cleaned", None) if _has(S, "cleaned") else None
    if cleaned is None:
        return set()
    closures = (getattr(S, "closures", None) if _has(S, "closures") else None) or {}
    return {d for d, w in cleaned.weekends.items() if a <= d <= b and d not in closures
            and not (listed_only and w.auto)}


def not_built_open_days(a: date, b: date) -> list[tuple[str, date, date, list[date]]]:
    """Scheduled runs overlapping a..b with days opened since they were built (so no practices can be
    on them yet): (key, start, end, those days)."""
    out = []
    parts = {(s_, e_): set(wd) for s_, e_, _, wd, _ in _built_parts()}
    for k, s_, e_ in runs():
        if s_ > b or e_ < a:
            continue
        new = sorted(open_now(s_, e_, listed_only=True) - parts.get((s_, e_), set()))
        if new:
            out.append((k, s_, e_, new))
    return out


def window(a: date, b: date, with_preview: bool = True) -> tuple[ScheduleResult | None, list[date]]:
    """Everything on the calendar from a to b: each scheduled run's practices and programs, plus
    (with_preview) the programs on dates not scheduled yet. Returns (result, days not scheduled yet);
    result is None when there's nothing to show."""
    frames, wk, pcs, covered = [], set(), [], set()
    for s_, e_, asg, wd, pc in _built_parts():
        lo, hi = max(a, s_), min(b, e_)
        if lo > hi:
            continue
        covered.update(_days(lo, hi))
        if len(asg):
            frames.append(asg[asg["date"].map(lambda d, lo=lo, hi=hi: lo <= d <= hi)])
        wk.update(d for d in wd if lo <= d <= hi)
        if pc is not None and len(pc) and "week_of" in pc.columns:
            pcs.append(pc[pc["week_of"].map(lambda w, lo=lo, hi=hi: week_start(lo) <= w <= hi)])
    wk.update(d for d in open_now(a, b) if d in covered)  # open days as they are now, built or not
    gaps = [d for d in _days(a, b) if d not in covered]
    if gaps and with_preview:
        pv = preview(min(gaps), max(gaps))
        if pv is not None:
            g = set(gaps)
            if len(pv.assignments):
                frames.append(pv.assignments[pv.assignments["date"].isin(g)])
            wk.update(d for d in pv.weekend_dates if d in g)
    if not frames:
        return None, gaps
    a_all = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0].reset_index(drop=True)
    r = st.session_state.get("result") if _active_built() else None
    cleaned = getattr(S, "cleaned", None) if _has(S, "cleaned") else None
    tourn = cleaned.tournaments if cleaned is not None else (r.tournaments if r is not None else [])
    pattern = r.pattern if r is not None else pd.DataFrame(columns=["team", "day", "slot", "court"])
    counts = pd.concat(pcs, ignore_index=True) if pcs else pd.DataFrame()
    return ScheduleResult(status="OPTIMAL", assignments=a_all, pattern=pattern,
                          issues=pd.DataFrame(columns=["severity", "team", "week_of", "issue", "detail"]),
                          metrics=dict(r.metrics) if r is not None else {}, warnings=[], start=a, end=b,
                          weekend_dates=sorted(wk), tournaments=tourn, practice_counts=counts,
                          broken=[]), gaps


# ------------------------------------------------------------ pickers --

def _shift(days: int):
    st.session_state[VIEW_DAY] = view_day() + timedelta(days=days)


def _shift_month(n: int):
    d = view_day().replace(day=1)
    m = d.month - 1 + n
    st.session_state[VIEW_DAY] = date(d.year + m // 12, m % 12 + 1, 1)


def _picked(key: str):
    v = st.session_state.get(key)
    if v:
        st.session_state[VIEW_DAY] = v


def where_note(a: date, b: date, gaps: list[date]):
    """One line under the picker: which scheduled dates this is part of, or that it isn't scheduled yet."""
    for _k, s_, e_, days in not_built_open_days(a, b):
        st.warning(f"**{', '.join(f'{d:%a %b %-d}' for d in days[:6])}{' …' if len(days) > 6 else ''}** "
                   f"{'was' if len(days) == 1 else 'were'} opened after {range_label(s_, e_)} was scheduled, so "
                   "no practices are on "
                   + ("it" if len(days) == 1 else "them")
                   + (" yet. Press **Rebuild these dates** in the sidebar to use "
                      + ("it." if len(days) == 1 else "them.") if S.CAN else " yet."))
    rs = [r for r in runs() if r[1] <= b and a <= r[2]]
    bits = [range_label(s_, e_) + ("" if is_published(k) else " (not published)") for k, s_, e_ in rs]
    if gaps and not rs:
        st.caption("**Not scheduled yet:** programs, tournaments and open days only."
                   + (" Pick these dates under **Auto-schedule** in the sidebar to add practices." if S.CAN else ""))
    elif gaps:
        st.caption(f"Scheduled with {' and '.join(bits)}. "
                   f"{range_label(min(gaps), max(gaps))} isn't scheduled yet (programs only).")
    elif bits:
        st.caption(f"Scheduled with {' and '.join(bits)}.")


def _picker(prefix: str, back, fwd, title: str):
    c0, c1, c2, c3, c4 = st.columns([1, 2, 1, 1, 3], vertical_alignment="bottom")
    c0.button("◀", key=f"{prefix}_prev", on_click=back[0], args=back[1], help=back[2], width="stretch")
    pk = f"{prefix}_pick"
    st.session_state[pk] = view_day()
    c1.date_input("Go to", key=pk, format="MM/DD/YYYY", on_change=_picked, args=(pk,))
    c2.button("▶", key=f"{prefix}_next", on_click=fwd[0], args=fwd[1], help=fwd[2], width="stretch")
    c3.button("Today", key=f"{prefix}_today", on_click=set_day, args=(date.today(),), width="stretch")
    c4.markdown(f"#### {title}")


def week_picker(prefix: str) -> date:
    """◀ ▶ through the weeks (Sun–Sat), a date to jump to and Today. Returns the week's Sunday."""
    ws = week_start(view_day())
    _picker(prefix, (_shift, (-7,), "Previous week"), (_shift, (7,), "Next week"),
            f"Week of {range_label(ws, ws + timedelta(days=6))}")
    return ws


def month_picker(prefix: str) -> tuple[date, date]:
    """◀ ▶ through the months, a date to jump to and Today. Returns the month's first and last day."""
    first = view_day().replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    _picker(prefix, (_shift_month, (-1,), "Previous month"), (_shift_month, (1,), "Next month"),
            f"{first:%B %Y}")
    return first, last


def download_range(prefix: str, default: tuple[date, date]) -> tuple[date, date]:
    """From/To for printouts and downloads (any dates; ones not scheduled show their programs)."""
    c1, c2 = st.columns(2)
    a = c1.date_input("From", value=default[0], format="MM/DD/YYYY", key=f"{prefix}_dl_from_{default[0]}")
    b = c2.date_input("To", value=default[1], format="MM/DD/YYYY", key=f"{prefix}_dl_to_{default[1]}")
    return (a, b) if b >= a else (a, a)


def run_picker(key: str):
    """For pages that work on one run of scheduled dates at a time (Publish, Review): which one.
    Picking one looks at its first day, so the court view and calendar follow too."""
    rs = runs()
    if not rs:
        return
    cur = active_key()
    keys = [k for k, _, _ in rs]
    lab = {k: range_label(s_, e_) + ("" if is_published(k) else " · not published") for k, s_, e_ in rs}
    if cur not in keys:
        return
    if len(keys) == 1:
        st.caption(f"Scheduled dates: **{lab[cur]}**")
        return

    def _go():
        k = st.session_state[key]
        st.session_state[VIEW_DAY] = next(s_ for kk, s_, _ in rs if kk == k)
    st.session_state[key] = cur
    st.selectbox("Scheduled dates", keys, format_func=lab.get, key=key, on_change=_go,
                 help="Dates are scheduled and published a run at a time. Picking one here also moves the "
                      "court view and calendar there.")


# ------------------------------------------------- rebuilding changes --

def _row_spans(rows: list[dict], cols: tuple[str, ...]) -> list[tuple[date, date]] | None:
    """The dates a set of rows covers (their date columns); None if one has no dates (it covers
    every date)."""
    from scheduler.data import blank, parse_date_ranges
    year = st.session_state.range_start.year
    out = []
    for r in rows:
        got = []
        for c in cols:
            v = r.get(c)
            if v is None or blank(v) or not str(v).strip():
                continue
            try:
                got += parse_date_ranges(str(v), year)
            except (ValueError, TypeError):
                return None
        if not got:
            return None
        out.append((min(x for x, _ in got), max(y for _, y in got)))
    return out


def _changed(old: pd.DataFrame, new: pd.DataFrame) -> list[dict]:
    """Rows in one table but not the other (added, removed, or the before and after of an edit)."""
    def recs(df):
        df = df.dropna(how="all").fillna("").astype(str)
        return [tuple(sorted(r.items())) for r in df.to_dict("records")]
    a, b = recs(old), recs(new)
    diff = [x for x in a if x not in b] + [x for x in b if x not in a]
    return [dict(x) for x in diff]


def rebuild_for(changes: list[tuple[pd.DataFrame, pd.DataFrame, tuple[str, ...]]], autobuild: bool = True) -> str:
    """After programs, tournaments or open days change: rebuild every scheduled run of dates the change
    touches (the first now, the rest next), not just the one being worked on. Dates not scheduled yet
    need nothing (they show the change right away). `changes`: (table before, after, its date columns).
    Returns a sentence for the confirmation message."""
    from ui.session import switch_period
    spans: list | None = []
    for old, new, cols in changes:
        sp = _row_spans(_changed(old, new), cols)
        spans = None if sp is None or spans is None else spans + sp
    hits = [r for r in runs() if spans is None or any(x <= r[2] and r[1] <= y for x, y in spans)]
    if not hits:
        return "No scheduled dates are affected, so nothing needed rebuilding."
    labels = " and ".join(range_label(s_, e_) for _, s_, e_ in hits)
    if not autobuild:
        st.session_state.pending_rebuild = "Programs changed"
        return f"Press **Rebuild these dates** when you're done ({labels})."
    keys = [k for k, _, _ in hits]
    if active_key() in keys:
        keys.remove(active_key())
    else:
        switch_period(keys.pop(0))
    st.session_state.rebuild_queue = keys
    st.session_state.resolve = True
    return f"Rebuilt {labels}."
