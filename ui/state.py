"""What one page run knows: the schedule's inputs, the dates, the result, who's looking.

Each step of the page (setup, sidebar, build, header, the section) records
what it worked out on `S`, and later steps read it, e.g. `S.inp`, `S.CAN`.

`S` keeps its values in st.session_state, so every visitor's page has its own
(two coaches using the app at once never see each other's values), and it's
emptied at the start of every run by `begin()`.
"""

from __future__ import annotations

from types import SimpleNamespace

import streamlit as st

_KEY = "_page_state"


class _PageState:
    """`S.name` reads and writes this visitor's value for the current page run."""

    def __getattr__(self, name):
        try:
            return getattr(st.session_state[_KEY], name)
        except (KeyError, AttributeError):
            raise AttributeError(f"S.{name} isn't set yet on this page run") from None

    def __setattr__(self, name, value):
        setattr(st.session_state[_KEY], name, value)

    def update(self, **values):
        """Set several values at once: S.update(inp=inp, start=start)."""
        for k, v in values.items():
            setattr(self, k, v)


S = _PageState()


def begin():
    """Start a page run with nothing set."""
    st.session_state[_KEY] = SimpleNamespace()
