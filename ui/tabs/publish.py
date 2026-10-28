"""Publish: mark versions as sent to coaches, see what changed, email coaches, sync Google Calendars."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from scheduler import access, gcal, mail
from scheduler import google as gsvc
from scheduler.changes import add_version, diff, practices_of, published_at, snapshot
from scheduler.data import COLUMNS
from scheduler.storage import MAIN_ID
from scheduler.views import range_label
from ui.common import g_conn, g_session, plural, secret, send_mail
from ui.state import S


def coach_list() -> list[str]:
    """Coaches on at least one team, in the order teams are listed."""
    cleaned = S.cleaned
    out = []
    for t in (cleaned.teams if cleaned else []):
        for c in t.staff:
            if c not in out:
                out.append(c)
    return out


def email_practices(ver: dict, coaches: list[str], only_me: bool, progress=None) -> tuple[int, list[str]]:
    """Emails each coach their teams' practices in a published version (sorted by team, then date).
    only_me: every one of those emails goes to the signed-in admin instead, for testing.
    Returns (emails sent, problems)."""
    store, APP_LINK, VIEWER, start, end, cleaned = S.store, S.APP_LINK, S.VIEWER, S.start, S.end, S.cleaned
    emails = access.coach_emails(store.users(), cleaned.roster if cleaned else {})
    me_ = VIEWER.person.email if VIEWER.person else ""
    pr_ = practices_of(ver)
    sent, problems = 0, []
    for i, c in enumerate(coaches):
        teams_ = mail.teams_of(c, cleaned.teams)
        to = me_ if only_me else emails.get(c, "")
        if not to:
            problems.append(f"{c}: no email (add them in Access)")
            continue
        subj, text, html_ = mail.practices_email(c, teams_, pr_, range_label(start, end), ver["label"], APP_LINK,
                                                 [t for t in cleaned.tournaments if t.kind == "travel"])
        if only_me:
            subj = f"[Test: {c}'s email] {subj}"
        err = send_mail([to], (subj, text, html_))
        if err:
            problems.append(f"{c}: {err}")
            if "connected" in err or "redone" in err:
                break
        else:
            sent += 1
        if progress:
            progress((i + 1) / max(1, len(coaches)))
    store.log(VIEWER.log_name, "Practice emails", f"{ver['label']}: {sent} sent"
              + (" (test, to me)" if only_me else "") + (f", {len(problems)} not sent" if problems else ""), MAIN_ID)
    return sent, problems




def _main():
    """Publishing a version and what changed since."""
    start, end, published, result, cleaned, MY_COACH = S.start, S.end, S.published, S.result, S.cleaned, S.MY_COACH
    st.markdown("Publish the schedule when you send it to coaches. Later rebuilds keep practices where the "
                "published version had them where possible, and this page lists everything that changed since.")
    if result is None:
        st.caption("Build the schedule first.")
    else:
        with st.container(border=True):
            c1, c2 = st.columns([1, 2])
            p_label = c1.text_input("Version name", value=f"{range_label(start, end)} · v{len(published) + 1}",
                                    key=f"pub_label_{len(published)}")
            p_note = c2.text_input("Note (optional)", placeholder="e.g. sent to coaches Friday", key="pub_note")
            c1, c2 = st.columns([1, 2])
            p_mail = c1.toggle("Email coaches their practices", key="pub_mail",
                               help="Each coach gets an email listing their teams' practices, by team and date.")
            p_me = c2.radio("Send to", ["Only me (test)", "Every coach"], horizontal=True, key="pub_mail_to",
                            disabled=not p_mail,
                            help="Only me: you get the email your own coach name would get (or the first coach's, if "
                                 "your login isn't linked), so you can check it first.") == "Only me (test)"
            if st.button("Publish this schedule", type="primary"):
                ver_new = snapshot(result, p_label.strip() or "Published", p_note.strip())
                st.session_state.published = add_version(published, ver_new)
                st.session_state.base_version = None
                msg_ = f"Published “{p_label.strip() or 'Published'}”."
                if p_mail and cleaned is not None:
                    who_ = coach_list()
                    if p_me:
                        who_ = [MY_COACH] if MY_COACH in who_ else who_[:1]
                    with st.spinner("Emailing coaches…"):
                        n_, probs_ = email_practices(ver_new, who_, p_me)
                    msg_ += (f" Emailed {plural(n_, 'coach', 'coaches')}"
                             + (" (to you, as a test)" if p_me else "") + ".")
                    if probs_:
                        msg_ += " Not sent: " + "; ".join(probs_[:8]) + (" …" if len(probs_) > 8 else "")
                st.session_state.flash = msg_
                st.rerun()
        if not published:
            st.info("Nothing published yet.")
        else:
            labels = {i: f"{v['label']} · {published_at(v):%b %-d, %-I:%M %p}"
                      + (f" · {v['note']}" if v.get("note") else "") for i, v in enumerate(published)}
            order = list(reversed(range(len(published))))
            vi = st.selectbox("Compare the current schedule with", order, format_func=lambda i: labels[i],
                              key="pub_cmp")
            ver_ = published[vi]
            r1, r2 = st.columns([1, 3], vertical_alignment="center")
            if r1.button("Restore this version", key="pub_restore",
                         help="Make this version the current schedule again"):
                st.session_state.restore_version = ver_["id"]
                st.session_state.resolve = True
                st.session_state.flash = (f"Restored “{ver_['label']}”. Anything that can't happen with the "
                                          "current inputs is listed in the notes at the top and on the Review tab.")
                st.rerun()
            r2.caption("Brings back this version's practices exactly, rebuilt around your current inputs. "
                       "The current schedule is replaced, so publish it first if you want to keep it.")
            ch = diff(practices_of(ver_), result.assignments, cleaned, start, end,
                      pinned={(r.name, r.date, r.slot) for r in result.assignments.itertuples()
                              if getattr(r, "pinned", False) == True})  # noqa: E712
            if ch.empty:
                st.success("No changes: the current schedule matches this version.")
            else:
                counts = ch["change"].value_counts()
                c = st.columns(5)
                c[0].metric("Teams affected", ch["team"].nunique())
                for i, k in enumerate(["Moved", "Added", "Removed", "Court"]):
                    c[i + 1].metric({"Court": "Court changes"}.get(k, k), int(counts.get(k, 0)))
                who = st.multiselect("Only show these teams", sorted(ch["team"].unique()), key="pub_teams")
                view = ch[ch["team"].isin(who)] if who else ch
                st.dataframe(view.assign(week_of=view["week_of"].map(lambda d: f"{d:%b %-d}")).rename(columns={
                    "team": "Team", "week_of": "Week of", "change": "Change", "was": "Was", "now": "Now",
                    "why": "Why"}), hide_index=True, width="stretch")
                st.download_button("Download changes (CSV)", view.to_csv(index=False).encode(),
                                   f"changes_since_{ver_['id'][:8]}.csv", mime="text/csv")
            with st.expander(f"All published versions ({len(published)})"):
                st.dataframe(pd.DataFrame([{"Version": v["label"],
                                            "Published": f"{published_at(v):%b %-d, %Y %-I:%M %p}",
                                            "Note": v.get("note", ""), "Practices": len(v.get("practices", []))}
                                           for v in reversed(published)]), hide_index=True, width="stretch")
                drop = st.multiselect("Remove versions", order, format_func=lambda i: labels[i], key="pub_drop")
                if st.button("Remove selected", disabled=not drop):
                    st.session_state.published = [v for i, v in enumerate(published) if i not in drop]
                    if any(published[i]["id"] == st.session_state.get("base_version") for i in drop):
                        st.session_state.base_version = None
                    st.rerun()


def _emails():
    """Emailing coaches their practices."""
    store = S.store
    APP_LINK = S.APP_LINK
    VIEWER = S.VIEWER
    start = S.start
    end = S.end
    published = S.published
    cleaned = S.cleaned
    MY_COACH = S.MY_COACH
    st.subheader("Email coaches their practices")
    conn_ = g_conn()
    if not gsvc.can_email(conn_):
        st.info("Connect a Google account in **Access → Google connection** to send emails.")
    st.caption("Each coach gets one email: their teams' practices in a published version, by team and then date, "
               "with their travel tournaments. Test it on yourself first.")
    vlab_ = {i: f"{v['label']} · {published_at(v):%b %-d, %-I:%M %p}" for i, v in enumerate(published)}
    c1, c2 = st.columns([2, 1])
    e_vi = c1.selectbox("Version", list(reversed(range(len(published)))), format_func=vlab_.get, key="em_ver")
    e_me = c2.radio("Send to", ["Only me (test)", "Every coach"], key="em_to") == "Only me (test)"
    all_c = coach_list()
    emails_ = access.coach_emails(store.users(), cleaned.roster)
    if e_me:
        e_who = st.multiselect("Send me the emails these coaches would get", all_c,
                               default=[MY_COACH] if MY_COACH in all_c else all_c[:1], key="em_who")
    else:
        e_who = all_c
        no_mail = [c for c in all_c if c not in emails_]
        st.caption(f"{plural(len(all_c) - len(no_mail), 'coach', 'coaches')} will get an email."
                   + (f" No email for: {', '.join(no_mail)} (add them in **Access**)." if no_mail else ""))
    with st.expander("Preview"):
        pc_ = st.selectbox("Coach", all_c, index=all_c.index(e_who[0]) if e_who and e_who[0] in all_c else 0,
                           key="em_prev")
        if pc_:
            subj_, _t, html_p = mail.practices_email(pc_, mail.teams_of(pc_, cleaned.teams),
                                                     practices_of(published[e_vi]), range_label(start, end),
                                                     published[e_vi]["label"], APP_LINK,
                                                     [t for t in cleaned.tournaments if t.kind == "travel"])
            st.caption(f"To: {emails_.get(pc_, '(no email)')} · Subject: {subj_}")
            st.html(html_p)
    sure_ = e_me or st.checkbox(f"Yes, email {plural(len([c for c in e_who if c in emails_]), 'coach', 'coaches')}",
                                key="em_sure")
    me_mail = VIEWER.person.email if VIEWER.person else ""
    if st.button("Send " + (f"{plural(len(e_who), 'test email')} to {me_mail or 'me'}" if e_me else "to every coach"),
                 type="primary", disabled=not (gsvc.can_email(conn_) and e_who and sure_ and (me_mail or not e_me)),
                 key="em_go"):
        bar = st.progress(0.0)
        n_, probs_ = email_practices(published[e_vi], e_who, e_me, bar.progress)
        st.session_state.flash = (f"Sent {plural(n_, 'email')}" + (f" to {me_mail}" if e_me else "") + "."
                                  + (" Not sent: " + "; ".join(probs_[:10]) if probs_ else ""))
        st.rerun()


def _calendars():
    """Syncing to the club's Google Calendars."""
    SECRETS = S.SECRETS
    store = S.store
    VIEWER = S.VIEWER
    inp = S.inp
    start = S.start
    end = S.end
    published = S.published
    result = S.result
    cleaned = S.cleaned
    st.subheader("Google Calendar")
    st.markdown("Push a published version to the club's Google Calendars: each team's calendar gets its practices "
                "and tournaments, each club calendar gets every practice and tournament on that side, and each "
                "program's calendar gets its sessions. Only events this app made are ever changed or removed; "
                "anything added by hand is left alone.")
    client, g_cals = None, []
    conn_ = g_conn()
    if gsvc.can_calendar(conn_):
        sess_ = g_session()
        if sess_ is not None:
            client = gcal.CalendarClient({"client_email": conn_["email"]}, session=sess_)
            try:
                if "g_cals" not in st.session_state or st.session_state.g_cals[0] != conn_["email"]:
                    st.session_state.g_cals = (conn_["email"], gsvc.calendars(sess_))
                g_cals = st.session_state.g_cals[1]
            except Exception as e:  # noqa: BLE001
                st.warning(f"Couldn't list {conn_['email']}'s calendars: {e}")
    sa = gcal.service_account_info(SECRETS)
    if client is None and sa:  # older setup: a service account key in the secrets
        try:
            client = gcal.CalendarClient(sa)
        except Exception as e:  # noqa: BLE001
            st.error(f"The Google key in the secrets didn't work: {e}")
    if client is None:
        st.info("Google Calendar isn't connected yet. Connect a club Google account in **Access → Google "
                "connection**; you can link calendars below now.")
    else:
        st.caption(f"Syncing as **{client.robot}**: it can change any calendar it owns or that's shared with it "
                   "(\u201cMake changes to events\u201d).")

    with st.expander("Calendar links", expanded=not gcal.links_from(inp.calendars)):
        st.caption("Paste each calendar's ID (Google Calendar → the calendar's ⋮ → Settings and sharing → "
                   "Integrate calendar → Calendar ID). Leave a row blank to skip it.")
        clubs_ = sorted({t.club for t in cleaned.teams if t.club})
        progs_ = sorted({lp.name for lp in cleaned.locals})
        rows_ = gcal.link_rows([t.name for t in cleaned.teams], clubs_, progs_, inp.calendars)
        if g_cals:
            st.markdown("**Link from your Google calendars**")
            cal_name = {c["id"]: c["name"] + (" (main calendar)" if c["primary"] else "") for c in g_cals}
            l1, l2, l3 = st.columns([2, 2, 1], vertical_alignment="bottom")
            tgt = l1.selectbox("Calendar for", [f"{k}: {n}" for k, n in zip(rows_["kind"], rows_["name"])],
                               key="cal_pick_t")
            gid = l2.selectbox("Google calendar", list(cal_name), format_func=cal_name.get, key="cal_pick_c")
            if l3.button("Link", width="stretch", key="cal_pick_go"):
                k_, n_ = tgt.split(": ", 1)
                cur_ = gcal.links_from(inp.calendars)
                cur_[k_, n_] = gid
                inp.calendars = pd.DataFrame([{"kind": a, "name": b, "calendar_id": c} for (a, b), c in cur_.items()],
                                             columns=COLUMNS["Calendars"])
                st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
                st.session_state.flash = f"Linked {tgt} to {cal_name[gid]}."
                st.rerun()
            guess = gsvc.match_calendars([(k, n) for k, n, c in zip(rows_["kind"], rows_["name"], rows_["calendar_id"])
                                          if not str(c).strip()], g_cals)
            if guess:
                st.caption("Matched by name: "
                           + "; ".join(f"{n} → {cal_name[c]}" for (_k, n), c in list(guess.items())[:8])
                           + (f" and {len(guess) - 8} more" if len(guess) > 8 else ""))
                if st.button(f"Link {plural(len(guess), 'match', 'matches')}", key="cal_guess"):
                    cur_ = gcal.links_from(inp.calendars)
                    cur_.update(guess)
                    inp.calendars = pd.DataFrame(
                        [{"kind": a, "name": b, "calendar_id": c} for (a, b), c in cur_.items()],
                        columns=COLUMNS["Calendars"])
                    st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
                    st.session_state.flash = f"Linked {plural(len(guess), 'calendar')} by name."
                    st.rerun()
            st.markdown("**All links** (or paste Calendar IDs here)")
        ed_links = st.data_editor(rows_, hide_index=True, width="stretch", disabled=["kind", "name"],
                                  key=f"cal_links_{st.session_state.get('locals_ver', 0)}",
                                  column_config={"kind": "Calendar for", "name": "Name",
                                                 "calendar_id": st.column_config.TextColumn("Calendar ID",
                                                                                            width="large")})
        c1, c2 = st.columns(2)
        if c1.button("Save calendar links", type="primary", width="stretch", key="cal_save"):
            inp.calendars = ed_links[ed_links["calendar_id"].astype(str).str.strip() != ""].reset_index(drop=True)
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state.flash = f"Saved {plural(len(inp.calendars), 'calendar link')}."
            st.rerun()
        if c2.button("Check access", width="stretch", disabled=client is None, key="cal_check"):
            checks = []
            for (kind, name), cid in gcal.links_from(ed_links).items():
                try:
                    checks.append({"Calendar for": f"{kind}: {name}", "Google calendar": client.calendar_name(cid),
                                   "OK": "✓"})
                except Exception as e:  # noqa: BLE001
                    checks.append({"Calendar for": f"{kind}: {name}", "Google calendar": str(e), "OK": "✗"})
            st.dataframe(pd.DataFrame(checks), hide_index=True, width="stretch")

    if not published:
        st.caption("Publish the schedule first; only published versions go to Google Calendar.")
    else:
        links = gcal.links_from(inp.calendars)
        c1, c2 = st.columns([2, 1])
        vlabels = {i: f"{v['label']} · {published_at(v):%b %-d, %-I:%M %p}" for i, v in enumerate(published)}
        vi_ = c1.selectbox("Version to send", list(reversed(range(len(published)))), format_func=lambda i: vlabels[i],
                           key="cal_ver")
        test_mode = c2.toggle("Test mode", value=True, key="cal_test",
                              help="Send everything to one test calendar instead, with the calendar it would have "
                                   "gone to in front of each title. Nothing real is touched.")
        test_cal = st.text_input("Test calendar ID", key="cal_test_id",
                                 placeholder="ID of a throwaway calendar shared with the app") if test_mode else ""
        only_ = st.multiselect("Only sync these", [f"{k}: {n}" for k, n in links], key="cal_only",
                               placeholder="All linked calendars",
                               help="Pick one or a few to try a sync on just them (e.g. your own team's), "
                                    "leaving every other calendar alone.")
        if only_:
            links = {k: v for k, v in links.items() if f"{k[0]}: {k[1]}" in only_}
        ver_ = published[vi_]
        pr_ = practices_of(ver_)
        loc_ = result.assignments[result.assignments["kind"] == "Local"]
        side_ = {lp.name: lp.side for lp in cleaned.locals}
        evs, missing = gcal.desired_events(pr_, loc_, cleaned.tournaments, cleaned.teams, links, start, end,
                                           test_cal.strip() if test_mode else "", secret("CLUB_ADDRESS") or "",
                                           side_)
        if missing and not only_:
            st.caption(f"No calendar linked (skipped): {', '.join(missing[:12])}"
                       + (f" and {len(missing) - 12} more" if len(missing) > 12 else ""))
        all_cals = sorted({test_cal.strip()} if test_mode and test_cal.strip() else set(links.values()))
        ready = client is not None and (not test_mode or bool(test_cal.strip())) and bool(evs)
        sig_ = (ver_["id"], test_mode, test_cal.strip(), tuple(sorted(links.items())), tuple(only_))
        if st.button(f"Preview changes ({plural(len(evs), 'event')})", disabled=not ready, key="cal_preview",
                     help=None if ready else
                     "Connect Google, link calendars (and a test calendar in test mode) first."):
            with st.spinner("Reading the calendars…"):
                st.session_state.cal_plan = (sig_, gcal.plan(evs, client, start, end, all_cals))
        held = st.session_state.get("cal_plan")
        if held and held[0] == sig_:
            plan_ = held[1]
            for err in plan_.errors:
                st.error(err)
            c = st.columns(4)
            c[0].metric("Add", len(plan_.create))
            c[1].metric("Update", len(plan_.update))
            c[2].metric("Remove", len(plan_.delete))
            c[3].metric("Already right", plan_.unchanged)
            if plan_.n_changes:
                st.dataframe(plan_.table().rename(columns=str.title), hide_index=True, width="stretch",
                             height=min(400, 38 + 35 * plan_.n_changes))
                where = "the test calendar" if test_mode else f"{plural(len(all_cals), 'calendar')}"
                if st.button(f"Sync {plural(plan_.n_changes, 'change')} to {where}", type="primary", key="cal_go"):
                    bar = st.progress(0.0)
                    n, errs = gcal.apply(plan_, client, progress=bar.progress)
                    store.log(VIEWER.log_name, "Google Calendar",
                              f"{'test mode, ' if test_mode else ''}{ver_['label']}: {len(plan_.create)} added, "
                              f"{len(plan_.update)} updated, {len(plan_.delete)} removed"
                              + (f", {len(errs)} failed" if errs else ""), st.session_state.project_id)
                    st.session_state.pop("cal_plan", None)
                    if errs:
                        st.error(f"{plural(len(errs), 'change')} failed:\n" + "\n".join(f"- {e}" for e in errs[:10]))
                    st.session_state.flash = f"Synced {plural(n, 'change')} to {where}."
                    if not errs:
                        st.rerun()
            elif not plan_.errors:
                st.success("Google Calendar already matches this version.")


def render():
    """Publish section."""
    _main()
    if S.result is not None and S.cleaned is not None and S.published:
        _emails()
    if S.result is not None and S.cleaned is not None:
        _calendars()
