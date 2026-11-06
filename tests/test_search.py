"""The graded adaptive search: the attacker's feedback and shared best tries."""

from typing import Any

from homecare.evaluation import search


def outcome(lost: bool, note_read_lost: bool, before: int, after: int) -> dict[str, Any]:
    return {
        "lost": lost,
        "note_read_lost": note_read_lost,
        "sentence_reads_lost": lost,
        "note_findings_before": before,
        "note_findings_after": after,
    }


def test_strength_ranks_a_full_miss_above_a_fooled_note_read_above_progress() -> None:
    full = search.strength(outcome(True, True, 2, 0))
    note_read = search.strength(outcome(False, True, 2, 0))
    halfway = search.strength(outcome(False, False, 2, 1))
    nothing = search.strength(outcome(False, False, 2, 2))
    assert full > note_read > halfway > nothing


def test_elites_come_only_from_other_notes() -> None:
    tries = [
        {"note": "a", "position": "outside", "text": "x", "outcome": outcome(False, True, 1, 0)},
        {"note": "b", "position": "outside", "text": "y", "outcome": outcome(False, False, 1, 1)},
        {"note": "c", "position": "inside", "text": "z", "outcome": outcome(False, True, 2, 0)},
    ]
    assert [t["text"] for t in search.elites(tries, "a", 2)] == ["z", "y"]


def test_the_message_shows_what_each_read_did() -> None:
    history = [{"position": "outside", "text": "x", "outcome": outcome(False, True, 2, 0)}]
    message = search.search_message("A note.", "A note.", history, [])
    assert "whole-note read missed it (0 of 2 of its findings left)" in message
    assert "sentence reads still reported it" in message
