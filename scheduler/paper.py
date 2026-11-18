"""Printed weeks in the club's paper layout.

Like the hand-written sheets on the board: each week is two landscape
pages (courts 1-4, then courts 5-7 and the practice court), days Sun-Sat
across, and every court box lists every timeslot of that day as its own
line: filled in where something is booked, a blank line to write on where
it's open. The day's header carries tournaments and closures, and the
second page can end with the day's practice counts and the week's total (SHOW_COUNTS)
against what the teams need.
"""

from __future__ import annotations

import html
from datetime import date, timedelta

import pandas as pd

from .config import (
    COURTS,
    PRACTICE_COURT,
    SLOT_TIMES,
    WEEK_ORDER,
    WEEKDAY_KEYS,
    WEEKEND_KEYS,
    blocked_by_local_tournament,
    week_start,
)
from .labels import label_of, with_labels

HALVES = [([1, 2, 3, 4], "Courts 1–4"), ([5, 6, 7, PRACTICE_COURT], "Courts 5–7 & practice court")]

# Time labels as the club writes them.
LABEL = {"3:30": "3:30-4:30", "4:30": "4:30-5:30", "3:45": "3:45-5:30", "5:30": "5:30-7:20", "7:20": "7:20-9:10",
         "7:20s": "7:20-8:30",
         "9AM": "9-11", "11AM": "11-1", "1PM": "1-3", "3PM": "3-5", "5PM": "5-7", "7PM": "7-9"}

CSS = """<style>
body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 24px; color: #1f2329;
       background: #fff; }
h1 { font-size: 20px; margin: 0 0 4px; }
.cs-sub { color: #5b6475; font-size: 13px; margin-bottom: 8px; }
.cs-tip { background: #eef1f6; border-radius: 6px; padding: 8px 12px; font-size: 13px; }
.cs-week { box-sizing: border-box; overflow: hidden; padding-top: 14px; }
.cs-week h2 { font-size: 15px; margin: 0 0 3px; font-weight: 600; letter-spacing: .5px; }
.cs-week h2 span { font-weight: 400; color: #5b6475; font-size: 12px; letter-spacing: 0; }
.pp { border-collapse: collapse; width: 100%; table-layout: fixed; font-size: 13px; }
.pp th, .pp td { border: 1px solid #555; vertical-align: top; }
.pp th { font-weight: 500; font-size: 14px; padding: 1px 4px; background: #fff; }
.pp th.ct { width: 40px; }
.pp td.ct { font-weight: 600; text-align: center; vertical-align: middle; font-size: 14px; }
.pp .dh { text-align: left; height: 34px; }
.pp .dh .num { display: inline-block; border: 1px solid #555; min-width: 20px; padding: 0 3px; font-size: 14px; }
.pp .dh .note { display: block; font-size: 12.5px; font-weight: 600; margin-top: 2px; line-height: 1.2; }
/* The practice times sit at the bottom of each box (7:20 last). Above them on a weekday is
   blank space to write in privates and anything else happening during the day; whatever's already booked
   before the first practice (a private, a local tournament) is printed there. */
.pp td.box { padding: 0 0 2px; text-align: left; vertical-align: bottom; }
.wi { min-height: 50px; display: flex; flex-direction: column; justify-content: flex-start; padding: 2px 4px 1px; }
.wi .sl { background: #fff; border-bottom: none; padding: 0 2px; margin: 0 -2px 2px; }
/* Nov 2026: no line between the write-in space and the first practice time; it just runs on. */
.slots { background: #fff; padding: 0 4px; }
.sl { border-bottom: 1px dotted #9aa0aa; }
.slots .sl:last-child { border-bottom: none; }
/* Nov 18, 2026: bigger, more readable names and times; the boxes' spare space pays for it, so a week
   still prints on its two pages at about the same size. */
.ln { display: flex; gap: 3px; line-height: 18px; min-height: 19px; padding-top: 1px; box-sizing: border-box; }
.ln .t { flex: 0 0 auto; color: #555b66; font-size: 12.5px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.ln .w { flex: 1 1 0; min-width: 0; font-weight: 600; font-size: 17px; letter-spacing: -.1px; text-align: right; overflow-wrap: break-word; }
.wk .ln { min-height: 20px; padding-top: 1px; }
.pp td.box.wkb { padding: 4px 5px 2px; }
.ln .w small { font-weight: 400; color: #555; font-size: 12px; }
.pv .ln .w { font-weight: 500; font-style: italic; font-size: 14px; }
/* Nov 2026: a local tournament blocks out the times it takes on each of its courts (like the color
   view), in black and white: a hatched, outlined block in place of those times. */
.tnb { border: 1.6px solid #1f2329; margin: 2px 0 3px; padding: 3px 4px; box-sizing: border-box;
       display: flex; flex-direction: column; justify-content: center; text-align: center;
       background: repeating-linear-gradient(45deg, #fff, #fff 5px, #d9d9d9 5px, #d9d9d9 7px); }
.tnb b { display: block; font-size: 14px; line-height: 1.15; overflow-wrap: break-word; background: #fff;
         padding: 0 2px; }
.tnb small { display: block; font-size: 10.5px; letter-spacing: .3px; text-transform: uppercase; color: #333;
             background: #fff; padding: 0 2px; margin: 0 auto 1px; }
/* Circled marks, like on the board: a program's session numbers, LAST, a local tournament's number. */
.last { display: inline-block; border: 1.4px solid #1f2329; border-radius: 999px; padding: 0 4px; margin-left: 3px;
        font-size: 11.5px; font-weight: 700; line-height: 14px; letter-spacing: .4px; vertical-align: 1px; }
.tn .ln .w { letter-spacing: .2px; }
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


def last_sessions(locals_) -> set[tuple[str, date]]:
    """(program, date) of each locals program's last session: the last day it runs (by its dates,
    skipping cancelled sessions), across all its days and times."""
    last: dict[str, date] = {}
    for lp in locals_ or []:
        if not lp.ranges:
            continue  # no end date: no last session
        d = max(b for _, b in lp.ranges)
        lo = min(a for a, _ in lp.ranges)
        while d >= lo and not lp.runs_on(d):
            d -= timedelta(days=1)
        if d >= lo and (lp.name not in last or d > last[lp.name]):
            last[lp.name] = d
    return set(last.items())


def session_marks(locals_, closures=None) -> dict:
    """(Locals row, date) -> what the sheet writes after a session program's name: ' #9' and a circled 1
    on a session's first meeting, then the meeting's number circled (Jump Start, Nov 2026)."""
    from .data import session_meetings
    out = {}
    for key, (num, k, _m) in session_meetings(locals_, closures).items():
        out[key] = (f" #{html.escape(num)}" if k == 1 else "") + _circle(k)
    return out


def _circle(x) -> str:
    return f'<span class="last">{html.escape(str(x))}</span>'


def _name_key(name: str) -> str:
    return " ".join(str(name).lower().split())


def numbers_every_session(name: str) -> bool:
    """Programs whose every session is numbered on the sheet (Open Gym, Nov 2026); other locals programs
    number only their first two."""
    return "open gym" in _name_key(name)


def program_seasons(locals_) -> list[tuple[str, list]]:
    """Each program's rows (by name), split into seasons the way the Programs tab does it: rows whose
    dates overlap are one season, so a fall and a spring "Boys Locals" are counted apart. Rows without
    dates are one group of their own."""
    by_name: dict[str, list] = {}
    for lp in locals_ or []:
        by_name.setdefault(lp.name, []).append(lp)
    out = []
    for name, lps in by_name.items():
        groups: list[list] = []  # [rows, first day, last day]
        for lp in sorted((x for x in lps if x.ranges), key=lambda x: min(a for a, _ in x.ranges)):
            a, b = min(a for a, _ in lp.ranges), max(b for _, b in lp.ranges)
            if groups and a <= groups[-1][2]:
                groups[-1][0].append(lp)
                groups[-1][2] = max(groups[-1][2], b)
            else:
                groups.append([[lp], a, b])
        out += [(name, g[0]) for g in groups]
        if any(not x.ranges for x in lps):
            out.append((name, [x for x in lps if not x.ranges]))
    return out


def _meetings(lps, closures) -> list[date]:
    """Every day a season of a program meets (cancelled sessions, skipped holidays and closures don't count)."""
    if not all(lp.ranges for lp in lps):
        return []
    d, end = min(a for lp in lps for a, _ in lp.ranges), max(b for lp in lps for _, b in lp.ranges)
    out = []
    while d <= end:
        if d not in closures and any(lp.runs_on(d) for lp in lps):
            out.append(d)
        d += timedelta(days=1)
    return out


def program_marks(locals_, closures=None) -> dict:
    """(Locals row, date) -> the marks the sheet writes after a program's name (Nov 2026):
    - Jump Start and other programs in sessions: ' #9' on a session's first meeting, every meeting's
      number circled;
    - Open Gym: every session's number circled, in order;
    - every other program: its first two sessions circled 1 and 2;
    and a circled LAST on the last session of everything not in numbered sessions. A program's days and
    times are counted together, from its first day (not the first day printed)."""
    closures = closures or {}
    out = dict(session_marks(locals_, closures))
    for name, lps in program_seasons(locals_):
        if any(lp.sessions for lp in lps):
            continue  # numbered by session above
        days = _meetings(lps, closures)
        for k, d in enumerate(days, 1):
            mark = _circle(k) if (k <= 2 or numbers_every_session(name)) else ""
            if k == len(days):
                mark += _circle("LAST")
            if mark:
                for lp in lps:
                    if lp.runs_on(d):
                        out[lp.row, d] = mark
    return out


def tournament_marks(tourns, locals_=None) -> dict[int, str]:
    """id() of each local tournament -> its circled number in its program (Nov 2026: 1st, 2nd, 3rd of
    the program's tournaments, by date), or a circled 1 and a circled ONLY for a program's one and only
    tournament. A program with seasons (fall and spring) counts each season's apart: a tournament goes
    with the season nearest its date. Tournaments without a program go by name."""
    spans: dict[str, list] = {}
    for name, lps in program_seasons(locals_):
        if all(lp.ranges for lp in lps):
            spans.setdefault(_name_key(name), []).append(
                (min(a for lp in lps for a, _ in lp.ranges), max(b for lp in lps for _, b in lp.ranges)))
    groups: dict[tuple, list] = {}
    for tn in tourns or []:
        if tn.kind != "local":
            continue
        key = _name_key(tn.program or tn.name)
        season = 0
        if len(spans.get(key, [])) > 1:
            ss = spans[key]
            season = min(range(len(ss)), key=lambda i: 0 if ss[i][0] <= tn.start <= ss[i][1]
                         else min(abs((tn.start - ss[i][0]).days), abs((tn.start - ss[i][1]).days)))
        groups.setdefault((key, season), []).append(tn)
    out = {}
    for g in groups.values():
        if len(g) == 1:
            out[id(g[0])] = ONLY_MARK
            continue
        for k, tn in enumerate(sorted(g, key=lambda t: (t.start, t.end)), 1):
            out[id(tn)] = _circle(k)
    return out


def _tourn_names(here: list, tmarks: dict) -> list[str]:
    """Local tournaments' names with their marks; same-named ones (two programs hosting one) once."""
    names: dict[str, list] = {}
    shown: dict[str, str] = {}
    for tn in here:
        k = _name_key(tn.name)
        shown.setdefault(k, tn.name)
        m = tmarks.get(id(tn), "")
        if m and m not in names.setdefault(k, []):
            names[k].append(m)
        names.setdefault(k, [])
    return [html.escape(shown[k]) + "".join(names[k]) for k in shown]


def only_tournaments(tourns) -> set[int]:
    """id() of each local tournament that is its program's one and only tournament (Nov 2026: the
    paper sheet marks it with a circled 1 and a circled ONLY). Tournaments without a program go by
    name; a multi-day tournament counts once."""
    by_prog: dict[str, list] = {}
    for tn in tourns or []:
        if tn.kind == "local":
            by_prog.setdefault((tn.program or tn.name).strip().lower(), []).append(tn)
    return {id(g[0]) for g in by_prog.values() if len(g) == 1}


ONLY_MARK = '<span class="last">1</span><span class="last">ONLY</span>'


def _what(r, local_coaches: bool, last: set | frozenset = frozenset()) -> str:
    """One booking as it's written on the sheet; a program's last session gets LAST circled."""
    name = html.escape(label_of(r))
    mark = r.get("pmark") if r["kind"] == "Local" else ""
    if isinstance(mark, str):  # its program's marks: "JS #9 (1)", "GHS (2)", "OG (5)", "(LAST)" (Nov 2026)
        name += mark
    elif r["kind"] == "Local" and (str(r["name"]), r["date"]) in last:  # program row unknown
        name += '<span class="last">LAST</span>'
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


def _slot(label: str, what: str = "", cls: str = "", two: bool = True) -> str:
    """One timeslot. two=True: the time on one line and what's booked (or a blank to
    write on) on the next, using the box's full width. two=False: one line, time left."""
    c = f' class="sl {cls}"' if cls else ' class="sl"'
    if two:
        return (f'<div{c}><div class="ln"><span class="t">{label}</span></div>'
                f'<div class="ln"><span class="w">{what}</span></div></div>')
    return f'<div{c}><div class="ln"><span class="t">{label}</span><span class="w">{what}</span></div></div>'


def _start(slot: str) -> int:
    h, m = SLOT_TIMES[slot][0]
    return h * 60 + m


def _box(d: date, c: int, book: pd.DataFrame, tourns: list, privs: list, local_coaches: bool,
         block_days: set | frozenset = frozenset(), last: set | frozenset = frozenset(),
         only: dict | None = None) -> str:
    """Every timeslot of the day on court c, booked or blank. Weekends and
    block days (a school break's weekdays) list the 2-hour blocks."""
    weekend = d.weekday() >= 5 or d in block_days
    here = book[(book["date"] == d) & (book["court"] == c)] if len(book) else book
    by_slot: dict[str, list] = {}
    for _, r in (here.iterrows() if len(here) else []):
        by_slot.setdefault(r["slot"], []).append(_what(r, local_coaches, last))
    labels = dict(LABEL)
    if weekend:
        slots = list(WEEKEND_KEYS)
    else:
        if by_slot.get("3:30") and by_slot.get("3:30") == by_slot.get("4:30"):
            # the same program both JumpStart hours (the usual case): one 3:30-5:30 slot
            by_slot["3:30"] = by_slot.pop("4:30")
            labels["3:30"] = "3:30-5:30"
        early = [s for s in ("3:30", "4:30") if s in by_slot]
        late = "7:20s" if "7:20s" in by_slot and "7:20" not in by_slot else "7:20"  # Volley Up's 7:20-8:30
        slots = (early if early and "3:45" not in by_slot else ["3:45"]) + ["5:30", late]
    slots += [s for s in by_slot if s not in slots]  # anything else booked
    items = []  # (minutes, html)
    only = only or {}
    tn_here = [tn for tn in tourns if tn.kind == "local" and c in tn.courts and tn.covers(d)]
    tn_min = None
    if tn_here:  # a local tournament takes the court until 7 PM: one block in place of those times
        gone = [x for x in slots if blocked_by_local_tournament(x) and x not in by_slot]
        if gone:
            tn_min = min(_start(x) for x in gone)
            # about as tall as the lines it replaces, so the times after it stay where they'd be
            h = len(gone) * (24 if weekend else 39) - 5
            head = f"Local tournament{'s' if len(tn_here) > 1 else ''} · until 7 PM"
            names = "".join(f"<b>{n}</b>" for n in _tourn_names(tn_here, only))
            items.append((tn_min, f'<div class="tnb" style="min-height:{h}px"><small>{head}</small>{names}</div>'))
        slots = [x for x in slots if x not in gone]
    for s in slots:
        items.append((_start(s), _slot(labels.get(s, s), " / ".join(by_slot.get(s, [])),
                                       "wk" if weekend else "", two=not weekend)))
    for p in privs:
        if p["date"] == d and p["court"] == c:
            who = html.escape(p["coach"] + (f" · {p['team']}" if p.get("team") else ""))
            items.append((p["start"] + 0.5, _slot(html.escape(p["time"].replace(" ", "").lower()),
                                                  f"Private · {who}", "pv", two=False)))
    items.sort(key=lambda x: x[0])
    if weekend:  # the 2-hour blocks already cover the whole day
        return "".join(h for _, h in items)
    first = min([_start(s) for s in slots] + ([tn_min] if tn_min is not None else [])) if (slots or tn_min) \
        else 24 * 60
    early = "".join(h for m, h in items if m < first)
    rest = "".join(h for m, h in items if m >= first)
    return f'<div class="wi">{early}</div><div class="slots">{rest}</div>'


def _head(d: date, start: date, end: date, closures: dict, tourns: list, wd: str,
          only: dict | None = None) -> str:
    """The day's name and date, with closures and tournaments under it. Local tournaments that day
    share one line; each travel tournament gets its own."""
    notes, only = [], only or {}
    if start <= d <= end:
        if d in closures:
            notes.append(html.escape(closures[d] or "Closed"))
        here = [tn for tn in tourns if tn.covers(d)]
        local = _tourn_names([tn for tn in here if tn.kind == "local"], only)
        if local:
            notes.append(" / ".join(local))
        notes += [html.escape(f"{tn.name} (travel)") for tn in here if tn.kind != "local"]
    note = "".join(f'<span class="note">{n}</span>' for n in notes)
    return f'<th class="dh">{wd} <span class="num">{d.day}</span>{note}</th>'


# The practice counts under each day (and the week's total vs. needed) are off for now, until the
# club decides what they should show. Flip to True to bring them back.
SHOW_COUNTS = False


def _footer(book: pd.DataFrame, days: list[date], start: date, end: date, need: int | None,
            block_days: set | frozenset = frozenset()) -> str:
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
        if d.weekday() < 5 and d not in block_days:
            per = " · ".join(str(int((g["slot"] == s).sum())) for s in WEEKDAY_KEYS)
            cells.append(f'<td><b>{n}</b><br><span title="3:45 · 5:30 · 7:20">{per}</span></td>')
        else:
            cells.append(f"<td><b>{n}</b></td>")
    end_cell = f"<b>{total}</b>" + (f" of {need} needed" if need else " practices")
    return f'<tr class="foot"><td>{end_cell}</td>{"".join(cells)}</tr>'


def paper_weeks_html(assignments: pd.DataFrame, start: date, end: date, closures: dict | None = None,
                     tournaments: list | None = None, title: str = "Court schedule", privates: list | None = None,
                     practice_counts: pd.DataFrame | None = None, local_coaches: bool = False,
                     block_days=None, locals_=None) -> str:
    """Every week in the paper layout, ready to print (two landscape pages a week).
    block_days: weekdays run in 2-hour blocks (a school break). locals_: the locals programs, so each
    one's sessions get their circled numbers and LAST (program_marks), and local tournaments their
    number in the program (tournament_marks)."""
    last = last_sessions(locals_)
    only = tournament_marks(tournaments, locals_)
    marks = program_marks(locals_, closures)
    block_days = set(block_days or ())
    from .views import FIT_SCRIPT, PRINT_BUTTON
    closures = closures or {}
    tourns = list(tournaments or [])
    a = assignments if assignments is not None else pd.DataFrame(columns=["date", "court", "slot", "kind", "name"])
    if len(a):  # one row per local per court is how locals are stored already; practices are one row each
        a = a.drop_duplicates(subset=["date", "slot", "court", "name"])
    a = with_labels(a)  # short team labels (S14-B, 16-E), as on the club's handwritten sheets
    if marks and len(a):
        from .gcal import program_row
        a = a.copy()
        rows_ = [program_row(r, locals_) if r["kind"] == "Local" else -1 for r in a.to_dict("records")]
        # None: the program row isn't known (falls back to LAST by name); "" for no marks
        a["pmark"] = [marks.get((w, r["date"]), "") if w >= 0 else None
                      for w, r in zip(rows_, a.to_dict("records"))]
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
                    # tournaments go on the first page only (the second has room for the courts)
                    + "".join(_head(d, start, end, closures, tourns if i == 0 else [], wd, only)
                              for d, wd in zip(days, WEEK_ORDER))
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
                        kind = "wkb" if d.weekday() >= 5 or d in block_days else "day"
                        cells.append(f'<td class="box {kind}">'
                                     f'{_box(d, c, book, tourns, pv, local_coaches, block_days, last, only)}</td>')
                rows.append(f'<tr{cls}><td class="ct">{cname}</td>{"".join(cells)}</tr>')
            if SHOW_COUNTS and i == len(HALVES) - 1:
                rows.append(_footer(book, days, start, end, need_by_week.get(ws), block_days))
            rows.append("</tbody></table>")
            parts.append(f'<section class="cs-week"><h2>{month} <span>· week of {ws:%b %-d} · {label} · '
                         f'{html.escape(title)}</span></h2>{"".join(rows)}</section>')
        ws += timedelta(days=7)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>{CSS}</head><body>
{PRINT_BUTTON}
<h1>{html.escape(title)}</h1>
<div class="cs-sub">{start:%b %-d, %Y} – {end:%b %-d, %Y}</div>
<div class="cs-tip">To print or save as PDF: press <b>Print</b> (or Ctrl/Cmd + P).
Each week prints on two landscape pages
(courts 1–4, then courts 5–7 and the practice court). Every open time is a blank line to write in, and
the blank space above each weekday's practices is for privates and anything else during the day.</div>
{''.join(parts)}
{FIT_SCRIPT}
</body></html>"""
