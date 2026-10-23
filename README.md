# 🏐 Court Scheduler

Auto-schedules a volleyball club's team practices across 7 courts, one month at a time, and shows the result in the club's own court layout.

Each team practices twice a week on one court: weekdays in one of three timeslots (3:45–5:30, 5:30–7:20, 7:20–9:10), or on open weekend days in 2-hour blocks (9 AM–9 PM). Locals programs are blocked off first, no-dates are respected, coaches running two teams get them back-to-back, and the schedule stays the same week to week wherever possible. It handles a girls side and a boys side sharing the same courts and coaches.

![Court view](docs/court_view.png)

## How it works

This is a **constraint-optimization** problem, not a machine-learning one, so it needs no training data: the club spreadsheet is the input, and the tool works the day it exists. Until then, a built-in simulator generates a realistic club (teams, coaches, locals, no-dates) to test against.

The solver ([Google OR-Tools CP-SAT](https://developers.google.com/optimization/cp)) runs in two stages:

1. **Days and times.** It decides when each team practices and gives every team a recurring weekday pattern (the weekly template). Practices stay on that pattern except where a no-date, a closure or the team's season start forces a change.
2. **Courts.** It gives each team a home court and keeps the team there as much as possible.

**Hard rules (never broken)**

- On-site locals block their courts; off-site programs block only their coaches
- One team per court per timeslot
- A head coach is never in two places at once (two teams, or a team plus a local they work)
- Nothing on a team's or head coach's no-dates, on closure days, or before a team's season start
- Only 14s and under practice at 3:45, and 14s and under never practice at 7:20 (unless the team is marked `late_ok`, and then only when needed)
- Weekends: only open dates and blocks, only a team's allowed weekend options, and never one team alone in a block
- At most one practice per team per day, and two per week
- The weekly template obeys the same rules, so it can be read as a real week

**Preferences (traded off by weights in [`scheduler/config.py`](scheduler/config.py))**

| Preference | Notes |
|---|---|
| 2 practices per week | Heaviest weight. Older teams win if courts run out, and shortfalls are spread so teams keep at least 1 practice. |
| Required weekends | e.g. "the 14th or 15th must be used" |
| Same weekly schedule, weekdays over weekends | Makeups are flagged in the Review tab |
| Ranked wishlists | 1st choice, then 2nd choice day/time combos |
| Coach double-ups | A coach's two teams back-to-back on the same day (girls at 5:30, boys at 7:20) |
| Assistant coaches free | Assistants can be double-booked, at a cost |
| Not-preferred days, weekend AM/PM preferences | |
| Non-consecutive days, same slot both days, home court | |

Closures default to the major family holidays: New Year's Day, July 4, Thanksgiving and the day after, Christmas Eve and Day, and New Year's Eve. The club practices on minor holidays like Labor Day.

## App

- **Court view:** courts as rows and days as columns, with each practice, its time and its coaches in the box. Colors show age group, and a stripe shows the club. You can view the recurring weekly template or any specific week, including open weekends, makeups and closures. You can also highlight a club, team or coach.
- **Calendar:** a month view, filterable by club, team or coach.
- **Coaches:** each coach's head and assistant teams, locals, no-dates and full schedule, plus an `.ics` download for Google Calendar.
- **Review:** everything that differs from the ideal schedule, with the reason.
- **Inputs:** edit any table in place and re-run.
- **Exports:** an Excel workbook in the club's grid layout (template, one sheet per week, all bookings, review).

## Input spreadsheet

One `.xlsx` with these sheets. Download a filled-in example from the app's sidebar, or see [`data/sample_input.xlsx`](data/sample_input.xlsx).

| Sheet | Columns |
|---|---|
| Teams | `team`, `club`, `age`, `head_coach` (girls teams can leave it blank: `18-Jordan` means Jordan), `assistants`, `start_date`, `priority` (optional 1–3), `weekend_only` (hard: `11/1 AM; 11/8 3PM`), `weekend_pref` (soft: `AM`, `Sat AM; Sun PM`), `late_ok` (`Yes` lets a 14s-and-under team practice at 7:20 when needed) |
| Availability | one row per rule: `who` (team or coach), `type` (`No` or `Not preferred`), `days` (`Mon`, `Weekdays`), `dates` (`11/10-11/16`), `times` (`5:30`, `3:45`), `note` |
| Wishlist | `team`, `choice` (1 or 2), `days` (`Tue, Thu`; blank = any), `times` (`5:30, 7:20`) |
| Locals | `program`, `days` (`Tue, Thu`), `time`, `courts` (`1-4`; blank until known), `coaches`, `on_site` (`No` for programs elsewhere), `dates` (optional) |
| Weekends | `date`, `open` (`9AM-9PM`), `courts`, `group` + `required` (days in a required group: at least one gets used) |
| Closures | `date`, `reason` |

Every field is forgiving: `Tuesdays and Thursdays`, `Tue/Thu`, `530pm` and `Nov 15-16` all work.

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
  config.py             courts, timeslots, weekend blocks, age rules, weights
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
- [x] Weekends, season start dates, assistants, ranked wishlists, coach double-ups
- [x] Court view, calendar view, coach view
- [x] Excel export, per-coach calendar files
- [ ] Coach profiles with logins (Supabase or similar)
- [ ] Coaches request changes and the director approves them, with the solver checking each change
- [ ] Live Google Calendar sync
- [ ] Monthly prompt for coaches to submit next month's no-dates
- [ ] Tournament-aware rules (2 practices before a tournament weekend, rest after multi-day events)
