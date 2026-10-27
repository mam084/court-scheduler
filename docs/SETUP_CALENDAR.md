# Connecting Google Calendar

The app pushes a **published** schedule to the club's Google Calendars:

| Calendar | Gets |
|---|---|
| Each team's | its practices (time, court, coaches) and its tournaments |
| Each club side's (girls, boys) | every practice and tournament on that side |
| Each locals program's | its sessions (with courts) and a local tournament it runs |

Every event the app makes carries a hidden tag, and a sync only ever changes or removes tagged events. **Anything added by hand is never touched.** Each sync adds new practices, updates moved ones in place and removes ones that are gone.

## Safe testing first

On the Publish tab, **Test mode** is on by default: everything goes to one throwaway calendar, with the calendar it would have gone to in front of each title (`[Team: 16 Black] 16 Black practice · Court 3`). **Preview changes** only reads calendars; nothing is written until you press **Sync**. Turn test mode off only once the test calendar looks right.

## 1. Create the app's Google "robot" account (10 minutes)

Use the same Google Cloud project as sign-in (or create one at [console.cloud.google.com](https://console.cloud.google.com)), signed in with a club account.

1. **APIs & Services → Library** → search **Google Calendar API** → **Enable**.
2. **IAM & Admin → Service accounts → Create service account**. Name it `court-scheduler`, then **Done** (no roles needed).
3. Open it → **Keys → Add key → Create new key → JSON**. A key file downloads. Keep it private; it lets the app edit any calendar shared with it.
4. Note its email address, like `court-scheduler@your-project.iam.gserviceaccount.com`.

## 2. Add the key to the app's secrets

Streamlit Cloud → the app → **⋮ → Settings → Secrets**. Paste the key file's contents as one line in single quotes:

```toml
GOOGLE_SERVICE_ACCOUNT = '{"type": "service_account", "project_id": "...", "private_key": "-----BEGIN PRIVATE KEY-----\n...", "client_email": "court-scheduler@....iam.gserviceaccount.com", ...}'
CLUB_ADDRESS = "Club address for the event location (optional)"
```

## 3. Make a test calendar and share it

1. In Google Calendar: **Other calendars → + → Create new calendar** → name it `Scheduler test`.
2. Its **Settings and sharing → Share with specific people** → add the robot's email with **Make changes to events**.
3. Copy its **Calendar ID** (same page, *Integrate calendar*).

> **If sharing with the robot only offers "See only free/busy"**, the club's Google Workspace blocks sharing calendars outside sdvbc.org (the robot's address isn't on the club domain). A Workspace admin can allow it: **admin.google.com → Apps → Google Workspace → Calendar → Sharing settings → External sharing options for primary calendars / secondary calendars → "Share all information, and outsiders can change calendars"**.

In the app: Publish → Google Calendar → turn on **Test mode**, paste the test calendar's ID, **Preview changes**, then **Sync**. Check the test calendar: times, courts, tournaments as all-day events. Change something, publish again and sync again to see moves and removals.

## 4. Link the real calendars

For each team, club and program calendar:

1. Share it with the robot's email (**Make changes to events**).
2. Copy its Calendar ID into **Calendar links** on the Publish tab and **Save calendar links**.
3. **Check access** confirms the app can reach each one.

## 5. The practices already on those calendars

The app doesn't touch events it didn't make, so practices entered by hand would sit next to the synced ones. Either delete the hand-made practices from the date the first real sync covers, or start syncing with the next schedule period so the two never overlap.

## After that

Each time a schedule is published, open Publish → Google Calendar, preview, and sync. Families who added a calendar to their Google account see changes within minutes; some phone calendar apps refresh subscribed calendars less often (up to a day).
