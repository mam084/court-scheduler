# 🏐 Court Scheduler

Auto-schedules a volleyball club's team practices across 7 courts for any date range (say Nov 3 – Dec 4), shows the result in the club's own court layout, and saves each schedule so the club can come back to it.

Each team practices twice a week on one of courts 1–7 (the club's practice court is for programs and privates only, never team practices): weekdays in one of three timeslots (3:45–5:30, 5:30–7:20, 7:20–9:10), or on open weekend days in 2-hour blocks (9 AM–9 PM). Locals programs are blocked off first, no-dates are respected, coaches running two teams get them back-to-back, and the schedule stays the same week to week wherever possible. It handles a girls side and a boys side sharing the same courts and coaches, local and travel tournaments, and a court quality ranking.

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
- Nothing on closure days, before a team's season start, or where a practice was removed by hand
- Every team gets 2 practices a week. When something has to give, the order is (least bad first): an **assistant's** no-date (the head coach runs practice), then **Saturday then Sunday**, then the **head coach's** no-date on a day an assistant is free, then both coaches', then the team's own. Each one is flagged at the top of the page and in the Review tab (Sat→Sun as High). Put an assistant's own name in the Availability `who` column so it counts as an assistant's no-date; a team row whose note names an assistant gets a reminder
- Only 14s and under practice at 3:45, and 14s and under never practice at 7:20 (unless the team is marked `late_ok`, and then only when needed)
- Weekends: only open dates and blocks, only a team's allowed weekend options, and never one team alone in a block
- Local tournaments take their courts (all by default) for the whole day, plus the coaches of their locals program
- Teams at a travel tournament don't practice on tournament days
- At most one practice per team per day, and two per week (weeks run Sunday–Saturday)
- JumpStart programs (3:30–4:30, 4:30–5:30) block 3:45 practices on their courts and for their coaches
- The weekly template obeys the same rules, so it can be read as a real week

**Preferences (traded off by weights in [`scheduler/config.py`](scheduler/config.py))**

| Preference | Notes |
|---|---|
| 2 practices per week | Heaviest weight. Older teams win if courts run out, and shortfalls are spread so teams keep at least 1 practice. |
| Required weekends | e.g. "the 14th or 15th must be used" |
| Weekdays over weekends | Weekends are used only when weekdays can't fit 2 practices, and then just 1 weekend practice per team that week. Back-to-back weekdays (Tue/Wed) beat a weekend plus a weekday (Sun/Tue) |
| Keep the published schedule | Rebuilds leave practices (and courts) where the last published version had them, unless something needs them to move |
| Same weekly schedule | Makeups are flagged in the Review tab. Consistency never beats a coach's request: when a slot the team asked for is open in a given week and its usual slot isn't one it asked for (or is marked not preferred), the practice moves there that week. These show as "Moved to a requested time" in Review |
| Ranked wishlists | 1st choice, then 2nd choice day/time combos. The weekly template follows them too, so a team starting late in the range still gets the usual week it asked for, and a team that barely plays this period doesn't crowd out one playing all month. Every team whose usual week misses its 1st choice is listed in the Review tab with what got in the way (a full court, the coach's other team, a local) |
| Coach double-ups | A coach's two teams back-to-back on the same day (girls at 5:30, boys at 7:20) |
| Travel tournament rest | No practice the day before or after (especially no late practice the night before). This outranks no-dates: a no-date is broken before a team practices next to a tournament. The week leading in gets priority for 2 practices |
| Court quality | Courts ranked 1 → 7 → 6 → 5 → 3 → 4 → 2. Teams spread across times instead of piling onto bad courts (Court 2 is a last resort), and within a timeslot older and 1s (Black) teams get the better courts |
| Assistant coaches free | Assistants can be double-booked, at a cost |
| 3:45 shared around | 3:45 is a last resort, and the youngest teams take it first (12s, then 13s, then 14s). When it's needed, it rotates: a team gets at most one 3:45 a week before another young team gets none. A team that asks for 3:45 on its wishlist gets it; a team marked "Not preferred" at 3:45 is kept off it when possible |
| Not-preferred days, weekend AM/PM preferences | |
| A day apart | Back-to-back weekdays are only lightly avoided, and not at all when the team asked for both days (e.g. Tue/Wed). Saturday then Sunday only to avoid a missed practice or a head coach's no-date (above) |
| Same slot both days, home court | |

Closures default to the major family holidays: New Year's Day, July 4, Thanksgiving and the day after, Christmas Eve and Day, and New Year's Eve. The club practices on minor holidays like Labor Day.

## App

- **Saved schedules:** create a schedule (name, dates, then upload the club spreadsheet or try the simulated club). Everything saves automatically: inputs, programs and tournaments you add, edits, dates, and the latest solved schedule. Refreshing or reopening the page's link (`?project=…`) picks up where you left off, and the **Schedule** menu lists every saved schedule.

- **Court view:** courts as rows and days as columns (Sunday–Saturday), with each practice, its time and its coaches in the box, and a dashed **Open** box for every free court time. Colors show age group, and a stripe shows the club. You can view the recurring weekly template or any specific week, including open weekends, makeups and closures. You can also highlight a club, team or coach.
- **Suggest a preference:** in Edit practices, tell the scheduler what a team would rather have ("Tue 7:20, 1st choice") or rather not have ("not Tue 3:45"). It's added to the Wishlist or as a "Not preferred" row and weighed against everyone else's requests, so it's kept when it fits. Each team's preferences are listed there and can be removed.
- **Edit practices:** move, remove or add a single practice, or set a team's usual days. Removing one can find a makeup or cancel it outright. Hand-set practices show a 📌 and are kept on every rebuild, with everything else fitted around them. If changes can't all happen (court taken, coach double-booked), the app says why and undoes the change. The full list is in the **Overrides** sheet and can be undone there.
- **Calendar:** a month view, filterable by club, team or coach.
- **Tournaments:** add a local tournament (dates, courts, locals program) or a travel tournament (dates, club, age groups, then trim the list of teams going). It shows on the court view, calendar and Excel export, and the schedule is rebuilt around it.
- **Programs:** add or edit a program (name, Girls/Boys side, days, every week or start/end dates, timeslots including JumpStart 3:30–4:30 and 4:30–5:30, courts, coaches, on/off-site). Editing lets you drop a single court when sign-ups are low. It's blocked off on the courts and calendar right away, with the practice schedule rebuilt around it. Girls programs show in blue, boys programs in green.
- **Coaches:** add coaches who aren't on a team yet (e.g. ones who only work programs) to the roster.
- **Coaches:** each coach's head and assistant teams, locals, no-dates and full schedule, plus an `.ics` download for Google Calendar.
- **Publish:** mark the schedule as sent to coaches. Rebuilds then keep practices where they were, and this page lists every practice that moved, appeared, disappeared or changed court since a published version, with the reason (a new no-date, a program on the court, a tournament…). Downloadable as CSV.
- **Review:** everything that differs from the ideal schedule, with the reason, plus a per-team count of the less-wanted spots (Court 2, 3:45, weekends, makeups, back-to-back days) for this schedule or every published version.
- **Inputs:** edit any table in place and re-run.
- **Exports:** an Excel workbook in the club's grid layout (template, one sheet per week, all bookings, review).

## Saving schedules permanently

Saved schedules go to a database when `DATABASE_URL` is set, and to a local SQLite file (`storage/`, git-ignored) otherwise. The local file is fine on your own computer, but on Streamlit Community Cloud it's wiped whenever the app restarts or redeploys. To keep schedules for good, connect a free Postgres database:

1. Create a free project at [supabase.com](https://supabase.com) (or [neon.tech](https://neon.tech)).
2. Copy its connection string. In Supabase: **Connect** → **Session pooler** → URI, with your database password filled in.
3. In Streamlit Community Cloud, go to your app's **Settings → Secrets** and add:

   ```toml
   DATABASE_URL = "postgresql://postgres.xxxx:YOUR-PASSWORD@aws-0-us-west-1.pooler.supabase.com:5432/postgres"
   APP_PASSWORD = "something-the-coaches-know"   # optional: asks for a password before showing anything
   ```

The table (`court_scheduler_projects`) is created automatically on first run, with row-level security turned on so Supabase's public API can't read it; only the app, connecting with the database password, can. Since saved schedules contain real coach data, also set `APP_PASSWORD` or make the Streamlit app private.

## Input spreadsheet

One `.xlsx` with these sheets. Download a filled-in example from the app's sidebar, or see [`data/sample_input.xlsx`](data/sample_input.xlsx).

| Sheet | Columns |
|---|---|
| Teams | `team`, `club`, `age`, `head_coach` (girls teams can leave it blank: `18-Jordan` means Jordan), `assistants`, `start_date`, `priority` (optional 1–3), `weekend_only` (hard: `11/1 AM; 11/8 3PM`), `weekend_pref` (soft: `AM`, `Sat AM; Sun PM`), `late_ok` (`Yes` lets a 14s-and-under team practice at 7:20 when needed), `level` (`1` or `2`; blank = from the name, "Black" 1s and "Green" 2s) |
| Availability | one row per rule: `who` (team or coach), `type` (`No` or `Not preferred`), `days` (`Mon`, `Weekdays`), `dates` (`11/10-11/16`), `times` (`5:30`, `3:45`), `note` |
| Wishlist | `team`, `choice` (1 or 2), `days` (`Tue, Thu`; blank = any), `times` (`5:30, 7:20`) |
| Locals | `program`, `side` (`Girls` / `Boys`; blank = guessed from a name like "G HS L"), `days` (`Tue, Thu`), `time` (`5:30`, `3:45-7:20`, JumpStart `3:30` / `4:30`, weekend `9AM-1PM`), `courts` (`1-4`, `P` = practice court; blank until known), `coaches`, `on_site` (`No` for programs elsewhere), `dates` (optional, e.g. `11/02/2026-12/18/2026`) |
| Coaches | `coach`, `email`, `phone`, `note`: roster for coaches who aren't on a team yet |
| Weekends | `date`, `open` (`9AM-9PM`), `courts`, `group` + `required` (days in a required group: at least one gets used) |
| Closures | `date`, `reason` |
| Overrides | hand edits: `team`, `action` (`Add`, `Remove`, `Cancel`, `Usual`), `date`, `days` (Usual: `Tue, Thu`), `time`, `court` (optional) |
| Tournaments | `name`, `type` (`Local` or `Travel`), `start_date`, `end_date`, `club`, `ages` (`15-16`, `14U`), `teams` (optional: overrides club + ages), `program` (local: its coaches are busy), `courts` (local; blank = all) |

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
  storage.py            saved schedules (SQLite or Postgres via DATABASE_URL)
  changes.py            published versions, what changed, per-team burden counts
tests/                  hard-rule and parsing tests
```

## Roadmap

- [x] Auto-schedule with locals, no-dates, holidays and preferences
- [x] Weekends, season start dates, assistants, ranked wishlists, coach double-ups
- [x] Court view, calendar view, coach view
- [x] Excel export, per-coach calendar files
- [x] Any date range; saved schedules that survive refreshes (Postgres via `DATABASE_URL`)
- [ ] Coach profiles with logins
- [ ] Coaches request changes and the director approves them, with the solver checking each change
- [ ] Live Google Calendar sync
- [ ] Monthly prompt for coaches to submit next month's no-dates
- [x] Local and travel tournaments, court quality ranking
- [x] Published versions, stable rebuilds and a "what changed" list
