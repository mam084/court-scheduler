"""The sections (tabs) and who sees them."""

from __future__ import annotations

# Navigation. Only the chosen section is built on each click (tabs would
# build every section every time), and the choice is remembered, so saving a
# program or removing something keeps you where you were.
ALL_VIEWS = ["Court view", "Calendar", "Edit practices", "Requests", "Programs", "Tournaments", "Open days",
             "Privates", "Coaches", "Pecking order", "Publish", "Review", "Inputs", "Access"]
(TAB_COURT, TAB_CAL, TAB_EDIT, TAB_REQ, TAB_PROG, TAB_TOURN, TAB_OPEN, TAB_PRIV, TAB_COACH, TAB_PECK, TAB_PUB,
 TAB_REVIEW, TAB_INPUTS, TAB_ACCESS) = ALL_VIEWS
ADMIN_ONLY = {TAB_PROG, TAB_TOURN, TAB_OPEN, TAB_PECK, TAB_PUB, TAB_REVIEW, TAB_INPUTS}
