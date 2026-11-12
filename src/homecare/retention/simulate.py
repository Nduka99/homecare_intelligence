"""The SYNTHETIC workforce the retention model learns from: about 1,500 care workers over 24 months.

Every leaving rate is Skills for Care's for care workers in 2025/26 ("The state of the adult social
care sector and workforce in England 2026", chapter 9). Where a share or effect is not published,
it is marked as an assumption. A person's true leaving probability for month m is a logistic
function of what is known up to the end of month m - 1 (see `features`), so a model given the same
information can be checked against the truth.

Plain data and the standard library only; one seed fixes everything.
"""

import math
import random
from typing import Any

from homecare.retention import features

SEED = 2026
MONTHS = 24
HEADCOUNT = 1500
# Domiciliary care workers: 44% on zero-hours leave at 29.5% a year, the rest at 22.1%.
TARGET = 0.44 * 0.295 + 0.56 * 0.221

# (share of the workforce, annual turnover). The first entry of each is the reference group.
AGE = {
    "under 25": (0.07, 0.355),
    "25-39": (0.34, 0.255),
    "40-54": (0.32, 0.200),
    "55+": (0.27, 0.198),
}
ETHNICITY = {
    "White": (0.61, 0.245),
    "Black": (0.23, 0.233),
    "Asian": (0.13, 0.187),
    "Mixed or other": (0.03, 0.236),
}
GENDER = {"female": 0.77, "male": 0.23}  # "no notable effect" on turnover
HOURS_RATE = {"zero-hours": 0.298, "1-15": 0.297, "16-25": 0.253, "26-35": 0.215, "over 35": 0.191}
HOURS_SHARE = {"1-15": 0.15, "16-25": 0.30, "26-35": 0.30, "over 35": 0.25}  # assumed
# The planted proxy: zero-hours is more common among under-25s; about 44% overall.
ZERO_HOURS = {"under 25": 0.70, "other": 0.42}
PAY = {  # pay against the area median; shares assumed
    "on or above": (0.45, 0.229),
    "up to 5% below": (0.25, 0.235),
    "6-10% below": (0.15, 0.241),
    "over 10% below": (0.15, 0.274),
}
QUALIFIED_SHARE = 0.45  # assumed
QUALIFIED_RATE = {True: 0.194, False: 0.244}
TRAINED_RATE = {True: 0.230, False: 0.315}  # any training in the last six months
TRAINING_CHANCE = 0.35  # chance of training in a month (assumed)
SICK_DAYS_A_MONTH = 0.8  # mean (assumed), so a typical year has about 10
# Rota measures: odds ratios per standard deviation. Not published (Skills for Care could not
# measure travel between clients in home care), so these are assumptions, kept small.
ROTA_ODDS = {"hours_shortfall": 1.20, "volatility": 1.20, "unpaid_travel": 1.30, "continuity": 0.85}
REASONS = {"resigned": 0.75, "dismissed": 0.10, "contract ended": 0.10, "transferred": 0.05}


def logit(p: float) -> float:
    return math.log(p / (1 - p))


def weighted(rng: random.Random, weights: dict[Any, float]) -> Any:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def band_rate(value: float, bands: list[tuple[float | None, float]]) -> float:
    """The rate of the first band whose upper bound is above the value (None is unbounded)."""
    for upper, rate in bands:
        if upper is None or value < upper:
            return rate
    raise ValueError(value)


def effects(person: dict[str, Any], known: dict[str, Any]) -> float:
    """The log-odds of leaving within a year, before the intercept: each variable's published
    difference from its reference group, plus the assumed rota effects."""
    total = logit(AGE[person["age"]][1]) - logit(AGE["under 25"][1])
    total += logit(ETHNICITY[person["ethnicity"]][1]) - logit(ETHNICITY["White"][1])
    total += logit(HOURS_RATE[person["hours"]]) - logit(HOURS_RATE["zero-hours"])
    total += logit(PAY[person["pay"]][1]) - logit(PAY["on or above"][1])
    total += logit(QUALIFIED_RATE[person["qualified"]]) - logit(QUALIFIED_RATE[False])
    total += logit(TRAINED_RATE[known["trained_recently"]]) - logit(TRAINED_RATE[False])
    tenure = band_rate(known["tenure_months"], features.TENURE_BANDS)
    total += logit(tenure) - logit(features.TENURE_BANDS[0][1])
    if known["sick_days_year"] is not None:  # not yet known: no sickness effect
        sickness = band_rate(known["sick_days_year"], features.SICKNESS_BANDS)
        total += logit(sickness) - logit(features.SICKNESS_BANDS[0][1])
    for name, odds in ROTA_ODDS.items():
        total += math.log(odds) * known[name]
    return total


def monthly(annual: float) -> float:
    return float(1 - (1 - annual) ** (1 / 12))


def hire(rng: random.Random, number: int, month: int, tenure: int) -> dict[str, Any]:
    age = weighted(rng, {band: share for band, (share, _) in AGE.items()})
    zero_share = ZERO_HOURS["under 25" if age == "under 25" else "other"]
    hours = "zero-hours" if rng.random() < zero_share else weighted(rng, HOURS_SHARE)
    return {
        "id": f"W{number:05d}",
        "hired": month - tenure,
        "age": age,
        "ethnicity": weighted(rng, {group: share for group, (share, _) in ETHNICITY.items()}),
        "gender": weighted(rng, GENDER),
        "hours": hours,
        "pay": weighted(rng, {band: share for band, (share, _) in PAY.items()}),
        "qualified": rng.random() < QUALIFIED_SHARE,
        # Each person's usual rota experience; their months vary around it.
        "rota_traits": {name: rng.gauss(0, 0.8) for name in ROTA_ODDS},
        "months": {},
    }


def live_month(rng: random.Random, person: dict[str, Any], month: int) -> None:
    """What happened to the person in a month: training, sickness and four rota measures."""
    sick = sum(1 for _ in range(4) if rng.random() < SICK_DAYS_A_MONTH / 4)
    rota = {name: trait + rng.gauss(0, 0.6) for name, trait in person["rota_traits"].items()}
    person["months"][month] = {"trained": rng.random() < TRAINING_CHANCE, "sick_days": sick, **rota}


def simulate(intercept: float, seed: int = SEED) -> list[dict[str, Any]]:
    """Person-months for months 1..MONTHS: what was known before each month, the true hazard, and
    what happened (None, or the reason for an event)."""
    rng = random.Random(seed)
    staff = []
    for number in range(HEADCOUNT):
        person = hire(rng, number, 0, tenure=int(rng.expovariate(1 / 48)))
        for month in range(-12, 1):  # a year of history before the first predicted month
            live_month(rng, person, month)
        staff.append(person)
    next_number = HEADCOUNT
    rows = []
    for month in range(1, MONTHS + 1):
        stayed = []
        for person in staff:
            known = features.known_before(person, month)
            hazard = monthly(1 / (1 + math.exp(-(intercept + effects(person, known)))))
            event = weighted(rng, REASONS) if rng.random() < hazard else None
            rows.append({"person": person["id"], "month": month, "known": known,
                         "protected": {k: person[k] for k in ("age", "ethnicity", "gender")},
                         "true_hazard": hazard, "event": event})  # fmt: skip
            live_month(rng, person, month)
            if event in (None, "transferred"):
                stayed.append(person)
            else:
                replacement = hire(rng, next_number, month, tenure=0)
                live_month(rng, replacement, month)
                stayed.append(replacement)
                next_number += 1
        staff = stayed
    return rows


def annual_rate(rows: list[dict[str, Any]]) -> float:
    """Events (all reasons) per person-year in the simulated rows."""
    events = sum(1 for row in rows if row["event"] is not None)
    return 12 * events / len(rows)


def tune_intercept(seed: int = SEED) -> float:
    """The intercept that makes the simulated annual rate meet TARGET, by bisection on the mean
    true hazard (not on noisy event counts, so it is stable)."""
    low, high = -6.0, 4.0
    for _ in range(25):
        middle = (low + high) / 2
        rows = simulate(middle, seed)
        rate = 12 * sum(row["true_hazard"] for row in rows) / len(rows)
        if rate < TARGET:
            low = middle
        else:
            high = middle
    return (low + high) / 2
