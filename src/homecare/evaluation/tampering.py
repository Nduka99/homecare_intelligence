"""The tamper-tag study, whose pass rule was fixed before any run.

Plain data and arithmetic: the CLI computes P(injection) with `homecare.tamper` and passes it in.
Every note in the study's sets is free of injections, so any tag on one is a false tag.
"""

from typing import Any

from homecare.evaluation.metrics import proportion
from homecare.tamper import tagged

MIN_GROUP = 20  # the harm-by-writer rule looks only at personas with at least this many notes
CONTROL = "control"


def share_tagged(ids: list[str], probabilities: dict[str, float]) -> dict[str, float | int]:
    return proportion(sum(1 for i in ids if tagged(probabilities[i])), len(ids))


def false_tags(notes: list[dict[str, Any]], probabilities: dict[str, float]) -> dict[str, Any]:
    """Clean notes tagged, overall and by writer persona."""
    by_persona: dict[str, list[str]] = {}
    for note in notes:
        by_persona.setdefault(note.get("persona", "unknown"), []).append(note["id"])
    return {
        "overall": share_tagged([note["id"] for note in notes], probabilities),
        "by_persona": {
            persona: share_tagged(ids, probabilities)
            for persona, ids in sorted(by_persona.items())
            if len(ids) >= MIN_GROUP
        },
    }


def detection(copies: list[dict[str, str]], probabilities: dict[str, float]) -> dict[str, Any]:
    """Attacked copies tagged: pooled (without the benign control), by source and by position.
    Each copy has an `id`, the attack's `source` and the insertion's `position`."""
    by_source: dict[str, list[str]] = {}
    by_position: dict[str, list[str]] = {}
    for copy in copies:
        by_source.setdefault(copy["source"], []).append(copy["id"])
        if copy["source"] != CONTROL:
            by_position.setdefault(copy["position"], []).append(copy["id"])
    attacks = [copy["id"] for copy in copies if copy["source"] != CONTROL]
    return {
        "pooled": share_tagged(attacks, probabilities),
        "by_source": {s: share_tagged(ids, probabilities) for s, ids in sorted(by_source.items())},
        "by_position": {
            p: share_tagged(ids, probabilities) for p, ids in sorted(by_position.items())
        },
    }
