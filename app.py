"""Court Scheduler – the Streamlit app.

Run locally:  streamlit run app.py      (checks: pytest, ruff check .)

The scheduling engine is the `scheduler` package; the app around it is the
`ui` package (its docstring walks through a page run). This file only sets
up the page and runs it.
"""

from __future__ import annotations

import sys

import streamlit as st

# Always load the current code. After a push, Streamlit Cloud can rerun the
# app while older modules are still in memory, which shows up as an
# ImportError or a missing attribute until the app is rebooted.
for _mod in [m for m in list(sys.modules) if m.split(".")[0] in ("scheduler", "ui")]:
    del sys.modules[_mod]

from scheduler.views import DARK_CSS  # noqa: E402
from ui import main  # noqa: E402

st.set_page_config(page_title="Court Scheduler", layout="wide")
try:
    DARK = st.context.theme.type == "dark"
except Exception:  # noqa: BLE001
    DARK = False
if DARK:  # the court grid, calendar and counts follow the app's theme (⋮ → Settings)
    st.markdown(DARK_CSS, unsafe_allow_html=True)

main.run()
