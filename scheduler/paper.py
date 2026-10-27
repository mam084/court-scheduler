"""Printed weeks in the club's paper layout.

Like the hand-written sheets on the board: each week is two landscape
pages (courts 1-4, then courts 5-7 and the practice court), days Sun-Sat
across, and every court box lists every timeslot of that day as its own
line: filled in where something is booked, a blank line to write on where
it's open. The day's header carries tournaments and closures, and the
second page ends with the day's practice counts and the week's total
against what the teams need.
"""

from __future__ import annotations

import html
from datetime import date, timedelta

import pandas as pd

from .config import (
    ALL_COURTS, COURTS, PRACTICE_COURT, SLOT_TIMES, WEEK_ORDER, WEEKDAY_KEYS, WEEKEND_KEYS,
    blocked_by_local_tournament, court_name, week_start,
)

HALVES = [([1, 2, 3, 4], "Courts 1–4"), ([5, 6, 7, PRACTICE_COURT], "Courts 5–7 & practice court")]

# Time labels as the club writes them.
LABEL = {"3:30": "3:30-4:30", "4:30": "4:30-5:30", "3:45": "3:45-5:30", "5:30": "5:30-7:20", "7:20": "7:20-9:10",
         "9AM": "9-11", "11AM": "11-1", "1PM": "1-3", "3PM": "3-5", "5PM": "5-7", "7PM": "7-9"}

CSS = """<style>
body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 24px; color: #1f2329; background: #fff; }
h1 { font-size: 20px; margin: 0 0 4px; }
.cs-sub { color: #5b6475; font-size: 13px; margin-bottom: 8px; }
.cs-tip { background: #eef1f6; border-radius: 6px; padding: 8px 12px; font-size: 13px; }
.cs-week { box-sizing: border-box; overflow: hidden; padding-top: 14px; }
.cs-week h2 { font-size: 17px; margin: 0 0 6px; font-weight: 600; letter-spacing: .5px; }
.cs-week h2 span { font-weight: 400; color: #5b6475; font-size: 12px; letter-spacing: 0; }
.pp { border-collapse: collapse; width: 100%; table-layout: fixed; font-size: 11.5px; }
.pp th, .pp td { border: 1px solid #555; vertical-align: top; }
.pp th { font-weight: 500; font-size: 12px; padding: 2px 4px; background: #fff; }
.pp th.ct { width: 46px; }
.pp td.ct { font-weight: 600; text-align: center; vertical-align: middle; font-size: 12px; }
.pp .dh { text-align: left; height: 46px; }
.pp .dh .num { display: inline-block; border: 1px solid #555; min-width: 20px; padding: 0 3px; font-size: 13px; }
.pp .dh .note { display: block; font-size: 10.5px; font-weight: 600; margin-top: 2px; line-height: 1.2; }
/* Lines sit at the bottom of each box (7:20 last), leaving room above for early privates. */
.pp td.box { padding: 6px 4px 2px; text-align: left; vertical-align: bottom; }
.ln { display: flex; gap: 3px; line-height: 14px; min-height: 18px; padding-top: 3px; box-sizing: border-box;
      border-bottom: 1px dotted #b3b8c2; text-align: left; }
.ln:last-child { border-bottom: none; }
.ln.cont { border-bottom-style: solid; border-bottom-color: #d5d8de; }
.ln .t { flex: 0 0 auto; width: 49px; color: #6b7280; font-size: 10px; font-variant-numeric: tabular-nums;
         white-space: nowrap; }
.ln .w { flex: 1 1 0; min-width: 0; font-weight: 600; font-size: 11px; text-align: left;
         overflow-wrap: break-word; hyphens: none; }
.ln .w small { font-weight: 400; color: #555; }
.ln.pv .w { font-weight: 500; font-style: italic; }
.ln.tn .w { text-transform: uppercase; letter-spacing: .3px; }
td.off { background: repeating-linear-gradient(45deg,#f4f4f4,#f4f4f4 6px,#e8e8e8 6px,#e8e8e8 12px); color: #666;
         text-align: center; vertical-align: middle; font-style: italic; font-size: 11px; }
td.out { background: #f3f3f3; }
.pp tr.pc td { border-top: 2px solid #222; }
.foot td { border: none !important; font-size: 11px; text-align: center; padding-top: 3px; color: #333; }
.foot td b { font-size: 13px; }
@page { size: landscape; margin: 0.35in; }
@media print {
  body { margin: 0; } h1, .cs-tip, .cs-sub { display: none; }
  .cs-week { break-after: page; break-inside: avoid; padding-top: 0; }
  .cs-week:last-child { break-after: auto; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
</style>"""


def _txt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    v = str(v).strip()
    return "" if v.lower() in ("nan", "none") else v


def _what(r, local_coaches: bool) -> str:
    """One booking as it's written on the sheet."""
    name = html.escape(str(r["name"]))
    bits = []
    if r["kind"] == "Local" and local_coaches:
        who = " · ".join(c.strip() for c in _txt(r.get("coach")).split(";") if c.strip())
        if who:
            bits.append(html.escape(who))
    if r.get("extra") == True:  # noqa: E712
        bits.append("extra")
    if _txt(r.get("paired")):
        bits.append("3 on 2")
    return name + (f" <small>({', '.join(bits)})</small>" if bits else "")


def _line(label: str, what: str = "", cls: str = "") -> str:
    return f'<div class="ln{(" " + cls) if cls else ""}"><span class="t">{label}</span><span class="w">{what}</span></div>'


def _start(slot: str) -> int:
    h, m = SLOT_TIMES[slot][0]
    return h * 60 + m


def _box(d: date, c: int, book: pd.DataFrame, tourns: list, privs: list, local_coaches: bool) -> str:
    """Every timeslot of the day on court c, booked or blank."""
    weekend = d.weekday() >= 5
    here = book[(book["date"] == d) & (book["court"] == c)] if len(book) else book
    by_slot: dict[str, list] = {}
    for _, r in (here.iterrows() if len(here) else []):
        by_slot.setdefault(r["slot"], []).append(_what(r, local_coaches))
    if weekend:
        slots = list(WEEKEND_KEYS)
    else:  # JumpStart hours replace the 3:45 line when they're booked here
        early = [s for s in ("3:30", "4:30") if s in by_slot]
        slots = (early if early and "3:45" not in by_slot else ["3:45"]) + ["5:30", "7:20"]
    slots += [s for s in by_slot if s not in slots]  # anything else booked
    items = []  # (minutes, html)
    tn_here = [tn for tn in tourns if tn.kind == "local" and c in tn.courts and tn.covers(d)]
    if tn_here:  # a local tournament takes the court until 7 PM
        items.append((0, _line("until 7", html.escape(" / ".join(tn.name for tn in tn_here)), "tn")))
        slots = [s for s in slots if not blocked_by_local_tournament(s) or s in by_slot]
    for s in slots:
        what = " / ".join(by_slot.get(s, []))
        line = _line(LABEL.get(s, s), what)
        if not what:  # open: a second line, so there's room to write something in each hour
            line += '<div class="ln cont"><span class="t"></span><span class="w"></span></div>'
        items.append((_start(s), line))
    for p in privs:
        if p["date"] == d and p["court"] == c:
            who = html.escape(p["coach"] + (f" · {p['team']}" if p.get("team") else ""))
            items.append((p["start"] + 0.5, _line(html.escape(p["time"].replace(" ", "").lower()),
                                                  f"Private · {who}", "pv")))
    return "".join(h for _, h in sorted(items, key=lambda x: x[0]))


def _head(d: date, start: date, end: date, closures: dict, tourns: list, wd: str) -> str:
    notes = []
    if start <= d <= end:
        if d in closures:
            notes.append(html.escape(closures[d] or "Closed"))
        for tn in tourns:
            if tn.covers(d):
                kind = "" if tn.kind == "local" else " (travel)"
                notes.append(html.escape(tn.name + kind))
    note = "".join(f'<span class="note">{n}</span>' for n in notes)
    return f'<th class="dh">{wd} <span class="num">{d.day}</span>{note}</th>'


def _footer(book: pd.DataFrame, days: list[date], start: date, end: date, need: int | None) -> str:
    """Team practices per day (and per weekday time), the week's total vs. what teams need."""
    pr = book[book["kind"] == "Practice"] if len(book) else book
    cells, total = [], 0
    for d in days:
        g = pr[pr["date"] == d] if len(pr) else pr
        n = len(g)
        total += n
        if not (start <= d <= end) or n == 0:
            cells.append("<td></td>")
            continue
        if d.weekday() < 5:
            per = " · ".join(str(int((g["slot"] == s).sum())) for s in WEEKDAY_KEYS)
            cells.append(f'<td><b>{n}</b><br><span title="3:45 · 5:30 · 7:20">{per}</span></td>')
        else:
            cells.append(f"<td><b>{n}</b></td>")
    end_cell = f"<b>{total}</b>" + (f" of {need} needed" if need else " practices")
    return f'<tr class="foot"><td>{end_cell}</td>{"".join(cells)}</tr>'


def paper_weeks_html(assignments: pd.DataFrame, start: date, end: date, closures: dict | None = None,
                     tournaments: list | None = None, title: str = "Court schedule", privates: list | None = None,
                     practice_counts: pd.DataFrame | None = None, local_coaches: bool = False) -> str:
    """Every week in the paper layout, ready to print (two landscape pages a week)."""
    from .views import FIT_SCRIPT
    closures = closures or {}
    tourns = list(tournaments or [])
    a = assignments if assignments is not None else pd.DataFrame(columns=["date", "court", "slot", "kind", "name"])
    if len(a):  # one row per local per court is how locals are stored already; practices are one row each
        a = a.drop_duplicates(subset=["date", "slot", "court", "name"])
    need_by_week = {}
    if practice_counts is not None and len(practice_counts):
        need_by_week = practice_counts.groupby("week_of")["target"].sum().to_dict()
    parts = []
    ws = week_start(start)
    while ws <= end:
        days = [ws + timedelta(days=i) for i in range(7)]
        book = a[a["date"].map(week_start) == ws] if len(a) else a
        pv = [p for p in (privates or []) if week_start(p["date"]) == ws]
        month = f"{days[0]:%b %Y}" if days[0].month == days[-1].month else f"{days[0]:%b} – {days[-1]:%b %Y}"
        for i, (courts, label) in enumerate(HALVES):
            rows = ['<table class="pp"><thead><tr><th class="ct"></th>'
                    + "".join(_head(d, start, end, closures, tourns, wd) for d, wd in zip(days, WEEK_ORDER))
                    + "</tr></thead><tbody>"]
            for c in courts:
                cls = ' class="pc"' if c not in COURTS else ""
                cname = "PR CT" if c == PRACTICE_COURT else f"CT {c}"
                cells = []
                for d in days:
                    if not (start <= d <= end):
                        cells.append('<td class="out"></td>')
                    elif d in closures:
                        cells.append(f'<td class="off">{html.escape(closures[d] or "Closed")}</td>')
                    else:
                        cells.append(f'<td class="box">{_box(d, c, book, tourns, pv, local_coaches)}</td>')
                rows.append(f'<tr{cls}><td class="ct">{cname}</td>{"".join(cells)}</tr>')
            if i == len(HALVES) - 1:
                rows.append(_footer(book, days, start, end, need_by_week.get(ws)))
            rows.append("</tbody></table>")
            parts.append(f'<section class="cs-week"><h2>{month} <span>· week of {ws:%b %-d} · {label} · '
                         f'{html.escape(title)}</span></h2>{"".join(rows)}</section>')
        ws += timedelta(days=7)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>{CSS}</head><body>
<h1>{html.escape(title)}</h1>
<div class="cs-sub">{start:%b %-d, %Y} – {end:%b %-d, %Y}</div>
<div class="cs-tip">To print or save as PDF: press Ctrl/Cmd + P. Each week prints on two landscape pages
(courts 1–4, then courts 5–7 and the practice court). Every open time is a blank line to write in.</div>
{''.join(parts)}
{FIT_SCRIPT}
</body></html>"""
