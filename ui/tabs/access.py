"""Access: roles, linking sign-ins to coaches, the Google connection and the activity log."""

from __future__ import annotations

from html import escape as html_escape

import pandas as pd
import streamlit as st

from scheduler import access, mail
from scheduler import google as gsvc
from ui.common import coach_names, g_conn, g_session, plural, send_mail
from ui.state import S


def render():
    """Access section."""
    SECRETS = S.SECRETS
    AUTH_ON = S.AUTH_ON
    store = S.store
    ME = S.ME
    GCFG = S.GCFG
    APP_LINK = S.APP_LINK
    VIEWER = S.VIEWER
    saved = S.saved
    cleaned = S.cleaned
    st.markdown("Who can use the app and what they can do. **Coaches** see the whole schedule, change their own "
                "teams' preferences and ask for changes. **Scheduling admins** edit the schedule, rebuild, publish "
                "and approve coaches' requests (and get emailed about new ones). **App admins** manage this page and "
                "can use *View as* to test any role.")
    if not AUTH_ON:
        st.warning("Google sign-in isn't set up on this copy of the app yet, so anyone with the link can still do "
                   "everything. The setup steps are in `docs/SETUP_LOGINS.md`.")
    # ---- Google connection (calendars + email)
    st.subheader("Google connection")
    conn_ = g_conn()
    if GCFG is None:
        st.caption("Set up Google sign-in first (`docs/SETUP_LOGINS.md`); the connection uses the same Google client.")
    elif conn_:
        caps = [x for x, ok in (("Google Calendar", gsvc.can_calendar(conn_)), ("email", gsvc.can_email(conn_))) if ok]
        when_ = pd.Timestamp(conn_["at"]).tz_convert("America/Los_Angeles").strftime("%b %-d, %Y")
        st.success(f"Connected: **{conn_['email']}** (by {conn_['by']}, {when_}). The app uses it for "
                   + (" and ".join(caps) or "nothing yet: reconnect and allow calendar and email") + ".")
        c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
        t_to = c1.text_input("Send a test email to", value=ME.email if ME else "", key="g_test_to")
        if c2.button("Send test", width="stretch", key="g_test", disabled=not (gsvc.can_email(conn_) and "@" in t_to)):
            err = send_mail([t_to], ("Court Scheduler test email",
                                     f"This is a test from the Court Scheduler, sent as {conn_['email']}.",
                                     mail._wrap("Test email", f"<p>This is a test from the Court Scheduler, sent as "
                                                              f"{html_escape(conn_['email'])}.</p>", APP_LINK)))
            if err:
                st.error(f"Not sent: {err}")
            else:
                st.success(f"Sent to {t_to}.")
        if c3.button("Disconnect", width="stretch", key="g_disc"):
            gsvc.disconnect(store, GCFG, secrets=SECRETS)
            st.session_state.pop("_gconn", None)
            st.session_state.pop("g_cals", None)
            store.log(VIEWER.log_name, "Google connection", f"disconnected {conn_['email']}")
            st.session_state.flash = f"Disconnected {conn_['email']}."
            st.rerun()
        if g_session() is None:
            st.error("Google no longer accepts this connection (it was removed or expired). Connect again.")
    else:
        st.markdown("Connect the club Google account that should **send the app's emails** and **edit the club's "
                    "Google Calendars**. You'll sign in with Google and allow both. No key files: the app keeps "
                    "Google's access token encrypted, and you can disconnect any time.")
    if GCFG is not None and ME is not None:
        if st.button("Connect a different account" if conn_ else "Connect a Google account", key="g_connect"):
            st.session_state.g_url = gsvc.start(store, GCFG, ME.email, access.allowed_domains(SECRETS)[0])
        if st.session_state.get("g_url"):
            st.link_button("Continue to Google →", st.session_state.g_url, type="primary")
            st.caption("Opens Google in a new tab. Choose the account, allow access, and you'll land back in the app. "
                       f"If Google says *redirect_uri_mismatch*, add `{GCFG['redirect_uri']}` to the sign-in client's "
                       "Authorized redirect URIs (steps in `docs/SETUP_GOOGLE.md`).")

    always = set(access.app_admins(SECRETS))
    people = store.users()
    guests = access.guest_emails(store)
    club = {p_["email"]: access.is_club(p_["email"], SECRETS) for p_ in people}
    names_ = coach_names()
    link_opts = [""] + sorted(set(names_) | {p_["coach"] for p_ in people if p_["coach"]})
    table = pd.DataFrame([{"email": p_["email"], "name": p_["name"], "coach": p_["coach"],
                           "app_admin": bool(p_["app_admin"]) or p_["email"] in always,
                           "sched_admin": bool(p_["sched_admin"]),
                           "sign_in": club[p_["email"]] or p_["email"] in guests} for p_ in people],
                         columns=["email", "name", "coach", "app_admin", "sched_admin", "sign_in"])
    st.subheader("People")
    if table.empty:
        st.caption("Nobody has signed in yet.")
    else:
        outside_ = [e for e, c in club.items() if not c]
        if outside_:
            st.caption("Outside (non-club) addresses get the app's emails. Tick **Can sign in** to also let them sign "
                       "in as a coach (needs the sign-in set to External in Google: `docs/SETUP_LOGINS.md`). Club "
                       "addresses can always sign in.")
        unlinked = table[(table["coach"] == "") & ~table["sched_admin"]]["email"].tolist()
        if unlinked:
            st.info(f"Not linked to a coach name yet: {', '.join(unlinked)}. Pick their name in the **Coach** column "
                    "so they can edit their teams' preferences.")
        edited = st.data_editor(
            table, hide_index=True, width="stretch", key=f"access_{st.session_state.get('access_ver', 0)}",
            disabled=["email"],
            column_config={
                "email": "Email", "name": "Name",
                "coach": st.column_config.SelectboxColumn("Coach", options=link_opts,
                                                          help="Their name on the schedule (Teams / Locals sheets)"),
                "app_admin": st.column_config.CheckboxColumn("App admin", help="Manages this page"),
                "sched_admin": st.column_config.CheckboxColumn("Scheduling admin",
                                                               help="Edits the schedule and approves requests"),
                "sign_in": st.column_config.CheckboxColumn(
                    "Can sign in", help="Club addresses always can. For an outside address: unticked = emails only")})
        changes = []
        for (_, old), (_, new) in zip(table.iterrows(), edited.iterrows()):
            diff_ = [f"{lab} {old[c]!s} → {new[c]!s}" for c, lab in (("name", "name"), ("coach", "coach"),
                     ("app_admin", "app admin"), ("sched_admin", "scheduling admin"), ("sign_in", "can sign in"))
                     if old[c] != new[c] and not (c == "sign_in" and club[new["email"]])]
            if diff_:
                changes.append((new, diff_))
        if st.button("Save changes", type="primary", disabled=not changes, key="access_save"):
            kept = []
            for new, diff_ in changes:
                admin = bool(new["app_admin"])
                if new["email"] in always and not admin:
                    admin = True
                    kept.append(new["email"])
                store.save_user(new["email"], new["name"] or "", new["coach"] or "", admin, bool(new["sched_admin"]))
                if not club[new["email"]]:
                    access.set_guest(store, new["email"], bool(new["sign_in"]))
                store.log(VIEWER.log_name, "Access", f"{new['email']}: " + "; ".join(diff_))
            st.session_state.pop("_me", None)
            st.session_state.access_ver = st.session_state.get("access_ver", 0) + 1
            st.session_state.flash = (f"Saved {plural(len(changes), 'change')}."
                                      + (f" {', '.join(kept)} stays an app admin (set in the app's secrets, so the "
                                         "club can't get locked out)." if kept else ""))
            st.rerun()

    # ---- every coach on the schedule, at firstname@club
    _dom = access.allowed_domains(SECRETS)[0]
    sugg = access.suggest_people(names_, people, cleaned.roster if cleaned else {}, _dom)
    todo_ = [r for r in sugg if r["status"] != "linked"]
    with st.expander(f"Add coaches from the schedule ({len(todo_)} not in Access yet)" if todo_ else
                     "Add coaches from the schedule (everyone's linked)", expanded=bool(todo_) and len(people) <= 3):
        st.caption(f"Every coach on the schedule, at `firstname@{_dom}`. Check the emails (two coaches with the same "
                   "first name are left blank to fill in), untick anyone to skip, then **Add**. They're added as "
                   "coaches already linked to their name, so they can use the app the first time they sign in.")
        if todo_:
            tbl = pd.DataFrame([{"add": r["status"] in ("new", "link"), "coach": r["coach"], "email": r["email"],
                                 "status": {"new": "Add", "link": "Link existing login", "check": "Needs an email",
                                            "taken": "Email used by another coach"}[r["status"]], "why": r["why"]}
                                for r in todo_])
            ed_s = st.data_editor(tbl, hide_index=True, width="stretch",
                                  key=f"acc_sugg_{st.session_state.get('access_ver', 0)}",
                                  disabled=["coach", "status", "why"],
                                  column_config={"add": st.column_config.CheckboxColumn("Add", width="small"),
                                                 "coach": "Coach", "email": st.column_config.TextColumn("Email"),
                                                 "status": "Status", "why": "Why"})
            pick_ = [r for r in ed_s.to_dict("records") if r["add"] and "@" in str(r["email"])]
            if st.button(f"Add {plural(len(pick_), 'coach', 'coaches')}", type="primary", disabled=not pick_,
                         key="acc_sugg_go"):
                added, skipped, outside_added = [], [], []
                linked_to = {u["email"]: u["coach"] for u in people if u.get("coach")}
                for r in pick_:
                    e_ = str(r["email"]).strip().lower()
                    if linked_to.get(e_) and linked_to[e_] != r["coach"]:
                        skipped.append(f"{r['coach']} ({e_} is {linked_to[e_]}'s)")
                        continue
                    ex = store.get_user(e_)
                    store.save_user(e_, (ex or {}).get("name") or r["coach"], r["coach"],
                                    bool((ex or {}).get("app_admin")), bool((ex or {}).get("sched_admin")))
                    added.append(r["coach"])
                    if not access.is_club(e_, SECRETS) and e_ not in guests:
                        outside_added.append(e_)
                if added:
                    store.log(VIEWER.log_name, "Access", f"added from the schedule: {', '.join(added)}")
                st.session_state.pop("_me", None)
                st.session_state.access_ver = st.session_state.get("access_ver", 0) + 1
                st.session_state.flash = (f"Added {plural(len(added), 'coach', 'coaches')}."
                                          + (f" Outside addresses ({', '.join(outside_added)}) get emails only; "
                                             "tick **Can sign in** in the table to let them sign in."
                                             if outside_added else "")
                                          + (" Skipped: " + "; ".join(skipped) if skipped else ""))
                st.rerun()

    with st.expander("Add someone (club or outside address)"), st.form("access_add", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        n_email = c1.text_input("Email", help="A club address, or an outside one (Gmail, school...)")
        n_name = c2.text_input("Name")
        n_coach = c3.selectbox("Coach name on the schedule", link_opts)
        c1, c2, c3 = st.columns(3)
        n_app = c1.checkbox("App admin")
        n_sched = c2.checkbox("Scheduling admin")
        n_guest = c3.checkbox("Can sign in (outside address)",
                              help="Club addresses can always sign in. Leave unticked for an outside address that "
                                   "should only get the app's emails.")
        if st.form_submit_button("Add", type="primary"):
            e_ = n_email.strip().lower()
            if not e_ or "@" not in e_:
                st.error("Enter an email address.")
            elif store.get_user(e_):
                st.warning(f"{e_} is already listed above.")
            else:
                outside = not access.is_club(e_, SECRETS)
                store.save_user(e_, n_name.strip(), n_coach, n_app, n_sched)
                if outside and n_guest:
                    access.set_guest(store, e_, True)
                store.log(VIEWER.log_name, "Access", f"added {e_}"
                          + "".join(f"; {r}" for r, on in (("app admin", n_app), ("scheduling admin", n_sched),
                                                          ("outside, can sign in", outside and n_guest),
                                                          ("outside, emails only", outside and not n_guest)) if on))
                st.session_state.access_ver = st.session_state.get("access_ver", 0) + 1
                st.session_state.flash = f"Added {e_}." + (
                    "" if not outside else " They can sign in once Google sign-in is set to External."
                    if n_guest else " They'll get the app's emails but can't sign in.")
                st.rerun()

    removable = [p_["email"] for p_ in people if p_["email"] not in always and (ME is None or p_["email"] != ME.email)]
    if removable:
        c1, c2 = st.columns([3, 1])
        gone = c1.multiselect("Remove access", removable, key="access_rm",
                              help="A club address can still sign in but comes back as a plain coach. "
                                   "An outside address loses sign-in and emails.")
        if c2.button("Remove", disabled=not gone, width="stretch", key="access_rm_btn"):
            for e_ in gone:
                store.delete_user(e_)
                access.set_guest(store, e_, False)
                store.log(VIEWER.log_name, "Access", f"removed {e_}")
            st.session_state.access_ver = st.session_state.get("access_ver", 0) + 1
            st.session_state.flash = f"Removed {plural(len(gone), 'person', 'people')}."
            st.rerun()

    st.subheader("Activity")
    st.caption("Every role change, coach preference, request and decision, newest first. Nobody can approve "
               "their own request.")
    log_ = store.activity(300)
    if not log_:
        st.caption("Nothing yet.")
    else:
        lg = pd.DataFrame(log_)
        lg["at"] = (pd.to_datetime(lg["at"], utc=True).dt.tz_convert("America/Los_Angeles")
                    .dt.strftime("%b %-d, %-I:%M %p"))
        pnames = {p_["id"]: p_["name"] for p_ in saved}
        lg["project"] = lg["project"].map(lambda x: pnames.get(x, "") if x else "")
        st.dataframe(lg[["at", "who", "action", "detail", "project"]].rename(columns={
            "at": "When", "who": "Who", "action": "What", "detail": "Details", "project": "Schedule"}),
            hide_index=True, width="stretch", height=min(500, 38 + 35 * len(lg)))
