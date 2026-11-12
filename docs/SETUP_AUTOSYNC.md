# Automatic Google Calendar updates

Once this is set up and turned on, nobody has to press Sync for the calendars to
stay current:

- **Every evening around 5 PM Pacific**, a GitHub Actions job
  (`.github/workflows/calendar-sync.yml`) brings every linked calendar in line
  with the schedule.
- **When a request is approved**, the app updates that team's calendar and its
  club calendar right away. This part works as soon as the switch is on; it
  doesn't need the GitHub setup below.

What gets sent: the schedule **as it is now** (hand edits and approved requests
included), on **published dates only**. Unpublished dates are never sent. Like a
manual sync, it only changes events the app made, and events added by hand are
left alone. Team and club calendars get practices. Programs and tournaments go
too only if "Include programs and tournaments" is on.

## 1. Turn it on in the app

Publish → Google Calendar → **Automatic updates**:

1. Do one manual sync first (test mode, then for real), so you've seen what the
   calendars will look like.
2. Turn on **Update Google Calendar automatically**.

"Update every calendar now" runs the same update right away, whether the switch
is on or not.

## 2. Give GitHub the app's secrets (for the 5 PM update)

The evening job runs on GitHub, not Streamlit, so it needs its own copy of a few
of the app's secrets. On GitHub: the repo → **Settings → Secrets and variables →
Actions → New repository secret**. Copy each value from the Streamlit app's
secrets (share.streamlit.io → the app → ⋮ → Settings → Secrets):

| GitHub secret          | From the Streamlit secrets               | Needed? |
|------------------------|------------------------------------------|---------|
| `DATABASE_URL`         | `DATABASE_URL`                           | yes |
| `GOOGLE_CLIENT_ID`     | `client_id` under `[auth]` (or `[auth.google]`) | yes |
| `GOOGLE_CLIENT_SECRET` | `client_secret` under `[auth]`           | yes |
| `COOKIE_SECRET`        | `cookie_secret` under `[auth]`           | yes, unless you have `TOKEN_KEY` |
| `TOKEN_KEY`            | `TOKEN_KEY`                              | only if the app's secrets have it |
| `CLUB_ADDRESS`         | `CLUB_ADDRESS`                           | if the app's secrets have it (event locations) |
| `ADMIN_EMAILS`         | `ADMIN_EMAILS`                           | if the app's secrets have it (who gets problem emails) |

The Google connection's token is stored encrypted in the database. The job needs
the same `COOKIE_SECRET`/`TOKEN_KEY` and client secret as the app to unlock it,
so copy them exactly.

**Supabase:** if the run says it can't reach the database, use the **Session
pooler** connection string (Supabase → Connect) for `DATABASE_URL`. GitHub's
machines can't reach Supabase's direct (IPv6) address.

## 3. Try it

Actions tab → **Evening calendar update** → **Run workflow**. A run started by
hand goes right away; the scheduled ones wait until 5 PM. The run's log shows
what changed ("3 added, 1 updated, 0 removed" or "already up to date"), and so
does Publish → Automatic updates ("Last update").

## Good to know

- GitHub fires the schedule at 4 and 5 PM Pacific (UTC doesn't follow daylight
  saving), and the job runs once a day at or after 5 PM. GitHub can start
  scheduled runs a few minutes late.
- GitHub pauses scheduled jobs in a repo with no commits for 60 days. If that
  happens, there's a button on the Actions tab to turn it back on.
- If a run fails, the app admins get an email (when the Google connection can
  send email) and it's logged under Access → Activity as an Error.
