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

from .config import (
    ALL_COURTS,
    COURTS,
    DAYS,
    PRACTICE_COURT,
    SLOT_KEYS,
    SLOT_TIMES,
    SLOTS,
    WEEK_ORDER,
    WEEKDAY_KEYS,
    WEEKDAYS,
    blocked_by_local_tournament,
    court_name,
    court_short,
    slots_overlap,
    week_start,
)

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
.cs-grid td.cs-daycell { background: #EEF1F6; color: #1F2A44; font-weight: 600; text-align: center;
                        vertical-align: middle; }
.cs-grid td.cs-closed { background: repeating-linear-gradient(45deg,#F4F4F4,#F4F4F4 8px,#E9E9E9 8px,#E9E9E9 16px);
                        color: #6B6B6B; text-align: center; vertical-align: middle; font-style: italic; }
.cs-chip { border-radius: 6px; padding: 3px 6px 3px 7px; margin: 0 0 4px 0; line-height: 1.25;
           border: 1px solid rgba(0,0,0,.08); border-left-width: 4px; }
.cs-chip b { display: block; font-size: 12.5px; }
.cs-chip span { font-size: 11.5px; opacity: .85; }
.cs-chip.cs-local { background-image: repeating-linear-gradient(135deg, transparent, transparent 6px,
                                                                 rgba(0,0,0,.04) 6px, rgba(0,0,0,.04) 12px); }
.cs-chip.cs-moved { outline: 2px dashed #C0392B; outline-offset: -2px; }
.cs-chip.cs-dim { opacity: .2; }
.cs-time { font-variant-numeric: tabular-nums; font-weight: 600; }
.cs-legend { display:flex; flex-wrap:wrap; gap:6px; margin: 6px 0 10px; font-family: system-ui, sans-serif;
             font-size: 12px; align-items:center; }
.cs-legend .cs-chip { margin: 0; }
.cs-legend .cs-lbl { color: #5A6478; margin: 0 2px 0 8px; }
.cs-cal td { height: 92px; }
.cs-cal .cs-datenum { font-weight: 700; color: #1F2A44; margin-bottom: 3px; }
.cs-cal .cs-out { background: #F1F2F4; }
.cs-cal .cs-chip { padding: 2px 5px; margin-bottom: 3px; font-size: 11.5px; }
.cs-tourn { background-color: #ECEFF3; color: #3B4250; font-weight: 700;
            background-image: repeating-linear-gradient(135deg, transparent, transparent 6px,
                                                        rgba(0,0,0,.04) 6px, rgba(0,0,0,.04) 12px); }
.cs-travel { background: #E8F0FF; color: #1F3F7A; border-left-color: #4A7BD0 !important; font-weight: 600; }
.cs-notes { font-family: system-ui, sans-serif; font-size: 12.5px; margin: 2px 0 8px; display:flex; flex-wrap:wrap;
            gap:6px; }
.cs-notes .cs-chip { margin: 0; }
.cs-open { border: 1px dashed #B7BECB; border-radius: 6px; padding: 2px 6px; margin: 0 0 4px 0; color: #8A93A5;
           font-size: 11.5px; line-height: 1.25; background: transparent; }
.cs-open .cs-time { font-weight: 600; }
.cs-open .cs-write { display: none; }
.cs-tag { display: inline-block; font-size: 10.5px; font-weight: 700; border-radius: 4px; padding: 0 4px;
          margin-top: 2px; background: #FDE68A; color: #6B4A00; }
.cs-tag.cs-xtra { background: #E0E7FF; color: #3730A3; }
.cs-tblock { border-radius: 6px; padding: 6px 8px 6px 9px; margin: 0 0 4px 0; min-height: 56px;
             border: 1px solid rgba(0,0,0,.08); border-left-width: 5px; display: flex; flex-direction: column;
             justify-content: center; line-height: 1.25;
             background-image: repeating-linear-gradient(135deg, transparent, transparent 6px,
                                                         rgba(0,0,0,.035) 6px, rgba(0,0,0,.035) 12px); }
.cs-tblock-wk { min-height: 110px; }
.cs-tb-head { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; opacity: .7;
              margin-bottom: 3px; }
.cs-tb-name { font-weight: 700; font-size: 12.5px; }
.cs-tb-name + .cs-tb-name { border-top: 1px solid rgba(0,0,0,.12); margin-top: 3px; padding-top: 3px; }
.cs-grid td.cs-tcell { vertical-align: middle; }
.cs-chip.cs-priv { background: #F5EEFF; color: #4C1D95; border-left-color: #8B5CF6; padding: 2px 5px; }
.cs-chip.cs-priv span { font-size: 11px; }
.cs-grid th .cs-thead-t { margin-top: 4px; text-align: left; font-weight: 600; }
.cs-grid th .cs-thead-t .cs-chip { margin: 0 0 3px 0; padding: 2px 5px; font-size: 11px; line-height: 1.2; }
.cs-grid th .cs-thead-t .cs-chip span { display: block; font-weight: 400; font-size: 10.5px; opacity: .9; }
.cs-grid tr.cs-pc td, .cs-grid tr.cs-pcb td { background: #F6F3EC; }
.cs-grid tr.cs-pc td.cs-daycell { background: #EDE6D6; color: #5A4A2A; border-top: 3px solid #1F2A44; }
.cs-grid tr.cs-pc td:not(.cs-daycell) { border-top: 3px solid #1F2A44; }
/* weekday time bands: same time at the same height every day; dotted lines between bands */
.cs-grid td.cs-band { height: 22px; padding: 3px 4px 1px; }
.cs-grid td.cs-b0 { border-bottom: 1px dotted #C9CED8; }
.cs-grid td.cs-b1 { border-top: 1px dotted #C9CED8; border-bottom: 1px dotted #C9CED8; }
.cs-grid td.cs-b2 { border-top: 1px dotted #C9CED8; }
</style>
"""


# Dark theme: the grids, calendar and counts on a dark page. Chips keep their
# own colors. "body" in front makes these win over CSS above.
DARK_CSS = """<style>
body .cs-grid th { background: #0E1422; color: #E8ECF3; }
body .cs-grid th.cs-wkend { background: #1B2438; }
body .cs-grid td { background: #1B2130; border-color: #394155; color: #E3E7EE; }
body .cs-grid td.cs-wkend { background: #20283A; }
body .cs-grid td.cs-daycell { background: #283044; color: #E8ECF3; }
body .cs-grid td.cs-closed { background: repeating-linear-gradient(45deg,#232833,#232833 8px,#2B313D 8px,#2B313D 16px);
                             color: #A3AAB8; }
body .cs-grid tr.cs-pc td, body .cs-grid tr.cs-pcb td { background: #26241C; }
body .cs-grid td.cs-b0, body .cs-grid td.cs-b1, body .cs-grid td.cs-b2 { border-color: #394155; }
body .cs-grid tr.cs-pc td.cs-daycell { background: #343024; color: #EADFC4; border-top-color: #8A93A8; }
body .cs-grid tr.cs-pc td:not(.cs-daycell) { border-top-color: #8A93A8; }
body .cs-open { border-color: #566079; color: #A0A9BC; }
body .cs-cal .cs-datenum { color: #E8ECF3; }
body .cs-cal td.cs-out { background: #161A24; }
body .cs-legend .cs-lbl { color: #A3AAB8; }
body .cs-counts td.cs-ok { background: #1D3A25; color: #A6E3B0; }
body .cs-counts td.cs-short1 { background: #4A1F24; color: #F6B8B8; }
body .cs-counts td.cs-short2 { background: #6E2329; color: #FFD3D3; }
body .cs-counts td.cs-none { background: #20252F; color: #808A9C; }
body .cs-counts td.cs-team { background: #283044; color: #E8ECF3; }
body .cs-counts-wrap { border-color: #394155; }
</style>"""


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


def _chip(r, stripes, highlight=None, compact=False, local_coaches=False) -> str:
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
    coaches = " · ".join(c.strip() for c in str(r.get("coach") or "").split(";") if c.strip())
    style = f"background:{bg};color:{fg};border-left-color:{stripe}"
    paired = _text(r.get("paired"))
    extra = r.get("extra") == True  # noqa: E712
    title = f"{r['slot_label']} · {coaches}" + ("" if "cs-moved" not in cls else " · moved from usual time") \
        + (" · extra practice" if extra else "") + (f" · {paired}" if paired else "")
    tags = ""
    if paired:
        tags += f'<span class="cs-tag" title="{html.escape(paired)}">Paired · 3 on 2</span> '
    if extra:
        tags += '<span class="cs-tag cs-xtra">Extra</span>'
    if compact:
        if is_local:
            cts = _text(r.get("courts_text"))
            court = f" · Ct {html.escape(cts)}" if cts else ""
        else:
            court = f" · Ct {court_short(r['court'])}"
        small = (" · paired" if paired else "") + (" · extra" if extra else "")
        lc = f' · {html.escape(coaches)}' if is_local and local_coaches and coaches else ""
        return (f'<div class="{" ".join(cls)}" style="{style}" title="{html.escape(title)}">'
                f'<span class="cs-time">{r["slot"]}</span> {name}{court}{small}{lc}</div>')
    who = f'<br><span>{html.escape(coaches)}</span>' if local_coaches and coaches else ""
    if is_local:
        return (f'<div class="{" ".join(cls)}" style="{style}" title="{html.escape(title)}">'
                f'<span class="cs-time">{r["slot"]}</span> {name}{who}</div>')
    return (f'<div class="{" ".join(cls)}" style="{style}" title="{html.escape(title)}">'
            f'<b><span class="cs-time">{r["slot"]}</span> {name}</b><span>{html.escape(coaches)}</span>'
            + (f"<div>{tags}</div>" if tags else "") + '</div>')


def _text(v) -> str:
    """'' for blank / NaN / None, else the text."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    v = str(v).strip()
    return "" if v.lower() in ("nan", "none") else v


def _minutes(slot: str) -> int:
    h, m = SLOT_TIMES[slot][0]
    return h * 60 + m


def _minutes_end(slot: str) -> int:
    h, m = SLOT_TIMES[slot][1]
    return h * 60 + m


def courts_label(courts) -> str:
    """[1, 2, 3, 8] -> '1-3, P'."""
    nums = sorted({int(c) for c in courts if pd.notna(c)})
    parts, run = [], []
    for c in nums + [None]:
        if c is not None and c != PRACTICE_COURT and run and c == run[-1] + 1:
            run.append(c)
            continue
        if run:
            parts.append(court_short(run[0]) if len(run) == 1 else f"{run[0]}-{run[-1]}")
        run = [c] if c is not None else []
    return ", ".join(parts)


def _private_chip(p) -> str:
    who = html.escape(str(p["coach"]))
    what = html.escape(p.get("what") or "")
    title = f"Private {p['time']}–{p['end_label']} · {p['coach']}" + (f" · {p['what']}" if p.get("what") else "")
    return (f'<div class="cs-chip cs-priv" title="{html.escape(title)}">'
            f'<span class="cs-time">{html.escape(p["time"])}</span>'
            f' Private · {who}' + (f'<br><span>{what}</span>' if what else "") + "</div>")


def _tourn_style(tn) -> str:
    """A local tournament wears its program's colors (girls blue, boys green), like the program itself."""
    if tn.kind != "local":
        return ""
    bg, fg = SIDE_COLORS.get(getattr(tn, "side", "") or "", LOCAL_COLORS)
    stripe = {"Girls": CLUB_STRIPES[0], "Boys": CLUB_STRIPES[1]}.get(getattr(tn, "side", "") or "", "#9AA3B2")
    return f' style="background:{bg};color:{fg};border-left-color:{stripe}"'


def _tourn_chip(tn, compact=False) -> str:
    cls = "cs-tourn" if tn.kind == "local" else "cs-travel"
    extra = ""
    if tn.kind == "travel" and not compact:
        extra = f"<span style='font-weight:400'> · {html.escape(', '.join(tn.teams))}</span>"
    if tn.kind == "local":
        extra = "<span style='font-weight:400'> · until 7 PM" + ("" if compact else f", Ct {courts_label(tn.courts)}") \
                + "</span>"
    span = f"{tn.start:%a %-m/%-d}" + ("" if not tn.multi_day else f"–{tn.end:%a %-m/%-d}")
    kind = ("local until 7 PM, " + ", ".join(court_name(c) for c in tn.courts)) if tn.kind == "local" else "travel"
    title = f"{tn.name} ({kind}) {span}"
    return (f'<div class="cs-chip {cls}"{_tourn_style(tn)} title="{html.escape(title)}">'
            f'{html.escape(tn.label())}{extra}</div>')


def _tourn_block(tns: list, weekend: bool = False) -> str:
    """One block for a court's local tournaments that day: 'Local tournament · until 7 PM', then each
    tournament's name. Girls blue / boys green when they're all one side, else grey."""
    sides = {getattr(tn, "side", "") or "" for tn in tns}
    side = sides.pop() if len(sides) == 1 else ""
    bg, fg = SIDE_COLORS.get(side, LOCAL_COLORS)
    stripe = {"Girls": CLUB_STRIPES[0], "Boys": CLUB_STRIPES[1]}.get(side, "#9AA3B2")
    head = f"Local tournament{'s' if len(tns) > 1 else ''} · until 7 PM"
    names = "".join(f'<div class="cs-tb-name">{html.escape(tn.label())}</div>' for tn in tns)
    title = "; ".join(f"{tn.name} (until 7 PM)" for tn in tns)
    return (f'<div class="cs-tblock{" cs-tblock-wk" if weekend else ""}" '
            f'style="background-color:{bg};color:{fg};border-left-color:{stripe}" title="{html.escape(title)}">'
            f'<div class="cs-tb-head">{head}</div>{names}</div>')


def _tourn_head_chip(tn) -> str:
    """A tournament under its day's name in the weekly grid."""
    cls = "cs-tourn" if tn.kind == "local" else "cs-travel"
    if tn.kind == "local":
        sub = f"Local · until 7 PM · Ct {courts_label(tn.courts)}"
    else:
        sub = "Travel" + (f" · {', '.join(tn.teams)}" if tn.teams else "")
    span = f"{tn.start:%a %-m/%-d}" + ("" if not tn.multi_day else f"–{tn.end:%a %-m/%-d}")
    title = f"{tn.name} · {sub} · {span}"
    return (f'<div class="cs-chip {cls}"{_tourn_style(tn)} title="{html.escape(title)}">{html.escape(tn.label())}'
            f'<span>{html.escape(sub)}</span></div>')


def court_grid_html(
    bookings: pd.DataFrame,
    day_dates: dict[str, date] | None = None,
    closures: dict[date, str] | None = None,
    highlight: set[str] | None = None,
    stripes: dict | None = None,
    tournaments: list | None = None,
    weekend_dates=None,
    open_slots: dict | None = None,
    privates: list | None = None,
    courts: list | None = None,
    local_coaches: bool = False,
) -> str:
    """Club layout: courts as rows, days as columns (always Sun–Sat),
    practices listed with their times in each box. `day_dates` is an ordered
    {day: date} for a specific week (date.min = outside the period); None
    shows the recurring weekly template. Weekend days not in `weekend_dates`
    are shown as not open. `open_slots` ({day: {slot: courts}}, from
    open_slot_map) adds an "Open" box for every free practice time.
    `privates`: rows from privates.display_rows for this week."""
    closures = closures or {}
    stripes = stripes or club_stripes(bookings.get("club", []))
    cols = list(day_dates) if day_dates else list(WEEK_ORDER)
    open_wk = None if weekend_dates is None else set(weekend_dates)

    def block_day(wd) -> bool:
        """A weekend day, or a weekday run in 2-hour blocks (a school break)."""
        d = (day_dates or {}).get(wd)
        return wd not in WEEKDAYS or bool(open_wk and d in open_wk)

    tournaments = tournaments or []
    blocked, partial, on_day = {}, {}, {}
    for wd in cols:
        d = (day_dates or {}).get(wd)
        if d is None or d == date.min:
            if d == date.min:
                blocked[wd] = "Outside this schedule period"
            continue
        on_day[wd] = [tn for tn in tournaments if tn.covers(d)]  # shown under the day's name
        # until 7 PM: its courts show it, and 7:20 practices still show
        partial[wd] = [tn for tn in on_day[wd] if tn.kind == "local"]
        if d in closures:
            blocked[wd] = f"Closed: {html.escape(closures[d])}"
        elif (open_wk is not None and d.weekday() >= 5 and d not in open_wk and not partial[wd]
              and not (len(bookings) and (bookings["day"] == wd).any())):
            blocked[wd] = "Weekend not open for practices (open it on the Open days tab)"

    out = [CSS]
    out.append('<div class="cs-wrap"><table class="cs-grid"><thead><tr><th class="cs-day"></th>')
    for wd in cols:
        d = (day_dates or {}).get(wd)
        sub = f'<br><small style="font-weight:400;opacity:.8">{d:%b %-d}</small>' if d and d != date.min else ""
        tn_html = "".join(_tourn_head_chip(tn) for tn in on_day.get(wd, []))
        if tn_html:
            sub += f'<div class="cs-thead-t">{tn_html}</div>'
        cls = ' class="cs-wkend"' if block_day(wd) else ""
        out.append(f"<th{cls}>{wd}{sub}</th>")
    out.append("</tr></thead><tbody>")
    rows_ = list(courts) if courts else list(ALL_COURTS)  # a subset, for a printed half-week
    # Each court is three rows, one per weekday time band (JumpStart/3:45, 5:30, 7:20), so a
    # time sits at the same height every day. Weekend days span all three with their blocks.
    n_bands = len(BAND_STARTS)
    # Bookings by (court, day), as plain records: grouped once instead of filtering
    # the table for every box.
    by_cell: dict = {}
    for r in (bookings.to_dict("records") if len(bookings) else []):
        by_cell.setdefault((r["court"], r["day"]), []).append(r)
    for i, c in enumerate(rows_):
        pc = c not in COURTS
        cells: dict[str, list] = {}  # wd -> [(band, minutes, html)]
        merged: set = set()          # (court, wd): a weekday whose first two bands are one tournament block
        for wd in cols:
            if wd in blocked:
                continue
            weekend = block_day(wd)
            cell = by_cell.get((c, wd), [])
            tns = [tn for tn in partial.get(wd, []) if c in tn.courts]
            # Local tournaments on this court: one block, listing each of them, in place of the
            # times they take (a weekday's 3:45 and 5:30; a weekend's blocks until 7 PM).
            items = [(0, -1, _tourn_block(tns, weekend))] if tns else []
            if tns and not weekend:
                merged.add((c, wd))
            for r in cell:
                m = _minutes(r["slot"])
                items.append((0 if weekend else _band(m), m, _chip(r, stripes, highlight, local_coaches=local_coaches)))
            here_pv = [pv for pv in (privates or []) if pv["day"] == wd and pv["court"] == c]
            for pv in here_pv:
                items.append((0 if weekend else _band(pv["start"]), pv["start"] + 0.25, _private_chip(pv)))
            if open_slots:
                taken = [r["slot"] for r in cell]
                free = [sl for sl, cs in (open_slots.get(wd) or {}).items()
                        if c in cs and not any(slots_overlap(sl, x) for x in taken)
                        and not any(pv["start"] < _minutes_end(sl) and _minutes(sl) < pv["end"] for pv in here_pv)]
                for run in _runs(free):
                    a_, b_ = run[0], run[-1]
                    if len(run) == 1:
                        txt, tip = a_, SLOTS[a_]
                    else:  # back-to-back weekend blocks read as one span: "9AM–1PM Open"
                        tip = f"{SLOTS[a_].split('–')[0]}–{SLOTS[b_].split('–')[-1]}"
                        txt = f"{a_}–{WEEKEND_ENDS.get(b_, b_)}"
                    m = _minutes(a_)
                    items.append((0 if weekend else _band(m), m + 0.5,
                                  f'<div class="cs-open" title="Court {c} is free {html.escape(tip)}">'
                                  f'<span class="cs-time">{txt}</span> Open<span class="cs-write"></span></div>'))
            cells[wd] = items
        for band in range(n_bands):
            tr_cls = ("cs-pc" if band == 0 else "cs-pcb") if pc else ("cs-cfirst" if band == 0 else "")
            out.append(f'<tr class="{tr_cls}">' if tr_cls else "<tr>")
            if band == 0:
                out.append(f'<td class="cs-daycell" rowspan="{n_bands}">{court_name(c)}'
                           + ('<br><small style="font-weight:400">programs &amp; privates</small>' if pc else "")
                           + "</td>")
            for wd in cols:
                if wd in blocked:
                    if i == 0 and band == 0:
                        out.append(f'<td class="cs-closed" rowspan="{len(rows_) * n_bands}">{blocked[wd]}</td>')
                    continue
                weekend = block_day(wd)
                if weekend and band > 0:
                    continue  # covered by the weekend cell's rowspan
                joined = (c, wd) in merged
                if joined and band == 1:
                    continue  # covered by the tournament block above
                its = [x for x in cells[wd] if weekend or x[0] == band or (joined and band == 0 and x[0] == 1)]
                html_ = "".join(h for _, _, h in sorted(its, key=lambda x: x[1]))
                if weekend:
                    out.append(f'<td class="cs-wkend cs-wkcell" rowspan="{n_bands}">{html_}</td>')
                elif joined and band == 0:
                    out.append(f'<td class="cs-band cs-b0 cs-tcell" rowspan="2">{html_}</td>')
                else:
                    out.append(f'<td class="cs-band cs-b{band}">{html_}</td>')
            out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


# Weekday time bands in the court grid: JumpStart / 3:45, then 5:30, then 7:20.
BAND_STARTS = [0, 17 * 60 + 30, 19 * 60 + 20]


def _band(minutes: float) -> int:
    """Which weekday band something starting at `minutes` (from midnight) goes in."""
    return sum(1 for b in BAND_STARTS[1:] if minutes >= b)


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
        if data is not None and d in data.weekends:  # a weekend or a break day: 2-hour blocks
            slots, courts = data.weekends[d].blocks, set(data.weekends[d].courts) & set(COURTS)
        elif d.weekday() < 5:
            slots, courts = WEEKDAY_KEYS, set(COURTS)
        else:
            continue
        per = {s: set(courts) for s in slots}
        if data is not None:
            for tn in data.tournaments:
                for s in slots:
                    if tn.kind == "local" and tn.covers(d) and blocked_by_local_tournament(s):
                        per[s] -= set(tn.courts)
        per = {s: cs for s, cs in per.items() if cs}
        if per:
            out[wd] = per
    return out


def legend_html(ages, stripes: dict, show_moved: bool = True) -> str:
    """The color key above the court view: age groups, clubs, programs and tags."""
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
    items.append(f'<div class="cs-chip cs-local" style="background:{bg};color:{fg};'
                 'border-left-color:#9AA3B2">Other</div>')
    if show_moved:
        items.append('<div class="cs-chip cs-moved" style="background:#fff;color:#C0392B;border-left-color:#fff">'
                     'Moved / makeup</div>')
    items.append('<span class="cs-tag">Paired · 3 on 2</span><span class="cs-tag cs-xtra">Extra</span>')
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
    local_coaches: bool = False,
) -> str:
    """Sun–Sat calendar; each day lists its practices (time, team, court).
    Weekend days that aren't open are greyed out."""
    closures = closures or {}
    weekend_dates = set(weekend_dates or [])
    stripes = club_stripes(assignments["club"]) if "club" in assignments else {}
    df = assignments if include_locals else assignments[assignments["kind"] == "Practice"]
    if include_locals and len(df):
        # One chip per local, not per court, listing the courts it uses.
        loc = df["kind"] == "Local"
        if loc.any():
            cts = df[loc].groupby(["date", "slot", "name"])["court"].agg(courts_label).rename("courts_text")
            df = df.merge(cts, left_on=["date", "slot", "name"], right_index=True, how="left")
        df = df.drop_duplicates(subset=["date", "slot", "name"])
    if highlight and len(df):
        df = df[[_hit(r, highlight) for r in df.to_dict("records")]]
    # Each day's chips in time order, then court. Sorted once and grouped as plain
    # records: much faster than sorting and walking a table day by day.
    by_date: dict = {}
    if len(df):
        df = df.assign(o=df["slot"].map(SLOT_KEYS.index)).sort_values(["date", "o", "court"], kind="stable")
        for r in df.to_dict("records"):
            by_date.setdefault(r["date"], []).append(r)

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
                out.append(f'<td class="cs-closed"><div class="cs-datenum">{_daynum(d, start)}</div>'
                           f'{html.escape(closures[d])}</td>')
                continue
            has_tourn = any(tn.covers(d) for tn in (tournaments or []))
            if d.weekday() >= 5 and d not in weekend_dates and d not in by_date and not has_tourn:
                out.append(f'<td class="cs-out"><div class="cs-datenum" style="opacity:.5">{d:%-d}</div></td>')
                continue
            chips = "".join(_tourn_chip(tn, compact=True) for tn in (tournaments or []) if tn.covers(d)
                            and (not highlight or tn.kind == "local" or set(tn.teams) & set(highlight)))
            chips += "".join(_chip(r, stripes, None, compact=True, local_coaches=local_coaches)
                             for r in by_date.get(d, []))
            cls = ' class="cs-wkend"' if d.weekday() >= 5 or d in weekend_dates else ""
            out.append(f'<td{cls}><div class="cs-datenum">{_daynum(d, start)}</div>{chips}</td>')
        out.append("</tr>")
        ws += timedelta(days=7)
    out.append("</tbody></table></div>")
    return "".join(out)


def _daynum(d: date, start: date) -> str:
    """Day number, with the month on the 1st and on the first day shown (ranges can span months)."""
    return f"{d:%b %-d}" if d.day == 1 or d == start else f"{d:%-d}"


def range_label(start: date, end: date) -> str:
    """'Nov 3 – Dec 4, 2026', 'December 2026' or 'Dec 6–20, 2026'."""
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
    """'December 2026'."""
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
.cs-counts td.cs-team { text-align:left; font-weight:600; background:#EEF1F6; color:#1F2A44;
                        position: sticky; left: 0; z-index: 1; }
.cs-counts-wrap { max-height: 70vh; overflow: auto; border: 1px solid #C9CED8; border-radius: 6px; }
.cs-counts thead th { position: sticky; top: 0; z-index: 2; }
.cs-counts thead th:first-child { left: 0; z-index: 3; }
.cs-counts tr.cs-hl td.cs-team { outline: 2px solid #1F4E99; outline-offset:-2px; }
</style>""", '<div class="cs-counts-wrap"><table class="cs-grid cs-counts" '
                'style="min-width:0;border-collapse:separate;border-spacing:0">'
                '<thead><tr><th class="cs-day" style="width:150px">Team</th>']
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
                plural_s = "" if r.short == 1 else "s"
                out.append(f'<td class="{c}" title="Short {r.short} practice{plural_s}">'
                           f'{r.scheduled} of {r.target}</td>')
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
                        title: str = "Practice calendar", local_coaches: bool = False) -> str:
    """A standalone page of the calendar, one month per printed page
    (open it and print, or save as PDF from the print dialog)."""
    parts = []
    d = date(start.year, start.month, 1)
    while d <= end:
        last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        a, b = max(d, start), min(last, end)
        parts.append(f'<section class="cs-month"><h2>{d:%B %Y}</h2>'
                     + month_calendar_html(assignments, a, b, closures, highlight, include_locals,
                                           weekend_dates, tournaments, local_coaches) + "</section>")
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
{PRINT_BUTTON}
<h1>{html.escape(title)}</h1>
<div class="cs-sub">{start:%b %-d, %Y} – {end:%b %-d, %Y}{who}</div>
<div class="cs-sub">Dashed outline = a makeup or a practice off the team's usual day/time.</div>
<div class="cs-tip">To print or save as PDF: press <b>Print</b> (or Ctrl/Cmd + P).
Each month prints on its own landscape page.</div>
{''.join(parts)}
</body></html>"""


# A Print button for the downloadable print pages (stays at the top right, never printed).
PRINT_BUTTON = """<style>
.cs-print { position: fixed; top: 14px; right: 16px; z-index: 10; font: 600 15px -apple-system, Segoe UI, Roboto,
            sans-serif; background: #1f2a44; color: #fff; border: 0; border-radius: 8px; padding: 10px 18px;
            cursor: pointer; box-shadow: 0 2px 6px rgba(0,0,0,.2); }
.cs-print:hover { background: #33416a; }
@media print { .cs-print { display: none !important; } }
</style><button class="cs-print" type="button" onclick="window.print()">&#128424;&#xFE0E; Print</button>"""


# Shrinks a printed week that would spill onto an extra page (shared by the print views).
FIT_SCRIPT = """<script>
// Only a page that's still too tall (a very busy week) gets shrunk to fit.
// The box is sized for the smaller of US Letter and A4 landscape inside the
// 0.35in margins, minus a safety buffer, so nothing spills onto an extra page.
function fitWeeks() {
  var pageH = 7.25 * 96, pageW = 10.1 * 96;
  document.querySelectorAll('.cs-week').forEach(function (s) {
    s.style.zoom = 1; s.style.width = pageW + 'px';
    var z = 1;
    for (var k = 0; k < 4; k++) {  // a shrunk page gets wider (fewer wrapped lines), then re-fits
      s.style.width = (pageW / z) + 'px';
      z = Math.min(1, pageH / s.scrollHeight);
    }
    s.style.width = (pageW / z) + 'px';
    s.style.zoom = z.toFixed(3);
  });
}
window.addEventListener('load', fitWeeks);
window.addEventListener('beforeprint', fitWeeks);
if (window.matchMedia) {  // re-measure once the print layout is actually applied
  var mq = window.matchMedia('print');
  (mq.addEventListener ? mq.addEventListener.bind(mq, 'change') : mq.addListener.bind(mq))(function (e) {
    if (e.matches) fitWeeks();
  });
}
window.addEventListener('afterprint', function () {
  document.querySelectorAll('.cs-week').forEach(function (s) { s.style.zoom = 1; s.style.width = ''; });
});
</script>"""

PRINT_HALVES = [([1, 2, 3, 4], "Courts 1–4"), ([5, 6, 7, PRACTICE_COURT], "Courts 5–7 & practice court")]


def weeks_print_html(assignments: pd.DataFrame, start: date, end: date, closures: dict | None = None,
                     highlight: set[str] | None = None, tournaments: list | None = None, weekend_dates=None,
                     title: str = "Court schedule", privates: list | None = None, show_moved: bool = False,
                     open_for=None, local_coaches: bool = False) -> str:
    """Every week of the court view on one standalone page. Printed (or
    saved as PDF), each week takes two landscape pages: courts 1-4, then
    courts 5-7 and the practice court. open_for(day_dates) -> open slots
    adds the "Open" boxes, with room to write a name in on paper."""
    a = assignments if show_moved else assignments.assign(on_pattern=True)
    stripes = club_stripes(a["club"]) if "club" in a else {}
    ages = a.loc[a["kind"] == "Practice", "age"] if len(a) else []
    parts = []
    ws = week_start(start)
    while ws <= end:
        dd = week_dates(ws, start, end, weekend_dates or [])
        book = a[a["date"].map(week_start) == ws] if len(a) else a
        pv = [p for p in (privates or []) if week_start(p["date"]) == ws]
        for courts, label in PRINT_HALVES:
            parts.append(f'<section class="cs-week"><h2>Week of {ws:%b %-d, %Y} · {label}'
                         f'<span class="cs-ttl"> · {html.escape(title)}</span></h2>'
                         + court_grid_html(book, dd, closures, highlight, stripes, tournaments, weekend_dates,
                                           open_slots=open_for(dd) if open_for else None,
                                           privates=pv, courts=courts, local_coaches=local_coaches) + "</section>")
        ws += timedelta(days=7)
    who = f" · {html.escape(', '.join(sorted(highlight)))}" if highlight else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; color: #1f2a44; background: #fff; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }} h2 {{ font-size: 16px; margin: 18px 0 6px; }}
.cs-ttl {{ font-weight: 400; color: #5b6475; font-size: 13px; }}
.cs-sub {{ color: #5b6475; font-size: 13px; margin-bottom: 8px; }}
.cs-tip {{ background: #eef1f6; border-radius: 6px; padding: 8px 12px; font-size: 13px; }}
/* Same layout on screen and on paper, so the size measured on screen is the printed size. */
.cs-week {{ box-sizing: border-box; overflow: hidden; }}
.cs-week h2 {{ margin: 0 0 6px; padding-top: 14px; }}
.cs-week .cs-grid {{ min-width: 0 !important; font-size: 13px !important; }}
.cs-week .cs-grid th {{ font-size: 14px; }}
.cs-week .cs-grid td {{ height: auto !important; }}
.cs-week .cs-chip b {{ font-size: 13.5px !important; }} .cs-week .cs-chip span {{ font-size: 12px !important; }}
.cs-week .cs-wrap {{ overflow: visible !important; }}
.cs-week .cs-open {{ min-height: 34px; color: #7A8396; }}
.cs-week .cs-open .cs-write {{ display: block; border-bottom: 1px solid #B7BECB; height: 16px; margin: 0 2px 2px; }}
@page {{ size: landscape; margin: 0.35in; }}
@media print {{
  body {{ margin: 0; }} h1, .cs-tip, .cs-legend-top, .cs-sub {{ display: none; }}
  .cs-week {{ break-after: page; break-inside: avoid; }} .cs-week:last-child {{ break-after: auto; }}
  .cs-week h2 {{ padding-top: 0; }}
  * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
}}
</style></head><body>
{PRINT_BUTTON}
<h1>{html.escape(title)}</h1>
<div class="cs-sub">{start:%b %-d, %Y} – {end:%b %-d, %Y}{who}</div>
<div class="cs-tip">To print or save as PDF: press <b>Print</b> (or Ctrl/Cmd + P).
Each week prints on two landscape pages: courts 1–4, then courts 5–7 and the practice court.</div>
<div class="cs-legend-top">{legend_html(ages, stripes, show_moved)}</div>
{''.join(parts)}
{FIT_SCRIPT}
</body></html>"""
