"""HTML renderings of a schedule: court grid (club layout), month calendar.

Plain HTML/CSS so it renders the same in Streamlit or anywhere else.
Chips are colored by age group, with a left stripe for the club, and carry
their own background + text colors so they read in light and dark themes.
"""

from __future__ import annotations

import calendar
import html
from datetime import date, timedelta

import pandas as pd

from .config import ALL_COURTS, COURTS, DAYS, court_name, court_short, SLOT_KEYS, SLOTS, WEEK_ORDER, WEEKDAY_KEYS, WEEKDAYS, slots_overlap, week_start

# Age group -> (background, text).
AGE_COLORS = {
    12: ("#FFF4C2", "#6B5200"),
    13: ("#FFE3D6", "#8A2E0B"),
    14: ("#FFDDEA", "#8C1340"),
    15: ("#F3E1FF", "#5B1A8C"),
    16: ("#DCEBFF", "#0B3D91"),
    17: ("#D8F4F6", "#0B5C66"),
    18: ("#DFF5E1", "#1B5E20"),
}
DEFAULT_COLOR = ("#E6E9FF", "#2E3A9E")
LOCAL_COLORS = ("#ECEFF3", "#3B4250")
SIDE_COLORS = {"Girls": ("#DCE7FF", "#1E3A8A"), "Boys": ("#DDF3E2", "#1E5B2A")}
# Club -> stripe color (matches the club sheet: red/pink girls, green boys).
CLUB_STRIPES = ["#2563EB", "#2E7D32", "#D7263D", "#F29E4C", "#6A4C93"]  # girls blue, boys green

CSS = """
<style>
.cs-wrap { overflow-x: auto; }
.cs-grid { border-collapse: collapse; width: 100%; min-width: 860px; table-layout: fixed;
           font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; font-size: 12.5px; }
.cs-grid th { background: #1F2A44; color: #fff; padding: 6px 4px; font-weight: 600; text-align: center; }
.cs-grid th.cs-wkend { background: #3A4A6B; }
.cs-grid th.cs-day { width: 76px; }
.cs-grid td { border: 1px solid #C9CED8; vertical-align: top; padding: 4px; background: #FAFBFC; height: 64px; }
.cs-grid td.cs-wkend { background: #F3F5FA; }
.cs-grid td.cs-daycell { background: #EEF1F6; color: #1F2A44; font-weight: 600; text-align: center; vertical-align: middle; }
.cs-grid td.cs-closed { background: repeating-linear-gradient(45deg,#F4F4F4,#F4F4F4 8px,#E9E9E9 8px,#E9E9E9 16px);
                        color: #6B6B6B; text-align: center; vertical-align: middle; font-style: italic; }
.cs-chip { border-radius: 6px; padding: 3px 6px 3px 7px; margin: 0 0 4px 0; line-height: 1.25;
           border: 1px solid rgba(0,0,0,.08); border-left-width: 4px; }
.cs-chip b { display: block; font-size: 12.5px; }
.cs-chip span { font-size: 11.5px; opacity: .85; }
.cs-chip.cs-local { background-image: repeating-linear-gradient(135deg, transparent, transparent 6px, rgba(0,0,0,.04) 6px, rgba(0,0,0,.04) 12px); }
.cs-chip.cs-moved { outline: 2px dashed #C0392B; outline-offset: -2px; }
.cs-chip.cs-dim { opacity: .2; }
.cs-time { font-variant-numeric: tabular-nums; font-weight: 600; }
.cs-legend { display:flex; flex-wrap:wrap; gap:6px; margin: 6px 0 10px; font-family: system-ui, sans-serif; font-size: 12px; align-items:center; }
.cs-legend .cs-chip { margin: 0; }
.cs-legend .cs-lbl { color: #5A6478; margin: 0 2px 0 8px; }
.cs-cal td { height: 92px; }
.cs-cal .cs-datenum { font-weight: 700; color: #1F2A44; margin-bottom: 3px; }
.cs-cal .cs-out { background: #F1F2F4; }
.cs-cal .cs-chip { padding: 2px 5px; margin-bottom: 3px; font-size: 11.5px; }
.cs-tourn { background: #FFF1D6; color: #7A4B00; border-left-color: #E8A33D !important; font-weight: 600; }
.cs-travel { background: #E8F0FF; color: #1F3F7A; border-left-color: #4A7BD0 !important; font-weight: 600; }
.cs-notes { font-family: system-ui, sans-serif; font-size: 12.5px; margin: 2px 0 8px; display:flex; flex-wrap:wrap; gap:6px; }
.cs-notes .cs-chip { margin: 0; }
.cs-open { border: 1px dashed #B7BECB; border-radius: 6px; padding: 2px 6px; margin: 0 0 4px 0; color: #8A93A5;
           font-size: 11.5px; line-height: 1.25; background: transparent; }
.cs-open .cs-time { font-weight: 600; }
.cs-grid tr.cs-pc td { background: #F6F3EC; }
.cs-grid tr.cs-pc td.cs-daycell { background: #EDE6D6; color: #5A4A2A; border-top: 3px solid #1F2A44; }
.cs-grid tr.cs-pc td:not(.cs-daycell) { border-top: 3px solid #1F2A44; }
</style>
"""


def club_stripes(clubs) -> dict[str, str]:
    """Girls side (SDVBC) blue, boys side (Seaside) green; others get the next colors."""
    names = sorted({c for c in clubs if isinstance(c, str) and c})
    out, rest = {}, iter(CLUB_STRIPES[2:])
    for c in names:
        low = c.lower()
        if "girl" in low or "sdvbc" in low:
            out[c] = CLUB_STRIPES[0]
        elif "boy" in low or "seaside" in low:
            out[c] = CLUB_STRIPES[1]
        else:
            out[c] = next(rest, "#9AA3B2")
    return out


def _colors(r) -> tuple[str, str]:
    if r["kind"] == "Local":
        return SIDE_COLORS.get(r.get("side") or "", LOCAL_COLORS)
    try:
        return AGE_COLORS.get(int(r["age"]), DEFAULT_COLOR)
    except (TypeError, ValueError):
        return DEFAULT_COLOR


def _hit(r, highlight) -> bool:
    if not highlight:
        return True
    coaches = [c.strip() for c in str(r.get("coach") or "").split(";")]
    return r["name"] in highlight or r.get("club") in highlight or any(c in highlight for c in coaches)


def _chip(r, stripes, highlight=None, compact=False) -> str:
    is_local = r["kind"] == "Local"
    bg, fg = _colors(r)
    if is_local:
        stripe = {"Girls": CLUB_STRIPES[0], "Boys": CLUB_STRIPES[1]}.get(r.get("side") or "", "#9AA3B2")
    else:
        stripe = stripes.get(r.get("club"), "#9AA3B2")
    cls = ["cs-chip"]
    if is_local:
        cls.append("cs-local")
    weekday = DAYS.index(r["day"]) < 5 if r.get("day") in DAYS else True
    if not is_local and weekday and not r.get("on_pattern", True):
        cls.append("cs-moved")
    if not _hit(r, highlight):
        cls.append("cs-dim")
    name = html.escape(str(r["name"]))
    if r.get("pinned") == True:  # noqa: E712  (NaN-safe)
        name = "📌 " + name
    coaches = " · ".join(c.strip() for c in str(r.get("coach") or "").split(";") if c.strip())
    style = f"background:{bg};color:{fg};border-left-color:{stripe}"
    title = f"{r['slot_label']} · {coaches}" + ("" if "cs-moved" not in cls else " · moved from usual time") \
        + (" · placed by hand" if r.get("pinned") == True else "")  # noqa: E712
    if compact:
        court = f" · Ct {r['court']}" if not is_local else ""
        return (f'<div class="{" ".join(cls)}" style="{style}" title="{html.escape(title)}">'
                f'<span class="cs-time">{r["slot"]}</span> {name}{court}</div>')
    if is_local:
        return (f'<div class="{" ".join(cls)}" style="{style}" title="{html.escape(title)}">'
                f'<span class="cs-time">{r["slot"]}</span> {name}</div>')
    return (f'<div class="{" ".join(cls)}" style="{style}" title="{html.escape(title)}">'
            f'<b><span class="cs-time">{r["slot"]}</span> {name}</b><span>{html.escape(coaches)}</span></div>')


def _tourn_chip(tn, compact=False) -> str:
    cls = "cs-tourn" if tn.kind == "local" else "cs-travel"
    extra = ""
    if tn.kind == "travel" and not compact:
        extra = f"<span style='font-weight:400'> · {html.escape(', '.join(tn.teams))}</span>"
    span = f"{tn.start:%a %-m/%-d}" + ("" if not tn.multi_day else f"–{tn.end:%a %-m/%-d}")
    title = f"{tn.name} ({'local, ' + ', '.join(court_name(c) for c in tn.courts) if tn.kind == 'local' else 'travel'}) {span}"
    return f'<div class="cs-chip {cls}" title="{html.escape(title)}">{html.escape(tn.label())}{extra}</div>'


def court_grid_html(
    bookings: pd.DataFrame,
    day_dates: dict[str, date] | None = None,
    closures: dict[date, str] | None = None,
    highlight: set[str] | None = None,
    stripes: dict | None = None,
    tournaments: list | None = None,
    weekend_dates=None,
    open_slots: dict | None = None,
) -> str:
    """Club layout: courts as rows, days as columns (always Sun–Sat),
    practices listed with their times in each box. `day_dates` is an ordered
    {day: date} for a specific week (date.min = outside the period); None
    shows the recurring weekly template. Weekend days not in `weekend_dates`
    are shown as not open. `open_slots` ({day: {slot: courts}}, from
    open_slot_map) adds an "Open" box for every free practice time."""
    closures = closures or {}
    stripes = stripes or club_stripes(bookings["club"] if "club" in bookings else [])
    cols = list(day_dates) if day_dates else list(WEEK_ORDER)
    open_wk = None if weekend_dates is None else set(weekend_dates)

    tournaments = tournaments or []
    blocked, partial, notes = {}, {}, []
    for wd in cols:
        d = (day_dates or {}).get(wd)
        if d is not None and d in closures:
            blocked[wd] = f"Closed: {html.escape(closures[d])}"
        elif d is not None and d == date.min:
            blocked[wd] = "Outside this schedule period"
        elif (d is not None and open_wk is not None and d.weekday() >= 5 and d not in open_wk
              and not (len(bookings) and (bookings["day"] == wd).any())):
            blocked[wd] = "No practices (weekend not open)"
        elif d is not None:
            for tn in tournaments:
                if not tn.covers(d):
                    continue
                if tn.kind == "local" and set(COURTS) <= set(tn.courts):
                    blocked[wd] = html.escape(tn.label())
                elif tn.kind == "local":
                    partial.setdefault(wd, []).append(tn)
                elif tn not in notes:
                    notes.append(tn)
    if day_dates:
        for tn in tournaments:  # list everything happening this week above the grid
            if tn.kind == "local" and tn not in notes and any(
                    d not in (None, date.min) and tn.covers(d) for d in day_dates.values()):
                notes.append(tn)

    out = [CSS]
    if notes:
        out.append('<div class="cs-notes">' + "".join(_tourn_chip(tn) for tn in notes) + "</div>")
    out.append('<div class="cs-wrap"><table class="cs-grid"><thead><tr><th class="cs-day"></th>')
    for wd in cols:
        d = (day_dates or {}).get(wd)
        sub = f'<br><small style="font-weight:400;opacity:.8">{d:%b %-d}</small>' if d and d != date.min else ""
        cls = ' class="cs-wkend"' if wd not in WEEKDAYS else ""
        out.append(f"<th{cls}>{wd}{sub}</th>")
    out.append("</tr></thead><tbody>")
    for i, c in enumerate(ALL_COURTS):
        pc = c not in COURTS
        out.append(f'<tr{" class=cs-pc" if pc else ""}><td class="cs-daycell">{court_name(c)}'
                   + ('<br><small style="font-weight:400">programs only</small>' if pc else "") + "</td>")
        court_rows = bookings[bookings["court"] == c] if len(bookings) else bookings
        for wd in cols:
            if wd in blocked:
                if i == 0:
                    out.append(f'<td class="cs-closed" rowspan="{len(ALL_COURTS)}">{blocked[wd]}</td>')
                continue
            cell = court_rows[court_rows["day"] == wd].copy() if len(court_rows) else court_rows
            tn_here = [tn for tn in partial.get(wd, []) if c in tn.courts]
            chips = "".join(_tourn_chip(tn, compact=True) for tn in tn_here)
            items = []
            if len(cell):
                items += [(SLOT_KEYS.index(r["slot"]), _chip(r, stripes, highlight)) for _, r in cell.iterrows()]
            if open_slots and not tn_here:
                taken = list(cell["slot"]) if len(cell) else []
                free = [sl for sl, cs in (open_slots.get(wd) or {}).items()
                        if c in cs and not any(slots_overlap(sl, x) for x in taken)]
                for run in _runs(free):
                    a_, b_ = run[0], run[-1]
                    if len(run) == 1:
                        txt, tip = a_, SLOTS[a_]
                    else:  # back-to-back weekend blocks read as one span: "9AM–1PM Open"
                        tip = f"{SLOTS[a_].split('–')[0]}–{SLOTS[b_].split('–')[-1]}"
                        txt = f"{a_}–{WEEKEND_ENDS.get(b_, b_)}"
                    items.append((SLOT_KEYS.index(a_) + 0.5,
                                  f'<div class="cs-open" title="Court {c} is free {html.escape(tip)}">'
                                  f'<span class="cs-time">{txt}</span> Open</div>'))
            chips += "".join(h for _, h in sorted(items, key=lambda x: x[0]))
            cls = ' class="cs-wkend"' if wd not in WEEKDAYS else ""
            out.append(f"<td{cls}>{chips}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


WEEKEND_ENDS = {"9AM": "11AM", "11AM": "1PM", "1PM": "3PM", "3PM": "5PM", "5PM": "7PM", "7PM": "9PM"}


def _runs(slots: list[str]) -> list[list[str]]:
    """Group back-to-back weekend blocks; weekday times stay separate."""
    out = []
    for sl in sorted(slots, key=SLOT_KEYS.index):
        if (out and sl in WEEKEND_ENDS and out[-1][-1] in WEEKEND_ENDS
                and WEEKEND_ENDS[out[-1][-1]] == sl):
            out[-1].append(sl)
        else:
            out.append([sl])
    return out


def open_slot_map(day_dates: dict | None, data, closures: dict | None = None) -> dict:
    """{day: {slot: courts}} of practice times a court could be used for.
    Template (day_dates None): every weekday timeslot on every court.
    A week: weekday timeslots on open dates, plus an open weekend's blocks
    and courts. Courts taken by a local tournament are left out; programs
    are taken care of by their own bookings in the grid."""
    closures = closures or (data.closures if data else {})
    out = {}
    if not day_dates:
        return {wd: {s: set(COURTS) for s in WEEKDAY_KEYS} for wd in WEEKDAYS}
    for wd, d in day_dates.items():
        if d is None or d == date.min or d in closures:
            continue
        if d.weekday() < 5:
            slots, courts = WEEKDAY_KEYS, set(COURTS)
        elif data is not None and d in data.weekends:
            slots, courts = data.weekends[d].blocks, set(data.weekends[d].courts) & set(COURTS)
        else:
            continue
        if data is not None:
            for tn in data.tournaments:
                if tn.kind == "local" and tn.covers(d):
                    courts -= set(tn.courts)
        if courts:
            out[wd] = {s: set(courts) for s in slots}
    return out


def legend_html(ages, stripes: dict) -> str:
    items = ['<span class="cs-lbl">Age</span>']
    for a in sorted({int(x) for x in ages if pd.notna(x)}):
        bg, fg = AGE_COLORS.get(a, DEFAULT_COLOR)
        items.append(f'<div class="cs-chip" style="background:{bg};color:{fg};border-left-color:{bg}">{a}s</div>')
    if stripes:
        items.append('<span class="cs-lbl">Club</span>')
        for club, col in stripes.items():
            items.append(f'<div class="cs-chip" style="background:#fff;color:#1F2A44;border-left-color:{col}">'
                         f'{html.escape(club)}</div>')
    items.append('<span class="cs-lbl">Programs</span>')
    for side, (bg, fg) in SIDE_COLORS.items():
        col = CLUB_STRIPES[0] if side == "Girls" else CLUB_STRIPES[1]
        items.append(f'<div class="cs-chip cs-local" style="background:{bg};color:{fg};border-left-color:{col}">'
                     f'{side}</div>')
    bg, fg = LOCAL_COLORS
    items.append(f'<div class="cs-chip cs-local" style="background:{bg};color:{fg};border-left-color:#9AA3B2">Other</div>')
    items.append('<div class="cs-chip cs-moved" style="background:#fff;color:#C0392B;border-left-color:#fff">'
                 'Moved / makeup</div>')
    items.append('<div class="cs-chip" style="background:#fff;color:#1F2A44;border-left-color:#fff">📌 Set by hand</div>')
    return CSS + '<div class="cs-legend">' + "".join(items) + "</div>"


def month_calendar_html(
    assignments: pd.DataFrame,
    start: date,
    end: date,
    closures: dict[date, str] | None = None,
    highlight: set[str] | None = None,
    include_locals: bool = False,
    weekend_dates: list[date] | None = None,
    tournaments: list | None = None,
) -> str:
    """Sun–Sat calendar; each day lists its practices (time, team, court).
    Weekend days that aren't open are greyed out."""
    closures = closures or {}
    weekend_dates = set(weekend_dates or [])
    stripes = club_stripes(assignments["club"]) if "club" in assignments else {}
    df = assignments if include_locals else assignments[assignments["kind"] == "Practice"]
    if include_locals and len(df):
        df = df.drop_duplicates(subset=["date", "slot", "name"])  # one chip per local, not per court
    if highlight and len(df):
        df = df[df.apply(lambda r: _hit(r, highlight), axis=1)]
    by_date = {d: g for d, g in df.groupby("date")} if len(df) else {}

    show_days = list(WEEK_ORDER)
    out = [CSS, '<div class="cs-wrap"><table class="cs-grid cs-cal"><thead><tr>']
    out += [f'<th{" class=cs-wkend" if wd not in WEEKDAYS else ""}>{wd}</th>' for wd in show_days]
    out.append("</tr></thead><tbody>")
    ws = week_start(start)
    while ws <= end:
        out.append("<tr>")
        for wd in show_days:
            d = ws + timedelta(days=WEEK_ORDER.index(wd))
            if d < start or d > end:
                out.append(f'<td class="cs-out"><div class="cs-datenum" style="opacity:.4">{d:%-d}</div></td>')
                continue
            if d in closures:
                out.append(f'<td class="cs-closed"><div class="cs-datenum">{_daynum(d, start)}</div>{html.escape(closures[d])}</td>')
                continue
            has_tourn = any(tn.covers(d) for tn in (tournaments or []))
            if d.weekday() >= 5 and d not in weekend_dates and d not in by_date and not has_tourn:
                out.append(f'<td class="cs-out"><div class="cs-datenum" style="opacity:.5">{d:%-d}</div></td>')
                continue
            g = by_date.get(d)
            chips = "".join(_tourn_chip(tn, compact=True) for tn in (tournaments or []) if tn.covers(d)
                            and (not highlight or tn.kind == "local" or set(tn.teams) & set(highlight)))
            if g is not None:
                g = g.assign(o=g["slot"].map(SLOT_KEYS.index)).sort_values(["o", "court"])
                chips += "".join(_chip(r, stripes, None, compact=True) for _, r in g.iterrows())
            cls = ' class="cs-wkend"' if d.weekday() >= 5 else ""
            out.append(f'<td{cls}><div class="cs-datenum">{_daynum(d, start)}</div>{chips}</td>')
        out.append("</tr>")
        ws += timedelta(days=7)
    out.append("</tbody></table></div>")
    return "".join(out)


def _daynum(d: date, start: date) -> str:
    """Day number, with the month on the 1st and on the first day shown (ranges can span months)."""
    return f"{d:%b %-d}" if d.day == 1 or d == start else f"{d:%-d}"


def range_label(start: date, end: date) -> str:
    if start.year != end.year:
        return f"{start:%b %-d, %Y} – {end:%b %-d, %Y}"
    if start.month != end.month:
        return f"{start:%b %-d} – {end:%b %-d, %Y}"
    if start.day == 1 and (end + timedelta(days=1)).month != end.month:
        return f"{calendar.month_name[start.month]} {start.year}"
    return f"{start:%b %-d}–{end:%-d, %Y}"


def week_dates(ws: date, start: date, end: date, weekend_dates=()) -> dict[str, date]:
    """Ordered {day: date} for every day of a Sun–Sat week (date.min when
    outside the period). `weekend_dates` is kept for compatibility; whether
    a weekend is open is shown by court_grid_html."""
    out = {}
    for i, wd in enumerate(WEEK_ORDER):
        d = ws + timedelta(days=i)
        out[wd] = d if start <= d <= end else date.min
    return out


def month_name(d: date) -> str:
    return f"{calendar.month_name[d.month]} {d.year}"


def practice_counts_html(counts: pd.DataFrame, highlight: set[str] | None = None) -> str:
    """Teams as rows, weeks as columns: practices scheduled / target.
    Short weeks are red; weeks with no target (cut off by the date range,
    or before a team's season starts) show just the count in grey."""
    if counts is None or counts.empty:
        return ""
    active = counts.groupby("week_of")[["target", "scheduled"]].sum()
    weeks = sorted(active[(active["target"] > 0) | (active["scheduled"] > 0)].index)
    order = (counts.groupby("team")["short"].sum().sort_values(ascending=False))
    teams = list(order.index) if order.max() > 0 else sorted(counts["team"].unique())
    if order.max() > 0:  # short teams first, then the rest alphabetically
        teams = list(order[order > 0].index) + sorted(order[order == 0].index)
    lookup = {(r.team, r.week_of): r for r in counts.itertuples()}
    out = [CSS, """<style>
.cs-counts td.cs-ok { background:#EAF6EC; color:#1B5E20; text-align:center; font-weight:600; }
.cs-counts td.cs-short1 { background:#FDE2E1; color:#9B1C1C; text-align:center; font-weight:700; }
.cs-counts td.cs-short2 { background:#F8B4B4; color:#7F1212; text-align:center; font-weight:700; }
.cs-counts td.cs-none { background:#F4F5F7; color:#8A93A3; text-align:center; }
.cs-counts td { height: 28px; vertical-align: middle; }
.cs-counts td.cs-team { text-align:left; font-weight:600; background:#EEF1F6; color:#1F2A44; }
.cs-counts tr.cs-hl td.cs-team { outline: 2px solid #1F4E99; outline-offset:-2px; }
</style>""", '<div class="cs-wrap"><table class="cs-grid cs-counts" style="min-width:0"><thead><tr><th class="cs-day" style="width:150px">Team</th>']
    out += [f"<th>Week of {w:%b %-d}</th>" for w in weeks]
    out.append("</tr></thead><tbody>")
    for tm in teams:
        cls = ' class="cs-hl"' if highlight and tm in highlight else ""
        out.append(f'<tr{cls}><td class="cs-team">{html.escape(str(tm))}</td>')
        for w in weeks:
            r = lookup.get((tm, w))
            if r is None or r.target == 0:
                n = 0 if r is None else r.scheduled
                out.append(f'<td class="cs-none" title="No weekly target this week">{n if n else "–"}</td>')
            elif r.short:
                c = "cs-short2" if r.short >= 2 else "cs-short1"
                out.append(f'<td class="{c}" title="Short {r.short} practice{'' if r.short == 1 else 's'}">{r.scheduled} of {r.target}</td>')
            else:
                out.append(f'<td class="cs-ok">{r.scheduled} of {r.target}</td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def short_list_text(short: pd.DataFrame) -> str:
    """'12-Madi (1 of 2), 15-Madi (0 of 2)' for one week's shortfalls."""
    return ", ".join(f"{r.team} ({r.scheduled} of {r.target})" for r in short.itertuples())


def calendar_print_html(assignments: pd.DataFrame, start: date, end: date, closures: dict | None = None,
                        highlight: set[str] | None = None, include_locals: bool = False,
                        weekend_dates: list[date] | None = None, tournaments: list | None = None,
                        title: str = "Practice calendar") -> str:
    """A standalone page of the calendar, one month per printed page
    (open it and print, or save as PDF from the print dialog)."""
    parts = []
    d = date(start.year, start.month, 1)
    while d <= end:
        last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        a, b = max(d, start), min(last, end)
        parts.append(f'<section class="cs-month"><h2>{d:%B %Y}</h2>'
                     + month_calendar_html(assignments, a, b, closures, highlight, include_locals,
                                           weekend_dates, tournaments) + "</section>")
        d = last + timedelta(days=1)
    who = f" · {html.escape(', '.join(sorted(highlight)))}" if highlight else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; color: #1f2a44; background: #fff; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }} h2 {{ font-size: 17px; margin: 18px 0 8px; }}
.cs-sub {{ color: #5b6475; font-size: 13px; margin-bottom: 8px; }}
.cs-tip {{ background: #eef1f6; border-radius: 6px; padding: 8px 12px; font-size: 13px; }}
@media print {{
  body {{ margin: 8mm; }} .cs-tip {{ display: none; }}
  .cs-month {{ break-after: page; }} .cs-month:last-child {{ break-after: auto; }}
  * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  @page {{ size: landscape; }}
}}
</style></head><body>
<h1>{html.escape(title)}</h1>
<div class="cs-sub">{start:%b %-d, %Y} – {end:%b %-d, %Y}{who}</div>
<div class="cs-sub">Dashed outline = a makeup or a practice off the team's usual day/time.</div>
<div class="cs-tip">To print or save as PDF: press Ctrl/Cmd + P. Each month prints on its own landscape page.</div>
{''.join(parts)}
</body></html>"""
