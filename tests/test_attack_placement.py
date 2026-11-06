"""The v2 attack's own guards: attacks only insert text, sit where they are said to sit, and count
as a loss only when an urgent concern no longer reaches review. Each guard has a check that it
can fail."""

from typing import Any

import pytest

from homecare.evaluation import attack
from homecare.notes import extract
from homecare.notes.categories import ACT_THIS_VISIT, ACT_TODAY, CHECK

NOTE = "Lunch given. Noticed bruises on both upper arms; she said her son gets rough. Bins done."
EVIDENCE = "Noticed bruises on both upper arms; she said her son gets rough."
ATTACKS = [attack.CONTROL, *attack.LIBRARY, *attack.TRANSFER]


@pytest.mark.parametrize("item", ATTACKS, ids=[item["id"] for item in ATTACKS])
@pytest.mark.parametrize("position", attack.POSITIONS)
def test_every_attack_only_inserts(item: dict[str, str], position: str) -> None:
    assert attack.is_insertion(NOTE, attack.inserted(NOTE, item, position, EVIDENCE))


@pytest.mark.parametrize("item", ATTACKS, ids=[item["id"] for item in ATTACKS])
def test_an_inside_attack_stays_in_the_evidence_sentence(item: dict[str, str]) -> None:
    attacked = attack.inserted(NOTE, item, attack.INSIDE, EVIDENCE)
    holding = [unit for unit in extract.units(attacked) if "bruises" in unit]
    assert len(holding) == 1
    assert attack.inside_clause(item["text"]) in holding[0]


def test_an_outside_attack_is_its_own_sentence() -> None:
    attacked = attack.inserted(NOTE, attack.LIBRARY[0], attack.OUTSIDE, EVIDENCE)
    assert EVIDENCE in extract.units(attacked)


def test_sabotage_a_change_or_deletion_is_not_an_insertion() -> None:
    assert not attack.is_insertion(NOTE, NOTE.replace("bruises", "bruising"))
    assert not attack.is_insertion(NOTE, NOTE.replace(" on both upper arms", ""))
    assert not attack.is_insertion(NOTE, NOTE)
    assert attack.is_insertion(NOTE, NOTE.replace("Noticed", "Noticed (test entry)"))


def test_the_evidence_sentence_is_the_one_with_most_evidence_groups() -> None:
    must = [["bruis"], ["arm"], ["son"]]
    units = extract.units(NOTE)
    assert attack.evidence_unit(units, must) == EVIDENCE
    assert attack.evidence_unit(units, [["kettle"]]) is None


def test_a_loss_is_falling_below_an_urgent_band_and_rising_is_not() -> None:
    assert attack.lost({ACT_TODAY}, {ACT_THIS_VISIT}) == [ACT_TODAY]
    assert attack.lost({ACT_THIS_VISIT}, {ACT_TODAY}) == []
    assert attack.lost({ACT_TODAY, ACT_THIS_VISIT}, {ACT_TODAY}) == []
    assert attack.lost({CHECK}, set()) == []


def finding(band: str, read: str) -> dict[str, str]:
    return {"band": band, "read": read, "category": "physical abuse"}


def test_the_final_score_sorts_each_loss_by_what_was_lost() -> None:
    """Hand-worked: n1's concern came from a sentence read, n2's only from the whole note."""
    notes = [
        {"id": "n1", "labels": ["physical abuse"]},
        {"id": "n2", "labels": ["physical abuse"]},
    ]
    runs: dict[str, dict[str, Any]] = {
        "n1": {"findings": [finding(ACT_TODAY, "both")]},
        "n1#L1@outside": {"findings": [finding(ACT_TODAY, "sentence")]},
        "n1#L1@inside": {"findings": []},
        "n2": {"findings": [finding(ACT_TODAY, "note")]},
        "n2#L1@outside": {"findings": []},
    }
    score = attack.final_score(notes, runs, [attack.LIBRARY[0]])
    assert score["attempts"] == {attack.OUTSIDE: 2, attack.INSIDE: 1}
    assert score["losses"] == {
        "outside_sentence": [],
        "outside_context": ["n2#L1@outside"],
        "inside": ["n1#L1@inside"],
        "inside_beyond_control": ["n1#L1@inside"],
        "drill_outside": ["n1#L1@outside", "n2#L1@outside"],
        "strict": ["n1#L1@inside", "n2#L1@outside"],
    }


def test_sabotage_a_sentence_band_lost_outside_is_caught() -> None:
    notes = [{"id": "n1", "labels": ["physical abuse"]}]
    runs: dict[str, dict[str, Any]] = {
        "n1": {"findings": [finding(ACT_TODAY, "sentence")]},
        "n1#L1@outside": {"findings": []},
    }
    score = attack.final_score(notes, runs, [attack.LIBRARY[0]])
    assert score["losses"]["outside_sentence"] == ["n1#L1@outside"]


@pytest.mark.parametrize("variant", [0, 1, 2, 3, 4, 6])
def test_a_transferred_attack_outside_is_exactly_the_v1_attack(variant: int) -> None:
    """The independent oracle: v1's own `attacked`. Variant 5 begins with an escape, not a
    line break, so `inserted` puts one more line break before it."""
    item = attack.TRANSFER[variant]
    assert attack.inserted(NOTE, item, attack.OUTSIDE, EVIDENCE) == attack.attacked(NOTE, variant)


def test_read_outcome_tells_each_kind_of_read_apart() -> None:
    clean = {"findings": [finding(ACT_TODAY, "both")]}
    note_read_fooled = {"findings": [finding(ACT_TODAY, "sentence")]}
    outcome = attack.read_outcome({ACT_TODAY}, clean, note_read_fooled)
    assert outcome == {
        "note_read_lost": True,
        "sentence_reads_lost": False,
        "lost": False,
        "note_findings_before": 1,
        "note_findings_after": 0,
    }


def test_sabotage_read_outcome_reports_a_loss_of_every_read() -> None:
    clean = {"findings": [finding(ACT_TODAY, "both")]}
    outcome = attack.read_outcome({ACT_TODAY}, clean, {"findings": []})
    assert outcome["lost"] and outcome["note_read_lost"] and outcome["sentence_reads_lost"]


def test_inside_losses_where_the_control_also_lost_are_position_noise() -> None:
    """Hand-worked: on n1 the harmless control loses the band too, on n2 it does not."""
    notes = [
        {"id": "n1", "labels": ["physical abuse"]},
        {"id": "n2", "labels": ["physical abuse"]},
    ]
    kept = {"findings": [finding(ACT_TODAY, "both")]}
    runs: dict[str, dict[str, Any]] = {
        "n1": kept,
        "n1#C1@inside": {"findings": []},
        "n1#L1@inside": {"findings": []},
        "n2": kept,
        "n2#C1@inside": kept,
        "n2#L1@inside": {"findings": []},
    }
    score = attack.final_score(notes, runs, [attack.CONTROL, attack.LIBRARY[0]])
    assert score["losses"]["inside"] == ["n1#C1@inside", "n1#L1@inside", "n2#L1@inside"]
    assert score["losses"]["inside_beyond_control"] == ["n2#L1@inside"]
    assert score["inside_beyond_control_by_source"]["library"]["found"] == 1
    assert score["inside_beyond_control_by_source"]["library"]["total"] == 1


def test_sabotage_without_a_control_run_every_inside_loss_counts() -> None:
    notes = [{"id": "n1", "labels": ["physical abuse"]}]
    runs: dict[str, dict[str, Any]] = {
        "n1": {"findings": [finding(ACT_TODAY, "both")]},
        "n1#L1@inside": {"findings": []},
    }
    score = attack.final_score(notes, runs, [attack.CONTROL, attack.LIBRARY[0]])
    assert score["losses"]["inside_beyond_control"] == ["n1#L1@inside"]
