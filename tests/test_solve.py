"""The OR-Tools rota on the hand-made day from test_matching, each answer checked by the
independent checker and worked out by hand."""

from typing import Any

from homecare.matching import check, solve
from test_matching import VALID, visit, world


def solved(day_world: dict[str, Any], continuity: int = 15, high_need: int = 3) -> dict[str, Any]:
    return solve.solve(day_world, 0, continuity, high_need, solutions=50, seconds=2)


def test_the_rota_passes_the_checker_and_matches_the_hand_answer() -> None:
    rota = solved(world())
    assert check.check(rota, world(), 0)["problems"] == []
    assert rota["search"]["stopped"] == "solution limit"
    assert rota["routes"] == VALID["routes"]
    assert sorted(rota["uncovered"], key=lambda item: item["visit"]) == VALID["uncovered"]


def one_visit_world(tier: int = 1) -> dict[str, Any]:
    """V1 at C1, whose usual carer K1 is 10 minutes away; K2 is 5 minutes away.
    Out and back: K1 drives 20 minutes, K2 drives 10."""
    near = world()
    near["visits"] = [visit("V1", "C1", 420, 600, 45, ["personal care"]) | {"tier": tier}]
    return near


def test_continuity_cost_moves_a_visit_to_the_usual_carer() -> None:
    assert solved(one_visit_world(), continuity=0)["routes"] == {"K2": ["V1"]}
    # A stranger costs 15 minutes: K2 at 10 + 15 = 25 loses to K1 at 20.
    assert solved(one_visit_world(), continuity=15)["routes"] == {"K1": ["V1"]}


def test_the_high_need_multiplier_applies_only_above_tier_one() -> None:
    # 6 minutes for a stranger: K2 at 10 + 6 = 16 beats K1 at 20.
    assert solved(one_visit_world(tier=3), continuity=6, high_need=1)["routes"] == {"K2": ["V1"]}
    # Tripled for a high-need client: K2 at 10 + 18 = 28 loses to K1 at 20.
    assert solved(one_visit_world(tier=3), continuity=6, high_need=3)["routes"] == {"K1": ["V1"]}
    assert solved(one_visit_world(tier=1), continuity=6, high_need=3)["routes"] == {"K2": ["V1"]}


def test_a_visit_nobody_may_do_is_never_assigned() -> None:
    # V3 needs dementia skills nobody has; an empty allowed list would wrongly allow everyone.
    rota = solved(world())
    assert {"visit": "V3", "reason": "no skilled carer"} in rota["uncovered"]
    assert all("V3" not in route for route in rota["routes"].values())


def test_a_two_carer_visit_is_never_on_a_route() -> None:
    paired = world()
    paired["visits"][0]["two_carers"] = True
    rota = solved(paired)
    assert {"visit": "V1", "reason": "needs two carers (not planned yet)"} in rota["uncovered"]
    assert check.check(rota, paired, 0)["problems"] == []


def test_a_carer_on_a_day_off_is_not_planned() -> None:
    resting = world()
    resting["carers"][0]["days_off"] = [0, 1]  # K1, who would otherwise do V1 and V2
    rota = solved(resting)
    assert rota["routes"] == {"K2": ["V1", "V2"]}
    assert check.check(rota, resting, 0)["problems"] == []


def test_the_daily_limit_drops_a_visit_rather_than_break_it() -> None:
    short = world()
    short["carers"] = short["carers"][:1]
    short["carers"][0]["weekly_hours"] = 5  # 60 minutes: V1 out and back is 65
    rota = solved(short)
    assert check.check(rota, short, 0)["problems"] == []
    assert rota["routes"] == {}
    # Only one rota is possible, so the search runs out of new ones: the safety net stops it.
    assert rota["search"]["stopped"] == "time limit"


def test_the_same_inputs_give_the_same_rota() -> None:
    assert solved(world()) == solved(world())
