"""The tamper-tag study's measures: false tags by persona, and detection without the control."""

from homecare.evaluation import tampering
from homecare.tamper import tagged


def test_the_tag_needs_more_than_half() -> None:
    assert tagged(0.51) and not tagged(0.5)


def test_false_tags_count_every_note_and_judge_only_personas_with_enough_notes() -> None:
    notes = [{"id": f"a{i}", "persona": "voice"} for i in range(20)]
    notes += [{"id": f"b{i}", "persona": "rare"} for i in range(5)]
    probabilities = {note["id"]: 0.0 for note in notes}
    probabilities["a0"] = 0.9
    probabilities["b0"] = 0.9
    result = tampering.false_tags(notes, probabilities)
    assert (result["overall"]["found"], result["overall"]["total"]) == (2, 25)
    assert list(result["by_persona"]) == ["voice"]
    assert result["by_persona"]["voice"]["found"] == 1


def test_detection_pools_attacks_without_the_benign_control() -> None:
    copies = [
        {"id": "n#C1@outside", "source": "control", "position": "outside"},
        {"id": "n#T1@outside", "source": "transfer", "position": "outside"},
        {"id": "n#T1@inside", "source": "transfer", "position": "inside"},
    ]
    probabilities = {"n#C1@outside": 0.9, "n#T1@outside": 0.9, "n#T1@inside": 0.1}
    result = tampering.detection(copies, probabilities)
    assert (result["pooled"]["found"], result["pooled"]["total"]) == (1, 2)
    assert result["by_source"]["control"]["found"] == 1
    assert result["by_position"]["inside"]["found"] == 0
