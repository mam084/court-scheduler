"""The whole page, top to bottom, on every click (see the ui package docstring)."""

from __future__ import annotations

from ui import build, header, session, setup, sidebar, state
from ui.nav import (
    TAB_ACCESS,
    TAB_CAL,
    TAB_COACH,
    TAB_COURT,
    TAB_EDIT,
    TAB_INPUTS,
    TAB_OPEN,
    TAB_PECK,
    TAB_PRIV,
    TAB_PROG,
    TAB_PUB,
    TAB_REQ,
    TAB_REVIEW,
    TAB_TOURN,
)
from ui.state import S
from ui.tabs import (
    access,
    calendar_view,
    coaches,
    court_view,
    edit,
    inputs,
    open_days,
    pecking,
    privates,
    programs,
    publish,
    requests,
    review,
    tournaments,
)

SECTIONS = {
    TAB_COURT: court_view, TAB_CAL: calendar_view, TAB_EDIT: edit, TAB_REQ: requests, TAB_PROG: programs,
    TAB_TOURN: tournaments, TAB_OPEN: open_days, TAB_PRIV: privates, TAB_COACH: coaches, TAB_PECK: pecking,
    TAB_PUB: publish, TAB_REVIEW: review, TAB_INPUTS: inputs, TAB_ACCESS: access,
}


def run():
    """One page run."""
    state.begin()
    setup.run()
    session.ensure_open()
    sidebar.render()
    build.run()
    header.render()
    SECTIONS[S.VIEW].render()
    session.autosave()
