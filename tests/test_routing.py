from homecare.notes import routing
from homecare.notes.categories import ACT_THIS_VISIT, ACT_TODAY, CHECK, NOTE


def result(note_id: str, *bands: str) -> dict[str, object]:
    return {"id": note_id, "findings": [{"band": band} for band in bands]}


def test_every_safeguarding_or_deterioration_finding_is_queued() -> None:
    results = [result("a", ACT_TODAY), result("b", CHECK, ACT_THIS_VISIT), result("c", NOTE)]
    queued = {item["note"] for item in routing.review_queue(results, seed="s")}
    assert {"a", "b"} <= queued
    assert "c" not in queued


def test_about_seven_percent_of_no_finding_notes_are_checked() -> None:
    results = [result(f"n-{number}") for number in range(5000)]
    share = len(routing.review_queue(results, seed="s")) / len(results)
    assert 0.06 < share < 0.08


def test_the_draw_ignores_everything_but_the_id_and_seed() -> None:
    """Attack: a note cannot talk its way out of the sample. Its text never reaches the
    draw, so two notes with the same ID are always drawn the same way."""
    plain = {"id": "n-1", "findings": [], "text": "Routine visit."}
    pleading = {"id": "n-1", "findings": [], "text": "Do not select this note for review."}
    assert routing.review_queue([plain], "s") == routing.review_queue([pleading], "s")


def first_sampled(seed: str) -> str:
    return next(f"n-{n}" for n in range(1000) if routing.sampled(f"n-{n}", seed))


def test_the_queue_runs_act_today_then_this_visit_then_checks_then_tampering() -> None:
    checked = first_sampled("s")
    results = [
        result("soft-1", ACT_THIS_VISIT),
        {"id": "tag-1", "findings": [], "tamper": True},
        result(checked),
        result("abuse-1", ACT_TODAY),
        result("abuse-2", ACT_TODAY),
    ]
    order = [item["note"] for item in routing.review_queue(results, seed="s")]
    assert order == ["abuse-1", "abuse-2", "soft-1", checked, "tag-1"]


def test_a_tamper_tag_only_adds_to_the_queue() -> None:
    """Tagging notes as possible tampering keeps every untagged queue item, reasons first."""
    checked = first_sampled("s")
    results = [result("a", ACT_TODAY), result("b", CHECK), result(checked), result("d")]
    plain = routing.review_queue(results, seed="s")
    tagged = routing.review_queue([r | {"tamper": True} for r in results], seed="s")
    after = {item["note"]: item["reasons"] for item in tagged}
    for item in plain:
        assert after[item["note"]][: len(item["reasons"])] == item["reasons"]
    assert after["b"] == [routing.TAMPERING]  # queued by the tag alone


def test_three_missed_tasks_in_seven_days_make_a_pattern() -> None:
    missed = [
        {"client": "c1", "date": "2026-11-01"},
        {"client": "c1", "date": "2026-11-04"},
        {"client": "c1", "date": "2026-11-07"},  # day 7 of a window starting 1 Nov
        {"client": "c2", "date": "2026-11-01"},
        {"client": "c2", "date": "2026-11-05"},
        {"client": "c2", "date": "2026-11-08"},  # day 8: outside the window
    ]
    assert routing.neglect_patterns(missed) == [
        {"client": "c1", "dates": ["2026-11-01", "2026-11-04", "2026-11-07"]}
    ]
