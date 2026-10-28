"""Requests: coaches' change requests for the scheduling admins to approve or decline."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from scheduler import google as gsvc
from scheduler.storage import MAIN_ID, period_key
from scheduler.views import range_label
from ui.common import decided, g_conn
from ui.edits import apply_overrides
from ui.session import switch_period
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




def render():
    """Requests section."""
    store, VIEWER, CAN, start, end = S.store, S.VIEWER, S.CAN, S.start, S.end
    me_email = VIEWER.person.email if VIEWER.person else ""
    if CAN:
        st.markdown("Coaches' requests: moves, extra practices, cancellations and privates. **Approve** puts the "
                    "change in the schedule (it's rebuilt around it) and emails the coach; **Decline** emails "
                    "them your note. Nobody approves their own request.")
        conn_ = g_conn()
        if not gsvc.can_email(conn_):
            st.caption("Email isn't connected yet (Access → Google connection), so nobody gets emailed for now.")
        pend = store.requests("pending")
        if not pend:
            st.success("No requests waiting.")
        for r in pend:
            with st.container(border=True):
                same_period = r["period"] in ("", period_key(start, end))
                st.markdown(f"**{r['summary']}**  \n{r['who']} · {r['email'] or 'no email'} · {_req_when(r)}"
                            + (f"  \n_{r['note']}_" if r["note"] else ""))
                own = bool(me_email) and r["email"] == me_email
                if own:
                    st.caption("You asked for this, so another scheduling admin has to decide it.")
                c1, c2, c3 = st.columns([3, 1, 1], vertical_alignment="bottom")
                note_ = c1.text_input("Note to the coach (optional)", key=f"rq_note_{r['id']}",
                                      placeholder="e.g. moved to Court 5 instead")
                if not same_period:
                    try:
                        ps, pe = (date.fromisoformat(x) for x in r["period"].split("_"))
                        lab_ = range_label(ps, pe)
                    except ValueError:
                        lab_ = "its dates"
                    if c2.button(f"Open {lab_}", key=f"rq_go_{r['id']}", width="stretch",
                                 disabled=r["period"] not in st.session_state.periods):
                        switch_period(r["period"])
                        st.rerun()
                    c3.caption("Approve it from its own dates.")
                else:
                    if c2.button("Approve", type="primary", key=f"rq_ok_{r['id']}", width="stretch", disabled=own):
                        approve(r, note_.strip())
                if c3.button("Decline", key=f"rq_no_{r['id']}", width="stretch", disabled=own):
                    st.session_state.flash = decided(r, "denied", note_.strip())
                    st.rerun()
        done_ = [r for r in store.requests() if r["status"] != "pending"][:50]
        if done_:
            with st.expander(f"Decided ({len(done_)})"):
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
