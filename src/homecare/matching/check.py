"""The independent checker for one day's rota. It shares no code with the OR-Tools solver, so it
can catch the solver's mistakes.

It walks each carer's route with plain arithmetic and reports every broken rule:
- a visit that starts before its window opens is waited for; one that cannot finish by the window's
  close is late;
- the carer needs every skill the visit needs, and the client's preferred gender where the visit has
  one;
- a visit needing two carers is never on one carer's route: pairing is a stated limit, so such
  visits are uncovered;
- a carer works only on their working days, leaves home no earlier than the shift starts and is
  home by the shift's end; split shifts are allowed;
- working time is care plus all driving, home journeys included; waiting between visits is not
  work. It is at most the daily limit (a fifth of weekly hours), and over 6 hours of it needs a
  wait of at least 20 minutes somewhere (Working Time Regulations 1998, reg 12);
- every visit of the day is either on exactly one route or listed as uncovered with a reason.

A rota is {"routes": {carer id: [visit id, ...]}, "uncovered": [{"visit": id, "reason": text}]}.
Times are minutes from midnight; driving times in the matrix are seconds, rounded up to minutes.
"""

import math
from typing import Any

REST_AFTER = 360  # minutes of work after which a break is due (WTR reg 12)
REST_BREAK = 20


def minutes(seconds: int) -> int:
    return math.ceil(seconds / 60)


def day_limit(carer: dict[str, Any]) -> int:
    return round(float(carer["weekly_hours"]) * 60 / 5)


def walk(
    route: list[dict[str, Any]],
    carer: dict[str, Any],
    nodes: dict[str, str],
    world: dict[str, Any],
) -> tuple[list[str], int]:
    """The problems on one carer's route, and its driving minutes."""
    if not route:
        return [], 0
    shift_start, shift_end = world["shift"]
    travel = world["travel"]
    problems = []
    first = route[0]
    to_first = minutes(travel[carer["home"]][nodes[first["client"]]])
    leaves = max(shift_start, first["open"] - to_first)
    clock = leaves
    here = carer["home"]
    driving = 0
    longest_wait = 0
    for visit in route:
        there = nodes[visit["client"]]
        leg = minutes(travel[here][there])
        driving += leg
        starts = max(clock + leg, visit["open"])
        longest_wait = max(longest_wait, starts - clock - leg)
        if starts + visit["duration"] > visit["close"]:
            problems.append(f"{carer['id']} is late for {visit['id']}")
        if not set(visit["skills"]) <= set(carer["skills"]):
            problems.append(f"{carer['id']} lacks a skill for {visit['id']}")
        if visit.get("gender") and carer.get("gender") != visit["gender"]:
            problems.append(f"{carer['id']} is not the preferred gender for {visit['id']}")
        if visit.get("two_carers"):
            problems.append(f"{visit['id']} needs two carers but has one")
        clock = starts + visit["duration"]
        here = there
    home_leg = minutes(travel[here][carer["home"]])
    driving += home_leg
    home = clock + home_leg
    if home > shift_end:
        problems.append(f"{carer['id']} gets home after the shift ends")
    worked = sum(visit["duration"] for visit in route) + driving
    if worked > day_limit(carer):
        problems.append(f"{carer['id']} works over the daily limit")
    if worked > REST_AFTER and longest_wait < REST_BREAK:
        problems.append(f"{carer['id']} has no 20-minute break in over 6 hours of work")
    return problems, driving


def check(rota: dict[str, Any], world: dict[str, Any], day: int) -> dict[str, Any]:
    """Every broken rule in a day's rota, and its driving minutes, continuity breaks (all, and
    for high-need clients, tier above 1) and uncovered visits.

    A break is avoidable when the client's usual carer works that day; on the usual carer's day
    off no rota can avoid it."""
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    carers = {carer["id"]: carer for carer in world["carers"]}
    visits = {v["id"]: v for v in world["visits"] if v["day"] == day}
    problems = []
    seen: list[str] = []
    driving = 0
    breaks = 0
    high_need_breaks = 0
    avoidable = 0
    avoidable_high_need = 0
    for carer_id, visit_ids in rota["routes"].items():
        if carer_id not in carers:
            problems.append(f"unknown carer {carer_id}")
            continue
        if day % 7 in carers[carer_id].get("days_off", []):
            problems.append(f"{carer_id} works on a day off")
        route = []
        for visit_id in visit_ids:
            if visit_id not in visits:
                problems.append(f"{visit_id} is not a visit of day {day}")
                continue
            route.append(visits[visit_id])
            seen.append(visit_id)
            usual = world["usual"][visits[visit_id]["client"]]
            if usual != carer_id:
                high_need = visits[visit_id].get("tier", 1) > 1
                working = usual in carers and day % 7 not in carers[usual].get("days_off", [])
                breaks += 1
                high_need_breaks += high_need
                avoidable += working
                avoidable_high_need += working and high_need
        found, minutes_driving = walk(route, carers[carer_id], nodes, world)
        problems += found
        driving += minutes_driving
    uncovered = [item["visit"] for item in rota["uncovered"]]
    for visit_id in sorted(set(seen + uncovered)):
        if seen.count(visit_id) + uncovered.count(visit_id) > 1:
            problems.append(f"{visit_id} is placed more than once")
    for visit_id in sorted(set(visits) - set(seen) - set(uncovered)):
        problems.append(f"{visit_id} is neither on a route nor uncovered")
    return {
        "problems": problems,
        "driving_minutes": driving,
        "continuity_breaks": breaks,
        "high_need_breaks": high_need_breaks,
        "avoidable_breaks": avoidable,
        "avoidable_high_need_breaks": avoidable_high_need,
        "covered": len(set(seen)),
        "uncovered": len(set(uncovered)),
    }
