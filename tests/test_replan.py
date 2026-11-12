"""Re-plan on a carer's drop-out, on the hand-made day from test_matching: each answer
checked by the independent checker with its holds, and worked out by hand."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from homecare.app import seed
from homecare.matching import check, replan
from test_matching import VALID, world

ROTA = seed.FILE.with_name("rota.json")

TEN = 600  # 10:00


def replanned(day_world: dict[str, Any], rota: dict[str, Any], out: str) -> dict[str, Any]:
    return replan.replan(day_world, rota, 0, out, TEN, 15, 3, solutions=50, seconds=2)


def test_a_drop_out_passes_the_checker_and_matches_the_hand_answer() -> None:
    # K1 does V1 at 07:10 (before 10:00, so it stays) and was to do V2 at 12:00. K1 stops at
    # 10:00; K2, free at home from 10:00, takes V2: leaves 11:45, 15 minutes' drive.
    after = replanned(world(), VALID, "K1")
    assert after["routes"] == {"K1": ["V1"], "K2": ["V2"]}
    assert after["holds"] == {"K2": (0, TEN)}
    assert sorted(after["uncovered"], key=lambda item: item["visit"]) == VALID["uncovered"]
    assert check.check(after, world(), 0, after["holds"])["problems"] == []
    assert check.unmoved(VALID, after, world(), 0, "K1", TEN) == []
    assert check.reason_problems(after, world(), 0, after["holds"], "K1") == []


def test_a_visit_nobody_left_can_do_is_uncovered_with_a_true_reason() -> None:
    skilled = world()
    skilled["carers"][0]["skills"] = ["personal care", "dementia"]
    skilled["visits"][1]["skills"] = ["dementia"]  # V2: only K1, who drops out
    after = replanned(skilled, VALID, "K1")
    assert after["routes"] == {"K1": ["V1"]}
    assert {"visit": "V2", "reason": "no skilled carer"} in after["uncovered"]
    assert check.check(after, skilled, 0, after["holds"])["problems"] == []
    assert check.reason_problems(after, skilled, 0, after["holds"], "K1") == []


def test_drill_a_done_visit_moved_is_caught() -> None:
    after = replanned(world(), VALID, "K1")
    moved = copy.deepcopy(after)
    moved["routes"] = {"K2": ["V1", "V2"]}
    assert "K1's visits before the re-plan changed" in check.unmoved(
        VALID, moved, world(), 0, "K1", TEN
    )


def test_drill_the_dropped_carer_kept_on_is_caught() -> None:
    kept_on = {"routes": {"K1": ["V1", "V2"]}, "uncovered": VALID["uncovered"]}
    assert "K1 has visits after dropping out" in check.unmoved(
        VALID, kept_on, world(), 0, "K1", TEN
    )


def test_drill_a_re_planned_visit_needing_an_earlier_start_is_caught() -> None:
    # K2 does V1 (C1, window 07:00-10:00, 45 minutes). Free from 07:00 it fits; held until 09:30
    # it starts at 09:35 and ends 10:20, after the window closes.
    rota = {"routes": {"K2": ["V1"]}, "uncovered": VALID["uncovered"] + [
        {"visit": "V2", "reason": "clashes with other visits"}]}  # fmt: skip
    assert "K2 is late for V1" not in check.check(rota, world(), 0)["problems"]
    held = check.check(rota, world(), 0, {"K2": (0, 570)})["problems"]
    assert "K2 is late for V1" in held


def test_drill_a_reason_that_is_untrue_is_caught() -> None:
    # V2 fits K1's day after V1, so "clashes" is untrue; so is "no skilled carer" for V1.
    claims = {"routes": {"K1": ["V1"]}, "uncovered": VALID["uncovered"] + [
        {"visit": "V2", "reason": "clashes with other visits"}]}  # fmt: skip
    assert "V2: 'clashes with other visits' is untrue" in check.reason_problems(claims, world(), 0)
    wrong = {"routes": {"K1": ["V2"]}, "uncovered": VALID["uncovered"] + [
        {"visit": "V1", "reason": "no skilled carer"}]}  # fmt: skip
    assert "V1: 'no skilled carer' is untrue" in check.reason_problems(wrong, world(), 0)
    assert check.reason_problems(VALID, world(), 0) == []


def test_the_same_drop_out_gives_the_same_re_plan() -> None:
    assert replanned(world(), VALID, "K1") == replanned(world(), VALID, "K1")


def test_the_committed_scenario_passes_every_check_again() -> None:
    """The saved re-plan the coordinator sees (K15 out at 10:00 on day 27), checked again from
    the file. The world it was planned on stays on the laptop, so elsewhere this is skipped."""
    world_file = Path("data/world/agency.json")
    if not world_file.exists():
        pytest.skip("the built world is laptop-only")
    full = json.loads(world_file.read_text(encoding="utf-8"))
    saved = seed.load(ROTA, ROTA.with_name("rota.json.sha256"))["replan"]
    plan = json.loads(Path("data/rota/demo-week.json").read_text(encoding="utf-8"))
    before = plan["rotas"][saved["day"] - 21]
    after = saved["rota"]
    holds = {carer: (hold[0], hold[1]) for carer, hold in after["holds"].items()}
    day, out, at = saved["day"], saved["out"], saved["at"]
    assert check.check(after, full, day, holds)["problems"] == []
    assert check.unmoved(before, after, full, day, out, at) == []
    assert check.reason_problems(after, full, day, holds, out) == []
    again = after | {"holds": holds, "search": saved["search"]}
    assert replan.summarise(full, day, before, again, out, at)["changes"] == saved["changes"]
