# 🏐 Court Scheduler

Auto-schedules a volleyball club's team practices across 7 courts, one month at a time, and shows the result in the club's own court layout.

Each team practices twice a week (Mon–Fri) on one court in one of three timeslots (3:45–5:30, 5:30–7:20, 7:20–9:10). Weekly locals programs are blocked off first, coaches' no-dates are respected, and the schedule stays the same week to week wherever possible.

![Court view](docs/court_view.png)

## How it works

This is a **constraint-optimization** problem, not a machine-learning one, so it needs no training data: the club spreadsheet is the input, and the tool works the day it exists. Until then, a built-in simulator generates a realistic club (teams, coaches, locals, no-dates) to test against.

The solver ([Google OR-Tools CP-SAT](https://developers.google.com/optimization/cp)) runs in two stages:

1. **Days and times.** It decides when each team practices and gives every team a recurring weekly pattern. Practices stay on that pattern except where a coach's no-date or a closure forces a makeup.
2. **Courts.** It gives each team a home court and keeps the team there as much as possible.

**Hard rules (never broken)**

- Locals programs block their courts
- One team per court per timeslot
- A coach is never in two places at once: two teams, or a team plus a local they're working
- Nothing on a coach's no-dates or on closure days
- At most one practice per team per day, and two per week

**Preferences (traded off by weights in [`scheduler/config.py`](scheduler/config.py))**

| Preference | Notes |
|---|---|
| 2 practices every full week | Heaviest weight. Older teams win if courts run out, and shortfalls are spread so teams keep at least 1 practice. |
| Same weekly schedule | Makeups are flagged in the Review tab. |
| Younger teams earlier, older later | 3:45 is used only when needed, and then for the youngest teams first. |
| Non-consecutive days | |
| Same timeslot both days | |
| Home court | |
| Coaches' preferred days and times | |

Closures default to the major family holidays: New Year's Day, July 4, Thanksgiving and the day after, Christmas Eve and Day, and New Year's Eve. The club practices on minor holidays like Labor Day.

## App

- **Court view:** courts as rows and weekdays as columns, with each practice and its time in the box. You can view the recurring weekly template or any specific week, including makeups and closures.
- **Calendar:** a month view, filterable by team or coach.
- **Coaches:** each coach's teams, locals, preferences, no-dates and full schedule, plus an `.ics` download for Google Calendar.
- **Review:** everything that differs from the ideal schedule, with the reason.
- **Inputs:** edit any table in place and re-run.
- **Exports:** an Excel workbook in the club's grid layout (template, one sheet per week, all bookings, review).

## Input spreadsheet

One `.xlsx` with these sheets. Download a filled-in example from the app's sidebar, or see [`data/sample_input.xlsx`](data/sample_input.xlsx).

| Sheet | Columns |
|---|---|
| Teams | `team`, `age`, `coach`, `priority` (optional 1–3; blank means by age) |
| Coaches | `coach`, `email`, `preferred_days` (`Mon, Wed`), `preferred_slots` (`5:30, 7:20`) |
| NoDates | `coach`, `date`, `slot` (`All`, or `3:45` / `5:30` / `7:20`), `note` |
| Locals | `program`, `days` (`Tue, Thu`), `slot`, `courts` (`1-4` or `1,3,5`), `coaches` (`Ana; Ben`) |
| Closures | `date`, `reason` (optional; defaults to major holidays) |

> **Privacy:** real coach and team data should never be committed. `.gitignore` excludes everything in `data/` except the simulated sample.

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py
pytest              # checks every hard rule on solved schedules
```

## Project layout

```
app.py                  Streamlit app
scheduler/
  config.py             courts, timeslots, weights, age/timeslot costs
  data.py               input tables, validation, Excel IO
  closures.py           default holiday closures
  simulate.py           simulated club data
  solver.py             two-stage CP-SAT scheduler
  views.py              court grid + month calendar (HTML)
  export.py             Excel workbook + per-coach .ics
tests/                  hard-rule and parsing tests
```

## Roadmap

- [x] Auto-schedule with locals, no-dates, holidays and preferences
- [x] Court view, calendar view, coach view
- [x] Excel export, per-coach calendar files
- [ ] Coach profiles with logins (Supabase or similar)
- [ ] Coaches request changes and the director approves them, with the solver checking each change
- [ ] Live Google Calendar sync
- [ ] Monthly prompt for coaches to submit next month's no-dates
