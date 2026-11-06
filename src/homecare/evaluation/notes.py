"""Scores for one extraction run on one set of labelled notes.

A note's labels are the categories of the templates it was written from. Each measure is per
note, with a Wilson 95% interval:
- recall by band: of the notes with a label in the band, the share where the run found a
  verified finding in that band (safeguarding, "act today", comes first);
- precision by band: of the notes the run put in a band, the share with a label in it;
- type agreement: of the safeguarding notes caught, the share whose exact type was found;
- false alarms: routine notes given an "act today" or "act this visit" finding;
- quote drops: findings dropped because their quote is not in the note;
- urgent bands needing the whole note: of the urgent bands caught, the share no sentence read
  found, which an injection anywhere in the note could still hide;
- alert burden, the reviewer's load (alert fatigue: clinicians override 49–96% of drug-safety
  alerts, van der Sijs et al., JAMIA 2006): urgent flags per 100 notes, false urgent flags per
  100 routine notes, and precision at the top of today's queue order.
Model errors count as "no findings", which can only lower recall.
"""

from typing import Any

from homecare.evaluation.metrics import proportion
from homecare.notes.categories import ACT_THIS_VISIT, ACT_TODAY, BANDS, SAFEGUARDING, band


def found(run: dict[str, Any]) -> list[str]:
    """The categories of a note's verified findings."""
    return [finding["category"] for finding in run.get("findings", [])]


def bands_of(categories: list[str]) -> set[str]:
    return {band(category) for category in categories}


def in_band(name: str, categories: list[str]) -> bool:
    return name in bands_of(categories)


def sentence_bands(run: dict[str, Any]) -> set[str]:
    """Bands a sentence read found (v2 only; v1 runs read only the whole note)."""
    return {f["band"] for f in run.get("findings", []) if f.get("read") in ("sentence", "both")}


QUEUE_TOP = 20
URGENT_ORDER = (ACT_TODAY, ACT_THIS_VISIT)


def queue_position(run: dict[str, Any]) -> int:
    """The note's most urgent band, as its place in URGENT_ORDER; len(URGENT_ORDER) if none."""
    flagged = bands_of(found(run))
    for position, name in enumerate(URGENT_ORDER):
        if name in flagged:
            return position
    return len(URGENT_ORDER)


def queue(notes: list[dict[str, Any]], runs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Today's review queue: notes with an urgent finding, most urgent band first, then in
    arrival order (a note's id stands for when it arrived)."""
    items = []
    for note in notes:
        position = queue_position(runs[note["id"]])
        if position < len(URGENT_ORDER):
            items.append((position, note["id"], note))
    items.sort(key=lambda item: (item[0], item[1]))
    return [note for _, _, note in items]


def is_urgent(note: dict[str, Any]) -> bool:
    return bool(bands_of(note["labels"]) & set(URGENT_ORDER))


def burden(
    notes: list[dict[str, Any]], runs: dict[str, dict[str, Any]], top: int = QUEUE_TOP
) -> dict[str, Any]:
    """How much the review queue asks of one reviewer, and how much of it is real."""
    items = queue(notes, runs)
    routine = [n for n in notes if not n["labels"]]
    false_routine = [n for n in items if not n["labels"]]
    head = items[:top]
    return {
        "urgent_flags_per_100_notes": round(100 * len(items) / len(notes), 1) if notes else 0.0,
        "false_urgent_per_100_routine": (
            round(100 * len(false_routine) / len(routine), 1) if routine else 0.0
        ),
        "queue_precision": proportion(sum(1 for n in items if is_urgent(n)), len(items)),
        "precision_at_top": proportion(sum(1 for n in head if is_urgent(n)), len(head)),
        "top": top,
    }


def score(notes: list[dict[str, Any]], runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    recall = {}
    precision = {}
    for name in BANDS:
        positives = [n for n in notes if in_band(name, n["labels"])]
        caught = [n for n in positives if in_band(name, found(runs[n["id"]]))]
        recall[name] = proportion(len(caught), len(positives))
        flagged = [n for n in notes if in_band(name, found(runs[n["id"]]))]
        right = [n for n in flagged if in_band(name, n["labels"])]
        precision[name] = proportion(len(right), len(flagged))

    safeguarding = [n for n in notes if in_band(ACT_TODAY, n["labels"])]
    caught = [n for n in safeguarding if in_band(ACT_TODAY, found(runs[n["id"]]))]
    exact = [n for n in caught if set(n["labels"]) & set(found(runs[n["id"]])) & set(SAFEGUARDING)]
    missed = []
    for note in safeguarding:
        if note not in caught:
            missed.append(
                {"id": note["id"], "labels": note["labels"], "found": found(runs[note["id"]])}
            )

    routine = [n for n in notes if not n["labels"]]
    urgent = {ACT_TODAY, ACT_THIS_VISIT}
    alarmed = [n for n in routine if bands_of(found(runs[n["id"]])) & urgent]
    caught_urgent = 0
    whole_note_only = 0
    for note in notes:
        run = runs[note["id"]]
        for name in bands_of(note["labels"]) & bands_of(found(run)) & urgent:
            caught_urgent += 1
            if name not in sentence_bands(run):
                whole_note_only += 1
    kept = sum(len(runs[n["id"]].get("findings", [])) for n in notes)
    dropped = sum(runs[n["id"]].get("dropped", 0) for n in notes)
    return {
        "notes": len(notes),
        "recall": recall,
        "precision": precision,
        "safeguarding_type_agreement": proportion(len(exact), len(caught)),
        "routine_false_alarms": proportion(len(alarmed), len(routine)),
        "urgent_needing_whole_note": proportion(whole_note_only, caught_urgent),
        "quote_drops": proportion(dropped, kept + dropped),
        "errors": [n["id"] for n in notes if "error" in runs[n["id"]]],
        "missed_safeguarding": missed,
        "alert_burden": burden(notes, runs),
    }
