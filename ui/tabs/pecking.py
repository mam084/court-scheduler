"""Pecking order: the club's team ranking, editable and importable."""

from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from scheduler import pecking as pk
from scheduler.data import blank, parse_rank
from ui.common import plural
from ui.state import S


def _rebuild(message: str):
    st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
    st.session_state.resolve = True
    st.session_state.flash = message
    st.rerun()




def render():
    """Pecking order section."""
    inp = S.inp
    st.markdown("The club's pecking order: **rank within each age group**, written like `16-3` (the third 16s "
                "team) or just `3`. Boys and girls teams can share a rank; at the same rank the boys team goes just "
                "above the girls (`14-1` boys, then `14-1` girls). Edit any rank in the table and press "
                "**Save ranks**. "
                "Higher teams get the better courts (1 → 7 → 6 → 5 → 3 → 4 → 2), and when Court 2 is needed, "
                "the lowest team gets it.")
    order = pk.order_table(inp.teams)
    if order.empty:
        st.caption("No teams yet.")
    else:
        missing = order[~order["_set"]]["team"].tolist()
        if missing:
            st.info(f"**{plural(len(missing), 'team')} {'has' if len(missing) == 1 else 'have'} no rank yet**, "
                    "so they're placed between their age group's "
                    f"1s and 2s: {', '.join(missing)}.", icon="ℹ️")
        sig_key = abs(hash((st.session_state.get("inp_sig"), st.session_state.get("locals_ver", 0)))) % 10**8
        shown = order[["order", "rank", "team", "club", "head_coach", "assistants"]]
        edited = st.data_editor(
            shown, hide_index=True, width="stretch", key=f"peck_edit_{sig_key}",
            disabled=["order", "team", "club", "head_coach", "assistants"],
            column_config={
                "order": st.column_config.NumberColumn("#", help="Position in the pecking order (1 = top). "
                                                       "At a shared rank, boys come before girls.", width="small"),
                "rank": st.column_config.TextColumn("Rank", help="Like 16-3, or just 3", width="small"),
                "team": "Team", "club": "Club", "head_coach": "Head coach", "assistants": "Assistants"})
        changed = {r.team: r.rank for r, (_, o) in zip(edited.itertuples(), shown.iterrows())
                   if str(r.rank or "").strip() != str(o["rank"] or "").strip()}
        if st.button("Save ranks", type="primary", disabled=not changed,
                     help="Saves the ranks you changed above and rebuilds the schedule"):
            t = inp.teams.copy()
            if "rank" not in t.columns:
                t["rank"] = ""
            for team, rank in changed.items():
                t.loc[t["team"].astype(str).str.strip() == team, "rank"] = str(rank or "").strip()
            inp.teams = t
            _rebuild(f"Saved {plural(len(changed), 'rank')}. Schedule rebuilt.")

        dl = order.rename(columns={"rank": "Rank", "team": "Team", "club": "Club", "head_coach": "Head Coach",
                                   "assistants": "Asst. Coach"})[["Rank", "Club", "Team", "Head Coach", "Asst. Coach"]]
        buf = io.BytesIO()
        dl.to_excel(buf, index=False, sheet_name="Pecking Order")
        st.download_button("Download pecking order (Excel)", buf.getvalue(), "pecking_order.xlsx")

    with st.expander("Import a pecking order from a spreadsheet", expanded=order.empty or bool(
            len(order) and (~order["_set"]).all())):
        st.caption("Upload any spreadsheet with a **Rank** column and a **Team** column (Head Coach, Asst. Coach "
                   "and Gender help match teams but are optional). Teams are matched by name first "
                   "(`18-Black` = `18 Black`), then by age and head coach, so nicknames like `16-D&D` still work. "
                   "Ranks Excel turned into dates (`12-1` → Dec 1) are read back correctly.")
        up = st.file_uploader("Pecking order (.xlsx or .csv)", type=["xlsx", "csv"], key="peck_upload")
        if up is not None:
            try:
                table = pk.read_pecking(up, up.name)
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
                table = None
            if table is not None:
                matches = pk.match_teams(table, inp.teams)
                team_opts = ["(skip)"] + [str(t) for t in inp.teams["team"] if not blank(t)]
                preview = pd.DataFrame([{
                    "In the sheet": m.sheet_team, "Rank": m.rank_text,
                    "Team in the app": m.team or "(skip)",
                    "Matched by": m.how or "not matched", "Note": m.note} for m in matches])
                st.caption(f"From the **{table.attrs.get('sheet', 'sheet')}** tab: "
                           f"{sum(1 for m in matches if m.team)} of {len(matches)} rows matched. "
                           "Fix any match in the **Team in the app** column.")
                fixed = st.data_editor(
                    preview, hide_index=True, width="stretch", key=f"peck_preview_{up.file_id}",
                    disabled=["In the sheet", "Matched by", "Note"],
                    column_config={"Team in the app": st.column_config.SelectboxColumn(options=team_opts),
                                   "Rank": st.column_config.TextColumn(help="Like 16-3")})
                for m, (_, r) in zip(matches, fixed.iterrows()):
                    m.team = None if r["Team in the app"] == "(skip)" else r["Team in the app"]
                    if str(r["Rank"]).strip() != m.rank_text:
                        m.rank_text = str(r["Rank"]).strip()
                        m.rank, _ = parse_rank(m.rank_text)
                picked = [m.team for m in matches if m.team]
                dupes = sorted({t for t in picked if picked.count(t) > 1})
                left = pk.unranked(matches, inp.teams)
                if dupes:
                    st.error(f"Matched more than once: {', '.join(dupes)}. Fix the Team in the app column.")
                if left:
                    st.warning(f"Not in this sheet, so their ranks stay as they are: {', '.join(left)}")
                changes = pk.assistant_changes(matches, inp.teams)
                upd = False
                if changes:
                    _differ = "differs" if len(changes) == 1 else "differ"
                    upd = st.checkbox(f"Also update assistant coaches from this sheet "
                                      f"({plural(len(changes), 'team')} {_differ})",
                                      key="peck_upd_asst")
                    st.dataframe(pd.DataFrame(changes, columns=["Team", "Assistants now", "In the sheet"]),
                                 hide_index=True, width="stretch")
                if st.button("Use this pecking order", type="primary", disabled=bool(dupes)):
                    inp.teams = pk.apply(inp.teams, matches, update_assistants=upd)
                    n = sum(1 for m in matches if m.team and m.rank is not None)
                    _rebuild(f"Pecking order set for {plural(n, 'team')}"
                             + (f", assistants updated for {len(changes)}" if upd and changes else "")
                             + ". Schedule rebuilt.")

    _rename_team()


def _rename_team():
    """Rename a team everywhere: its no-dates, wishlist, hand edits, tournaments, privates, calendar link,
    every scheduled run's practices and published versions, and its waiting requests."""
    from scheduler.data import _norm_name, rename_team
    from scheduler.storage import rename_team_in_period
    from ui.dates import active_key
    inp = S.inp
    names = [str(t).strip() for t in inp.teams["team"] if not blank(t)]
    if not names:
        return
    with st.expander("Rename a team"):
        st.caption("Changes the name everywhere: the schedule, published versions, no-dates, wishlists, hand "
                   "edits, tournaments, privates and requests. Nothing gets rebuilt or moved.")
        c1, c2, c3 = st.columns([2, 2, 1], vertical_alignment="bottom")
        old = c1.selectbox("Team", names, key="rn_old")
        new = c2.text_input("New name", key="rn_new", placeholder="e.g. 16-Danielle&Darci").strip()
        clash = new and any(_norm_name(n) == _norm_name(new) and n != old for n in names)
        if clash:
            st.error(f"There's already a team called {new}.")
        if c3.button("Rename", type="primary", width="stretch", key="rn_go",
                     disabled=not new or new == old or bool(clash)):
            rename_team(inp, old, new)
            r = st.session_state.get("result")
            if r is not None:
                for df_attr, col in (("assignments", "name"), ("pattern", "team"), ("practice_counts", "team"),
                                     ("issues", "team")):
                    df = getattr(r, df_attr, None)
                    if isinstance(df, pd.DataFrame) and col in df.columns and len(df):
                        df = df.copy()
                        df[col] = df[col].map(lambda v: new if v == old else v)
                        setattr(r, df_attr, df)
                r.broken = [dict(b, team=new) if b.get("team") == old else b for b in r.broken]
            st.session_state.published = rename_team_in_period(
                {"published": st.session_state.get("published") or []}, old, new)["published"]
            for k, ent in list(st.session_state.get("periods", {}).items()):
                if k != active_key():
                    st.session_state.periods[k] = rename_team_in_period(ent, old, new)
                    st.session_state.periods_dirty.add(k)
            n_req = S.store.rename_team_in_requests(old, new)
            st.session_state.pop("_season_cache", None)
            st.session_state.result = r
            st.session_state.locals_ver = st.session_state.get("locals_ver", 0) + 1
            st.session_state._force_save = True
            st.session_state.flash = f"Renamed {old} to {new}" + (
                f" (and {plural(n_req, 'waiting request')})" if n_req else "") + "."
            st.rerun()
