"""What is known about a person before a month: the retention model's inputs.

Each time-varying measure reads a stated window of earlier months, written as (first, last)
offsets back from the month being predicted. `leaks` checks that no window reaches the month it
predicts, and the same table drives the computation, so the check and the code cannot disagree.
Age, ethnicity and gender are never inputs; they are kept apart for the fairness check.
"""

from typing import Any

# Skills for Care 2026, chapter 9: annual care-worker turnover by band.
TENURE_BANDS: list[tuple[float | None, float]] = [  # months in role
    (12, 0.337), (36, 0.228), (60, 0.197), (84, 0.166), (120, 0.149), (240, 0.128), (None, 0.126),
]  # fmt: skip
SICKNESS_BANDS: list[tuple[float | None, float]] = [  # sick days a year; the first band is none
    (0.5, 0.198), (6, 0.201), (15, 0.223), (30, 0.233), (None, 0.284),
]  # fmt: skip
WINDOWS = {"trained_recently": (6, 1), "sick_days_year": (12, 1), "rota": (1, 1)}
# Under this many months of records, sickness is not yet known. It is never zero: a new starter
# has no history, and counting that as "no sick days" made newness look like good health.
SICKNESS_MIN_MONTHS = 6
ROTA = ("hours_shortfall", "volatility", "unpaid_travel", "continuity")
HOURS = ("1-15", "16-25", "26-35", "over 35")  # zero-hours is the reference
PAY = ("up to 5% below", "6-10% below", "over 10% below")  # "on or above" is the reference


def months_read(name: str, month: int) -> range:
    first, last = WINDOWS[name]
    return range(month - first, month - last + 1)


def leaks(windows: dict[str, tuple[int, int]]) -> list[str]:
    """Windows that read the month being predicted, or later."""
    return [name for name, (_, last) in windows.items() if last < 1]


def known_before(person: dict[str, Any], month: int) -> dict[str, Any]:
    """Everything the model may know about a person when predicting `month`."""
    history = person["months"]
    trained = any(
        history[m]["trained"] for m in months_read("trained_recently", month) if m in history
    )
    sick_months = [m for m in months_read("sick_days_year", month) if m in history]
    sick_days = sum(history[m]["sick_days"] for m in sick_months)
    latest_rota = history.get(max(months_read("rota", month)), {})
    return {
        "tenure_months": max(0, month - 1 - person["hired"]),
        "hours": person["hours"],
        "pay": person["pay"],
        "qualified": person["qualified"],
        "trained_recently": trained,
        "sick_days_year": (
            12 * sick_days / len(sick_months) if len(sick_months) >= SICKNESS_MIN_MONTHS else None
        ),
        **{name: latest_rota.get(name, 0.0) for name in ROTA},
    }


def band_index(value: float, bands: list[tuple[float | None, float]]) -> int:
    for index, (upper, _) in enumerate(bands):
        if upper is None or value < upper:
            return index
    raise ValueError(value)


def variable(input_name: str) -> str:
    """The person-level variable an input belongs to: a band's indicators belong together."""
    for prefix, name in (("hours ", "contract"), ("pay ", "pay"), ("tenure band", "time in role"),
                         ("sickness band", "sickness")):  # fmt: skip
        if input_name.startswith(prefix):
            return name
    if input_name == "sickness not yet known":
        return "sickness"
    return {"trained recently": "training", "qualified": "qualification"}.get(
        input_name, input_name
    )


def describe(name: str, known: dict[str, Any]) -> str:
    """A variable as a manager would say it, from this person's actual value."""
    if name == "contract":
        hours = known["hours"]
        return (
            "Zero-hours contract" if hours == "zero-hours" else f"Contract of {hours} hours a week"
        )
    if name == "pay":
        return f"Pay {known['pay']} the area median"
    if name == "time in role":
        months = known["tenure_months"]
        return f"In role {months} month{'' if months == 1 else 's'}"
    if name == "sickness":
        if known["sick_days_year"] is None:
            return "Sickness not yet known"
        return f"About {round(known['sick_days_year'])} sick days a year"
    if name == "training":
        return (
            "Trained in the last 6 months"
            if known["trained_recently"]
            else "No training in the last 6 months"
        )
    if name == "qualification":
        return (
            "Holds a social care qualification"
            if known["qualified"]
            else "No social care qualification"
        )
    week = known.get("week")
    if week is not None:
        return describe_week(name, week, known["hours"] == "zero-hours")
    level = "higher" if known[name] > 0 else "lower"
    labels = {
        "hours_shortfall": "Hours short of contract",
        "volatility": "Rota changes week to week",
        "unpaid_travel": "Unpaid travel between visits",
        "continuity": "Visits to usual clients",
    }
    return f"{labels[name]}: {level} than average"


def describe_week(name: str, week: dict[str, Any], zero_hours: bool) -> str:
    """A rota measure in the week's own numbers, when they are known (the demo agency)."""
    if name == "hours_shortfall":
        usual = "usual" if zero_hours else "contracted"  # a zero-hours contract guarantees none
        worked, contracted = week["worked_hours"], week["contracted_hours"]
        return f"Planned {worked:.1f} of {contracted:g} {usual} hours this week"
    if name == "volatility":
        if week["volatility"] is None:
            return "Worked one day this week"
        return f"Daily hours varied by about {week['volatility'] / 60:.1f} hours this week"
    if name == "unpaid_travel":
        return f"Travel took {week['unpaid_travel']:.0%} of working time this week"
    return f"{week['continuity']:.0%} of visits were to usual clients this week"


def inputs(known: dict[str, Any]) -> dict[str, float]:
    """The model's numeric inputs: one indicator per non-reference band, and the rota measures."""
    row: dict[str, float] = {}
    for band in HOURS:
        row[f"hours {band}"] = float(known["hours"] == band)
    for band in PAY:
        row[f"pay {band}"] = float(known["pay"] == band)
    row["qualified"] = float(known["qualified"])
    row["trained recently"] = float(known["trained_recently"])
    tenure = band_index(known["tenure_months"], TENURE_BANDS)
    for index in range(1, len(TENURE_BANDS)):
        row[f"tenure band {index}"] = float(tenure == index)
    days = known["sick_days_year"]
    sickness = None if days is None else band_index(days, SICKNESS_BANDS)
    for index in range(1, len(SICKNESS_BANDS)):
        row[f"sickness band {index}"] = float(sickness == index)
    row["sickness not yet known"] = float(sickness is None)
    for name in ROTA:
        row[name] = float(known[name])
    return row
