"""Making changes to the schedule: hand edits (overrides), coaches' preferences and change requests.

Shared by Edit practices, Requests, Coaches and Privates."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from scheduler import ScheduleInput, mail
from scheduler.data import blank
from scheduler.storage import MAIN_ID, make_payload, period_key, read_payload, read_published
from ui.common import decided, saved_changed, sched_admin_emails, send_mail
from ui.session import open_project
from ui.state import S


def without_pin(ov: pd.DataFrame, team: str, d: date, slot: str) -> pd.DataFrame:
    """The overrides minus any hand 'Add' that placed this team's practice at d/slot, so a hand-placed
    practice can be moved or removed again (instead of the old Add fighting the new change)."""
    if ov.empty:
        return ov
    ds = pd.to_datetime(ov["date"].astype(str), errors="coerce").dt.date
    hit = ((ov["team"] == team) & ov["action"].astype(str).str.strip().str.lower().isin(["add", "extra"])
           & (ds == d) & (ov["time"].astype(str).str.strip() == slot))
    return ov[~hit].reset_index(drop=True)


def _row_date(v) -> date | None:
    try:
        return pd.to_datetime(str(v)).date() if str(v).strip() else None
    except (ValueError, TypeError):
        return None


def apply_overrides(rows: list[dict], message: str, backup: pd.DataFrame | None = None,
                    replace_pin: tuple | None = None):
    """Add manual-change rows and rebuild the scheduled dates they land on (the run holding the
    first date now, any other run next); rolled back if the rebuild can't honor them. Dates that
    aren't scheduled yet just keep the change: it goes in when they're built.
    replace_pin=(team, date, slot) first drops the hand 'Add' that put that practice there."""
    from ui import dates
    from ui.session import switch_period
    inp = S.inp
    st.session_state.overrides_backup = inp.overrides.copy() if backup is None else backup
    base = without_pin(inp.overrides, *replace_pin) if replace_pin else inp.overrides
    inp.overrides = pd.concat([base, pd.DataFrame(rows)], ignore_index=True)
    st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
    days = [d for d in [_row_date(r.get("date")) for r in rows] + [replace_pin[1] if replace_pin else None] if d]
    keys = []
    for d in days:
        hit = dates.run_at(d)
        if hit and hit[0] not in keys:
            keys.append(hit[0])
    if days and not keys:  # none of its dates are scheduled yet
        st.session_state.pop("overrides_backup", None)
        appr = st.session_state.pop("approving", None)
        st.session_state.flash = ((decided(appr, "approved", st.session_state.pop("approving_note", ""))
                                   if appr else message) + " It goes in when those dates are scheduled.")
        st.rerun()
    if keys and dates.active_key() not in keys:
        switch_period(keys.pop(0))
    elif keys:
        keys.remove(dates.active_key())
    st.session_state.rebuild_queue = keys
    st.session_state.resolve = True
    st.session_state.flash = message
    st.rerun()


def court_use(d: date, slot: str) -> dict | None:
    """Who has each court at slot on day d: {court: what}; courts the day doesn't open (a block day
    on fewer courts, a closure) read 'not open then'. None when d isn't scheduled yet (nothing to
    check against)."""
    from scheduler.config import COURTS, SLOT_TIMES, WEEKDAY_KEYS, slots_overlap
    from ui import dates
    if dates.run_at(d) is None:
        return None
    cleaned, closures = S.cleaned, S.closures or {}
    if d in closures:
        return {c: "closed" for c in COURTS}
    if cleaned is not None and d in cleaned.weekends:
        wk = cleaned.weekends[d]
        if slot not in wk.blocks:
            return {c: "not open then" for c in COURTS}
        used = {c: "not open then" for c in COURTS if c not in wk.courts}
    elif d.weekday() >= 5 or slot not in WEEKDAY_KEYS:
        return {c: "not open then" for c in COURTS}
    else:
        used = {}
    r, _ = dates.window(d, d)
    if r is not None and len(r.assignments):
        for x in r.assignments.to_dict("records"):
            if x["date"] != d or not slots_overlap(str(x["slot"]), slot) or pd.isna(x.get("court")):
                continue
            used.setdefault(int(x["court"]), str(x["name"]))
    for tn in (cleaned.tournaments if cleaned is not None else []):
        if tn.blocks(d, slot):
            for c in tn.courts:
                used.setdefault(c, tn.name)
    if slot in SLOT_TIMES:
        a_, b_ = (h * 60 + m for h, m in SLOT_TIMES[slot])
        for p_ in S.privs or []:
            if p_["date"] == d and p_["start"] < b_ and a_ < p_["end"]:
                used.setdefault(p_["court"], f"private ({p_['coach']})")
    return used


def open_courts(d: date, slot: str, team: str = "", moving: tuple | None = None) -> list[int] | None:
    """Courts free at slot on day d (team practice courts, best first), None if d isn't scheduled yet.
    moving=(date, slot): that practice of `team` is the one being moved, so its court counts as free."""
    from scheduler.config import COURTS
    used = court_use(d, slot)
    if used is None:
        return None
    mine_ = _team_court(team, *moving) if moving and team and moving[0] == d and moving[1] == slot else None
    if mine_ is not None and used.get(mine_) == team:
        used.pop(mine_)
    return [c for c in COURTS if c not in used]


def _team_court(team: str, d: date, slot: str) -> int | None:
    from ui import dates
    r, _ = dates.window(d, d, with_preview=False)
    if r is None or not len(r.assignments):
        return None
    a = r.assignments
    hit = a[(a["kind"] == "Practice") & (a["name"] == team) & (a["date"] == d) & (a["slot"] == slot)]
    return int(hit["court"].iloc[0]) if len(hit) else None


def pref_key(r: dict, who_col: str) -> tuple:
    return tuple(str(r.get(c, "") if not blank(r.get(c, "")) else "").strip()
                 for c in (who_col, "type" if who_col == "who" else "choice", "days", "dates", "times", "note"))


def change_prefs(team: str, add_wish: dict | None, add_avoid: dict | None, drop_wish: list, drop_avoid: list,
                 message: str, what: str):
    """Saves a team's preference change. Scheduling admins edit the open
    schedule and rebuild. A coach's change is applied to the latest saved
    copy (so nobody else's edits are overwritten), saved right away, and
    listed for the admins to fit in at the next Auto-schedule."""
    store, ME, VIEWER, CAN, inp = S.store, S.ME, S.VIEWER, S.CAN, S.inp
    def apply(i: ScheduleInput):
        if add_wish:
            i.wishlist = pd.concat([i.wishlist, pd.DataFrame([add_wish])], ignore_index=True)
        if add_avoid:
            i.availability = pd.concat([i.availability, pd.DataFrame([add_avoid])], ignore_index=True)
        for attr, col, keys in (("wishlist", "team", drop_wish), ("availability", "who", drop_avoid)):
            df = getattr(i, attr).fillna("")
            left = list(keys)
            keep = []
            for idx, r in df.iterrows():
                k = pref_key(r.to_dict(), col)
                if k in left:
                    left.remove(k)
                else:
                    keep.append(idx)
            setattr(i, attr, getattr(i, attr).loc[keep].reset_index(drop=True))
    if ME is not None:
        store.log(VIEWER.log_name, "Preference", f"{team}: {what}", st.session_state.project_id)
    if CAN:
        apply(inp)
        st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
        st.session_state.resolve = True
        st.session_state.flash = message + " Schedule rebuilt."
        st.rerun()
    pid = st.session_state.project_id
    rec = store.get(pid)
    if rec is None:
        st.error("This schedule was deleted.")
        return
    i_, s_, e_, settings_, r_ = read_payload(rec["payload"])
    apply(i_)
    settings_["pending_prefs"] = list(settings_.get("pending_prefs") or []) + [
        f"{VIEWER.who.label if VIEWER.who else 'A coach'} · {team}: {what}"]
    store.save(pid, rec["name"], make_payload(i_, s_, e_, settings_, r_, read_published(rec["payload"]),
                                              periods=rec["payload"].get("periods")))
    saved_changed()
    open_project(pid)
    st.session_state.flash = message + " The scheduling admins will fit it in at the next rebuild."
    st.rerun()


def submit_request(kind: str, team: str, summary: str, detail: dict, note: str = ""):
    """A coach asks for a change. Saved for the scheduling admins (Requests tab) and emailed to them."""
    store, APP_LINK, VIEWER, start, end = S.store, S.APP_LINK, S.VIEWER, S.start, S.end
    me_ = VIEWER.person
    who = VIEWER.who.label if VIEWER.who else "A coach"
    email = me_.email if me_ else ""
    rid = store.add_request(email, who, kind, team, period_key(start, end), summary, detail, note)
    store.log(VIEWER.log_name, "Request", summary + (f" ({note})" if note else ""), MAIN_ID)
    req = store.get_request(rid)
    to = sched_admin_emails(exclude=email)
    err = send_mail(to, mail.new_request(req, APP_LINK), reply_to=email or None) if to else "no scheduling admins"
    st.session_state.flash = (f"Sent your request: {summary}. "
                              + ("The scheduling admins were emailed." if not err else
                                 "The scheduling admins will see it on the Requests tab.")
                              + " You'll hear back by email when it's decided.")
    st.rerun()
