"""The queue-ranking study: a score only reorders inside a band; the add-only check can fail."""

from typing import Any

import pytest

from homecare.evaluation import ranking

ABUSE = "physical abuse"  # act today
SOFT = "possible deterioration (soft sign)"  # act this visit
MISSED = "missed task"  # check: never queued


def finding(category: str, reason: str = "a reason") -> dict[str, str]:
    return {"category": category, "quote": "q", "reason": reason}


def world() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    notes = [
        {"id": "n1", "text": "one", "labels": [ABUSE]},
        {"id": "n2", "text": "two", "labels": []},
        {"id": "n3", "text": "three", "labels": [SOFT]},
        {"id": "n4", "text": "four", "labels": []},
    ]
    runs = {
        "n1": {"findings": [finding(ABUSE)]},
        "n2": {"findings": [finding(ABUSE), finding(MISSED)]},
        "n3": {"findings": [finding(SOFT)]},
        "n4": {"findings": [finding(MISSED)]},
    }
    return notes, runs


def test_jobs_cover_only_urgent_findings_of_queued_notes_with_the_whole_note() -> None:
    notes, runs = world()
    pairs = ranking.jobs(notes, runs, "claim")
    assert [(p["note"], p["premise"]) for p in pairs] == [
        ("n1", "one"),
        ("n2", "two"),
        ("n3", "three"),
    ]


def test_the_category_arm_uses_the_category_meaning() -> None:
    text = ranking.hypothesis(finding(SOFT), "category")
    assert text.startswith("The note describes an observed change")


def test_a_note_scores_its_best_supported_finding() -> None:
    pairs = [{"note": "a"}, {"note": "a"}, {"note": "b"}]
    assert ranking.note_scores(pairs, [0.2, 0.9, 0.4]) == {"a": 0.9, "b": 0.4}


def test_bands_stay_first_and_the_score_orders_inside_a_band() -> None:
    notes, runs = world()
    scores = {"n1": 0.1, "n2": 0.8, "n3": 0.99}
    order = [n["id"] for n in ranking.reorder(notes, runs, scores)]
    assert order == ["n2", "n1", "n3"]  # n3's high score never lifts it above act today


def test_the_add_only_check_catches_a_ranker_that_drops_a_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes, runs = world()
    scores = {"n1": 0.1, "n2": 0.8, "n3": 0.99}
    assert ranking.study(notes, runs, scores)["same_items"] is True
    real_reorder = ranking.reorder
    monkeypatch.setattr(ranking, "reorder", lambda *args: real_reorder(*args)[:-1])
    assert ranking.study(notes, runs, scores)["same_items"] is False


def test_a_perfect_score_reaches_auroc_one_and_the_perfect_order_ranks_real_notes_first() -> None:
    notes, runs = world()
    result = ranking.study(notes, runs, {"n1": 0.9, "n2": 0.1, "n3": 0.8})
    assert result["auroc"]["auroc"] == 1.0
    assert result["perfect_real_in_top"] == 2
    assert result["false_items"] == 1
