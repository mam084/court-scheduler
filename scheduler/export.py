"""Exports: an Excel workbook in the club's grid layout, and .ics calendar
files per coach (importable into Google Calendar today; a live Google
Calendar sync can replace this later)."""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timedelta

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import COURTS, DAYS, SLOT_KEYS, SLOT_TIMES, SLOTS, WEEKDAYS
from .solver import ScheduleResult, week_start

_thin = Side(style="thin", color="C9CED8")
_border = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
_head = PatternFill("solid", fgColor="1F2A44")
_day = PatternFill("solid", fgColor="EEF1F6")
_closed = PatternFill("solid", fgColor="EDEDED")


def _cell_text(rows: pd.DataFrame) -> str:
    rows = rows.assign(o=rows["slot"].map(SLOT_KEYS.index)).sort_values("o")
    lines = []
    for _, r in rows.iterrows():
        weekday = r.get("day") in WEEKDAYS
        mark = " *" if r["kind"] == "Practice" and weekday and not r.get("on_pattern", True) else ""
        who = f" ({str(r['coach']).replace('; ', ', ')})" if r["coach"] else ""
        lines.append(f"{r['slot']} {r['name']}{who}{mark}")
    return "\n".join(lines)


def _grid_sheet(wb, title, bookings, day_labels, closed=None, cols=None):
    """Courts as rows, days as columns."""
    ws = wb.create_sheet(title=title[:31])
    closed = closed or {}
    cols = cols or list(WEEKDAYS)
    ws.cell(1, 1, "").fill = _head
    for j, wd in enumerate(cols, start=2):
        cell = ws.cell(1, j, day_labels.get(wd, wd))
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), _head
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 32 if day_labels else 18
    for i, c in enumerate(COURTS, start=2):
        rc = ws.cell(i, 1, f"Court {c}")
        rc.font, rc.fill, rc.border = Font(bold=True), _day, _border
        rc.alignment = Alignment(horizontal="center", vertical="center")
        court_rows = bookings[bookings["court"] == c] if len(bookings) else bookings
        n_lines = 1
        for j, wd in enumerate(cols, start=2):
            if wd in closed:
                cell = ws.cell(i, j, closed[wd] if i == 2 else "")
                cell.fill = _closed
            else:
                rows = court_rows[court_rows["day"] == wd] if len(court_rows) else court_rows
                txt = _cell_text(rows) if len(rows) else ""
                cell = ws.cell(i, j, txt)
                n_lines = max(n_lines, txt.count("\n") + 1)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = _border
        ws.row_dimensions[i].height = max(30, 15 * n_lines + 4)
    ws.column_dimensions["A"].width = 10
    for j in range(2, len(cols) + 2):
        ws.column_dimensions[get_column_letter(j)].width = 30
    ws.freeze_panes = "B2"
    return ws


def to_excel(result: ScheduleResult, closures: dict) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    a = result.assignments

    # Recurring template = each team's pattern + the weekly locals.
    tmpl = result.pattern.rename(columns={"team": "name"}).assign(kind="Practice", on_pattern=True)
    locals_ = a[a["kind"] == "Local"].drop_duplicates(subset=["day", "slot", "court", "name"])
    tmpl = pd.concat([tmpl, locals_[tmpl.columns.intersection(locals_.columns)]], ignore_index=True)
    _grid_sheet(wb, "Weekly Template", tmpl, {})

    weekend_open = set(result.weekend_dates)
    for ws_date, g in a.groupby(a["date"].map(week_start)):
        labels, closed, cols = {}, {}, []
        for i, wd in enumerate(DAYS):
            d = ws_date + timedelta(days=i)
            if i >= 5 and d not in weekend_open:
                continue
            cols.append(wd)
            labels[wd] = f"{wd}\n{d:%b %-d}"
            full = [tn for tn in result.tournaments
                    if tn.kind == "local" and tn.covers(d) and set(COURTS) <= set(tn.courts)]
            if d in closures:
                closed[wd] = f"Closed: {closures[d]}"
            elif not (result.start <= d <= result.end):
                closed[wd] = "Outside this schedule period"
            elif full:
                closed[wd] = f"Local tournament: {full[0].name}"
            else:
                trav = [tn.name for tn in result.tournaments if tn.covers(d)]
                if trav:
                    labels[wd] += "\n" + "; ".join(trav)
        _grid_sheet(wb, f"Week of {ws_date:%b %-d}", g, labels, closed, cols)

    flat = a.copy()
    flat["date"] = pd.to_datetime(flat["date"]).dt.strftime("%Y-%m-%d")
    flat = flat[["date", "day", "slot_label", "court", "kind", "name", "club", "coach", "on_pattern"]]
    flat.columns = ["Date", "Day", "Time", "Court", "Type", "Team / Program", "Club", "Coach(es)", "On usual schedule"]
    sh = wb.create_sheet("All Bookings")
    sh.append(list(flat.columns))
    for row in flat.itertuples(index=False):
        sh.append(list(row))
    for j, w in enumerate([12, 6, 14, 7, 9, 22, 10, 24, 18], start=1):
        sh.column_dimensions[get_column_letter(j)].width = w
    for c in sh[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), _head

    if result.tournaments:
        sh = wb.create_sheet("Tournaments")
        sh.append(["Tournament", "Type", "Start", "End", "Teams / courts"])
        for tn in result.tournaments:
            sh.append([tn.name, tn.kind.title(), f"{tn.start:%Y-%m-%d}", f"{tn.end:%Y-%m-%d}",
                       ", ".join(tn.teams) if tn.kind == "travel" else "Courts " + ", ".join(map(str, tn.courts))])
        for j, w in enumerate([28, 9, 12, 12, 50], start=1):
            sh.column_dimensions[get_column_letter(j)].width = w
        for c in sh[1]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), _head

    if len(result.issues):
        sh = wb.create_sheet("Review")
        cols = ["severity", "team", "week_of", "issue", "detail"]
        sh.append([c.replace("_", " ").title() for c in cols])
        for row in result.issues[cols].itertuples(index=False):
            sh.append([str(v) for v in row])
        for j, w in enumerate([10, 14, 12, 34, 50], start=1):
            sh.column_dimensions[get_column_letter(j)].width = w
        for c in sh[1]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), _head

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def to_ics(result: ScheduleResult, coach: str, club_name: str = "Club Practice",
           tz: str = "America/Los_Angeles") -> bytes:
    """Calendar file of one coach's practices and locals."""
    a = result.assignments
    mine = a[a["coach"].fillna("").map(lambda s: coach in [x.strip() for x in s.split(";")])]
    mine = mine.drop_duplicates(subset=["date", "slot", "name"])  # locals span several courts
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//court-scheduler//EN",
             f"X-WR-CALNAME:{_ics_escape(club_name)} – {_ics_escape(coach)}"]
    for _, r in mine.iterrows():
        (sh, sm), (eh, em) = SLOT_TIMES[r["slot"]]
        d = r["date"]
        dstart = datetime(d.year, d.month, d.day, sh, sm).strftime("%Y%m%dT%H%M%S")
        dend = datetime(d.year, d.month, d.day, eh, em).strftime("%Y%m%dT%H%M%S")
        courts = a[(a["date"] == d) & (a["slot"] == r["slot"]) & (a["name"] == r["name"])]["court"].tolist()
        where = "Court " + ", ".join(str(c) for c in sorted(courts))
        key = f"{d:%Y%m%d}|{r['slot']}|{r['name']}|{coach}"
        uid = hashlib.md5(key.encode()).hexdigest() + "@court-scheduler"
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
                  f"DTSTART;TZID={tz}:{dstart}", f"DTEND;TZID={tz}:{dend}",
                  f"SUMMARY:{_ics_escape(r['name'])} ({r['kind'].lower()})",
                  f"LOCATION:{_ics_escape(where)}",
                  f"DESCRIPTION:{_ics_escape(SLOTS[r['slot']] + ' · ' + where)}",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode()
