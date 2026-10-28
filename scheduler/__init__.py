"""The court scheduling engine, separate from the Streamlit app.

data       the club's input tables, parsing and validation (clean)
solver     the two-stage CP-SAT scheduler (solve)
config     courts, timeslots, age rules and the rule weights
views      the court grid and calendar as HTML; paper: the board-style print
export     Excel workbooks and .ics calendar files
storage    saving the schedule, users, the activity log and requests
access     who can sign in and what each role can change
changes    published versions and what changed between them
gcal       Google Calendar sync; google: the Google connection; mail: emails
pecking    reading a pecking-order sheet; privates: private lessons
simulate   a made-up club for demos and tests; closures: default holidays
"""

from .data import ScheduleInput, clean
from .simulate import month_range, simulate
from .solver import ScheduleResult, solve

__all__ = ["ScheduleInput", "ScheduleResult", "clean", "month_range", "simulate", "solve"]
