"""The retention model's guards: no feature reads the month it predicts, only resignations are
the event, the plain-Python scorer equals scikit-learn's, and reasons read as a manager would."""

import copy
import math
import random
from typing import Any

from sklearn.linear_model import LogisticRegression

from homecare.retention import evaluate, features, model, simulate


def person_with_history() -> dict[str, Any]:
    person = simulate.hire(random.Random(1), 1, month=0, tenure=20)
    rng = random.Random(2)
    for month in range(-12, 6):
        simulate.live_month(rng, person, month)
    return person


def test_no_feature_window_reads_the_month_it_predicts() -> None:
    assert features.leaks(features.WINDOWS) == []
    assert features.leaks({"rota": (0, 0)}) == ["rota"]


def test_changing_the_predicted_month_changes_nothing_known_before_it() -> None:
    """The leak guard by behaviour: rewrite month 5 completely; what is known before month 5 must
    not move."""
    person = person_with_history()
    before = features.known_before(person, 5)
    changed = copy.deepcopy(person)
    rota = {"hours_shortfall": 9.0, "volatility": 9.0, "unpaid_travel": 9.0, "continuity": -9.0}
    changed["months"][5] = {"trained": True, "sick_days": 30, **rota}
    assert features.known_before(changed, 5) == before


def test_a_new_starters_sickness_is_unknown_not_zero() -> None:
    person = simulate.hire(random.Random(3), 2, month=10, tenure=0)
    simulate.live_month(random.Random(4), person, 10)
    assert features.known_before(person, 11)["sick_days_year"] is None


def test_only_a_resignation_is_the_event() -> None:
    labels = {reason: model.label({"event": reason}) for reason in simulate.REASONS}
    assert labels == {"resigned": 1, "dismissed": 0, "contract ended": 0, "transferred": 0}
    assert model.label({"event": None}) == 0


def test_the_plain_python_score_equals_scikit_learn() -> None:
    """An independent check: scikit-learn fitted on the same scaled inputs gives the same
    probabilities as the JSON model scored in plain Python."""
    rows = simulate.simulate(0.49, seed=7)[:6000]
    fitted = model.fit(rows)
    train = [row for row in rows if row["month"] in model.TRAIN_MONTHS]
    names, values = model.matrix(train)
    scaled = [
        [(v - m) / s for v, m, s in zip(row, fitted["means"], fitted["scales"], strict=True)]
        for row in values
    ]
    reference = LogisticRegression(C=1.0, max_iter=2000).fit(
        scaled, [model.label(r) for r in train]
    )
    for row, x in list(zip(train, scaled, strict=True))[:200]:
        expected = reference.predict_proba([x])[0][1]
        assert math.isclose(model.score(fitted, row["known"]), expected, rel_tol=1e-6)


def test_reasons_are_whole_variables_in_plain_words() -> None:
    rows = simulate.simulate(0.49, seed=7)[:6000]
    fitted = model.fit(rows)
    for row in rows[-500:]:
        for text, push in model.reasons(fitted, row["known"]):
            assert push > 0
            assert "band" not in text and "not yet known" not in text


def test_calibration_of_a_perfect_prediction() -> None:
    truth = [0.01, 0.02, 0.05, 0.10]
    found = evaluate.calibration(truth, truth)
    assert abs(found["annual_gap"]) < 1e-12 and math.isclose(found["slope"], 1.0)
