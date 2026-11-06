"""The queue-ranking study, whose pass rule was fixed before any run. It was not adopted: the score
ranked the type of concern, not whether a concern was real.

An NLI model scores each urgent finding against its whole note, and the score reorders the review
queue inside each band: bands first, then the score (highest first), then arrival. Add-only: the
reordered queue holds exactly today's notes, so a low score can never hide one.

Plain data and arithmetic here; the NLI model runs in `homecare.nli`.
"""

import random
from typing import Any

from homecare.evaluation.notes import URGENT_ORDER, is_urgent, queue, queue_position
from homecare.notes.categories import MEANINGS, band

ARMS = ("claim", "category")
TOP = 20
RESAMPLES = 2_000
SEED = 2026


def hypothesis(finding: dict[str, Any], arm: str) -> str:
    """Arm "claim" checks the finding's own reason; arm "category" checks its category's meaning."""
    if arm == "claim":
        return str(finding["reason"])
    return f"The note describes {MEANINGS[finding['category']]}."


def jobs(
    notes: list[dict[str, Any]], runs: dict[str, dict[str, Any]], arm: str
) -> list[dict[str, str]]:
    """One NLI pair per urgent finding of each queued note; the whole note is the premise."""
    found = []
    for note in queue(notes, runs):
        for finding in runs[note["id"]]["findings"]:
            if band(finding["category"]) in URGENT_ORDER:
                found.append(
                    {
                        "note": note["id"],
                        "premise": note["text"],
                        "hypothesis": hypothesis(finding, arm),
                    }
                )
    return found


def note_scores(pairs: list[dict[str, str]], entailment: list[float]) -> dict[str, float]:
    """A note's score is its best-supported urgent finding."""
    scores: dict[str, float] = {}
    for pair, value in zip(pairs, entailment, strict=True):
        scores[pair["note"]] = max(value, scores.get(pair["note"], 0.0))
    return scores


def reorder(
    notes: list[dict[str, Any]], runs: dict[str, dict[str, Any]], scores: dict[str, float]
) -> list[dict[str, Any]]:
    """Today's queue, ordered by score inside each band."""

    def key(note: dict[str, Any]) -> tuple[int, float, str]:
        return (queue_position(runs[note["id"]]), -scores[note["id"]], note["id"])

    return sorted(queue(notes, runs), key=key)


def real_in_top(items: list[dict[str, Any]]) -> int:
    return sum(1 for note in items[:TOP] if is_urgent(note))


def auroc(items: list[dict[str, Any]], scores: dict[str, float]) -> dict[str, float]:
    """Real against false queue items, with a bootstrap 95% interval over notes."""
    # Imported here: cli.py imports this module, and the serving image has no scikit-learn.
    from sklearn.metrics import roc_auc_score

    labels = [is_urgent(note) for note in items]
    values = [scores[note["id"]] for note in items]
    rng = random.Random(SEED)
    estimates = []
    for _ in range(RESAMPLES):
        picks = [rng.randrange(len(items)) for _ in items]
        picked = [labels[i] for i in picks]
        if all(picked) or not any(picked):
            continue  # AUROC needs both kinds in the resample
        estimates.append(float(roc_auc_score(picked, [values[i] for i in picks])))
    estimates.sort()
    return {
        "auroc": float(roc_auc_score(labels, values)),
        "low": estimates[int(0.025 * len(estimates))],
        "high": estimates[int(0.975 * len(estimates)) - 1],
    }


def study(
    notes: list[dict[str, Any]], runs: dict[str, dict[str, Any]], scores: dict[str, float]
) -> dict[str, Any]:
    """The study's measures and controls for one set."""
    today = queue(notes, runs)
    ranked = reorder(notes, runs, scores)
    rng = random.Random(SEED)
    shuffled = {note["id"]: rng.random() for note in today}
    oracle = {note["id"]: float(is_urgent(note)) for note in today}
    return {
        "queue": len(today),
        "false_items": sum(1 for note in today if not is_urgent(note)),
        "same_items": sorted(n["id"] for n in today) == sorted(n["id"] for n in ranked),
        "auroc": auroc(today, scores),
        "top": TOP,
        "real_in_top_today": real_in_top(today),
        "real_in_top_ranked": real_in_top(ranked),
        "shuffled_auroc": auroc(today, shuffled),
        "oracle_real_in_top": real_in_top(reorder(notes, runs, oracle)),
    }
