"""The baseline rota a coordinator might make by hand: visits in time order, each given to the
client's usual carer if it fits, otherwise to the nearest skilled carer it fits, otherwise left
uncovered with a reason. The optimised rota is judged against it.

Only carers working that day are planned. `reason` explains any uncovered visit, for the
baseline and the solver alike, among those carers:
- "needs two carers (not planned yet)": pairing two carers on one visit is not built yet;
- "no skilled carer": no carer has every skill the visit needs;
- "no carer of the preferred gender": skilled carers exist, but none of the client's preferred
  gender for this intimate-care visit;
- "outside every shift": its window lies outside the working day;
- "too far": no skilled carer could reach it in its window even with an empty day;
- "clashes with other visits": some skilled carer could do it alone, but not with their day.
"""

from typing import Any

from homecare.matching.check import walk
from homecare.world.agency import can_do, on_duty


def day_visits(world: dict[str, Any], day: int) -> list[dict[str, Any]]:
    visits = [v for v in world["visits"] if v["day"] == day]
    return sorted(visits, key=lambda v: (v["open"], v["id"]))


def skilled(visit: dict[str, Any], world: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in world["carers"] if set(visit["skills"]) <= set(c["skills"])]


def able(visit: dict[str, Any], world: dict[str, Any]) -> list[dict[str, Any]]:
    """The carers who may do this visit alone: every skill and the preferred gender."""
    return [c for c in world["carers"] if can_do(c, visit)]


def reason(visit: dict[str, Any], world: dict[str, Any]) -> str:
    if visit.get("two_carers"):
        return "needs two carers (not planned yet)"
    if not skilled(visit, world):
        return "no skilled carer"
    allowed = able(visit, world)
    if not allowed:
        return "no carer of the preferred gender"
    shift_start, shift_end = world["shift"]
    if visit["open"] + visit["duration"] > shift_end or visit["close"] < shift_start:
        return "outside every shift"
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    if all(walk([visit], carer, nodes, world)[0] for carer in allowed):
        return "too far"
    return "clashes with other visits"


def last_place(route: list[dict[str, Any]], home: str, nodes: dict[str, str]) -> str:
    return nodes[route[-1]["client"]] if route else home


def plan_day(world: dict[str, Any], day: int) -> dict[str, Any]:
    # Only the carers working today; every reason below is about them.
    world = world | {"carers": [c for c in world["carers"] if on_duty(c, day)]}
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    by_id = {carer["id"]: carer for carer in world["carers"]}
    routes: dict[str, list[dict[str, Any]]] = {carer["id"]: [] for carer in world["carers"]}
    uncovered = []
    for visit in day_visits(world, day):
        here = nodes[visit["client"]]
        allowed = [c["id"] for c in able(visit, world)]
        usual = world["usual"][visit["client"]]
        others = [c for c in allowed if c != usual]
        others.sort(
            key=lambda c: (world["travel"][last_place(routes[c], by_id[c]["home"], nodes)][here], c)
        )
        order = [usual] + others if usual in allowed else others
        for carer_id in order:
            trial = routes[carer_id] + [visit]
            if not walk(trial, by_id[carer_id], nodes, world)[0]:
                routes[carer_id] = trial
                break
        else:
            uncovered.append({"visit": visit["id"], "reason": reason(visit, world)})
    return {
        "routes": {carer: [v["id"] for v in route] for carer, route in routes.items() if route},
        "uncovered": uncovered,
    }
