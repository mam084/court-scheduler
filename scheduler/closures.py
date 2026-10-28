"""Club closure days.

The club practices on minor holidays (Labor Day, Memorial Day, MLK Day...)
but closes for big family holidays. This builds that default list; the
club can add or remove dates in the app or the spreadsheet.
"""

from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th occurrence (1-based) of a weekday (Mon=0) in a month."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def major_holidays(year: int) -> dict[date, str]:
    """The family holidays the club is closed for in `year`, by date."""
    thanksgiving = _nth_weekday(year, 11, 3, 4)  # 4th Thursday of November
    return {
        date(year, 1, 1): "New Year's Day",
        date(year, 7, 4): "Independence Day",
        thanksgiving: "Thanksgiving",
        thanksgiving + timedelta(days=1): "Day after Thanksgiving",
        date(year, 12, 24): "Christmas Eve",
        date(year, 12, 25): "Christmas Day",
        date(year, 12, 31): "New Year's Eve",
    }


def default_closures(start: date, end: date) -> list[tuple[date, str]]:
    """Weekday closures between start and end (inclusive)."""
    out = []
    for year in range(start.year, end.year + 1):
        for d, name in major_holidays(year).items():
            if start <= d <= end and d.weekday() < 5:
                out.append((d, name))
    return sorted(out)
