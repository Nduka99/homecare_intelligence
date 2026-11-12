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


def timeline(
    route: list[dict[str, Any]],
    carer: dict[str, Any],
    nodes: dict[str, str],
    world: dict[str, Any],
    hold: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """When the carer leaves home, the drive to, wait before and start of each visit, then the
    drive home and the time home. The carer leaves in time for the first window, never before
    the shift starts, and waits when early. A `hold` (position, minute) keeps the carer from
    setting off for the visit at that position before that minute, as after a re-plan."""
    travel = world["travel"]
    to_first = minutes(travel[carer["home"]][nodes[route[0]["client"]]])
    leaves = max(world["shift"][0], route[0]["open"] - to_first)
    if hold and hold[0] == 0:
        leaves = max(leaves, hold[1])
    clock = leaves
    here = carer["home"]
    stops = []
    for position, visit in enumerate(route):
        if hold and position == hold[0]:
            clock = max(clock, hold[1])
        there = nodes[visit["client"]]
        leg = minutes(travel[here][there])
        starts = max(clock + leg, visit["open"])
        stops.append({"visit": visit["id"], "drive": leg, "wait": starts - clock - leg,
                      "starts": starts})  # fmt: skip
        clock = starts + visit["duration"]
        here = there
    home_drive = minutes(travel[here][carer["home"]])
    return {"leaves": leaves, "stops": stops, "home_drive": home_drive, "home": clock + home_drive}


def walk(
    route: list[dict[str, Any]],
    carer: dict[str, Any],
    nodes: dict[str, str],
    world: dict[str, Any],
    hold: tuple[int, int] | None = None,
) -> tuple[list[str], int]:
    """The problems on one carer's route, and its driving minutes."""
    if not route:
        return [], 0
    plan = timeline(route, carer, nodes, world, hold)
    problems = []
    for visit, stop in zip(route, plan["stops"], strict=True):
        if stop["starts"] + visit["duration"] > visit["close"]:
            problems.append(f"{carer['id']} is late for {visit['id']}")
        if not set(visit["skills"]) <= set(carer["skills"]):
            problems.append(f"{carer['id']} lacks a skill for {visit['id']}")
        if visit.get("gender") and carer.get("gender") != visit["gender"]:
            problems.append(f"{carer['id']} is not the preferred gender for {visit['id']}")
        if visit.get("two_carers"):
            problems.append(f"{visit['id']} needs two carers but has one")
    if plan["home"] > world["shift"][1]:
        problems.append(f"{carer['id']} gets home after the shift ends")
    driving = sum(stop["drive"] for stop in plan["stops"]) + plan["home_drive"]
    worked = sum(visit["duration"] for visit in route) + driving
    if worked > day_limit(carer):
        problems.append(f"{carer['id']} works over the daily limit")
    longest_wait = max(stop["wait"] for stop in plan["stops"])
    if worked > REST_AFTER and longest_wait < REST_BREAK:
        problems.append(f"{carer['id']} has no 20-minute break in over 6 hours of work")
    return problems, driving


def check(
    rota: dict[str, Any],
    world: dict[str, Any],
    day: int,
    holds: dict[str, tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """Every broken rule in a day's rota, and its driving minutes, continuity breaks (all, and
    for high-need clients, tier above 1) and uncovered visits.

    A break is avoidable when the client's usual carer works that day; on the usual carer's day
    off no rota can avoid it. `holds` maps a carer to (position, minute): after a re-plan, they
    may not set off for the visit at that position before that minute."""
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
        hold = (holds or {}).get(carer_id)
        found, minutes_driving = walk(route, carers[carer_id], nodes, world, hold)
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


def unmoved(
    before: dict[str, Any],
    after: dict[str, Any],
    world: dict[str, Any],
    day: int,
    out: str,
    at: int,
) -> list[str]:
    """What a re-plan changed that it must not: a visit planned to start before `at` that is no
    longer with the same carer at the same place in the route, or any visit after `at` for the
    carer who dropped out."""
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    carers = {carer["id"]: carer for carer in world["carers"]}
    visits = {v["id"]: v for v in world["visits"] if v["day"] == day}
    problems = []
    for carer_id, visit_ids in before["routes"].items():
        plan = timeline([visits[v] for v in visit_ids], carers[carer_id], nodes, world)
        done = [stop["visit"] for stop in plan["stops"] if stop["starts"] < at]
        now = after["routes"].get(carer_id, [])
        if now[: len(done)] != done:
            problems.append(f"{carer_id}'s visits before the re-plan changed")
        if carer_id == out and len(now) > len(done):
            problems.append(f"{out} has visits after dropping out")
    return problems


def working(world: dict[str, Any], day: int, out: str | None = None) -> list[dict[str, Any]]:
    """The carers at work that day, without one who dropped out."""
    found = []
    for carer in world["carers"]:
        if day % 7 not in carer.get("days_off", []) and carer["id"] != out:
            found.append(carer)
    return found


def fits(
    visit: dict[str, Any],
    rota: dict[str, Any],
    world: dict[str, Any],
    day: int,
    holds: dict[str, tuple[int, int]],
    out: str | None = None,
) -> list[str]:
    """Every working carer and place in their route where `visit` would break no rule: each
    route tried with the visit at every position from the carer's hold on (all of it, unheld).
    A carer with no route is tried with the visit alone, under any hold given for them."""
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    visits = {v["id"]: v for v in world["visits"] if v["day"] == day}
    found = []
    for carer in working(world, day, out):
        route = [visits[v] for v in rota["routes"].get(carer["id"], [])]
        hold = holds.get(carer["id"])
        first = hold[0] if hold else 0
        for position in range(first, len(route) + 1):
            trial = route[:position] + [visit] + route[position:]
            if not walk(trial, carer, nodes, world, hold)[0]:
                found.append(f"{carer['id']} at position {position}")
    return found


def reason_problems(
    rota: dict[str, Any],
    world: dict[str, Any],
    day: int,
    holds: dict[str, tuple[int, int]] | None = None,
    out: str | None = None,
) -> list[str]:
    """Every uncovered visit whose stated reason is untrue, found by trying it in every working
    carer's route at every position (`fits`) and alone. The solver gives the reasons; this tests
    them another way."""
    visits = {v["id"]: v for v in world["visits"] if v["day"] == day}
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    holds = holds or {}
    problems = []
    for item in rota["uncovered"]:
        visit = visits[item["visit"]]
        said = item["reason"]
        staff = working(world, day, out)
        skilled = [c for c in staff if set(visit["skills"]) <= set(c["skills"])]
        able = [c for c in skilled if not visit.get("gender") or c.get("gender") == visit["gender"]]
        if said == "needs two carers (not planned yet)":
            untrue = not visit.get("two_carers")
        elif said == "no skilled carer":
            untrue = bool(skilled)
        elif said == "no carer of the preferred gender":
            untrue = bool(able) or not skilled
        else:
            untrue = bool(fits(visit, rota, world, day, holds, out))
            for carer in able:
                hold = holds.get(carer["id"])
                kept = rota["routes"].get(carer["id"], [])[: hold[0] if hold else 0]
                alone = [visits[v] for v in kept] + [visit]
                could = not walk(alone, carer, nodes, world, hold)[0]
                if said in ("too far", "no free carer can reach it in time") and could:
                    untrue = True
        if untrue:
            problems.append(f"{visit['id']}: '{said}' is untrue")
    return problems
