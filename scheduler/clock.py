"""The club's "today".

The app is hosted on servers that run on UTC, so date.today() there flips to
tomorrow at 4 PM Pacific (5 PM in summer) -- right in the middle of practice.
Everything that asks "what day is it?" goes through today() instead, which
answers in the club's time zone.

Tests can pin the date with the COURT_TODAY environment variable
(YYYY-MM-DD), so they behave the same whatever day they're run on.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

CLUB_TZ = "America/Los_Angeles"


def now() -> datetime:
    """The current time in the club's time zone."""
    return datetime.now(ZoneInfo(CLUB_TZ))


def today() -> date:
    """Today's date at the club (Pacific time), or COURT_TODAY when it's set."""
    pinned = os.environ.get("COURT_TODAY", "").strip()
    if pinned:
        return date.fromisoformat(pinned)
    return now().date()
