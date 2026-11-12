# How the scheduler decides (current rules)

The one up-to-date list of the club's scheduling rules, as the app runs them now.
Earlier decisions that were later changed are left out; see "Changed along the way"
at the bottom for the reversals people may still remember.
Last checked against the code: **Nov 12, 2026**.

When the club changes a rule, update this file in the same commit as the code.
The numbers live in `scheduler/config.py` (`WEIGHTS`, court ranks) and the hard
rules in `scheduler/solver.py`.

## How it works

The scheduler works in two steps:

1. **Days and times.** It picks when every team practices: a weekday timeslot
   (3:45, 5:30, 7:20) or a weekend 2-hour block (9–11, 11–1, … 7–9).
2. **Courts.** With the times set, it picks a court for each practice.

**Hard rules** are never broken. Everything else is a **preference** with a cost,
and the scheduler finds the schedule with the lowest total cost. The preferences
below are listed most important first. Each step costs roughly 2–4× the one
below it, so breaking one higher rule outweighs breaking several lower ones.

## Hard rules (never broken)

- **One team per court per timeslot.** Locals programs and local tournaments
  block their courts (local tournaments only until 7 PM). Closures block whole
  days.
- **No team ever practices alone**, at any time on any day. A locals program or
  local tournament in the gym at the same time counts as company.
- **Ages and times:**
  - Only 14s and under practice at 3:45.
  - 14s and under never practice at 7:20 or 7 PM, unless the team is marked
    "late OK".
  - Teams over 14 never practice at 3:45.
- **A head coach is never in two places at once**: two teams, or a team and a
  locals program they work.
- **Travel tournaments:**
  - A coach at a travel tournament with one team has no practice with any other
    team during it.
  - No practice the day after a team's travel tournament.
- **A team's own "no" dates and times are kept**, unless breaking one is the
  only way the team gets its practices that week. When that happens, the
  practice is flagged.
- **Girls (SDVBC) head coaches' no-dates are never broken.** An assistant
  covering doesn't count, so that practice stays empty and the team is flagged
  as missing one.
- **Boys (Seaside) coaches' no-dates give way, as a last resort**, if the team
  would otherwise miss the practice.
- **Practices per day and week:** at most one practice per team per day, and
  two per week. A hand-placed extra practice can go past both limits.
- **No practices before a team's season start date.**
- **Weekends:** only open weekend dates and blocks, and only the weekend options
  a team allows.
- **Published dates are locked.** Nothing moves a practice on published dates
  except a hand edit or an approved request. Even then, only the practice being
  changed moves (see "Published dates" below).

## Preferences, most important first

1. **Every team gets its practices** (2 a week, or whatever its practices-per-week
   says). Losing a team's second practice of a week costs 4× more than losing
   its first, so shortfalls get spread across teams instead of one team losing
   both. Missed practices count 1.5× in the week before a tournament. Third or
   fourth practices for teams that asked for more fill in when there's room.
2. **Pairing (3 teams on 2 courts) is the absolute last option.** It's only used
   when there's no court space left and the alternative is a team missing a
   practice. It's never used to give a team an extra practice.
3. **No practice the day before a travel tournament.** A late practice that
   night costs a bit more again.
4. **Never break a team's own no-date.**
5. **No assistant missing practice because they're at a travel tournament with
   another team.** Rather than this, the team practices late the night before.
6. **No late practice (7:20 or 7 PM) the night before any of a coach's
   tournaments**, for any team that coach has.
7. **Never break a head coach's no-date** (boys teams only; girls head-coach
   no-dates are a hard rule). It's worse when no assistant is free to run
   practice.
8. **No coach working a locals program at the same time.** If it happens, the
   practice is flagged "possible if coach gets a sub for ___".
9. **Weekdays before weekends.** A weekend practice only happens when the
   weekdays can't take it, even if the weekday option is doubled up, on a day
   or time the team didn't ask for, or marked "rather not".
10. **The coach's day:** no empty timeslot between a coach's sessions on the same
    day (for example a 3:45 team, nothing at 5:30, then a 7:20 team).
11. **About the same weight as each other:**
    - No Thursday-then-Friday or Friday-then-Saturday practices.
    - Don't break an assistant's no-date.
    - Don't schedule an assistant with two teams at once.

    So the scheduler will sometimes have an assistant miss rather than practice
    Thu/Fri, and sometimes the other way round.
12. **No back-to-back days** unless it can't be avoided, or a coach asked for
    exactly those two days (for example "Tue, Wed"). A back-to-back weekday
    still beats a weekend.
13. **Avoid Friday 7:20** as much as possible (it still beats a weekend).
14. **Fill every open 5:30 court before using 7:20**, even for a team that asked
    for 7:20.
15. **A coach with a girls team and a boys team:** on a day both practice, the
    girls team goes first.
16. **Coaches' preferences:**
    - Preferred days: wishlist days, and days marked not preferred.
    - Preferred times: wishlist times, times marked "rather not" (3:45 a little
      more), and weekend AM/PM. Days count slightly more than times.
    - A coach's stated "rather not" beats a time another team simply didn't
      pick.
    - A young team moved to a 3:45 it didn't ask for is worse than an older team
      moved to a 7:20 it didn't ask for.
17. **Saturday then Sunday** is the worst kind of back-to-back, and it's always
    flagged.
18. **Small things,** roughly in this order:
    - A coach's two teams back to back on the same day: the app rewards these
      (the club wants more of them).
    - Each team's age fits its timeslot (younger teams earlier).
    - 3:45 rotates a little among the younger teams. A team that asked for 3:45
      keeps it, and a team that said "rather not" stays off it when possible.
    - The same days and times every week. Coaches' preferences win over keeping
      the same week, but practices shouldn't jump around.
    - Practices stay where the last published (or last built) schedule had them.
    - On weekends, fill one block first: 4 teams at 9–11 rather than 2 at 9–11
      and 2 at 11–1 when there's room.
    - When boys and girls teams of the same age are otherwise tied, the boys get
      the later timeslot.

## Courts (once the times are set)

Court ranks, best first: **1 → 7 → 3 → 6 → 5 → 4 → 2.**

1. **Never lower a net** between timeslots on a court (for example men's height
   at 5:30, then women's at 7:20). Raising a net is fine (12s → women's →
   men's).
2. **Courts 1 and 7 go strictly by the pecking order.** The top team in the
   timeslot gets Court 1 and the next gets Court 7. **Court 2 goes to the lowest
   team.**
3. **Middle courts (3, 6, 5, 4) follow rank, but a coach with back-to-back
   sessions stays on their court** when the teams are close in the pecking
   order. For example, 14-3 takes Court 6 over 14-2 so its coach doesn't move.
   A big gap still wins: a 16s 1s team keeps Court 1 over a 14s 2s coach's
   court.
4. **Same side (boys or girls) and similar ages on neighboring courts.** Courts
   next to each other: 1–7, 1–5, 7–6, 6–5, 5–4, 4–3, 3–2.
5. **Same net height** from one timeslot to the next on a court, if possible.

**The pecking order**, from the Pecking order tab, ranks teams like this:

- Older age groups first.
- Then rank within the age group (16-1 above 16-2).
- At the same rank, boys above girls (14-1 boys above 14-1 girls).
- Teams can be moved above or below any other team on that tab.

The practice court is never used for team practices; it's for locals programs
and privates.

## Weekends, holidays, breaks

- Weekends are open by default and used only after the weekdays.
- All holidays are open days. Locals programs skip holidays unless the program
  is set to "Runs on holidays". Closures are added by hand.
- Weeks with nothing scheduled are treated as breaks: no automatic practices.
  Coaches add their own.
- Daytime blocks (9–11, 11–1, 1–3) can go on a regular weekday (for example
  Veterans Day, or Thanksgiving week) without turning it into an open day. The
  day's other practices stay at their normal times.

## Published dates, edits and requests

- **Publishing locks those dates.** Program changes, preference changes and
  rebuilds never move a practice that's already on a published date. The only
  way to re-plan published dates is **Rebuild these dates**, which asks for
  confirmation first.
- **A hand edit or an approved request** moves only what it touches, and only
  that team can gain a practice. Every other team stays exactly where it is.
- **A request that can't fit around everyone else** isn't applied. It stays
  waiting, and the admin can decline it with a note.
- **Hand moves on unpublished dates** wait for the **Update schedule** button,
  so a batch of moves only rebuilds once.
- **Coaches** only ask for changes. Scheduling admins approve them, and only
  admins change preferences and no-dates.
- **Google Calendar:** each team's calendar gets its practices and tournaments.
  The club calendar for each side (SDVBC and Seaside) gets every practice and
  tournament on that side. Event titles are capitalized and don't use the word
  "practice". Only events the app made are ever changed. With automatic
  updates on, the calendars follow the schedule on published dates every
  evening and right after an approval (see `docs/SETUP_AUTOSYNC.md`).

## Open question

The club's Oct 26 order says to flag "possible if coach gets a sub" **before**
breaking any no-date, head coach's or assistant's. The app does that for head
coaches (rule 8 is below rule 7). For assistants it does the opposite: breaking
an assistant's no-date (rule 11) costs less than a coach needing a sub
(rule 8), so the scheduler breaks the assistant's no-date first. If the club
still wants subs asked for first, raise `assistant_nodate` above `locals_sub`
in `scheduler/config.py`.

## Changed along the way

Earlier decisions people may still remember, and what replaced them:

| Then | Now |
|------|-----|
| Back-to-back weekdays are fine (Oct 26) | Avoided unless unavoidable or asked for; Thu/Fri avoided even over assistants (Nov 3–4) |
| Head-coach no-dates unbreakable for everyone (Nov 3) | Still true for girls; boys' give way as a last resort (Nov 4) |
| Net height > coach stays > court rank (Nov 3) | Court rank first, with coach-stays only inside the middle courts (Nov 8) |
| Big family holidays closed automatically | Every holiday open; closures added by hand (Nov 8) |
| "Never alone" only on weekends | Never alone at any time, any day (Nov 4) |
| Home court for each team | Removed (Nov 8) |
