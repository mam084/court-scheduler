"""Printable pages: the month calendar, the coverage-flag list and every team's dates.

Each is one standalone HTML page (open it, press Print or save as PDF). Pages
are shrunk to fit their sheet of paper, so a busy month or a long team list
never spills onto an extra page.
"""

from __future__ import annotations

import calendar
import html
import re
from datetime import date, timedelta

import pandas as pd

from .config import SLOT_KEYS, WEEK_ORDER, court_short, team_is_boys, week_start
from .views import PRINT_BUTTON, _text, courts_label

GIRLS, BOYS, LOCAL = "#2563EB", "#2E7D32", "#F57C00"  # programs on neither side: tangerine
SECTION_NAMES = {"Girls": "SDVBC", "Boys": "Seaside"}

BASE_CSS = """
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 24px;
       color: #1f2a44; background: #fff; }
.pp-tip { background: #eef1f6; border-radius: 6px; padding: 8px 12px; font-size: 13px; margin: 0 0 14px;
          max-width: 760px; }
.pp-page { overflow: hidden; margin: 0 0 28px; }
.pp-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
           border-bottom: 2px solid #1f2a44; padding-bottom: 4px; margin-bottom: 8px; }
.pp-head h1 { font-size: 22px; margin: 0; letter-spacing: .2px; }
.pp-head .pp-sub { font-size: 12px; color: #5b6475; }
@media print {
  body { margin: 0; } .pp-tip { display: none; }
  .pp-page { margin: 0; break-after: page; break-inside: avoid; }
  .pp-page:last-child { break-after: auto; }
}
"""


def _fit_script(page_w_in: float, page_h_in: float) -> str:
    """Shrinks each .pp-page to its sheet (the smaller of US Letter and A4 inside the margins)."""
    return f"""<script>
function fitPages() {{
  var W = {page_w_in} * 96, H = {page_h_in} * 96;
  document.querySelectorAll('.pp-page').forEach(function (s) {{
    s.style.zoom = 1; s.style.width = W + 'px';
    var z = 1;
    for (var k = 0; k < 4; k++) {{  // shrinking makes room across, so re-measure a few times
      s.style.width = (W / z) + 'px';
      z = Math.min(1, H / s.scrollHeight);
    }}
    s.style.width = (W / z) + 'px';
    s.style.zoom = z.toFixed(3);
  }});
}}
window.addEventListener('load', fitPages);
window.addEventListener('beforeprint', fitPages);
if (window.matchMedia) {{
  var mq = window.matchMedia('print');
  (mq.addEventListener ? mq.addEventListener.bind(mq, 'change') : mq.addListener.bind(mq))(function (e) {{
    if (e.matches) fitPages();
  }});
}}
</script>"""


def _page(title: str, body: str, landscape: bool, tip: str) -> str:
    size = "landscape" if landscape else "portrait"
    # Usable area of the smaller of Letter / A4 inside 0.35in margins, less a little to spare.
    w, h = (10.1, 7.25) if landscape else (7.5, 10.4)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{BASE_CSS}
@page {{ size: {size}; margin: 0.35in; }}
</style></head><body>
{PRINT_BUTTON}
<div class="pp-tip">To print or save as PDF: press <b>Print</b> (or Ctrl/Cmd + P). {tip}</div>
{body}
{_fit_script(w, h)}
</body></html>"""


def _side(r) -> str:
    """'Girls', 'Boys' or '' for a booking."""
    if r.get("kind") == "Local":
        return r.get("side") or ""
    return "Boys" if team_is_boys(r.get("club"), r.get("name")) else "Girls"


def _span_text(days: list[date]) -> str:
    """[5, 6] -> '5, 6'; [11, 12, 13] -> '11–13'."""
    if len(days) >= 3:
        return f"{days[0].day}–{days[-1].day}"
    return ", ".join(str(d.day) for d in days)


# ------------------------------------------------------------ the calendar --

CAL_CSS = """
.pc { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 11px; }
.pc th { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .6px; color: #5b6475;
         padding: 3px 0 4px; border-bottom: 1.5px solid #1f2a44; }
.pc td { border: 1px solid #cfd4dd; vertical-align: top; padding: 3px 4px 4px; }
.pc td.pc-wk { background: #f6f7fa; }
.pc td.pc-out { background: #fff; color: #c3c8d1; }
.pc td.pc-closed { background: repeating-linear-gradient(135deg, #f4f5f8 0 6px, #eceef3 6px 12px); }
.pc .pc-date { font-weight: 800; font-size: 13px; margin-bottom: 2px; }
.pc .pc-closed-why { font-style: italic; color: #5b6475; font-size: 11px; margin-top: 6px; }
.pc col.pc-we { width: 9%; }
.pc .pc-body { columns: 2; column-gap: 6px; }
.pc td.pc-wk .pc-body { columns: 1; }
.pc .pc-grp { break-inside: avoid; margin-bottom: 3px; }
.pc .pc-t { font-weight: 800; font-size: 9.5px; color: #1f2a44; background: #eef1f6; border-radius: 2px;
            padding: 0 3px; margin-bottom: 1px; }
.pc .pc-ln { display: flex; gap: 4px; line-height: 1.32; white-space: nowrap; overflow: hidden; }
.pc .pc-ct { flex: 0 0 auto; min-width: 14px; font-weight: 700; color: #5b6475; text-align: right;
             font-variant-numeric: tabular-nums; }
.pc .pc-nm { overflow: hidden; text-overflow: ellipsis; padding-left: 4px; border-left: 3px solid; }
.pc .pc-loc .pc-nm { color: #5b6475; font-style: italic; }
.pc .pc-tn { font-size: 10px; font-weight: 700; border-radius: 3px; padding: 1px 4px; margin: 1px 0 3px;
             white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pc .pc-travel { color: #B21E1E; border: 1.5px solid #B21E1E; background: #fff; }
.pc .pc-local { color: #3b4250; background: #e9ecf1; }
.pc-key { display: flex; gap: 16px; font-size: 11px; color: #5b6475; margin-top: 6px; flex-wrap: wrap; }
.pc-key span::before { content: ""; display: inline-block; width: 3px; height: 11px; margin-right: 5px;
                       vertical-align: -1px; background: var(--c); }
"""


def _time(slot: str) -> str:
    """'5:30', or '9 AM' style for the 2-hour weekend blocks (whatever the slot's start is called)."""
    from .config import WEEKEND_SLOTS
    if slot in WEEKEND_SLOTS:  # '9–11', like the board
        return WEEKEND_SLOTS[slot].replace(" AM", "").replace(" PM", "")
    return str(slot)


def _cal_line(r) -> str:
    color = {"Girls": GIRLS, "Boys": BOYS}.get(_side(r), LOCAL) if r["kind"] != "Local" else \
        {"Girls": GIRLS, "Boys": BOYS}.get(r.get("side") or "", LOCAL)
    if r["kind"] == "Local":
        ct = _text(r.get("courts_text")) or court_short(r["court"])
        cls = " pc-loc"
    else:
        ct = court_short(r["court"])
        cls = ""
    extra = " +" if r.get("extra") == True else ""  # noqa: E712
    return (f'<div class="pc-ln{cls}"><span class="pc-ct">{html.escape(ct)}</span>'
            f'<span class="pc-nm" style="border-color:{color}">{html.escape(str(r["name"]))}{extra}</span></div>')


def calendar_print_html(assignments: pd.DataFrame, start: date, end: date, closures: dict | None = None,
                        highlight: set[str] | None = None, include_locals: bool = False,
                        weekend_dates: list[date] | None = None, tournaments: list | None = None,
                        title: str = "Practice calendar", local_coaches: bool = False) -> str:
    """The month calendar, one month to a landscape page. Each day lists its practices by start
    time, one per line with the court first; girls marked blue, boys green, programs grey."""
    from .views import _hit
    closures, tournaments = closures or {}, tournaments or []
    weekend_dates = set(weekend_dates or [])
    df = assignments if include_locals else assignments[assignments["kind"] == "Practice"]
    if len(df) and include_locals and (df["kind"] == "Local").any():
        loc = df["kind"] == "Local"  # one line per program, listing its courts
        cts = df[loc].groupby(["date", "slot", "name"])["court"].agg(courts_label).rename("courts_text")
        df = df.merge(cts, left_on=["date", "slot", "name"], right_index=True, how="left")
        df = df.drop_duplicates(subset=["date", "slot", "name"])
    if highlight and len(df):
        df = df[[_hit(r, highlight) for r in df.to_dict("records")]]
    by_date: dict = {}
    if len(df):
        df = df.assign(o=df["slot"].map(SLOT_KEYS.index), loc=df["kind"] == "Local") \
            .sort_values(["date", "o", "loc", "court"], kind="stable")
        for r in df.to_dict("records"):
            by_date.setdefault(r["date"], []).append(r)

    who = f" · {', '.join(sorted(highlight))}" if highlight else ""
    pages = []
    m = date(start.year, start.month, 1)
    while m <= end:
        last = date(m.year, m.month, calendar.monthrange(m.year, m.month)[1])
        a, b = max(m, start), min(last, end)
        rows = []
        ws = week_start(a)
        while ws <= b:
            cells = []
            for i in range(7):
                d = ws + timedelta(days=i)
                if d < a or d > b:
                    cells.append(f'<td class="pc-out"><div class="pc-date">{d.day}</div></td>')
                    continue
                num = f'<div class="pc-date">{d:%b %-d}</div>' if d.day == 1 or d == a else \
                    f'<div class="pc-date">{d.day}</div>'
                if d in closures:
                    cells.append(f'<td class="pc-closed">{num}<div class="pc-closed-why">'
                                 f'{html.escape(closures[d])}</div></td>')
                    continue
                tn_html = "".join(
                    f'<div class="pc-tn pc-{tn.kind}" title="{html.escape(tn.name)}">'
                    f'{html.escape(tn.label())}</div>'
                    for tn in tournaments if tn.covers(d)
                    and (not highlight or tn.kind == "local" or set(tn.teams) & set(highlight)))
                groups: dict = {}
                for r in by_date.get(d, []):
                    groups.setdefault(r["slot"], []).append(_cal_line(r))
                body = "".join(f'<div class="pc-grp"><div class="pc-t">{html.escape(_time(sl))}</div>'
                               f'{"".join(ls)}</div>' for sl, ls in groups.items())
                wk = d.weekday() >= 5 or d in weekend_dates
                cells.append(f'<td{" class=pc-wk" if wk else ""}>{num}{tn_html}'
                             f'{f"<div class=pc-body>{body}</div>" if body else ""}</td>')
            rows.append("<tr>" + "".join(cells) + "</tr>")
            ws += timedelta(days=7)
        head = "".join(f"<th>{wd}</th>" for wd in WEEK_ORDER)
        cols = '<colgroup><col class="pc-we">' + "<col>" * 5 + '<col class="pc-we"></colgroup>'
        pages.append(f'<section class="pp-page"><div class="pp-head"><h1>{m:%B %Y}</h1>'
                     f'<span class="pp-sub">{html.escape(title)}{html.escape(who)}</span></div>'
                     f'<table class="pc">{cols}<thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
                     f'<div class="pc-key"><span style="--c:{GIRLS}">Girls (SDVBC)</span>'
                     f'<span style="--c:{BOYS}">Boys (Seaside)</span>'
                     + (f'<span style="--c:{LOCAL}"><i>Programs</i></span>' if include_locals else "")
                     + '<b style="color:#B21E1E">▢ Travel tournament</b>'
                     '<span style="--c:transparent">Number before a team = court · + = extra practice</span>'
                     '</div></section>')
        m = last + timedelta(days=1)
    return _page(title, f"<style>{CAL_CSS}</style>" + "".join(pages), landscape=True,
                 tip="Each month prints on its own landscape page.")


# ------------------------------------------------------- coverage flags --

FLAG_CSS = """
.pf { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.pf th { text-align: left; font-size: 10.5px; text-transform: uppercase; letter-spacing: .6px; color: #5b6475;
         border-bottom: 1.5px solid #1f2a44; padding: 4px 6px; }
.pf td { border-bottom: 1px solid #d5d9e1; padding: 7px 6px; vertical-align: top; }
.pf td.pf-when { white-space: nowrap; font-weight: 700; width: 1%; }
.pf td.pf-tag { white-space: nowrap; color: #5b6475; font-size: 11px; width: 1%; }
.pf td.pf-cov { width: 30%; }
.pf td.pf-cov .pf-line { border-bottom: 1px solid #9aa3b2; height: 18px; }
.pf td.pf-box { width: 22px; }
.pf td.pf-box span { display: inline-block; width: 14px; height: 14px; border: 1.5px solid #1f2a44;
                     border-radius: 2px; }
.pf tr.pf-done td { color: #5b6475; }
.pf-h2 { font-size: 15px; margin: 18px 0 4px; }
.pf-none { font-size: 14px; color: #1B5E20; padding: 12px 0; }
"""


def _md_bold(s: str) -> str:
    """The flags' '**name**' as bold, everything else escaped."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(s))


def flags_print_html(flags: list[dict], resolved: dict, start: date, end: date,
                     title: str = "Coverage flags") -> str:
    """Every coverage flag for the scheduling admin: the open ones with a line to write in who
    covers it and a box to tick, then the ones already sorted out with how. Portrait."""
    open_ = [f for f in flags if f["id"] not in resolved]
    done = [f for f in flags if f["id"] in resolved]
    out = [f'<section class="pp-page"><div class="pp-head"><h1>{html.escape(title)}</h1>'
           f'<span class="pp-sub">{start:%b %-d, %Y} – {end:%b %-d, %Y} · printed {date.today():%-m/%-d/%Y}'
           f'</span></div>']
    if not flags:
        out.append('<div class="pf-none">No coverage flags for these dates.</div>')
    if open_:
        out.append(f'<h2 class="pf-h2">Needs coverage ({len(open_)})</h2><table class="pf"><thead><tr>'
                   '<th>When</th><th>Who / what</th><th>Type</th><th>Covered by</th><th></th></tr></thead><tbody>')
        for f in open_:
            out.append(f'<tr><td class="pf-when">{html.escape(f["when"])}</td><td>{_md_bold(f["text"])}</td>'
                       f'<td class="pf-tag">{html.escape(f["tag"])}</td>'
                       '<td class="pf-cov"><div class="pf-line"></div></td>'
                       '<td class="pf-box"><span></span></td></tr>')
        out.append("</tbody></table>")
    elif flags:
        out.append('<div class="pf-none">✓ Every flag has coverage.</div>')
    if done:
        out.append(f'<h2 class="pf-h2">Already covered ({len(done)})</h2><table class="pf"><thead><tr>'
                   '<th>When</th><th>Who / what</th><th>Type</th><th>Covered by</th></tr></thead><tbody>')
        for f in done:
            r = resolved[f["id"]]
            note = " · ".join(str(v) for v in (r.get("note"), r.get("who"), r.get("at")) if _text(v))
            out.append(f'<tr class="pf-done"><td class="pf-when">{html.escape(f["when"])}</td>'
                       f'<td>{_md_bold(f["text"])}</td><td class="pf-tag">{html.escape(f["tag"])}</td>'
                       f'<td>{html.escape(note) or "—"}</td></tr>')
        out.append("</tbody></table>")
    out.append("</section>")
    return _page(title, f"<style>{FLAG_CSS}</style>" + "".join(out), landscape=False,
                 tip="Prints portrait; write in who covers each one and tick it off.")


# ------------------------------------------------------ every team's dates --

TEAM_CSS = """
.pt { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 11px; }
.pt col.pt-coach { width: 92px; } .pt col.pt-team { width: 54px; } .pt col.pt-n { width: 20px; }
.pt th { font-weight: 700; color: #5b6475; padding: 1px 0; text-align: center; }
.pt thead tr.pt-mon th { font-size: 10px; text-transform: uppercase; letter-spacing: .5px; text-align: left;
                         border-bottom: 1px solid #c3c8d1; padding-left: 2px; color: #1f2a44; }
.pt thead tr.pt-dow th { font-size: 8px; font-weight: 600; }
.pt thead tr.pt-day th { font-size: 10px; color: #1f2a44; border-bottom: 1.5px solid #1f2a44; padding-bottom: 2px; }
.pt th.pt-sun { border-left: 1px solid #c3c8d1; }
.pt td { padding: 0; height: 21px; text-align: center; vertical-align: middle; font-variant-numeric: tabular-nums;
         border-bottom: 1px solid #e3e6ec; }
.pt td.pt-sun { border-left: 1px solid #e3e6ec; }
.pt td.pt-coach { text-align: left; font-size: 10px; line-height: 1.12; padding: 2px 4px 2px 0; }
.pt td.pt-coach b { font-size: 11px; } .pt td.pt-coach span { color: #5b6475; }
.pt td.pt-team { text-align: left; font-weight: 800; font-size: 11.5px; white-space: nowrap; }
.pt td.pt-n { color: #5b6475; font-size: 10px; border-left: 1px solid #c3c8d1; }
.pt td.pt-p { font-size: 11px; }
.pt td.pt-tn span { display: inline-block; border: 1.6px solid #B21E1E; color: #B21E1E; font-weight: 700;
                    border-radius: 999px; padding: 0 5px; line-height: 15px; white-space: nowrap; font-size: 10.5px; }
.pt tr.pt-sec td { text-align: left; font-weight: 800; font-size: 14px; letter-spacing: .4px; padding: 10px 0 3px;
                   border-bottom: 1.5px solid #1f2a44; height: auto; }
.pt tr.pt-closed td.pt-x { background: #eef0f4; }
.pt-key { font-size: 10.5px; color: #5b6475; margin-top: 6px; }
"""


def team_dates_print_html(result, closures: dict | None = None, order: list[str] | None = None,
                          title: str = "", sections: dict[str, str] | None = None,
                          coaches: dict[str, list[str]] | None = None) -> str:
    """Like the club's handwritten sheet: one line per team (coaches, then team) with the dates it
    practices across in date order, tournament days circled. One portrait page.

    Only dates when some team practices or travels get a column, so the dates line up down the page.
    `sections` (team -> 'Girls'/'Boys') puts the SDVBC and Seaside headings in; `coaches` (team ->
    names, head coach first) overrides the coaches read from the schedule."""
    start, end = result.start, result.end
    closures = closures or {}
    a = result.assignments
    pr = a[a["kind"] == "Practice"] if len(a) else a
    travel = [tn for tn in result.tournaments if tn.kind == "travel"]

    teams = list(order or [])
    for t in (list(result.pattern["team"]) if len(result.pattern) else []) + sorted(set(pr["name"]) if len(pr)
                                                                                    else []):
        if t not in teams:
            teams.append(t)
    who: dict[str, list[str]] = {}
    for src in (result.pattern, pr):
        if len(src):
            for t, c, h in zip(src["team" if "team" in src else "name"], src["coach"], src["head"]):
                if t in who:
                    continue
                names = [x.strip() for x in str(c or "").split(";") if x.strip() and x.strip().lower() != "nan"]
                h = _text(h)
                who[t] = ([h] if h else []) + [x for x in names if x != h]
    who.update(coaches or {})

    practice_days = {}
    for t, d in zip(pr["name"], pr["date"]) if len(pr) else []:
        practice_days.setdefault(t, set()).add(d)
    tourn_days = {}
    for tn in travel:
        for d in pd.date_range(max(tn.start, start), min(tn.end, end)).date:
            for t in tn.teams:
                tourn_days.setdefault(t, set()).add(d)
    used = set().union(*practice_days.values(), *tourn_days.values()) if (practice_days or tourn_days) else set()
    days = sorted(d for d in used if start <= d <= end)

    # Header: month, weekday initial, day number. A line before each Sunday marks the weeks.
    sun = {d for i, d in enumerate(days) if i and week_start(d) != week_start(days[i - 1])}
    mon_cells, i = [], 0
    while i < len(days):
        j = i
        while j < len(days) and (days[j].year, days[j].month) == (days[i].year, days[i].month):
            j += 1
        mon_cells.append(f'<th colspan="{j - i}"{" class=pt-sun" if days[i] in sun else ""}>{days[i]:%b}</th>')
        i = j
    dow = "".join(f'<th{" class=pt-sun" if d in sun else ""}>{"SMTWTFS"[(d.weekday() + 1) % 7]}</th>' for d in days)
    dnum = "".join(f'<th{" class=pt-sun" if d in sun else ""}>{d.day}</th>' for d in days)
    cols = '<col class="pt-coach"><col class="pt-team">' + "<col>" * len(days) + '<col class="pt-n">'
    head = (f'<thead><tr class="pt-mon"><th></th><th></th>{"".join(mon_cells)}<th></th></tr>'
            f'<tr class="pt-dow"><th></th><th></th>{dow}<th></th></tr>'
            f'<tr class="pt-day"><th></th><th></th>{dnum}<th title="Practices">#</th></tr></thead>')

    body, last_sec = [], None
    sections = sections or {}
    for t in teams:
        sec = sections.get(t)
        if sec and sec != last_sec:
            body.append(f'<tr class="pt-sec"><td colspan="{len(days) + 3}">'
                        f'{html.escape(SECTION_NAMES.get(sec, sec))}</td></tr>')
            last_sec = sec
        names = who.get(t, [])
        coach = (f"<b>{html.escape(names[0])}</b>" if names else "") + \
            "".join(f"<br><span>{html.escape(n)}</span>" for n in names[1:])
        cells, k = [], 0
        my_p, my_t = practice_days.get(t, set()), tourn_days.get(t, set())
        while k < len(days):
            d = days[k]
            cls = " pt-sun" if d in sun else ""
            if d in my_t:  # one circle around the run of tournament days
                run = [d]
                while k + len(run) < len(days) and days[k + len(run)] in my_t \
                        and days[k + len(run)] - run[-1] == timedelta(days=1):
                    run.append(days[k + len(run)])
                cells.append(f'<td class="pt-tn{cls}" colspan="{len(run)}"><span>{_span_text(run)}</span></td>')
                k += len(run)
                continue
            cells.append(f'<td class="pt-p{cls}">{d.day if d in my_p else ""}</td>')
            k += 1
        body.append(f'<tr><td class="pt-coach">{coach}</td><td class="pt-team">{html.escape(str(t))}</td>'
                    f'{"".join(cells)}<td class="pt-n">{len(my_p) or ""}</td></tr>')

    span = f"{start:%b %-d} – {end:%b %-d, %Y}" if (start.year, start.month) != (end.year, end.month) \
        else f"{start:%B %Y}" if start.day == 1 and end.day == calendar.monthrange(end.year, end.month)[1] \
        else f"{start:%b %-d}–{end:%-d, %Y}"
    page = (f'<section class="pp-page"><div class="pp-head"><h1>{html.escape(title or "Team practice dates")}</h1>'
            f'<span class="pp-sub">{span}</span></div>'
            f'<table class="pt"><colgroup>{cols}</colgroup>{head}<tbody>{"".join(body)}</tbody></table>'
            '<div class="pt-key">Numbers are practice dates · <b style="color:#B21E1E">circled</b> = tournament · '
            '# = practices · coaches: head coach first</div></section>')
    return _page(title or "Team practice dates", f"<style>{TEAM_CSS}</style>{page}", landscape=False,
                 tip="Fits on one portrait page.")
