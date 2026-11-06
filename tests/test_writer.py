import random

from homecare.notes.categories import CATEGORIES
from homecare.world import writer


def test_specs_are_the_same_on_every_run() -> None:
    assert writer.specs("notes-dev") == writer.specs("notes-dev")


def test_the_held_out_set_shares_no_template_or_persona_with_dev_and_test() -> None:
    held_out = writer.specs("notes-heldout")
    tuned = writer.specs("notes-dev") + writer.specs("notes-test")
    assert not {t for s in held_out for t in s["templates"]} & {
        t for s in tuned for t in s["templates"]
    }
    assert not {s["persona"] for s in held_out} & {s["persona"] for s in tuned}
    assert writer.SETS["notes-heldout"]["writer"] != writer.SETS["notes-test"]["writer"]


def test_every_template_label_is_a_known_category() -> None:
    for pool in ("A", "B"):
        for template in writer.templates(pool):
            assert template["category"] in ("", *CATEGORIES)
            assert template["category"] == "" or template["must"]


def test_a_note_that_lost_its_evidence_or_names_its_label_is_rejected() -> None:
    must = [["bruis"], ["arm"]]
    good = (
        "Helped Doreen wash and dress today. Saw bruises on both arms; she said her son is rough."
    )
    assert writer.problems(good, must) == []
    assert writer.problems(good.replace("arms", "legs"), must) == ["evidence lost: ['arm']"]
    assert "names a category: abuse" in writer.problems(good + " Possible abuse.", must)


def test_perturbation_is_seeded() -> None:
    note = "Gave the morning medication with breakfast because she asked, and helped with a wash."
    persona = writer.PERSONAS["B"][0]
    first = writer.perturbed(note, persona, random.Random(5))
    assert first == writer.perturbed(note, persona, random.Random(5))
    assert "meds" in first and "w/" in first
