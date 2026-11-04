from datetime import date
from types import SimpleNamespace as T

import pandas as pd

from scheduler.handwritten import read_rows, slot_for, to_overrides

TEAMS = [T(name=n, club=c) for n, c in [
    ("14 Black", "Seaside"), ("13 Black", "Seaside"), ("13-Bea", "SDVBC"),
    ("14-Connor", "SDVBC"), ("14-Cara", "SDVBC"), ("16-Edgar", "SDVBC"), ("16-D&D", "SDVBC")]]

SUN, WED = date(2026, 11, 29), date(2026, 12, 2)


def test_times_as_written_on_the_sheet():
    assert slot_for(WED, "17:30") == "5:30"
    assert slot_for(WED, "5:30") == "5:30"
    assert slot_for(WED, "5:30-7:20") == "5:30"
    assert slot_for(WED, "3:45") == "3:45"
    assert slot_for(WED, "19:20") == "7:20"
    assert slot_for(SUN, "9:00") == "9AM"
    assert slot_for(SUN, "1-3") == "1PM"
    assert slot_for(SUN, "17:00") == "5PM"


def test_rows_become_add_edits_with_sheet_labels():
    csv = ("date,day,court,start,end,team,note\n"
           "2026-12-02,Wed,2,17:30,19:20,S13-B,\n"
           "2026-12-03,Thu,1,15:45,17:30,13-B,\n"
           "2026-12-02,Wed,2,19:20,21:10,14-CA,\n"
           "2026-12-01,Tue,6,15:45,17:30,14-C,\n"
           "2026-11-29,Sun,2,11:00,13:00,S14-B,\n"
           "2026-11-29,Sun,7,9:00,11:00,16-D&D,\n"
           "2026-11-29,Sun,7,9:00,11:00,16-D&D,\n"
           "2026-12-02,Wed,3,17:30,19:20,99-Z,\n"
           "2026-12-02,Wed,9,17:30,19:20,16-E,\n").encode()
    rows, problems = to_overrides(read_rows(csv, "x.csv"), TEAMS)
    got = {(r["team"], r["date"], r["time"], r["court"]) for r in rows}
    assert got == {("13 Black", "12/02/2026", "5:30", "2"), ("13-Bea", "12/03/2026", "3:45", "1"),
                   ("14-Cara", "12/02/2026", "7:20", "2"), ("14-Connor", "12/01/2026", "3:45", "6"),
                   ("14 Black", "11/29/2026", "11AM", "2"), ("16-D&D", "11/29/2026", "9AM", "7")}
    assert all(r["action"] == "Add" for r in rows)
    assert len(problems) == 3  # the repeat, the unknown team, the bad court


def test_short_label_for_one_team():
    csv = b"date,court,start,team\n2026-12-02,1,17:30,16-D\n2026-12-02,3,17:30,14-X\n"
    rows, problems = to_overrides(read_rows(csv, "x.csv"), TEAMS)
    assert [r["team"] for r in rows] == ["16-D&D"]
    assert len(problems) == 1


def test_third_practice_in_a_week_goes_in_as_extra():
    teams = [T(name="16-Edgar", club="SDVBC", per_week=2), T(name="14 Black", club="Seaside", per_week=2)]
    csv = (b"date,court,start,team\n"
           b"2026-12-04,1,17:30,16-E\n2026-11-30,1,17:30,16-E\n2026-12-02,1,17:30,16-E\n"
           b"2026-12-07,1,17:30,16-E\n"  # next week: back to a regular practice
           b"2026-12-01,2,19:20,S14-B\n2026-12-03,2,19:20,S14-B\n")
    existing = pd.DataFrame([{"team": "14 Black", "action": "Add", "date": "11/30/2026", "days": "",
                              "time": "7:20", "court": "", "note": ""}])
    rows, problems = to_overrides(read_rows(csv, "x.csv"), teams, existing)
    act = {(r["team"], r["date"]): r["action"] for r in rows}
    assert act[("16-Edgar", "12/04/2026")] == "Extra"  # the latest of the three that week
    assert act[("16-Edgar", "11/30/2026")] == act[("16-Edgar", "12/02/2026")] == "Add"
    assert act[("16-Edgar", "12/07/2026")] == "Add"
    assert act[("14 Black", "12/01/2026")] == "Add" and act[("14 Black", "12/03/2026")] == "Extra"
    assert problems == []
