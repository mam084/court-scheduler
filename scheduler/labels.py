"""Short team labels for the printed sheets: S14-B, 16-E, 14-Ca...

The club writes teams this way on its paper court sheets, so the print
views use them. The online calendar keeps the full names.

- Boys (Seaside) teams get an S: "14 Black" / "Seaside 14 Black" -> S14-B, "16 Green" -> S16-G.
- Girls (SDVBC) teams: "<age>-<Head coach>" -> "<age>-<first letter>": "16-Edgar" -> 16-E.
  A coach part that's already short ("16-D&D") stays as it is.
- Two teams that would get the same label are told apart in SHORT_LABEL_OVERRIDES
  (e.g. 14-Connor is 14-C and 14-Cara is 14-Ca); any other clash gets the
  shortest prefix of the coach's name that tells them apart.
"""

from __future__ import annotations

import re

import pandas as pd

from .config import SHORT_LABEL_OVERRIDES, team_is_boys

_BOYS = re.compile(r"(\d{2})\s*[- ]?\s*([A-Za-z]+)\s*$")
_GIRLS = re.compile(r"^\s*(\d{2})\s*-\s*(.+?)\s*$")


def _parts(name: str, club=None) -> tuple[str, str, str] | None:
    """(prefix, age, coach-or-color part) for a team name, or None if it isn't one."""
    name = str(name or "")
    if team_is_boys(club, name):
        m = _BOYS.search(name)
        return ("S", m.group(1), m.group(2).title()) if m else None
    m = _GIRLS.match(name)
    return ("", m.group(1), m.group(2)) if m else None


def _make(prefix: str, age: str, part: str, n: int = 1) -> str:
    tail = part if len(part) <= 3 and not part.isalpha() else part[:n].title()
    return f"{prefix}{age}-{tail}"


def short_label(name: str, club=None) -> str:
    """One team's label on its own (no clash check). Not a team name: returned as is."""
    if name in SHORT_LABEL_OVERRIDES:
        return SHORT_LABEL_OVERRIDES[name]
    p = _parts(name, club)
    return _make(*p) if p else str(name)


def label_map(teams) -> dict[str, str]:
    """{full name: short label} for (name, club) pairs, with clashes told apart."""
    teams = list(dict.fromkeys((str(n), c) for n, c in teams))
    out = {n: SHORT_LABEL_OVERRIDES[n] for n, _ in teams if n in SHORT_LABEL_OVERRIDES}
    rest = [(n, _parts(n, c)) for n, c in teams if n not in out]
    for n, p in rest:
        if p is None:
            out[n] = n
    rest = [(n, p) for n, p in rest if p is not None]
    taken = set(out.values())
    for n, p in rest:
        lab = _make(*p)
        rivals = [q for m, q in rest if m != n and _make(*q) == lab] + \
                 ([None] if lab in taken else [])
        k = 1
        while rivals and k < len(p[2]):
            k += 1
            lab = _make(*p, n=k)
            rivals = [q for m, q in rest if m != n and _make(*q, n=k) == lab] + ([None] if lab in taken else [])
        out[n] = lab
    return out


# Programs' shorthand for the printouts (Nov 2026): {program name: shorthand}. The app fills it in
# from the Programs list each run; a program without one prints its full name.
PROGRAM_SHORT: dict[str, str] = {}


def set_program_shorts(locals_) -> None:
    PROGRAM_SHORT.clear()
    PROGRAM_SHORT.update({lp.name: lp.short for lp in locals_ or [] if getattr(lp, "short", "")})


def with_labels(df: pd.DataFrame) -> pd.DataFrame:
    """A copy of a bookings table with a `label` column: the short label for practices, a
    program's shorthand for its sessions, the name as is for everything else (privates)."""
    if df is None or not len(df) or "name" not in df.columns:
        return df
    df = df.copy()
    is_p = df["kind"] == "Practice" if "kind" in df.columns else pd.Series(True, index=df.index)
    clubs = df["club"] if "club" in df.columns else pd.Series(None, index=df.index)
    lm = label_map(zip(df.loc[is_p, "name"], clubs[is_p]))
    is_l = df["kind"] == "Local" if "kind" in df.columns else pd.Series(False, index=df.index)
    df["label"] = [lm.get(n, n) if p else PROGRAM_SHORT.get(n, n) if lo else n
                   for n, p, lo in zip(df["name"], is_p, is_l)]
    return df


def label_of(r) -> str:
    """What a print shows for a booking row: its label if it has one, else its name."""
    lab = r.get("label") if hasattr(r, "get") else None
    return str(lab if isinstance(lab, str) and lab else r["name"])
