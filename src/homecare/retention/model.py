"""The retention model: logistic regression on person-months, saved as plain JSON.

The event is a resignation. Dismissal and a contract's end stop a person's record without
counting as the event, and a transfer within the employer is not a leaving (the label audit).
`fit` uses scikit-learn on the laptop; `score` and `reasons` are plain Python, so the app can
score without scikit-learn, and a test checks the two agree.
"""

import math
from typing import Any

from homecare.retention import features

EVENT = "resigned"
TRAIN_MONTHS = range(1, 19)
TEST_MONTHS = range(19, 25)


def label(row: dict[str, Any]) -> int:
    return int(row["event"] == EVENT)


def matrix(rows: list[dict[str, Any]]) -> tuple[list[str], list[list[float]]]:
    names = list(features.inputs(rows[0]["known"]))
    values = [[features.inputs(row["known"])[name] for name in names] for row in rows]
    return names, values


def fit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fit on the training months and return the model as plain data."""
    from sklearn.linear_model import LogisticRegression  # laptop only

    train = [row for row in rows if row["month"] in TRAIN_MONTHS]
    names, values = matrix(train)
    means = [sum(column) / len(column) for column in zip(*values, strict=True)]
    scales = []
    for index, column in enumerate(zip(*values, strict=True)):
        spread = math.sqrt(sum((v - means[index]) ** 2 for v in column) / len(column))
        scales.append(spread or 1.0)
    scaled = [[(v - m) / s for v, m, s in zip(row, means, scales, strict=True)] for row in values]
    fitted = LogisticRegression(C=1.0, max_iter=2000).fit(scaled, [label(row) for row in train])
    return {
        "event": EVENT,
        "train_months": [TRAIN_MONTHS.start, TRAIN_MONTHS.stop - 1],
        "inputs": names,
        "means": means,
        "scales": scales,
        "coefficients": [float(c) for c in fitted.coef_[0]],
        "intercept": float(fitted.intercept_[0]),
    }


def contributions(model: dict[str, Any], known: dict[str, Any]) -> dict[str, float]:
    """Each input's push on the log-odds, against the average person in training."""
    row = features.inputs(known)
    found = {}
    for name, mean, scale, coefficient in zip(
        model["inputs"], model["means"], model["scales"], model["coefficients"], strict=True
    ):
        found[name] = coefficient * (row[name] - mean) / scale
    return found


def score(model: dict[str, Any], known: dict[str, Any]) -> float:
    """The probability of resigning in the coming month."""
    z = model["intercept"] + sum(contributions(model, known).values())
    return 1 / (1 + math.exp(-z))


def reasons(model: dict[str, Any], known: dict[str, Any], top: int = 3) -> list[tuple[str, float]]:
    """The variables pushing this person's risk up most, largest first, each described from the
    person's own value. A band's indicators are added up first: one indicator alone ("not in
    band 3") means nothing to a reader."""
    by_variable: dict[str, float] = {}
    for name, value in contributions(model, known).items():
        group = features.variable(name)
        by_variable[group] = by_variable.get(group, 0.0) + value
    # A value not yet known describes missing records, not the person, so it is never a reason.
    if known["sick_days_year"] is None:
        by_variable.pop("sickness", None)
    pushes = [(features.describe(group, known), value) for group, value in by_variable.items()]
    return sorted([p for p in pushes if p[1] > 0], key=lambda pair: -pair[1])[:top]
