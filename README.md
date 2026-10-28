# Court Scheduler

Auto-schedules a volleyball club's team practices across 7 courts for any date range (say Nov 3 – Dec 4), shows the result in the club's own court layout, and saves each schedule so the club can come back to it.

Each team practices twice a week (or as many times as it asks for) on one of courts 1–7 (the club's practice court is for programs and privates only, never team practices): weekdays in one of three timeslots (3:45–5:30, 5:30–7:20, 7:20–9:10), or on open weekend days in 2-hour blocks (9 AM–9 PM). Locals programs are blocked off first, no-dates are respected, coaches running two teams get them back-to-back, and the schedule stays the same week to week wherever possible. It handles a girls side and a boys side sharing the same courts and coaches, local and travel tournaments, and a court quality ranking.

![Court view](docs/court_view.png)

## How it works

This is a **constraint-optimization** problem, not a machine-learning one, so it needs no training data: the club spreadsheet is the input, and the tool works the day it exists. Until then, a built-in simulator generates a realistic club (teams, coaches, locals, no-dates) to test against.

The solver ([Google OR-Tools CP-SAT](https://developers.google.com/optimization/cp)) runs in two stages:

1. **Days and times.** It decides when each team practices and gives every team a recurring weekday pattern (the weekly template). Practices stay on that pattern except where a no-date, a closure or the team's season start forces a change. A one-off block (a single no-date, a tournament) never moves the pattern itself, so the usual week is the one the team actually plays most weeks.
2. **Courts.** It gives each team a home court and keeps the team there as much as possible.

**Hard rules (never broken)**

- On-site locals block their courts; off-site programs block only their coaches
- One team per court per timeslot, except pairing (3 teams on 2 courts), which is a last resort (below)
- A head coach is never on two teams at once, or on a team during a local tournament they work
- Nothing on closure days, before a team's season start, or where a practice was removed by hand
- At most the team's weekly number of practices (2 unless it asked for more), plus any extra practices added by hand
- Only 14s and under practice at 3:45, and 14s and under never practice at 7:20 (unless the team is marked `late_ok`, and then only when needed)
- Weekends: only open dates and blocks, only a team's allowed weekend options, and never one team alone in a block
- Local tournaments take their courts (all by default) until 7 PM, plus the coaches working them (listed, and their locals program's). 7:20 practices and the 7–9 PM weekend block still happen
- Teams at a travel tournament don't practice on tournament days
- At most one practice per team per day (weeks run Sunday–Saturday)
- JumpStart programs (3:30–4:30, 4:30–5:30) block 3:45 practices on their courts and for their coaches
- The weekly template obeys the same rules, so it can be read as a real week

**The club's rule order** (weights in [`scheduler/config.py`](scheduler/config.py); each rule outweighs several breaks of the one below it)

1. **Every team gets its 2 practices a week.** Older teams win if courts run out, and shortfalls are spread so teams keep at least 1. A team that asked for more (Teams `practices` column, or **Practices per week** in Edit practices) gets its 3rd+ wherever there's room without breaking rules 2–6.
2. **No practice the day before or after a travel tournament** (a late one the night before is worse). The week leading in gets priority for 2 practices.
3. **The team's own no-dates.**
4. **The head coach's no-dates/times** (worse when no assistant is free to run practice).
5. **An assistant's no-dates/times** (the head coach runs practice). Put the assistant's own name in the Availability `who` column; a team row whose note names an assistant gets a reminder.
6. **A coach who works a locals program then** (head or assistant): the practice can still happen, flagged *Needs a sub: possible if Matthew gets a sub for B 14 L*, so a sub is asked for before any no-date is broken. An assistant on two teams at once costs the same.
   - **Pairing** (3 teams sharing 2 courts) is the absolute last option: only when there's no court space left and the alternative is a team missing one of its 2 practices. Breaking no-dates, practicing next to a tournament, weekends and subs all come first, and it's never used for a 3rd practice. Paired practices are flagged at the top of the page and in Review, and tagged *Paired · 3 on 2* on the court view.
7. **Weekdays before weekends.** Two weekday practices, even doubled up, beat a weekend plus a weekday.
8. **Preferred days** (wishlist days, days marked not preferred).
9. **Preferred times** (wishlist times, times marked not preferred, 3:45 a bit more; weekend AM/PM).
10. **No Saturday then Sunday** (always flagged).
11. **One weekend practice per team per week.**
12. **No back-to-back days** (days a team asked for, e.g. Tue/Wed, are fine).
13. **Everything else:** age/timeslot fit and sharing 3:45 around, the 14s 7:20 exception, the same weekly schedule, keeping the published schedule, weekend teams sharing blocks (4 teams at 9–11 rather than 2 at 9–11 and 2 at 11–1 when there's room; on a busy weekend there isn't, and nobody is left alone in a block), coach double-ups, courts. When possible, **HS boys (15s–18s) at 5:30 and 7:20 share courts** so no net change is needed, and (less important) **a coach with back-to-back sessions stays on the same court**.

Every broken no-date, needed sub, pairing and Sat→Sun is flagged at the top of the page and in the Review tab, and so is any coach booked to work a local tournament while coaching a team at a travel tournament. Required weekends on the Weekends sheet are a note for coaches, not a rule.

| Detail | Notes |
|---|---|
| Keep the published schedule | Rebuilds leave practices (and courts) where the last published version had them, unless something needs them to move |
| Same weekly schedule | Makeups are flagged in the Review tab. Consistency never beats a coach's request: when a slot the team asked for is open in a given week and its usual slot isn't one it asked for (or is marked not preferred), the practice moves there that week. These show as "Moved to a requested time" in Review |
| Ranked wishlists | 1st choice, then 2nd choice day/time combos. The weekly template follows them too, so a team starting late in the range still gets the usual week it asked for, and a team that barely plays this period doesn't crowd out one playing all month. Every team whose usual week misses its 1st choice is listed in the Review tab with what got in the way (a full court, the coach's other team, a local) |
| Coach double-ups | A coach's two teams back-to-back on the same day (girls at 5:30, boys at 7:20) |
| Travel tournament rest | No practice the day before or after (especially no late practice the night before). This outranks no-dates: a no-date is broken before a team practices next to a tournament. The week leading in gets priority for 2 practices |
| Court quality | Courts ranked 1 → 7 → 6 → 5 → 3 → 4 → 2. Teams spread across times instead of piling onto bad courts (Court 2 is a last resort), and within a timeslot the better courts go by the club's **pecking order** (older age groups first, then rank within the age: `16-1` before `16-2`; at the same rank the boys team is just above the girls). When Court 2 is needed, the lowest-ranked team gets it |
| 3:45 shared around | 3:45 is a last resort, and the youngest teams take it first (12s, then 13s, then 14s). When it's needed, it rotates: a team gets at most one 3:45 a week before another young team gets none. A team that asks for 3:45 on its wishlist gets it; a team marked "Not preferred" at 3:45 is kept off it when possible |
| Not-preferred days, weekend AM/PM preferences | |
| A day apart | Back-to-back days are avoided (rule 12), and not at all when the team asked for both days (e.g. Tue/Wed). Saturday then Sunday is avoided more (rule 10) and always flagged |
| Same slot both days, home court | |

Closures default to the major family holidays: New Year's Day, July 4, Thanksgiving and the day after, Christmas Eve and Day, and New Year's Eve. The club practices on minor holidays like Labor Day.

## App

| The usual week (template) | Calendar | Fairness check |
|---|---|---|
| ![Weekly template](docs/court_week.png) | ![Calendar](docs/calendar.png) | ![Review](docs/review.png) |


- **One schedule:** the club has one schedule, built a run of dates at a time (Nov 3 – Dec 4, then Dec 5 – 20, then the winter break...). The sidebar's **Dates** menu lists every run of dates; **Schedule new dates** starts the next one with the club's teams, coaches, programs, tournaments and open days, and each run keeps its own built schedule and published versions. Coaches open on the published dates covering today. Everything saves automatically. The first time the app opens it moves the most recent saved schedule in; older saved schedules stay in the database, and **Old saved schedules** brings one's dates into the menu.
- **Getting new dates ready:** set up the locals in **Programs**, tournaments, and any weekends or break days in **Open days**, then **Import coaches' spreadsheet** and **Auto-schedule**.

- **Court view:** courts as rows and days as columns (Sunday–Saturday), with each practice, its time and its coaches in the box, privates, and a dashed **Open** box for every free court time. **Printable weeks · paper style** prints the club's board layout: each court box lists every timeslot of the day as a line, filled in where something's booked and blank where it's open (room to write privates and changes in), with tournaments and closures under each date and each day's practice count plus the week's total vs. needed at the bottom. Every printable page has a **Print** button at the top. **Printable weeks · color** downloads every week on one page (with *Show open court times* on, each open court time prints with a line to write a name in); each week prints on two landscape pages (courts 1–4, then courts 5–7 and the practice court). Colors show age group, and a stripe shows the club. You can view the recurring weekly template or any specific week, including open weekends, makeups and closures. You can also highlight a club, team or coach; a signed-in coach has a **Highlight my practices** toggle for their teams and the programs they work, and **Show program coaches** lists who's working each program (also on the Calendar). Tournaments are listed under their day's name, and a weekend day with a local tournament is shown even when it isn't open for practices.
- **Suggest a preference:** in Edit practices, tell the scheduler what a team would rather have ("Tue 7:20, 1st choice") or rather not have ("not Tue 3:45"). It's added to the Wishlist or as a "Not preferred" row and weighed against everyone else's requests, so it's kept when it fits. Each team's preferences are listed there and can be removed.
- **Edit practices:** move, remove or add a single practice (an added one can be an **extra**, on top of the week's practices; it can go on a day that already has a practice, at another time), set a team's usual days, or change how many practices a week it gets. Removing one can find a makeup or cancel it outright. Hand-set practices are kept on every rebuild, with everything else fitted around them. If changes can't all happen (court taken, coach double-booked), the app says why and undoes the change. The full list is in the **Overrides** sheet and can be undone there.
- **Calendar:** a month view, filterable by club, team or coach. Programs list the courts they use (*G HS L · Ct 1-4*). Download what's shown as a **Google Calendar file** (.ics, for one team, coach, club or everyone), a **printable calendar** (one month per landscape page; print or save as PDF), or an **Excel calendar** in the club's own layout (a block per week, each day with time · what · courts, courts kept as text so `5-7` never turns into a date). The full Excel export includes the calendar too.
- **Tournaments:** add a local tournament (dates, courts, locals program, coaches working it; it runs until 7 PM) or a travel tournament (dates, club, age groups, then trim the list of teams going). It shows on the court view, calendar and Excel export, and the schedule is rebuilt around it.
- **Programs:** add or edit a program (name, Girls/Boys side, days, every week or start/end dates, timeslots including JumpStart 3:30–4:30 and 4:30–5:30, courts, coaches, on/off-site) and its **local tournament dates** (as many as needed, several on one day is fine; each blocks its courts and the program's coaches until 7 PM). Editing lets you drop a single court when sign-ups are low, and **Add or cancel one session** adds a single extra session or cancels one date (and brings it back) without touching the rest of the program. It's blocked off on the courts and calendar right away, with the practice schedule rebuilt around it. Girls programs show in blue, boys programs in green. A coach listed on two programs that run at the same time is flagged at the top of the page and on this tab.
- **Pecking order:** the club's team ranking (`16-3` = third 16s team; boys and girls can share a rank, with the boys team just above), shown top to bottom and editable in place. **Import** it from any spreadsheet with a Rank and a Team column: teams are matched by name (`18-Black` = `18 Black`) or by age + head coach (`16-D&D` → `16-Danielle & Darci`), ranks Excel turned into dates (`12-1` → Dec 1) are read back, mismatches can be fixed in a dropdown, and the sheet's assistant coaches can update the app's (TBA = nobody). Downloadable as Excel.
- **Privates:** book 1-hour private lessons (coach, date, any start time on a 12-hour clock with AM/PM, court: the practice court by default), toggled **Team** (for one of the club's teams) or **Personal**. Each is checked against the schedule, so a court or coach that's busy then is flagged before and after booking. Privates show on the weekly court view. **Download every coach's privates** gives a spreadsheet with every coach and their team / personal counts, one list of all privates, and a sheet per coach.
- **Coaches:** each coach's head and assistant teams, locals, no-dates, privates and full schedule, plus an `.ics` download for Google Calendar. A signed-in coach lands on their own page and can add or remove their own no-dates and times (every week, or specific dates; "can't" or "rather not"). Coaches who aren't on a team yet (e.g. ones who only work programs) can be added to the roster here.
- **Importing the coaches' spreadsheet** adds what's new without overwriting what the club has: teams and coaches already here stay exactly as they are (names, coaches, ranks) and new ones are added. Everyone in the file gets the file's no-dates and wishlists, replacing their standing ones and anything from before these dates (a dated no-date from these dates on that a coach added in the app is kept); everyone else keeps theirs minus the old dates. Open days, closures and hand edits are combined; programs, tournaments and privates stay unless you tick *Also replace programs and tournaments* (an empty or missing sheet never wipes them).
- **Open days and school breaks:** the **Open days** tab opens weekends, or a whole break (e.g. Dec 21 – Jan 2, hours and courts), in 2-hour blocks. A weekday listed there runs in 9–11, 11–1 … 7–9 blocks like a weekend (closures stay closed, and teams still never practice alone in a block). Break **camps** are programs with the time `9AM-11AM` on weekdays; weekday programs that pause over the break get dates or skips.
- **Google Calendar** (Publish tab): push a published version to each team's, club's and program's Google Calendar. Preview shows every add, update and removal before anything is written; test mode sends it all to one throwaway calendar; events added by hand are never touched. Setup: [`docs/SETUP_CALENDAR.md`](docs/SETUP_CALENDAR.md).
- **Publish:** mark the schedule as sent to coaches. Rebuilds then keep practices where they were, and this page lists every practice that moved, appeared, disappeared or changed court since a published version, with the reason (a new no-date, a program on the court, a tournament…). Downloadable as CSV. **Restore this version** makes an older version the current schedule again, rebuilt around the current inputs: anything that can't happen any more is left out with a note, and restored practices that now break a rule are flagged on Review.
- **Review** (admins only): the practices-per-week grid (its title row stays put while you scroll), everything that differs from the ideal schedule, with the reason, plus a per-team count of the less-wanted spots (Court 2, 3:45, weekends, makeups, back-to-back days) for this schedule or every published version.
- **Inputs:** edit any table in place and re-run.
- **Exports:** an Excel workbook in the club's grid layout (template, one sheet per week, all bookings, review), and **Every team's dates**: one row per team, girls then boys, each in pecking order, with every practice (time and court) and tournament date, tournament days circled (drawn circles in Excel; red-outlined cells in Google Sheets, which drops shapes), plus a sortable list.

Warnings (short practices, broken no-dates, double-booked coaches...) sit in one box at the top that can be collapsed. **Dark mode:** ⋮ (top right) → Settings → choose Dark; the court view, calendar and counts follow it (printouts stay light).

## Sign-in and roles

With Google sign-in set up (steps in [`docs/SETUP_LOGINS.md`](docs/SETUP_LOGINS.md)), only club accounts get in, and each person's role decides what they can change:

- **Coaches** see the whole schedule (with only the warnings about their own teams), change their own teams' preferences and add their own no-dates. They can't see or change the Access tab. A coach's change is saved right away (onto the latest saved copy, so nobody's edits are overwritten) and listed for the admins to fit in at the next Auto-schedule.
- **Scheduling admins** edit everything on the schedule, rebuild and publish (and, next, approve coaches' change requests).
- **App admins** manage the **Access** tab (roles, linking logins to coach names, the activity log) and can **View as** any role to test it (while viewing as someone else, the Access tab is hidden just as it is for them; switch back to *Myself* to manage access).

Every role change and coach preference change is logged in the Access tab. Without sign-in configured, the app works as before: anyone with the link can do everything.

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
| Teams | `team`, `club`, `age`, `head_coach` (girls teams can leave it blank: `18-Jordan` means Jordan), `assistants`, `start_date`, `priority` (optional 1–3), `weekend_only` (hard: `11/1 AM; 11/8 3PM`), `weekend_pref` (soft: `AM`, `Sat AM; Sun PM`), `late_ok` (`Yes` lets a 14s-and-under team practice at 7:20 when needed), `rank` (pecking order within the age group: `16-3` or `3`; blank = from the name, "Black" 1 and "Green" 2; files using the older `level` column still work), `practices` (a week; blank = 2) |
| Availability | one row per rule: `who` (team or coach), `type` (`No` or `Not preferred`), `days` (`Mon`, `Weekdays`), `dates` (`11/10-11/16`), `times` (`5:30`, `3:45`), `note` |
| Wishlist | `team`, `choice` (1 or 2), `days` (`Tue, Thu`; blank = any), `times` (`5:30, 7:20`) |
| Locals | `program`, `side` (`Girls` / `Boys`; blank = guessed from a name like "G HS L"), `days` (`Tue, Thu`), `time` (`5:30`, `3:45-7:20`, JumpStart `3:30` / `4:30`, weekend or break camp `9AM-11AM`), `courts` (`1-4`, `P` = practice court; blank until known), `coaches`, `on_site` (`No` for programs elsewhere), `dates` (optional, e.g. `11/02/2026-12/18/2026`), `skip` (single dates it doesn't run, e.g. `11/12; 11/26`). A one-off extra session is its own row with one date |
| Coaches | `coach`, `email`, `phone`, `note`: roster for coaches who aren't on a team yet |
| Weekends | open days in 2-hour blocks: `date` (a day or a range like `12/21-1/2`; a weekday listed here runs in blocks, for school breaks), `open` (`9AM-9PM`), `courts`, `group` + `required` (days in a required group: at least one gets used) |
| Closures | `date`, `reason` |
| Overrides | hand edits: `team`, `action` (`Add`, `Extra`, `Remove`, `Cancel`, `Usual`), `date`, `days` (Usual: `Tue, Thu`), `time`, `court` (optional) |
| Tournaments | `name`, `type` (`Local` or `Travel`), `start_date`, `end_date`, `club`, `ages` (`15-16`, `14U`), `teams` (optional: overrides club + ages), `program` (local: its coaches are busy), `courts` (local; blank = all), `coaches` (local: who's working it) |
| Privates | `coach`, `date`, `time` (any start, e.g. `4:15 PM`; 1-hour sessions), `court` (`P` = practice court, the default, or 1–7), `type` (`Team` / `Personal`), `team`, `player`, `note` |

Every field is forgiving: `Tuesdays and Thursdays`, `Tue/Thu`, `530pm` and `Nov 15-16` all work.

Coach names are matched exactly, so write each coach the same way everywhere. Two coaches who share a first name need different names: `Tyler` and `Tyler Lyons` are two different people. To add a last name later, change it in every place it appears (Teams, Availability, Locals).

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
  storage.py            saved schedules, users and the activity log (SQLite or Postgres via DATABASE_URL)
  access.py             roles: who can sign in and what they can change
  gcal.py               Google Calendar sync: events to send, preview, apply
  changes.py            published versions, what changed, per-team burden counts
  pecking.py            read a pecking-order sheet and match it to the teams
  privates.py           private lessons: reading, conflict checks, per-coach spreadsheet
tests/                  hard-rule and parsing tests
```

## Roadmap

- [x] Auto-schedule with locals, no-dates, holidays and preferences
- [x] Weekends, season start dates, assistants, ranked wishlists, coach double-ups
- [x] Court view, calendar view, coach view
- [x] Excel export, per-coach calendar files
- [x] Any date range; saved schedules that survive refreshes (Postgres via `DATABASE_URL`)
- [x] Google sign-in with coach / scheduling admin / app admin roles, View as, activity log
- [ ] Coaches request changes (moves, privates, no-dates) and a scheduling admin approves them, with the solver checking each change; nobody approves their own
- [ ] Email notifications for new requests and decisions
- [x] Google Calendar sync of published schedules (team, club and program calendars; test mode and preview)
- [ ] Monthly prompt for coaches to submit next month's no-dates
- [x] Local and travel tournaments, court quality ranking
- [x] Published versions, stable rebuilds, a "what changed" list and restoring an older version
