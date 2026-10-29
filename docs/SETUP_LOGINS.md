# Setting up sign-in (Google) and the test copy

Sign-in lets coaches open the app with their club Google account. What each person can do depends on their role:

| Role | Who (to start) | Can |
|---|---|---|
| Coach | everyone with an @sdvbc.org account, plus outside addresses allowed in Access | see the whole schedule; change their own teams' preferences (saved right away, fitted in at the next rebuild) |
| Scheduling admin | Cliff | everything on the schedule: edit, rebuild, publish, programs, tournaments, privates, inputs; approve coaches' requests (emailed about new ones) |
| App admin | Matthew | the **Access** tab (who has which role, activity log) and **View as**, to see the app as any role |

Roles combine: an app admin can also be a scheduling admin (one checkbox in the Access tab). Nobody will be able to approve their own request, and every role change and coach preference change is in the Access tab's activity log.

Until sign-in is set up, an app works exactly as before (anyone with the link can do everything).

## 1. Make the test copy of the app (5 minutes)

New features land on the `staging` branch first, so the real app isn't touched while we try them.

1. Go to [share.streamlit.io](https://share.streamlit.io) → **Create app** → *Deploy a public app from GitHub*.
2. Repository `mam084/court-scheduler`, branch **`staging`**, main file `app.py`.
3. App URL: pick something like `sdvbc-scheduler-test` (the address becomes `https://sdvbc-scheduler-test.streamlit.app`).
4. Its data: make a **second free Supabase project** (Supabase allows two) and use its *Session pooler* connection string as this app's `DATABASE_URL`, so the test copy never touches the real schedules. (Or leave `DATABASE_URL` out: the test copy then saves to a temporary file that's wiped on restarts, which is fine for trying things.)

## 2. Create the Google sign-in (10 minutes)

Do this signed in with a **club Google account** (an @sdvbc.org admin, so the app can be limited to club accounts).

1. Open [console.cloud.google.com](https://console.cloud.google.com) → project picker → **New project** → name it `Court Scheduler` → Create.
2. **APIs & Services → OAuth consent screen** (may be called *Google Auth Platform → Branding/Audience*):
   - User type / audience: **Internal**. This alone means only @sdvbc.org accounts can sign in.
   - App name `Court Scheduler`, support email: yours. Save.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application**, name `Court Scheduler`.
   - **Authorized redirect URIs** → add:
     - `https://sdvbc-scheduler-test.streamlit.app/oauth2callback` (your test app's address + `/oauth2callback`)
     - later, the real app's address + `/oauth2callback`
   - Create, then copy the **Client ID** and **Client secret**.

## 3. Add the secrets to the test app

Streamlit Cloud → the test app → **⋮ → Settings → Secrets**, paste and fill in:

```toml
DATABASE_URL = "postgresql://postgres.xxxx:PASSWORD@aws-0-us-west-1.pooler.supabase.com:5432/postgres"

ALLOWED_DOMAINS = "sdvbc.org"           # who can sign in
ADMIN_EMAILS = "matthew@sdvbc.org"      # always app admins (can't be locked out)

[auth]
redirect_uri = "https://sdvbc-scheduler-test.streamlit.app/oauth2callback"
cookie_secret = "PASTE-A-LONG-RANDOM-STRING"   # any 40+ random characters (a password generator works)
client_id = "PASTE-CLIENT-ID.apps.googleusercontent.com"
client_secret = "PASTE-CLIENT-SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

Save; the app restarts with a **Sign in with Google** page.

## 4. First sign-in

1. Sign in as matthew@sdvbc.org. The first sign-in sets up Matthew as app admin and Cliff as scheduling admin.
2. Create or import a schedule (as an app admin you're not a scheduling admin, so use **View as → Scheduling admin** to set things up, or tick your own *Scheduling admin* box in **Access**).
3. In **Access**, link each person to their **coach name** on the schedule once they've signed in (or add them ahead of time). That's what lets a coach edit their own teams' preferences.
4. Use **View as → Coach: …** to check what a coach sees.

## Outside people (not @sdvbc.org)

In **Access → Add someone**, you can add any email (Gmail, school, etc.) and link it to a coach name:

- **Emails only** (leave *Can sign in* unticked): they get the app's emails, like their practice schedule when you publish. This works right away, with no Google changes.
- **Can sign in**: they can also sign in with that Google account and see what a coach sees. Only addresses you tick get in. Anyone else outside the club is still turned away.

Signing in from outside needs one Google change, because **Internal** lets only @sdvbc.org accounts through Google's sign-in screen:

1. Check whether the sign-in and the Google connection (email and calendars, `docs/SETUP_GOOGLE.md`) use **the same Google Cloud project**. Internal/External applies to the whole project.
2. **If they're separate projects:** in the sign-in project, go to **OAuth consent screen / Audience → Make external**, then **Publish app** (status *In production*). Sign-in only asks for name and email, so Google doesn't need to review it. Nothing else changes.
3. **If they're the same project:** leave that project **Internal**, since email and calendar access on External would need Google's review or expire every 7 days. Instead, make a **new project** just for sign-in (step 2 above, but External and published), create its OAuth client with the same redirect URIs, and put its **Client ID** and **Client secret** in the `[auth]` section of the app's secrets. The email/calendar connection keeps working as it is.

Outside people may see an extra Google screen the first time they sign in. That's normal.

## Moving it to the real app (later)

When the test copy looks right, the `staging` branch is merged into `main`, and the real app gets the same secrets with its own address in `redirect_uri` (plus that address added to the Google client's redirect URIs). The real app's `APP_PASSWORD`, if set, stops being used once sign-in is on.

## Trying roles on your own computer

Without Google set up, run `COURT_DEV_USER=someone@sdvbc.org streamlit run app.py` to open the app as that person (their role comes from the Access tab). It only works where sign-in isn't configured.
