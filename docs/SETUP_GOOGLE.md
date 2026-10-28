# Connecting Google (calendars and email), no key files

The app sends its emails and edits the club's Google Calendars **as one club Google account**. You connect it once in the app; there's no key file to download or paste.

| The connected account... | |
|---|---|
| sends | new change requests to the scheduling admins, approve/decline replies to coaches, each coach's practices when a schedule is published |
| edits | any Google Calendar it owns or that's shared with it ("Make changes to events"), and only events the app made |

For testing, connect your own account. For the real thing, a dedicated `scheduler@sdvbc.org` is cleaner: emails come from "Court Scheduler", and nothing depends on one person's account.

## 1. One-time Google Cloud setup (5 minutes)

In [console.cloud.google.com](https://console.cloud.google.com), in the **same project as sign-in** (`docs/SETUP_LOGINS.md`):

1. **APIs & Services → Library**: search **Google Calendar API** → **Enable**. Then search **Gmail API** → **Enable**.
2. **APIs & Services → OAuth consent screen** (or *Google Auth Platform → Audience*): check the user type is **Internal**. (If it's External and "Testing", Google would cut the connection every 7 days.)
3. **APIs & Services → Credentials** → open the sign-in OAuth client → **Authorized redirect URIs** → **Add URI**, the app's own address with a slash at the end:
   - test app: `https://sdvbc-scheduler-test.streamlit.app/`
   - real app: its address, e.g. `https://<real-app>.streamlit.app/`

   Keep the existing `.../oauth2callback` ones. **Save**. (It can take a few minutes to start working.)

Nothing goes in the app's secrets for this.

## 2. Connect in the app

1. Sign in as an app admin → **Access** tab → **Google connection** → **Connect a Google account** → **Continue to Google**.
2. In the Google tab that opens, choose the account to connect, and **allow** both calendar and email access.
3. You land back in the app on the Access tab, showing *Connected: you@sdvbc.org*. (Close the old tab.)
4. **Send test** emails yourself (or Cliff) to check email works.

If Google shows **redirect_uri_mismatch**, step 1.3 is missing or the address is slightly different (http vs https, missing slash). If it says the app is **blocked by your organization**, a Workspace admin allows it under admin.google.com → Security → API controls → *Trust internal, domain-owned apps*.

To switch accounts later (e.g. to `scheduler@sdvbc.org`), press **Connect a different account**. **Disconnect** removes it, and the account can also remove the app at myaccount.google.com → Security → Third-party connections.

## 3. Calendars

**Publish → Google Calendar → Calendar links**:
- **Link from your Google calendars**: pick the team (or club / program) and the Google calendar it goes to, **Link**. The list shows every calendar the connected account can change.
- **Link N matches** fills in every calendar whose name matches a team ("14 Black Practices" → 14 Black).

Try it on one calendar first: link your team to your own calendar, publish, then in the sync section pick it under **Only sync these**, turn **Test mode** off, **Preview** and **Sync**. Only that calendar is touched.

## 4. Emails

- **Requests**: a coach's request emails every scheduling admin (except the coach asking); the decision emails the coach back. Without a connection, requests still show on the Requests tab, just without emails.
- **Practices**: on **Publish**, turn on *Email coaches their practices* (or use the **Email coaches their practices** section for any published version). **Only me (test)** sends you the email your coach name would get (or any coaches you pick), marked *[Test]*. **Every coach** sends each coach theirs; coaches without an email in **Access** are listed and skipped. Use **Access → Add coaches from the schedule** to add everyone at firstname@sdvbc.org.
