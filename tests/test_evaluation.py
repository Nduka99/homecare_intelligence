from pathlib import Path
from typing import Any

import pytest

from homecare.evaluation import sets
from homecare.evaluation.metrics import wilson
from homecare.evaluation.notes import score


def test_wilson_matches_published_values() -> None:
    # 9/10: (0.596, 0.982), Wilson 1927 / Newcombe 1998 method 3.
    low, high = wilson(9, 10)
    assert round(low, 3) == 0.596
    assert round(high, 3) == 0.982
    assert wilson(0, 0) == (0.0, 0.0)


def test_scores_count_per_note_against_hand_worked_answers() -> None:
    notes = [
        {"id": "a", "labels": ["physical abuse"]},
        {"id": "b", "labels": ["financial or material abuse"]},
        {"id": "c", "labels": []},
        {"id": "d", "labels": ["missed task", "possible deterioration (soft sign)"]},
    ]
    runs = {
        "a": {"findings": [{"category": "domestic violence"}], "dropped": 1},
        "b": {"findings": [], "dropped": 0, "error": "timeout"},
        "c": {"findings": [{"category": "self-neglect"}], "dropped": 0},
        "d": {"findings": [{"category": "missed task"}], "dropped": 0},
    }
    report = score(notes, runs)
    assert report["recall"]["act today"]["found"] == 1  # a caught (wrong type); b missed
    assert report["recall"]["act today"]["total"] == 2
    assert report["precision"]["act today"]["found"] == 1  # a right, c a false alarm
    assert report["precision"]["act today"]["total"] == 2
    assert report["recall"]["act this visit"]["found"] == 0
    assert report["recall"]["check"]["found"] == 1
    assert report["safeguarding_type_agreement"]["found"] == 0
    assert report["routine_false_alarms"]["found"] == 1
    assert report["quote_drops"]["found"] == 1
    assert report["quote_drops"]["total"] == 4
    assert report["errors"] == ["b"]
    assert [m["id"] for m in report["missed_safeguarding"]] == ["b"]


def test_a_changed_set_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "eval"
    registry = tmp_path / "sets.json"
    (root / "s").mkdir(parents=True)
    (root / "s" / "notes.jsonl").write_text('{"id": "1"}\n', encoding="utf-8")
    sets.register("s", "test set", root, registry)
    assert sets.require("s", root, registry) == root / "s"
    (root / "s" / "notes.jsonl").write_text('{"id": "2"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        sets.require("s", root, registry)
    with pytest.raises(ValueError, match="changed"):
        sets.register("s", "replaced", root, registry)


def test_the_queue_puts_act_today_first_and_burden_counts_by_hand() -> None:
    from homecare.evaluation.notes import burden, queue

    notes = [
        {"id": "n1", "labels": []},
        {"id": "n2", "labels": ["physical abuse"]},
        {"id": "n3", "labels": ["possible deterioration (soft sign)"]},
        {"id": "n4", "labels": []},
    ]
    runs: dict[str, dict[str, Any]] = {
        "n1": {"findings": [{"category": "possible deterioration (soft sign)"}]},
        "n2": {"findings": [{"category": "physical abuse"}]},
        "n3": {"findings": [{"category": "possible deterioration (soft sign)"}]},
        "n4": {"findings": []},
    }
    assert [n["id"] for n in queue(notes, runs)] == ["n2", "n1", "n3"]
    load = burden(notes, runs, top=2)
    assert load["urgent_flags_per_100_notes"] == 75.0
    # One of the two routine notes is flagged urgent.
    assert load["false_urgent_per_100_routine"] == 50.0
    assert (load["queue_precision"]["found"], load["queue_precision"]["total"]) == (2, 3)
    # The top two are n2 (real) and n1 (routine).
    assert (load["precision_at_top"]["found"], load["precision_at_top"]["total"]) == (1, 2)
