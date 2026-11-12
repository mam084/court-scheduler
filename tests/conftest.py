"""Test setup shared by every test file.

The app opens lists and calendars on "today", so tests pin today's date
(through scheduler.clock) to a day before the sample November schedule.
Without this, tests that pick "the first practice" break as the real
calendar moves past the sample dates.
"""
import pytest

PINNED_TODAY = "2026-10-25"


@pytest.fixture(autouse=True)
def _pinned_today(monkeypatch):
    monkeypatch.setenv("COURT_TODAY", PINNED_TODAY)
