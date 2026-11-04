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


def with_labels(df: pd.DataFrame) -> pd.DataFrame:
    """A copy of a bookings table with a `label` column: the short label for practices,
    the name as is for everything else (locals, privates)."""
    if df is None or not len(df) or "name" not in df.columns:
        return df
    df = df.copy()
    is_p = df["kind"] == "Practice" if "kind" in df.columns else pd.Series(True, index=df.index)
    clubs = df["club"] if "club" in df.columns else pd.Series(None, index=df.index)
    lm = label_map(zip(df.loc[is_p, "name"], clubs[is_p]))
    df["label"] = [lm.get(n, n) if p else n for n, p in zip(df["name"], is_p)]
    return df


def label_of(r) -> str:
    """What a print shows for a booking row: its label if it has one, else its name."""
    lab = r.get("label") if hasattr(r, "get") else None
    return str(lab if isinstance(lab, str) and lab else r["name"])
