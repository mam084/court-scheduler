"""Coverage flags: coaches who can't be in two places at once, each resolvable once covered."""

from __future__ import annotations

from datetime import date
from html import escape as html_escape

import pandas as pd
import streamlit as st

from scheduler.data import blank
from scheduler.storage import MAIN_ID
from ui.common import plural
from ui.state import S


# A coach who can't be in two places at once (tournaments, programs). Each
# can be marked resolved once coverage is found, and stays resolved while
# the clash is the same (a changed date or team makes a new flag).
def _span(a, b) -> str:
    return f"{a:%a %b %-d}" + ("" if a == b else f"–{b:%a %b %-d}")


def coverage_flags() -> list[dict]:
    """Every coach clash (tournaments, programs) as a one-line flag with a stable id."""
    start, end, t_conflicts, p_conflicts = S.start, S.end, S.t_conflicts, S.p_conflicts
    out = []
    for c in t_conflicts:
        if c["end"] < date.today() and not (c["start"] <= end and c["end"] >= start):
            continue  # over and done, and not in the dates being looked at
        if c["kind"] == "two_travel":
            text = (f"**{c['coach']}** has two travel tournaments: **{c['travel']}** "
                    f"({', '.join(c['teams'])}) and **{c['travel2']}** ({', '.join(c['teams2'])})")
            tag = "Two travel tournaments"
        else:
            text = (f"**{c['coach']}** is working **{c['local']}** (local) and coaching "
                    f"{', '.join(c['teams'])} at **{c['travel']}**")
            tag = "Local + travel"
        out.append({"id": c["id"], "when": _span(c["start"], c["end"]), "sort": c["start"], "text": text,
                    "tag": tag, "area": "tournament"})
    for c in p_conflicts:
        (pa, pb), (sa, sb) = c["programs"], c["slots"]
        ds = c["dates"]
        when = (_span(ds[0], ds[0]) if len(ds) == 1 else f"{ds[0]:%b %-d} + {len(ds) - 1} more")
        out.append({"id": f"pp|{c['coach']}|{pa}|{pb}|{sa}|{sb}", "when": when, "sort": ds[0],
                    "text": f"**{c['coach']}** is on **{pa}** ({sa}) and **{pb}** ({sb}) at the same time",
                    "tag": "Two programs", "area": "program"})
    return sorted(out, key=lambda f: (f["sort"], f["text"]))




def sub_flags(result) -> list[dict]:
    """Practices the scheduler could only place if a coach finds a sub for a program ("Needs a sub"),
    as flags like the others."""
    iss = getattr(result, "issues", None)
    if iss is None or not len(iss):
        return []
    out = []
    for r in iss[iss["issue"].astype(str).str.startswith("Needs a sub")].itertuples():
        when = str(r.issue).split(": ", 1)[-1]
        detail = str(r.detail).replace("Possible if ", "")
        out.append({"id": f"sub|{r.team}|{when}", "when": when, "sort": pd.Timestamp(r.week_of).date(),
                    "text": f"**{r.team}** practice needs {detail}", "tag": "Needs a sub", "area": "program"})
    return out


def flags_print(start: date, end: date, result=None, title: str = "Coverage flags") -> bytes:
    """The printable coverage-flag list for the scheduling admin (open flags to cover, then the
    resolved ones), for these dates."""
    from scheduler.prints import flags_print_html
    flags = [f for f in S.FLAGS if f["sort"] <= end] + [f for f in sub_flags(result) if f["sort"] <= end]
    flags.sort(key=lambda f: (f["sort"], f["text"]))
    return flags_print_html(flags, S.RESOLVED, start, end, title).encode()


def compute():
    """Every flag for these dates and which are resolved."""
    inp = S.inp
    FLAGS = coverage_flags()
    _res = inp.resolved.dropna(how="all") if len(inp.resolved) else inp.resolved
    RESOLVED = {str(r["flag"]): r for _, r in _res.iterrows()} if len(_res) else {}
    S.update(FLAGS=FLAGS, RESOLVED=RESOLVED)


def resolve_flag(fid: str, note: str):
    """Mark a flag sorted out, with how it's covered; saved with the club's data."""
    store, ME, VIEWER, inp = S.store, S.ME, S.VIEWER, S.inp
    row = {"flag": fid, "note": note.strip(), "who": VIEWER.log_name if ME is not None else "",
           "at": f"{date.today():%m/%d/%Y}"}
    inp.resolved = pd.concat([inp.resolved[inp.resolved["flag"] != fid], pd.DataFrame([row])], ignore_index=True)
    if ME is not None:
        store.log(VIEWER.log_name, "Flag resolved", f"{fid}" + (f" · {note.strip()}" if note.strip() else ""),
                  MAIN_ID)


def reopen_flag(fid: str):
    """Put a resolved flag back on the list."""
    inp = S.inp
    inp.resolved = inp.resolved[inp.resolved["flag"] != fid].reset_index(drop=True)


def flags_panel(area: str | None = None, key: str = "flags", expanded: bool = False, result=None):
    """A collapsible list of coverage flags, one line each, with a Resolve
    button per flag (admins) and the resolved ones tucked underneath. With
    `result`, admins also get a printable list (including practices that need a sub)."""
    CAN, FLAGS, RESOLVED = S.CAN, S.FLAGS, S.RESOLVED
    flags = [f for f in FLAGS if area is None or f["area"] == area]
    if result is not None and area is None:  # the top panel also lists practices that need a sub
        flags = sorted(flags + sub_flags(result), key=lambda f: (f["sort"], f["text"]))
    if not flags:
        return
    open_ = [f for f in flags if f["id"] not in RESOLVED]
    done = [f for f in flags if f["id"] in RESOLVED]
    label = (f"🚩 {plural(len(open_), 'coverage flag')} to sort out" if open_ else "✅ Coverage flags all sorted out") \
        + (f" · {len(done)} resolved" if done else "")
    with st.expander(label, expanded=expanded and bool(open_), key=f"{key}_box"):
        if open_:
            st.caption("A coach who can't be in two places at once (or needs a sub to make a practice work). "
                       "Once you've found coverage, mark it resolved.")
        if CAN and result is not None:
            start, end, title = S.start, S.end, st.session_state.get("project_name") or ""
            st.download_button("🖨 Print coverage flags", lambda: flags_print(start, end, result, "Coverage flags"
                                                                             + (f" · {title}" if title else "")),
                               f"coverage_flags_{start:%Y%m%d}-{end:%Y%m%d}.html", mime="text/html",
                               key=f"{key}_print",
                               help="Every coach who needs coverage on these dates (tournaments, programs, and "
                                    "practices that need a sub), with a line to write in who covers it. "
                                    "Portrait. Open it in your browser, then print or save as PDF.")
        for f in open_:
            c1, c2 = st.columns([5, 1], vertical_alignment="center")
            c1.markdown(f"🔴 **{f['when']}** · {f['text']}  \n<small>{f['tag']}</small>", unsafe_allow_html=True)
            if CAN:
                with c2.popover("Resolve", width="stretch"):
                    note = st.text_input("How it's covered (optional)", key=f"{key}_note_{f['id']}",
                                         placeholder="e.g. Cliff subs for the local")
                    if st.button("Mark resolved", key=f"{key}_ok_{f['id']}", type="primary"):
                        resolve_flag(f["id"], note)
                        st.rerun()
        if done and st.toggle(f"Show resolved ({len(done)})", key=f"{key}_showdone"):
            for f in done:
                r = RESOLVED[f["id"]]
                extra = " · ".join(str(v) for v in (r.get("note"), r.get("who"), r.get("at")) if not blank(v))
                c1, c2 = st.columns([5, 1], vertical_alignment="center")
                c1.markdown(f"✅ ~~{f['when']}~~ · {f['text']}" + (f"  \n<small>{html_escape(extra)}</small>"
                                                                   if extra else ""), unsafe_allow_html=True)
                if CAN and c2.button("Reopen", key=f"{key}_re_{f['id']}", width="stretch"):
                    reopen_flag(f["id"])
                    st.rerun()
