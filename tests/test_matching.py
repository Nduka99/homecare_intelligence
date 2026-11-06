"""The rota checker and the baseline rota, on a hand-made day with known answers."""

import copy
from typing import Any

from homecare.matching import baseline, check

MINUTE = 60  # driving times are in seconds


def world() -> dict[str, Any]:
    travel = {
        "h1": {
            "h1": 0,
            "h2": 10 * MINUTE,
            "c1": 10 * MINUTE,
            "c2": 20 * MINUTE,
            "far": 200 * MINUTE,
        },
        "h2": {
            "h1": 10 * MINUTE,
            "h2": 0,
            "c1": 5 * MINUTE,
            "c2": 15 * MINUTE,
            "far": 200 * MINUTE,
        },
        "c1": {
            "h1": 10 * MINUTE,
            "h2": 5 * MINUTE,
            "c1": 0,
            "c2": 10 * MINUTE,
            "far": 200 * MINUTE,
        },
        "c2": {
            "h1": 20 * MINUTE,
            "h2": 15 * MINUTE,
            "c1": 10 * MINUTE,
            "c2": 0,
            "far": 200 * MINUTE,
        },
        "far": {
            "h1": 200 * MINUTE,
            "h2": 200 * MINUTE,
            "c1": 200 * MINUTE,
            "c2": 200 * MINUTE,
            "far": 0,
        },
    }
    return {
        "shift": [420, 1320],
        "travel": travel,
        "clients": [
            {"id": "C1", "node": "c1"},
            {"id": "C2", "node": "c2"},
            {"id": "C3", "node": "far"},
        ],
        "carers": [
            {"id": "K1", "home": "h1", "skills": ["personal care"], "weekly_hours": 37.5},
            {"id": "K2", "home": "h2", "skills": ["personal care"], "weekly_hours": 37.5},
        ],
        "usual": {"C1": "K1", "C2": "K1", "C3": "K2"},
        "visits": [
            visit("V1", "C1", 420, 600, 45, ["personal care"]),
            visit("V2", "C2", 720, 840, 30, ["personal care"]),
            visit("V3", "C2", 990, 1110, 30, ["dementia"]),
            visit("V4", "C3", 420, 480, 30, ["personal care"]),
        ],
    }


def visit(
    id_: str, client: str, opens: int, closes: int, duration: int, skills: list[str]
) -> dict[str, Any]:
    return {
        "id": id_,
        "client": client,
        "day": 0,
        "open": opens,
        "close": closes,
        "duration": duration,
        "skills": skills,
    }


VALID: dict[str, Any] = {
    "routes": {"K1": ["V1", "V2"]},
    "uncovered": [
        {"visit": "V3", "reason": "no skilled carer"},
        {"visit": "V4", "reason": "too far"},
    ],
}


def test_a_valid_rota_has_no_problems() -> None:
    result = check.check(VALID, world(), 0)
    assert result["problems"] == []
    # 10 to C1, 10 on to C2, 20 home.
    assert result["driving_minutes"] == 40
    assert (result["covered"], result["uncovered"], result["continuity_breaks"]) == (2, 2, 0)


def test_sabotage_each_broken_rule_is_reported() -> None:
    late = {"routes": {"K1": ["V2", "V1"]}, "uncovered": VALID["uncovered"]}
    assert "K1 is late for V1" in check.check(late, world(), 0)["problems"]
    unskilled = {"routes": {"K1": ["V1", "V2", "V3"]}, "uncovered": VALID["uncovered"][1:]}
    assert "K1 lacks a skill for V3" in check.check(unskilled, world(), 0)["problems"]
    twice = {"routes": {"K1": ["V1", "V2"], "K2": ["V2"]}, "uncovered": VALID["uncovered"]}
    assert "V2 is placed more than once" in check.check(twice, world(), 0)["problems"]
    missing = {"routes": {"K1": ["V1"]}, "uncovered": VALID["uncovered"]}
    assert "V2 is neither on a route nor uncovered" in check.check(missing, world(), 0)["problems"]
    short = world()
    short["carers"][0]["weekly_hours"] = 5  # one hour a day
    assert "K1 works over the daily limit" in check.check(VALID, short, 0)["problems"]


def test_a_visit_given_to_someone_other_than_the_usual_carer_is_a_continuity_break() -> None:
    rota = {"routes": {"K1": ["V1"], "K2": ["V2"]}, "uncovered": VALID["uncovered"]}
    assert check.check(rota, world(), 0)["continuity_breaks"] == 1


def test_the_baseline_passes_the_checker_and_gives_each_gap_a_reason() -> None:
    rota = baseline.plan_day(world(), 0)
    assert check.check(rota, world(), 0)["problems"] == []
    assert rota["routes"] == {"K1": ["V1", "V2"]}
    by_visit = sorted(rota["uncovered"], key=lambda item: item["visit"])
    assert by_visit == VALID["uncovered"]


def test_a_clash_is_told_apart_from_distance() -> None:
    busy = world()
    busy["carers"] = busy["carers"][:1]
    busy["visits"].append(visit("V5", "C2", 420, 480, 30, ["personal care"]))
    rota = baseline.plan_day(busy, 0)
    assert {"visit": "V5", "reason": "clashes with other visits"} in rota["uncovered"]


def test_the_checker_does_not_change_the_world() -> None:
    before = world()
    after = copy.deepcopy(before)
    check.check(VALID, after, 0)
    assert after == before


def test_sabotage_a_wrong_gender_carer_is_reported() -> None:
    gendered = world()
    gendered["carers"][0]["gender"] = "male"
    gendered["visits"][0]["gender"] = "female"
    found = check.check(VALID, gendered, 0)["problems"]
    assert "K1 is not the preferred gender for V1" in found


def test_a_two_carer_visit_is_never_on_one_route() -> None:
    paired = world()
    paired["visits"][0]["two_carers"] = True
    assert "V1 needs two carers but has one" in check.check(VALID, paired, 0)["problems"]
    rota = baseline.plan_day(paired, 0)
    assert {"visit": "V1", "reason": "needs two carers (not planned yet)"} in rota["uncovered"]
    assert check.check(rota, paired, 0)["problems"] == []


def test_the_baseline_names_a_missing_gender_as_the_reason() -> None:
    gendered = world()
    for carer in gendered["carers"]:
        carer["gender"] = "male"
    gendered["visits"][0]["gender"] = "female"
    rota = baseline.plan_day(gendered, 0)
    assert {"visit": "V1", "reason": "no carer of the preferred gender"} in rota["uncovered"]


def test_breaks_for_high_need_clients_are_counted_apart() -> None:
    tiered = world()
    tiered["visits"][1]["tier"] = 3
    rota = {"routes": {"K1": ["V1"], "K2": ["V2"]}, "uncovered": VALID["uncovered"]}
    result = check.check(rota, tiered, 0)
    assert (result["continuity_breaks"], result["high_need_breaks"]) == (1, 1)


def test_a_split_shift_is_allowed() -> None:
    split = world()
    split["visits"].append(visit("V6", "C1", 1170, 1320, 30, ["personal care"]))
    rota = {"routes": {"K1": ["V1", "V2", "V6"]}, "uncovered": VALID["uncovered"]}
    # From leaving home at 07:00 to getting back at 20:10 is 790 minutes, over the 450-minute
    # limit; the work itself is 105 care minutes and 40 driving, so the rota is valid.
    assert check.check(rota, split, 0)["problems"] == []


def test_sabotage_work_on_a_day_off_is_reported() -> None:
    resting = world()
    resting["carers"][0]["days_off"] = [0, 1]
    assert "K1 works on a day off" in check.check(VALID, resting, 0)["problems"]
    assert check.check(VALID, resting, 2)["problems"] != check.check(VALID, resting, 0)["problems"]
    rota = baseline.plan_day(resting, 0)
    assert "K1" not in rota["routes"]
    assert check.check(rota, resting, 0)["problems"] == []


def long_day(third_opens: int) -> dict[str, Any]:
    long = world()
    long["carers"][0]["weekly_hours"] = 50  # 600 minutes a day
    long["visits"] = [
        visit("V10", "C1", 420, 1320, 120, ["personal care"]),
        visit("V11", "C1", 420, 1320, 120, ["personal care"]),
        visit("V12", "C1", third_opens, 1320, 120, ["personal care"]),
    ]
    return long


def test_sabotage_over_six_hours_without_a_break_is_reported() -> None:
    rota = {"routes": {"K1": ["V10", "V11", "V12"]}, "uncovered": []}
    # 360 care minutes and 20 driving, back to back: no break.
    found = check.check(rota, long_day(420), 0)["problems"]
    assert found == ["K1 has no 20-minute break in over 6 hours of work"]
    # Leaving at 07:00, the second visit ends at 11:10; a third opening at 11:30 leaves the break.
    assert check.check(rota, long_day(690), 0)["problems"] == []


def test_a_break_on_the_usual_carers_day_off_is_not_avoidable() -> None:
    resting = world()
    resting["carers"][0]["days_off"] = [0, 1]  # K1, usual carer of C1 and C2
    resting["visits"][1]["tier"] = 3
    rota = {"routes": {"K2": ["V1", "V2"]}, "uncovered": VALID["uncovered"]}
    result = check.check(rota, resting, 0)
    assert (result["continuity_breaks"], result["avoidable_breaks"]) == (2, 0)
    working = check.check(rota, world() | {"visits": resting["visits"]}, 0)
    assert (working["avoidable_breaks"], working["avoidable_high_need_breaks"]) == (2, 1)
