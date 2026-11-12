"""What the retention model may know about the demo agency's carers before the next month.

The four rota measures come from each carer's actual routes in the planned demo week, as z-scores
within the agency, so "higher than average" on the screen means against these colleagues. The
world holds no pay, qualification, training or sickness records, so those are drawn from the
simulator's shares with their own seed: SYNTHETIC, like everything here.
"""

import random
import statistics
from typing import Any

from homecare.retention import features, simulate

SEED = 2027
CONTRACT_HOURS = {"zero-hours": "zero-hours", "part-time": "16-25", "full-time": "over 35"}


def week_measures(world: dict[str, Any], rotas: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per carer, over the planned week: hours short of contract, the spread of daily hours, the
    travel share of working time, and the share of visits to the carer's usual clients."""
    visits = {visit["id"]: visit for visit in world["visits"]}
    homes = {carer["id"]: carer["home"] for carer in world["carers"]}
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    travel = world["travel"]
    totals: dict[str, dict[str, Any]] = {}
    for rota in rotas:
        for carer, route in rota["routes"].items():
            stops = [homes[carer]] + [nodes[visits[v]["client"]] for v in route] + [homes[carer]]
            driving = sum(travel[a][b] for a, b in zip(stops, stops[1:], strict=False)) / 60
            care = sum(visits[v]["duration"] for v in route)
            usual = sum(1 for v in route if world["usual"].get(visits[v]["client"]) == carer)
            found = totals.setdefault(carer, {"days": [], "driving": 0.0, "care": 0, "visits": 0,
                                              "usual": 0})  # fmt: skip
            found["days"].append(care + driving)
            found["driving"] += driving
            found["care"] += care
            found["visits"] += len(route)
            found["usual"] += usual
    contracted = {carer["id"]: carer["weekly_hours"] * 60 for carer in world["carers"]}
    measures = {}
    for carer, found in totals.items():
        worked = found["care"] + found["driving"]
        measures[carer] = {
            "hours_shortfall": contracted[carer] - worked,
            # One working day cannot show variation; None counts as the agency average.
            "volatility": statistics.pstdev(found["days"]) if len(found["days"]) > 1 else None,
            "unpaid_travel": found["driving"] / worked,
            "worked_hours": worked / 60,
            "contracted_hours": contracted[carer] / 60,
            "continuity": found["usual"] / found["visits"],
        }
    return measures


def z_scores(measures: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Each measure as standard deviations from the agency's mean; a missing one is the mean."""
    scored: dict[str, dict[str, float]] = {carer: {} for carer in measures}
    for name in features.ROTA:
        values = [found[name] for found in measures.values() if found[name] is not None]
        mean, spread = statistics.fmean(values), statistics.pstdev(values) or 1.0
        for carer, found in measures.items():
            value = found[name]
            scored[carer][name] = 0.0 if value is None else (value - mean) / spread
    return scored


def months_between(start: str, day: str) -> int:
    first_year, first_month = int(start[:4]), int(start[5:7])
    year, month = int(day[:4]), int(day[5:7])
    return (year - first_year) * 12 + (month - first_month)


def known_now(world: dict[str, Any], rotas: list[dict[str, Any]], today: str) -> dict[str, Any]:
    """Each working carer's inputs, as the model would see them before next month."""
    rng = random.Random(SEED)
    week = week_measures(world, rotas)
    rota = z_scores(week)
    trained_chance = 1 - (1 - simulate.TRAINING_CHANCE) ** 6
    known = {}
    for carer in world["carers"]:
        pay = simulate.weighted(rng, {band: share for band, (share, _) in simulate.PAY.items()})
        qualified = rng.random() < simulate.QUALIFIED_SHARE
        trained = rng.random() < trained_chance
        sick_days = sum(1 for _ in range(48) if rng.random() < simulate.SICK_DAYS_A_MONTH / 4)
        if carer["id"] not in rota:
            continue  # no planned visits this week: nothing to score from
        tenure = months_between(carer["start"], today)
        known[carer["id"]] = {
            "tenure_months": tenure,
            "hours": CONTRACT_HOURS[carer["contract"]],
            "pay": pay,
            "qualified": qualified,
            "trained_recently": trained,
            "sick_days_year": float(sick_days) if tenure >= features.SICKNESS_MIN_MONTHS else None,
            **{name: round(value, 4) for name, value in rota[carer["id"]].items()},
            # The week's actual values, so the screen can say what the z-scores mean.
            "week": {k: v if v is None else round(v, 3) for k, v in week[carer["id"]].items()},
        }
    return known
