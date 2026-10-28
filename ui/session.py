"""The club's one schedule in st.session_state: opening it, switching between runs of dates, autosave."""

from __future__ import annotations

import hashlib
import json
from datetime import date

import streamlit as st

from scheduler import ScheduleInput
from scheduler.storage import MAIN_ID, all_periods, ensure_main, make_payload, period_key, read_period
from scheduler.views import range_label
from ui.common import next_month, payload_hash
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
        last_saved=payload_hash(make_payload(inp_, s_, e_, settings, result_, published_, periods=periods_)),
    )
    st.session_state.pop("_save_sig", None)


def open_project(pid: str = MAIN_ID, key: str | None = None) -> bool:
    """(Re)opens the club's schedule, at the period being viewed unless `key` says otherwise."""
    store = S.store
    rec = ensure_main(store, next_month(date.today()))
    if key is None and st.session_state.get("range_start"):
        key = period_key(st.session_state.range_start, st.session_state.range_end)
    _load_into_session(rec, key)
    return True


def _period_entry() -> dict:
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
    periods_[cur] = _period_entry()
    if CAN:
        st.session_state.periods_dirty.add(cur)
    if new is not None:
        s_, e_ = new
        settings = {"time_limit": st.session_state.time_limit, "makeup": st.session_state.makeup}
        result_, published_ = None, []
    else:
        s_, e_, settings, result_, published_ = read_period(periods_[key], st.session_state.inp)
    periods_.pop(period_key(s_, e_), None)
    st.session_state.update(
        result=result_, result_period=(result_.start, result_.end) if result_ else None,
        range_start=s_, range_end=e_,
        time_limit=int(settings.get("time_limit", 60)), makeup=bool(settings.get("makeup", True)),
        keep_prev=bool(settings.get("keep_prev", True)), published=published_,
        base_version=settings.get("base_version"), pending_prefs=list(settings.get("pending_prefs") or []))
    for k in ("w_range_start", "w_range_end"):
        st.session_state.pop(k, None)


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
            hashlib.sha1(json.dumps(inp.to_records(), sort_keys=True, default=str).encode()).hexdigest(),
            tuple(sorted(st.session_state.get("periods", {}))))
    if CAN and (_sig != st.session_state.get("_save_sig") or st.session_state.pop("_force_save", False)):
        try:
            _db = store.get(MAIN_ID)
            _payload = make_payload(inp, start, end, _settings, st.session_state.get("result"),
                                    st.session_state.get("published", []),
                                    periods=periods_for_save(_db["payload"] if _db else None))
            _h = payload_hash(_payload)
            if _h == st.session_state.get("last_saved"):
                st.session_state._save_sig = _sig
            else:
                store.save(MAIN_ID, st.session_state.project_name, _payload)
                st.session_state.last_saved = _h
                st.session_state._save_sig = _sig
                st.session_state.periods_dirty = set()
                st.session_state.periods_deleted = set()
                st.session_state.periods = {k: v for k, v in (_payload.get("periods") or {}).items()}
        except Exception as e:  # noqa: BLE001  (not marked saved, so the next click tries again)
            st.toast(f"Couldn't save: {e}", icon="⚠️")
