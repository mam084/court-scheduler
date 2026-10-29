"""Requests: coaches' change requests for the scheduling admins to approve or decline."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from scheduler import google as gsvc
from scheduler.config import DAYS, SLOTS
from scheduler.solver import week_start
from scheduler.storage import MAIN_ID
from scheduler.views import club_stripes, court_grid_html, week_dates
from ui import dates
from ui.common import decided, g_conn
from ui.edits import apply_overrides, court_use, open_courts
from ui.state import S


def _req_when(r: dict) -> str:
    ts = pd.Timestamp(r["created_at"])
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
    return ts.tz_convert("America/Los_Angeles").strftime("%b %-d, %-I:%M %p")


def approve(req: dict, note: str):
    """Puts a request's change into the schedule; it's marked approved once the rebuild works."""
    inp = S.inp
    d = req["detail"]
    if req["kind"] == "private":
        inp.privates = pd.concat([inp.privates, pd.DataFrame([d["private"]])], ignore_index=True)
        st.session_state.flash = decided(req, "approved", note)
        st.rerun()
    st.session_state.approving = req
    st.session_state.approving_note = note
    rp = d.get("replace_pin")
    apply_overrides(d["rows"], f"Applying: {req['summary']}…",
                    replace_pin=(rp[0], date.fromisoformat(rp[1]), rp[2]) if rp else None)


def _target(req: dict) -> tuple[dict | None, tuple | None]:
    """A move or extra request's new practice (its Add/Extra row) and, for a move, (date, slot) it leaves."""
    rows = (req.get("detail") or {}).get("rows") or []
    row = next((r for r in rows if str(r.get("action", "")).strip().lower() in ("add", "extra")), None)
    rp = (req.get("detail") or {}).get("replace_pin")
    moving = (date.fromisoformat(rp[1]), rp[2]) if rp else None
    return row, moving


def _preview(req: dict, row: dict, d: date, slot: str, court, moving: tuple | None):
    """The request drawn on its week's court view: the new practice (and, for a move, where it
    leaves from) standing out, everything else faded."""
    team = row["team"]
    ws = week_start(d)
    r, _ = dates.window(ws, ws + timedelta(days=6))
    if r is None:
        st.caption("Nothing on the calendar that week yet.")
        return
    a = r.assignments.copy()
    mine_ = a[(a["kind"] == "Practice") & (a["name"] == team)]
    info = mine_.iloc[0].to_dict() if len(mine_) else {}
    if not info and S.cleaned is not None:
        t = next((x for x in S.cleaned.teams if x.name == team), None)
        info = {"club": t.club, "age": t.age, "coach": "; ".join(t.staff)} if t else {}
    names = {f"{team} · requested"}
    if moving:
        hit = (a["kind"] == "Practice") & (a["name"] == team) & (a["date"] == moving[0]) & (a["slot"] == moving[1])
        a.loc[hit, "name"] = f"{team} · moving from here"
        names.add(f"{team} · moving from here")
    if court is not None:
        new = {"date": d, "day": DAYS[d.weekday()], "slot": slot, "slot_label": SLOTS.get(slot, slot),
               "court": int(court), "kind": "Practice", "name": f"{team} · requested", "club": info.get("club", ""),
               "age": info.get("age"), "coach": info.get("coach", ""), "on_pattern": True,
               "extra": str(row.get("action", "")).lower() == "extra", "paired": ""}
        a = pd.concat([a, pd.DataFrame([new])], ignore_index=True)
    dd = week_dates(ws, ws, ws + timedelta(days=6), r.weekend_dates)
    st.markdown(court_grid_html(a, dd, S.closures, names, club_stripes(a["club"]), r.tournaments,
                                r.weekend_dates, privates=[p for p in (S.privs or []) if week_start(p["date"]) == ws]),
                unsafe_allow_html=True)


def _review(req: dict) -> tuple[bool, dict | None]:
    """What approving a move or extra would do, checked against the calendar: is the court (or any
    court) open then? Approving never pushes out anything already there. Returns (can approve, the
    request with the court it goes on)."""
    row, moving = _target(req)
    if row is None:
        return True, req
    team, slot = row["team"], str(row["time"]).strip()
    try:
        d = pd.to_datetime(str(row["date"])).date()
    except (ValueError, TypeError):
        return True, req
    want = int(row["court"]) if str(row.get("court", "")).strip().isdigit() else None
    free = open_courts(d, slot, team, moving)
    rid = req["id"]
    court = want
    if free is None:
        st.caption(f"{d:%b %-d} isn't scheduled yet: approving saves it, and it goes in "
                   + (f"on Court {want} " if want else "") + "when those dates are scheduled.")
    else:
        used = court_use(d, slot) or {}
        if want and want in free:
            st.caption(f"✓ Court {want} is open then.")
        elif want:
            st.warning(f"Court {want} is taken then ({used.get(want, 'booked')}).")
        if not free:
            st.error(f"No court is open {d:%a %b %-d} at {slot}, so approving would push another team out. "
                     "Decline it with a note (e.g. another time that's open).")
            court = None
        elif not (want and want in free):
            court = st.selectbox("Put it on", free, format_func=lambda c: f"Court {c}", key=f"rq_court_{rid}",
                                 help="Only courts open then are listed, so nothing already there moves.")
    with st.expander("Where it would go", expanded=True):
        _preview(req, row, d, slot, court if court is not None else (want if free is None else None), moving)
    if free is not None and court is None:
        return False, None
    if court is None:
        return True, req
    rows = [dict(r, court=str(court)) if r is row else r for r in req["detail"]["rows"]]
    return True, dict(req, detail=dict(req["detail"], rows=rows))


def render():
    """Requests section."""
    store, VIEWER, CAN = S.store, S.VIEWER, S.CAN
    me_email = VIEWER.person.email if VIEWER.person else ""
    if CAN:
        st.markdown("Coaches' requests: moves, extra practices, cancellations and privates. Each move or extra "
                    "practice shows where it would go. **Approve** puts it on an open court (it never pushes out "
                    "anything already there) and emails the coach; **Decline** emails them your note. Nobody "
                    "approves their own request.")
        conn_ = g_conn()
        if not gsvc.can_email(conn_):
            st.caption("Email isn't connected yet (Access → Google connection), so nobody gets emailed for now.")
        pend = store.requests("pending")
        if not pend:
            st.success("No requests waiting.")
        for r in pend:
            with st.container(border=True):
                st.markdown(f"**{r['summary']}**  \n{r['who']} · {r['email'] or 'no email'} · {_req_when(r)}"
                            + (f"  \n_{r['note']}_" if r["note"] else ""))
                own = bool(me_email) and r["email"] == me_email
                if own:
                    st.caption("You asked for this, so another scheduling admin has to decide it.")
                ok_, r_go = _review(r) if r["kind"] in ("move", "extra") else (True, r)
                c1, c2, c3, c4 = st.columns([3, 1, 1, 1], vertical_alignment="bottom")
                note_ = c1.text_input("Note to the coach (optional)", key=f"rq_note_{r['id']}",
                                      placeholder="e.g. moved to Court 5 instead")
                if c2.button("Approve", type="primary", key=f"rq_ok_{r['id']}", width="stretch",
                             disabled=own or not ok_):
                    approve(r_go, note_.strip())
                if c3.button("Decline", key=f"rq_no_{r['id']}", width="stretch", disabled=own):
                    st.session_state.flash = decided(r, "denied", note_.strip())
                    st.rerun()
                if c4.button("Remove", key=f"rq_rm_{r['id']}", width="stretch",
                             help="Take it off the list without approving or declining it. Nobody is emailed."):
                    store.delete_requests([r["id"]])
                    store.log(VIEWER.log_name, "Request removed", r["summary"], MAIN_ID)
                    st.session_state.flash = f"Removed the request: {r['summary']}."
                    st.rerun()
        done_ = [r for r in store.requests() if r["status"] != "pending"][:50]
        if done_:
            with st.expander(f"Decided ({len(done_)})"):
                h1, h2 = st.columns([3, 1], vertical_alignment="bottom")
                sure_h = h1.checkbox(f"Yes, clear all {len(done_)} decided requests", key="rq_clear_sure",
                                     help="Removes them from this list for good. The activity log keeps a record.")
                if h2.button("Clear history", disabled=not sure_h, width="stretch", key="rq_clear"):
                    n_ = store.delete_requests(decided=True)
                    store.log(VIEWER.log_name, "Request history cleared", f"{n_} removed", MAIN_ID)
                    st.session_state.flash = f"Cleared {n_} decided request{'s' if n_ != 1 else ''}."
                    st.rerun()
                st.dataframe(pd.DataFrame([{
                    "Asked": _req_when(r), "Who": r["who"], "Request": r["summary"],
                    "Decision": {"approved": "Approved", "denied": "Declined", "withdrawn": "Withdrawn"}.get(
                        r["status"], r["status"]),
                    "By": r["decided_by"], "Note": r["decision_note"]} for r in done_]),
                    hide_index=True, width="stretch")
    else:
        st.markdown("Your requests. Ask for a move, extra practice or cancellation in **Edit practices**, and for a "
                    "private in **Privates**. You'll get an email when a scheduling admin decides.")
        mine_ = store.requests(email=me_email) if me_email else []
        if not mine_:
            st.caption("No requests yet.")
        elif any(r["status"] != "pending" for r in mine_):
            if st.button("Clear my finished requests", key="rq_mine_clear",
                         help="Removes your approved, declined and withdrawn requests from this list."):
                store.delete_requests(decided=True, email=me_email)
                st.session_state.flash = "Cleared your finished requests."
                st.rerun()
        for r in mine_:
            badge = {"pending": "⏳ Waiting", "approved": "✅ Approved", "denied": "✖ Declined",
                     "withdrawn": "Withdrawn"}.get(r["status"], r["status"])
            with st.container(border=True):
                st.markdown(f"**{r['summary']}**  \n{badge} · asked {_req_when(r)}"
                            + (f"  \n{r['decided_by']}: _{r['decision_note']}_" if r["decision_note"] else ""))
                if r["status"] == "pending" and st.button("Withdraw", key=f"rq_wd_{r['id']}"):
                    store.decide_request(r["id"], "withdrawn", me_email)
                    store.log(VIEWER.log_name, "Request withdrawn", r["summary"], MAIN_ID)
                    st.session_state.flash = "Withdrawn."
                    st.rerun()
