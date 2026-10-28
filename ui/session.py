"""The club's one schedule in st.session_state: opening it, switching between runs of dates, autosave."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from scheduler import ScheduleInput
from scheduler.solver import week_start
from scheduler.storage import MAIN_ID, all_periods, ensure_main, make_payload, period_key, read_period
from scheduler.views import range_label
from ui.common import inputs_fingerprint, next_month, payload_hash
from ui.state import S


def _load_into_session(rec: dict, key: str | None = None):
    """Opens the club's schedule at a period (key; default: the one stored on top)."""
    payload = rec["payload"]
    inp_ = ScheduleInput.from_records(payload.get("inputs", {}))
    periods_ = all_periods(payload)
    top = period_key(date.fromisoformat(payload["start"]), date.fromisoformat(payload["end"]))
    key = key if key in periods_ else top
    s_, e_, settings, result_, published_ = read_period(periods_.pop(key), inp_)
    inp_.start, inp_.end = s_, e_
    st.session_state.update(
        project_id=MAIN_ID, project_name=rec["name"], inp=inp_, result=result_,
        result_period=(result_.start, result_.end) if result_ else None,
        inp_sig=("project", MAIN_ID, str(rec["updated_at"])),
        range_start=s_, range_end=e_,
        time_limit=int(settings.get("time_limit", 60)), makeup=bool(settings.get("makeup", True)),
        keep_prev=bool(settings.get("keep_prev", True)), published=published_,
        base_version=settings.get("base_version"), pending_prefs=list(settings.get("pending_prefs") or []),
        periods=periods_, periods_dirty=set(), periods_deleted=set(),
        _db_stamp=str(rec["updated_at"]),
        last_saved=payload_hash(make_payload(inp_, s_, e_, settings, result_, published_, periods=periods_)),
    )
    st.session_state.pop("_save_sig", None)
    st.session_state._active_sig = payload_hash(period_entry())


def open_project(pid: str = MAIN_ID, key: str | None = None) -> bool:
    """(Re)opens the club's schedule, at the period being viewed unless `key` says otherwise."""
    store = S.store
    rec = ensure_main(store, next_month(date.today()))
    if key is None and st.session_state.get("range_start"):
        key = period_key(st.session_state.range_start, st.session_state.range_end)
    _load_into_session(rec, key)
    return True


def period_entry() -> dict:
    """The period being viewed, in the stored shape."""
    from scheduler.storage import result_to_dict
    return {"start": st.session_state.range_start.isoformat(), "end": st.session_state.range_end.isoformat(),
            "settings": {"time_limit": st.session_state.time_limit, "makeup": st.session_state.makeup,
                         "keep_prev": st.session_state.get("keep_prev", True),
                         "base_version": st.session_state.get("base_version"),
                         "pending_prefs": list(st.session_state.get("pending_prefs") or [])},
            "result": result_to_dict(st.session_state.get("result")),
            "published": list(st.session_state.get("published") or [])}


def switch_period(key: str, new: tuple[date, date] | None = None):
    """Shows another period (or starts new dates), keeping this one."""
    CAN = S.CAN
    cur = period_key(st.session_state.range_start, st.session_state.range_end)
    periods_ = st.session_state.periods
    ent = period_entry()
    unchanged = payload_hash(ent) == st.session_state.get("_active_sig")
    if ent["result"] is None and not ent["published"]:
        # Never built: it isn't on the calendar, so there's nothing to keep.
        periods_.pop(cur, None)
        if CAN:
            st.session_state.periods_deleted.add(cur)
            st.session_state._force_save = True
    else:
        periods_[cur] = ent
        if CAN and not unchanged:  # only looked at: another admin's newer copy isn't overwritten
            st.session_state.periods_dirty.add(cur)
    if new is not None:
        s_, e_ = new
        settings = {"time_limit": st.session_state.time_limit, "makeup": st.session_state.makeup}
        result_, published_ = None, []
    else:
        s_, e_, settings, result_, published_ = read_period(periods_[key], st.session_state.inp)
    periods_.pop(period_key(s_, e_), None)
    st.session_state.periods_deleted.discard(period_key(s_, e_))
    st.session_state.update(
        result=result_, result_period=(result_.start, result_.end) if result_ else None,
        range_start=s_, range_end=e_,
        time_limit=int(settings.get("time_limit", 60)), makeup=bool(settings.get("makeup", True)),
        keep_prev=bool(settings.get("keep_prev", True)), published=published_,
        base_version=settings.get("base_version"), pending_prefs=list(settings.get("pending_prefs") or []))
    for k in ("w_range_start", "w_range_end"):
        st.session_state.pop(k, None)
    st.session_state._active_sig = payload_hash(period_entry())


def refresh_periods():
    """Another admin saved since this page last did: bring in their copies of the other scheduled
    dates (the ones this page hasn't changed), so looking at them shows their latest."""
    store = S.store
    stamp = store.updated_at(MAIN_ID)
    if stamp is None or str(stamp) == st.session_state.get("_db_stamp"):
        return
    rec = store.get(MAIN_ID)
    if rec is None:
        return
    cur = period_key(st.session_state.range_start, st.session_state.range_end)
    dirty = st.session_state.get("periods_dirty", set())
    fresh = all_periods(rec["payload"])
    for k, ent in fresh.items():
        if k != cur and k not in dirty and k not in st.session_state.get("periods_deleted", set()):
            st.session_state.periods[k] = ent
    for k in [k for k in st.session_state.periods if k not in fresh and k not in dirty]:
        st.session_state.periods.pop(k, None)


def _trim(ent: dict, a: date, b: date) -> dict:
    """A stored run of dates cut down to a..b (its practices, counts and open days on those dates)."""
    out = dict(ent, start=a.isoformat(), end=b.isoformat())
    r = ent.get("result")
    if r:
        lo, hi = a.isoformat(), b.isoformat()
        in_ = lambda v: lo <= str(v)[:10] <= hi  # noqa: E731
        ws = week_start(a).isoformat()
        out["result"] = dict(
            r, start=lo, end=hi,
            assignments=[x for x in r.get("assignments", []) if in_(x.get("date"))],
            weekend_dates=[x for x in r.get("weekend_dates", []) if in_(x)],
            practice_counts=[x for x in r.get("practice_counts", []) if ws <= str(x.get("week_of"))[:10] <= hi],
            broken=[x for x in r.get("broken", []) if in_(x.get("date"))],
            issues=[x for x in r.get("issues", []) if not x.get("week_of") or ws <= str(x["week_of"])[:10] <= hi])
    return out


def schedule_dates(a: date, b: date) -> str | None:
    """Make a..b the dates being worked on, ready to Auto-schedule. Dates already scheduled that
    fall inside a..b come out of their runs (a run cut at its start or end keeps the rest); their
    practices are where the new build starts from, so nothing moves without a reason.
    Returns a problem to show, or None."""
    from ui.dates import active_key, runs
    if b < a:
        return "The end date is before the start date."
    if (a, b) == (st.session_state.range_start, st.session_state.range_end):
        return None  # these exact dates: rebuild them
    key = period_key(a, b)
    if key in st.session_state.periods:
        switch_period(key)
        return None
    over = [r for r in runs() if r[1] <= b and a <= r[2]]
    for _, s_, e_ in over:
        if s_ < a and e_ > b:
            return (f"{range_label(a, b)} is in the middle of {range_label(s_, e_)}. Pick dates that reach its "
                    f"first or last day, or exactly {range_label(s_, e_)} to rebuild all of it.")
    # Where the new build starts from: the practices already on these dates, and the usual
    # week of the dates being replaced (or of the latest dates scheduled before these).
    from ui.dates import _built_parts
    parts = _built_parts()
    seed = [asg[asg["date"].map(lambda d: a <= d <= b)] for s_, e_, asg, _, _ in parts if len(asg)
            and s_ <= b and a <= e_]
    seed = [x for x in seed if len(x)]
    prev = pd.concat(seed, ignore_index=True) if seed else None
    before = [k for k, s_, _ in runs() if s_ < a]
    pat = None
    pick = next((k for k, _, _ in reversed(over)), None) or (before[-1] if before else None)
    if pick == active_key() and st.session_state.get("result") is not None:
        pat = st.session_state.result.pattern
    elif pick:
        rr = (st.session_state.periods.get(pick) or {}).get("result") or {}
        from scheduler.storage import _frame_in
        pat = _frame_in(rr.get("pattern", []), date_cols=())
    switch_period(key, (a, b))
    for k, s_, e_ in over:
        ent = st.session_state.periods.pop(k, None)
        if ent is None:
            continue
        st.session_state.periods_deleted.add(k)
        st.session_state.periods_dirty.discard(k)
        if s_ < a or e_ > b:  # keep the part outside the new dates
            na, nb = (s_, a - timedelta(days=1)) if s_ < a else (b + timedelta(days=1), e_)
            nk = period_key(na, nb)
            st.session_state.periods[nk] = _trim(ent, na, nb)
            st.session_state.periods_dirty.add(nk)
            st.session_state.periods_deleted.discard(nk)
    st.session_state.seed_prev = (prev, pat)
    st.session_state._force_save = True
    return None


def period_label(entry_or_dates) -> str:
    """How a run of dates reads in the Dates menu: 'Dec 6–20, 2026' (+ ' · not published')."""
    if isinstance(entry_or_dates, dict):
        s_, e_ = date.fromisoformat(entry_or_dates["start"]), date.fromisoformat(entry_or_dates["end"])
        pub = bool(entry_or_dates.get("published"))
    else:
        (s_, e_), pub = entry_or_dates, bool(st.session_state.get("published"))
    return range_label(s_, e_) + ("" if pub else " · not published")


def periods_for_save(rec_payload: dict | None) -> dict:
    """The other periods to save: what's in the database (another admin may
    be working on other dates), with this session's changes on top."""
    out = all_periods(rec_payload) if rec_payload else {}
    for k in st.session_state.get("periods_deleted", set()):
        out.pop(k, None)
    for k in st.session_state.get("periods_dirty", set()):
        if k in st.session_state.periods:
            out[k] = st.session_state.periods[k]
    for k in st.session_state.periods:  # periods this session knows that the database lost track of
        out.setdefault(k, st.session_state.periods[k])
    out.pop(period_key(st.session_state.range_start, st.session_state.range_end), None)
    return out


def ensure_open():
    """Open the club's schedule the first time a visitor's page runs."""
    if st.session_state.get("project_id") != MAIN_ID:
        open_project()


def autosave():
    """Save the schedule whenever anything changed this run (inputs, dates, settings, the build)."""
    store, CAN, inp, start, end, time_limit, makeup = S.store, S.CAN, S.inp, S.start, S.end, S.time_limit, S.makeup
    # Save the inputs, dates, settings and latest schedule whenever anything
    # changed this run, so a refresh or a new visit picks up where you left off.
    _settings = {"time_limit": time_limit, "makeup": makeup, "keep_prev": st.session_state.get("keep_prev", True),
                 "base_version": st.session_state.get("base_version"),
                 "pending_prefs": list(st.session_state.get("pending_prefs") or [])}
    # Building the whole payload is the slow part, so only do it when something
    # could have changed: the solved schedule, published versions, settings,
    # dates or any input table.
    _sig = (id(st.session_state.get("result")), len(st.session_state.get("published", [])),
            tuple(v["id"] for v in st.session_state.get("published", [])), json.dumps(_settings), str(start), str(end),
            inputs_fingerprint(inp), tuple(sorted(st.session_state.get("periods", {}))))
    if CAN and (_sig != st.session_state.get("_save_sig") or st.session_state.pop("_force_save", False)):
        try:
            # The other dates are only re-read from the database when someone else
            # saved since this page last did (otherwise this page already has them).
            stamp = store.updated_at(MAIN_ID)
            if stamp is not None and str(stamp) == st.session_state.get("_db_stamp"):
                others = periods_for_save(None)
            else:
                _db = store.get(MAIN_ID)
                others = periods_for_save(_db["payload"] if _db else None)
            _payload = make_payload(inp, start, end, _settings, st.session_state.get("result"),
                                    st.session_state.get("published", []), periods=others)
            _h = payload_hash(_payload)
            if _h == st.session_state.get("last_saved"):
                st.session_state._save_sig = _sig
            else:
                store.save(MAIN_ID, st.session_state.project_name, _payload)
                st.session_state._db_stamp = str(store.updated_at(MAIN_ID))
                st.session_state.last_saved = _h
                st.session_state._save_sig = _sig
                st.session_state.periods_dirty = set()
                st.session_state.periods_deleted = set()
                st.session_state.periods = {k: v for k, v in (_payload.get("periods") or {}).items()}
        except Exception as e:  # noqa: BLE001  (not marked saved, so the next click tries again)
            st.toast(f"Couldn't save: {e}", icon="⚠️")
