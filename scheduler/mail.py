"""What the app's emails say. Plain functions: (subject, text, html).

Sending is in google.py; these only build the messages, so they're easy
to test and to preview in the app.
"""

from __future__ import annotations

import html as _h
from datetime import date

import pandas as pd

from .config import SLOT_KEYS, SLOTS, court_name

FOOT = "Sent by the Court Scheduler."


def _wrap(title: str, body_html: str, link: str = "") -> str:
    btn = (f'<p style="margin:18px 0"><a href="{_h.escape(link)}" style="background:#1F2A44;color:#fff;'
           f'padding:9px 16px;border-radius:6px;text-decoration:none">Open the Court Scheduler</a></p>') if link else ""
    return (f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:15px;color:#1f2329;'
            f'max-width:640px"><h2 style="font-size:18px;margin:0 0 10px">{_h.escape(title)}</h2>{body_html}{btn}'
            f'<p style="color:#888;font-size:12px;margin-top:22px">{FOOT}</p></div>')


# ------------------------------------------------------------ requests --

def new_request(req: dict, link: str = "") -> tuple[str, str, str]:
    """To the scheduling admins: a coach asked for a change."""
    who = req.get("who") or req.get("email")
    subject = f"Request from {who}: {req['summary']}"
    note = f"\nNote: {req['note']}" if req.get("note") else ""
    text = (f"{who} ({req.get('email')}) asked for a change:\n\n  {req['summary']}{note}\n\n"
            f"Approve or decline it on the Requests tab{': ' + link if link else ''}.\n\n{FOOT}")
    body = (f"<p><b>{_h.escape(who)}</b> ({_h.escape(req.get('email', ''))}) asked for a change:</p>"
            f'<p style="font-size:16px;padding:10px 12px;background:#f3f5fa;border-radius:6px">'
            f"{_h.escape(req['summary'])}</p>"
            + (f"<p><i>Note:</i> {_h.escape(req['note'])}</p>" if req.get("note") else "")
            + "<p>Approve or decline it on the <b>Requests</b> tab.</p>")
    return subject, text, _wrap("New change request", body, link)


def decision(req: dict, status: str, by: str, note: str = "", link: str = "") -> tuple[str, str, str]:
    """Back to the coach: approved or declined."""
    word = {"approved": "Approved", "denied": "Declined"}.get(status, status.title())
    subject = f"{word}: {req['summary']}"
    why = f"\n{by}: {note}" if note else ""
    text = (f"Your request was {word.lower()} by {by}:\n\n  {req['summary']}{why}\n\n"
            + ("It's on the schedule now.\n\n" if status == "approved" else "") + FOOT)
    body = (f"<p>Your request was <b>{word.lower()}</b> by {_h.escape(by)}:</p>"
            f'<p style="font-size:16px;padding:10px 12px;background:#f3f5fa;border-radius:6px">'
            f"{_h.escape(req['summary'])}</p>"
            + (f"<p><i>{_h.escape(by)}:</i> {_h.escape(note)}</p>" if note else "")
            + ("<p>It's on the schedule now.</p>" if status == "approved" else ""))
    return subject, text, _wrap(f"Request {word.lower()}", body, link)


# ------------------------------------------------- published schedule --

def coach_rows(practices: pd.DataFrame, teams: list[str]) -> pd.DataFrame:
    """A coach's teams' practices, sorted by team (in the order given) then date and time."""
    if practices is None or practices.empty or not teams:
        return pd.DataFrame(columns=["team", "date", "slot", "court"])
    p = practices[practices["name"].isin(teams)].copy()
    if "kind" in p.columns:
        p = p[p["kind"] == "Practice"]
    p["_t"] = p["name"].map({t: i for i, t in enumerate(teams)})
    p["_s"] = p["slot"].map(lambda s: SLOT_KEYS.index(s) if s in SLOT_KEYS else 99)
    p = p.sort_values(["_t", "date", "_s"])
    return p.rename(columns={"name": "team"})[["team", "date", "slot", "court"]].reset_index(drop=True)


def practices_email(coach: str, teams: list[str], practices: pd.DataFrame, period: str, version: str,
                    link: str = "", tournaments: list | None = None) -> tuple[str, str, str]:
    """To one coach: every practice of their teams in a published schedule,
    grouped by team, by date. Travel tournaments for those teams are listed
    under each team too."""
    rows = coach_rows(practices, teams)
    subject = f"Your practices: {period}"
    lines_t, parts_h = [], []
    for team in teams:
        g = rows[rows["team"] == team]
        tn = [t for t in (tournaments or []) if team in getattr(t, "teams", [])]
        lines_t.append(f"{team}" + ("" if len(g) else " (no practices in these dates)"))
        items = []
        for r in g.itertuples():
            when = f"{r.date:%a %b %-d}"
            lines_t.append(f"  {when:<11}  {SLOTS.get(r.slot, r.slot):<12}  {court_name(r.court)}")
            items.append(f"<tr><td style='padding:3px 14px 3px 0'>{when}</td>"
                         f"<td style='padding:3px 14px 3px 0'>{_h.escape(SLOTS.get(r.slot, r.slot))}</td>"
                         f"<td style='padding:3px 0'>{_h.escape(court_name(r.court))}</td></tr>")
        for t in tn:
            span = f"{t.start:%a %b %-d}" + ("" if t.end == t.start else f"–{t.end:%a %b %-d}")
            lines_t.append(f"  {span}  Tournament: {t.name}")
            items.append(f"<tr><td style='padding:3px 14px 3px 0'><b>{span}</b></td>"
                         f"<td colspan=2 style='padding:3px 0'><b>Tournament:</b> {_h.escape(t.name)}</td></tr>")
        lines_t.append("")
        parts_h.append(f"<h3 style='font-size:16px;margin:16px 0 4px'>{_h.escape(team)} "
                       f"<span style='font-weight:400;color:#666;font-size:13px'>· {len(g)} practices</span></h3>"
                       + (f"<table style='border-collapse:collapse;font-size:14px'>{''.join(items)}</table>"
                          if items else "<p style='color:#666'>No practices in these dates.</p>"))
    intro = f"Hi {coach}, here are your teams' practices for {period} ({version})."
    text = intro + "\n\n" + "\n".join(lines_t) + f"\n{'Open the schedule: ' + link if link else ''}\n\n{FOOT}"
    return subject, text, _wrap(f"Practices · {period}", f"<p>{_h.escape(intro)}</p>" + "".join(parts_h), link)


def teams_of(coach: str, teams) -> list[str]:
    """Teams a coach is on: head-coached first, then assistant, each in schedule order."""
    heads = [t.name for t in teams if coach in t.heads]
    assts = [t.name for t in teams if coach in t.assistants and coach not in t.heads]
    return heads + assts


def fmt_day(d: date) -> str:
    return f"{d:%a %b %-d}"
