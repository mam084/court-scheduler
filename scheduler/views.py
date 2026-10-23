"""HTML renderings of a schedule: court grid (club layout), month calendar.

Plain HTML/CSS so it renders the same in Streamlit, in an exported page,
or anywhere else. Chips carry their own background + text colors so they
read correctly in light and dark themes.
"""

from __future__ import annotations

import calendar
import hashlib
import html
from datetime import date, timedelta

import pandas as pd

from .config import COURTS, SLOT_KEYS, WEEKDAYS

# Soft, distinguishable team colors (background, text).
PALETTE = [
    ("#DCEBFF", "#0B3D91"), ("#FFE3D6", "#8A2E0B"), ("#DFF5E1", "#1B5E20"),
    ("#F3E1FF", "#5B1A8C"), ("#FFF4C2", "#6B5200"), ("#D8F4F6", "#0B5C66"),
    ("#FFDDEA", "#8C1340"), ("#E6E9FF", "#2E3A9E"), ("#D6DEE8", "#24384F"),
    ("#FCE6C9", "#7A4300"), ("#E0E0E0", "#2B2B2B"), ("#D9F2E9", "#0F5940"),
]
LOCAL_COLORS = ("#ECEFF3", "#3B4250")

CSS = """
<style>
.cs-wrap { overflow-x: auto; }
.cs-grid { border-collapse: collapse; width: 100%; min-width: 860px; table-layout: fixed;
           font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; font-size: 12.5px; }
.cs-grid th { background: #1F2A44; color: #fff; padding: 6px 4px; font-weight: 600; text-align: center; }
.cs-grid th.cs-day { width: 82px; }
.cs-grid td { border: 1px solid #C9CED8; vertical-align: top; padding: 4px; background: #FAFBFC; height: 70px; }
.cs-grid td.cs-daycell { background: #EEF1F6; color: #1F2A44; font-weight: 600; text-align: center; vertical-align: middle; }
.cs-grid td.cs-daycell small { display: block; font-weight: 400; color: #5A6478; }
.cs-grid td.cs-closed { background: repeating-linear-gradient(45deg,#F4F4F4,#F4F4F4 8px,#E9E9E9 8px,#E9E9E9 16px);
                        color: #6B6B6B; text-align: center; vertical-align: middle; font-style: italic; }
.cs-chip { border-radius: 6px; padding: 3px 6px; margin: 0 0 4px 0; line-height: 1.25; border: 1px solid rgba(0,0,0,.08); }
.cs-chip b { display: block; font-size: 12.5px; }
.cs-chip span { font-size: 11.5px; opacity: .85; }
.cs-chip.cs-local { background-image: repeating-linear-gradient(135deg, transparent, transparent 6px, rgba(0,0,0,.04) 6px, rgba(0,0,0,.04) 12px); }
.cs-chip.cs-moved { border: 2px dashed #C0392B; }
.cs-chip.cs-dim { opacity: .22; }
.cs-time { font-variant-numeric: tabular-nums; font-weight: 600; }
.cs-legend { display:flex; flex-wrap:wrap; gap:6px; margin: 6px 0 10px; font-family: system-ui, sans-serif; font-size: 12px; }
.cs-legend .cs-chip { margin: 0; }
.cs-cal td { height: 96px; }
.cs-cal .cs-datenum { font-weight: 700; color: #1F2A44; margin-bottom: 3px; }
.cs-cal .cs-out { background: #F1F2F4; }
.cs-cal .cs-chip { padding: 2px 5px; margin-bottom: 3px; font-size: 11.5px; }
</style>
"""


def team_color(name: str) -> tuple[str, str]:
    h = int(hashlib.md5(name.encode()).hexdigest(), 16)
    return PALETTE[h % len(PALETTE)]


def color_map(names) -> dict[str, tuple[str, str]]:
    """Stable, collision-avoiding colors for a set of team names."""
    out, used = {}, set()
    for n in sorted(set(names)):
        h = int(hashlib.md5(n.encode()).hexdigest(), 16)
        for k in range(len(PALETTE)):
            idx = (h + k) % len(PALETTE)
            if idx not in used or len(used) >= len(PALETTE):
                used.add(idx)
                out[n] = PALETTE[idx]
                break
    return out


def _chip(r, colors, highlight: set[str] | None, compact=False) -> str:
    is_local = r["kind"] == "Local"
    bg, fg = LOCAL_COLORS if is_local else colors.get(r["name"], team_color(r["name"]))
    cls = ["cs-chip"]
    if is_local:
        cls.append("cs-local")
    if not is_local and not r.get("on_pattern", True):
        cls.append("cs-moved")
    if highlight:
        hit = r["name"] in highlight or any(c.strip() in highlight for c in str(r["coach"]).split(";"))
        if not hit:
            cls.append("cs-dim")
    name = html.escape(str(r["name"]))
    coach = html.escape(str(r["coach"])) if r["coach"] else ""
    if compact:
        court = f" · Ct {r['court']}" if "court" in r and not is_local else ""
        return (f'<div class="{" ".join(cls)}" style="background:{bg};color:{fg}">'
                f'<span class="cs-time">{r["slot"]}</span> {name}{court}</div>')
    title = f"{r['slot_label']} · {coach}" + ("" if r.get("on_pattern", True) else " · moved from usual time")
    if is_local:  # one line; coaches show on hover
        return (f'<div class="{" ".join(cls)}" style="background:{bg};color:{fg}" title="{html.escape(title)}">'
                f'<span class="cs-time">{r["slot"]}</span> {name}</div>')
    return (f'<div class="{" ".join(cls)}" style="background:{bg};color:{fg}" title="{html.escape(title)}">'
            f'<b><span class="cs-time">{r["slot"]}</span> {name}</b><span>{coach}</span></div>')


def court_grid_html(
    bookings: pd.DataFrame,
    day_dates: dict[str, date | None] | None = None,
    closures: dict[date, str] | None = None,
    highlight: set[str] | None = None,
    colors: dict | None = None,
) -> str:
    """Club layout: weekdays as rows, courts as columns, practices listed
    with their times inside each day/court box.

    `bookings` needs columns: day, slot, slot_label, court, kind, name, coach
    (and optionally on_pattern). `day_dates` maps Mon..Fri to a real date
    for a specific week, or is None for the recurring template.
    """
    closures = closures or {}
    colors = colors or color_map(bookings.loc[bookings["kind"] == "Practice", "name"]) if len(bookings) else {}
    out = [CSS, '<div class="cs-wrap"><table class="cs-grid"><thead><tr><th class="cs-day"></th>']
    out += [f"<th>Court {c}</th>" for c in COURTS]
    out.append("</tr></thead><tbody>")
    for wd in WEEKDAYS:
        d = (day_dates or {}).get(wd)
        sub = f"<small>{d:%b %-d}</small>" if d else ""
        out.append(f'<tr><td class="cs-daycell">{wd}{sub}</td>')
        if d is not None and d in closures:
            out.append(f'<td class="cs-closed" colspan="{len(COURTS)}">Closed: {html.escape(closures[d])}</td></tr>')
            continue
        if d is not None and d == date.min:  # outside the scheduled period
            out.append(f'<td class="cs-closed" colspan="{len(COURTS)}">Outside this schedule period</td></tr>')
            continue
        day_rows = bookings[bookings["day"] == wd]
        for c in COURTS:
            cell = day_rows[day_rows["court"] == c].copy()
            cell["o"] = cell["slot"].map(SLOT_KEYS.index)
            chips = "".join(_chip(r, colors, highlight) for _, r in cell.sort_values("o").iterrows())
            out.append(f"<td>{chips}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def legend_html(colors: dict, show_local=True) -> str:
    items = [f'<div class="cs-chip" style="background:{bg};color:{fg}">{html.escape(n)}</div>'
             for n, (bg, fg) in sorted(colors.items(), key=lambda kv: (int(kv[0].split()[0]) if kv[0].split()[0].isdigit() else 99, kv[0]))]
    if show_local:
        bg, fg = LOCAL_COLORS
        items.append(f'<div class="cs-chip cs-local" style="background:{bg};color:{fg}">Locals program</div>')
    items.append('<div class="cs-chip cs-moved" style="background:#fff;color:#C0392B">Moved / makeup</div>')
    return CSS + '<div class="cs-legend">' + "".join(items) + "</div>"


def month_calendar_html(
    assignments: pd.DataFrame,
    start: date,
    end: date,
    closures: dict[date, str] | None = None,
    highlight: set[str] | None = None,
    colors: dict | None = None,
    include_locals: bool = False,
) -> str:
    """Mon-Fri month calendar; each day lists its practices (time, team, court)."""
    closures = closures or {}
    df = assignments if include_locals else assignments[assignments["kind"] == "Practice"]
    if include_locals and len(df):
        # one chip per local program per day, not one per court
        df = df.drop_duplicates(subset=["date", "slot", "name"])
    colors = colors or color_map(assignments.loc[assignments["kind"] == "Practice", "name"])
    if highlight:
        df = df[df.apply(lambda r: r["name"] in highlight or any(
            c.strip() in highlight for c in str(r["coach"]).split(";")), axis=1)]
    by_date = {d: g for d, g in df.groupby("date")} if len(df) else {}

    out = [CSS, '<div class="cs-wrap"><table class="cs-grid cs-cal"><thead><tr>']
    out += [f"<th>{wd}</th>" for wd in WEEKDAYS]
    out.append("</tr></thead><tbody>")
    first = start
    while first.weekday() >= 5:  # month starts on a weekend: begin at Monday
        first += timedelta(days=1)
    ws = first - timedelta(days=first.weekday())
    while ws <= end:
        out.append("<tr>")
        for i in range(5):
            d = ws + timedelta(days=i)
            if d < start or d > end:
                out.append(f'<td class="cs-out"><div class="cs-datenum" style="opacity:.4">{d:%-d}</div></td>')
                continue
            if d in closures:
                out.append(f'<td class="cs-closed"><div class="cs-datenum">{d:%-d}</div>{html.escape(closures[d])}</td>')
                continue
            g = by_date.get(d)
            chips = ""
            if g is not None:
                g = g.assign(o=g["slot"].map(SLOT_KEYS.index)).sort_values(["o", "court"])
                chips = "".join(_chip(r, colors, None, compact=True) for _, r in g.iterrows())
            out.append(f'<td><div class="cs-datenum">{d:%-d}</div>{chips}</td>')
        out.append("</tr>")
        ws += timedelta(days=7)
    out.append("</tbody></table></div>")
    return "".join(out)


def week_dates(ws: date, start: date, end: date) -> dict[str, date]:
    """Mon..Fri -> date, with date.min for days outside the period."""
    out = {}
    for i, wd in enumerate(WEEKDAYS):
        d = ws + timedelta(days=i)
        out[wd] = d if start <= d <= end else date.min
    return out


def month_name(d: date) -> str:
    return f"{calendar.month_name[d.month]} {d.year}"
