"""The retention model's checks, against the simulator's true probabilities (rules fixed before
any result): discrimination, calibration, fairness by group, the label audit and two
positive controls. Plain data in, plain data out; the CLI does the I/O.
"""

import math
import random
from typing import Any

from homecare.retention import model, simulate

RESAMPLES = 2_000
SEED = 2026
FLAG_SHARE = 0.10  # the manager's screen highlights the top tenth
GROUPS = ("age", "ethnicity", "gender")


def annual(monthly_probability: float) -> float:
    return 1 - (1 - monthly_probability) ** 12


def true_voluntary(row: dict[str, Any]) -> float:
    """The simulator's true chance of a resignation this month (the reason is drawn after)."""
    return float(row["true_hazard"]) * simulate.REASONS[model.EVENT]


def auroc(labels: list[int], scores: list[float]) -> dict[str, float]:
    from sklearn.metrics import roc_auc_score  # laptop only

    rng = random.Random(SEED)
    estimates = []
    for _ in range(RESAMPLES):
        picks = [rng.randrange(len(labels)) for _ in labels]
        picked = [labels[i] for i in picks]
        if all(picked) or not any(picked):
            continue
        estimates.append(float(roc_auc_score(picked, [scores[i] for i in picks])))
    estimates.sort()
    return {
        "auroc": float(roc_auc_score(labels, scores)),
        "low": estimates[int(0.025 * len(estimates))],
        "high": estimates[int(0.975 * len(estimates)) - 1],
    }


def calibration(predicted: list[float], truth: list[float]) -> dict[str, float]:
    """Against the truth: the gap in annual probability (negative = under-prediction), and the
    slope of the truth's log-odds on the prediction's (1 is ideal)."""
    gap = annual(sum(predicted) / len(predicted)) - annual(sum(truth) / len(truth))
    x = [math.log(p / (1 - p)) for p in predicted]
    y = [math.log(t / (1 - t)) for t in truth]
    mean_x, mean_y = sum(x) / len(x), sum(y) / len(y)
    spread = sum((a - mean_x) ** 2 for a in x)
    slope = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y, strict=True)) / spread
    return {"annual_gap": gap, "slope": slope}


def brier(labels: list[int], predicted: list[float]) -> dict[str, float]:
    base = sum(labels) / len(labels)
    model_score = sum((p - y) ** 2 for p, y in zip(predicted, labels, strict=True)) / len(labels)
    base_score = sum((base - y) ** 2 for y in labels) / len(labels)
    return {"model": model_score, "base_rate": base_score}


def false_positive_rate(labels: list[int], predicted: list[float], threshold: float) -> float:
    negatives = [p for p, y in zip(predicted, labels, strict=True) if y == 0]
    return sum(1 for p in negatives if p >= threshold) / len(negatives) if negatives else 0.0


def by_group(
    rows: list[dict[str, Any]], predicted: list[float], threshold: float
) -> dict[str, Any]:
    """Calibration against the truth and the false positive rate at the flag, per group."""
    found: dict[str, Any] = {}
    for attribute in GROUPS:
        found[attribute] = {}
        for group in sorted({row["protected"][attribute] for row in rows}):
            picks = [i for i, row in enumerate(rows) if row["protected"][attribute] == group]
            group_rows = [rows[i] for i in picks]
            group_predicted = [predicted[i] for i in picks]
            truth = [true_voluntary(row) for row in group_rows]
            labels = [model.label(row) for row in group_rows]
            found[attribute][group] = {
                "person_months": len(picks),
                **calibration(group_predicted, truth),
                "false_positive_rate": false_positive_rate(labels, group_predicted, threshold),
            }
    return found


def label_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What each kind of event is, and how the voluntary label differs from 'any leaver'."""
    counts: dict[str, int] = {}
    for row in rows:
        if row["event"]:
            counts[row["event"]] = counts.get(row["event"], 0) + 1
    leavers = sum(n for reason, n in counts.items() if reason != "transferred")
    return {"events": counts, "voluntary": counts.get(model.EVENT, 0), "all_leavers": leavers}


def evaluate(rows: list[dict[str, Any]], fitted: dict[str, Any]) -> dict[str, Any]:
    test = [row for row in rows if row["month"] in model.TEST_MONTHS]
    labels = [model.label(row) for row in test]
    predicted = [model.score(fitted, row["known"]) for row in test]
    truth = [true_voluntary(row) for row in test]
    threshold = sorted(predicted)[int((1 - FLAG_SHARE) * len(predicted))]
    shuffled = list(labels)
    random.Random(SEED).shuffle(shuffled)
    return {
        "test_person_months": len(test),
        "test_resignations": sum(labels),
        "auroc": auroc(labels, predicted),
        "calibration": calibration(predicted, truth),
        "brier": brier(labels, predicted),
        "fairness": by_group(test, predicted, threshold),
        "label_audit": label_audit(rows),
        "controls": {
            "shuffled_labels_auroc": auroc(shuffled, predicted)["auroc"],
            "truth_as_score_auroc": auroc(labels, truth)["auroc"],
        },
    }
