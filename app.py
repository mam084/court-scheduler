"""Court Scheduler – the Streamlit app.

Run locally:  streamlit run app.py      (checks: pytest, ruff check .)

The scheduling engine is the `scheduler` package; the app around it is the
`ui` package (its docstring walks through a page run). This file only sets
up the page and runs it.
"""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import streamlit as st

# Load the current code after a push. Streamlit Cloud can rerun the app while
# older modules are still in memory (an ImportError or a missing attribute
# until the app is rebooted), so when the code on disk has changed the app's
# modules are dropped and loaded fresh. Only then, and one page run at a time:
# dropping them on every run let two runs (a click's rerun, two tabs) import
# the same modules at once and deadlock (_DeadlockError, Nov 2026).
_HERE = Path(__file__).resolve().parent
_state = sys.modules.get("_court_scheduler_reload")
if _state is None:
    _state = types.ModuleType("_court_scheduler_reload")
    _state.lock, _state.sig = threading.Lock(), None
    _state = sys.modules.setdefault("_court_scheduler_reload", _state)


def _code_sig() -> tuple:
    return tuple(sorted((str(p), p.stat().st_mtime_ns) for pkg in ("scheduler", "ui")
                        for p in (_HERE / pkg).rglob("*.py")))


with _state.lock:
    _sig = _code_sig()
    if _sig != _state.sig:
        for _mod in [m for m in list(sys.modules) if m.split(".")[0] in ("scheduler", "ui")]:
            del sys.modules[_mod]
        _state.sig = _sig
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
