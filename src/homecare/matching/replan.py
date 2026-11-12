"""Re-plan the rest of a day when a carer drops out. Laptop only: it needs OR-Tools.

A visit whose planned start is before the drop-out time stays where it is, for every carer: it is
done or under way. Everyone else working that day starts from the place of their last visit that
stays (or from home), free from the later of the drop-out time and that visit's end, with the
daily limit minus the work already done. The remaining visits, the dropped carer's included, go
through the day's routing model (`solve.route`). A visit that changes carer from the plan costs
continuity, so the re-plan changes as little as it can; a dropped carer's visit prefers the
client's usual carer. `check.check` with the returned `holds` is the oracle.
"""

from typing import Any

from homecare.matching import check, solve
from homecare.matching.baseline import able, reason
from homecare.matching.check import day_limit, timeline, walk
from homecare.world.agency import on_duty

# Tenths of a driving minute that one minute of moving a visit from its planned time costs
# (measured on six fresh drop-outs by `homecare rota replan-sweep`, chosen by its knee rule).
MOVED_TENTHS = 1


def split_at(
    rota: dict[str, Any], world: dict[str, Any], day: int, at: int
) -> tuple[dict[str, list[str]], list[str]]:
    """Each carer's visits that stay (planned to start before `at`), and the visits to re-plan.
    Starts rise along a route, so what stays is always the front of it."""
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    carers = {carer["id"]: carer for carer in world["carers"]}
    by_id = {visit["id"]: visit for visit in world["visits"] if visit["day"] == day}
    stay = {}
    later = []
    for carer_id, route in rota["routes"].items():
        plan = timeline([by_id[v] for v in route], carers[carer_id], nodes, world)
        stay[carer_id] = [stop["visit"] for stop in plan["stops"] if stop["starts"] < at]
        later += route[len(stay[carer_id]) :]
    return stay, later


def why(
    visit: dict[str, Any],
    today: dict[str, Any],
    stay: dict[str, list[str]],
    at: int,
    by_id: dict[str, dict[str, Any]],
) -> str:
    """Why a re-planned visit is left uncovered, among the carers still working: the day's
    reasons, except that a visit no free carer could reach in time even with nothing else to do
    is said so, rather than "too far" or "clashes"."""
    found = reason(visit, today)
    if found not in ("too far", "clashes with other visits"):
        return found
    nodes = {client["id"]: client["node"] for client in today["clients"]}
    for carer in able(visit, today):
        kept = [by_id[v] for v in stay.get(carer["id"], [])]
        if not walk([*kept, visit], carer, nodes, today, (len(kept), at))[0]:
            return "clashes with other visits"
    return "no free carer can reach it in time"


def replan(
    world: dict[str, Any],
    rota: dict[str, Any],
    day: int,
    out: str,
    at: int,
    continuity: int,
    high_need: int,
    solutions: int = solve.SOLUTIONS,
    seconds: int = solve.SECONDS,
    moved_tenths: int = MOVED_TENTHS,
) -> dict[str, Any]:
    """The day's rota after carer `out` stops at minute `at`, with the holds the checker needs.
    Moving a visit from its planned time costs `moved_tenths` tenths of a driving minute per
    minute."""
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    by_id = {visit["id"]: visit for visit in world["visits"] if visit["day"] == day}
    planned_by = {v: carer for carer, route in rota["routes"].items() for v in route}
    stay, later = split_at(rota, world, day, at)
    working = [c for c in world["carers"] if on_duty(c, day) and c["id"] != out]
    today = world | {"carers": working}

    vehicles = []
    for carer in working:
        kept = [by_id[v] for v in stay.get(carer["id"], [])]
        start, free, done = carer["home"], world["shift"][0], 0
        if kept:
            plan = timeline(kept, carer, nodes, world)
            start = nodes[kept[-1]["client"]]
            free = plan["stops"][-1]["starts"] + kept[-1]["duration"]
            done = sum(v["duration"] for v in kept) + sum(s["drive"] for s in plan["stops"])
        vehicles.append({"start": start, "home": carer["home"], "ready": max(at, free),
                         "work": day_limit(carer) - done})  # fmt: skip

    planned = []
    allowed = []
    uncovered = list(rota["uncovered"])
    for visit in sorted((by_id[v] for v in later), key=lambda v: (v["open"], v["id"])):
        able_ids = {carer["id"] for carer in able(visit, today)}
        vehicles_able = [k for k, carer in enumerate(working) if carer["id"] in able_ids]
        if vehicles_able:
            planned.append(visit)
            allowed.append(vehicles_able)
        else:
            uncovered.append({"visit": visit["id"], "reason": why(visit, today, stay, at, by_id)})
    preferred = []
    for visit in planned:
        carer = planned_by[visit["id"]]
        preferred.append(world["usual"][visit["client"]] if carer == out else carer)

    # The plan as it stands: each carer's visits still to come, the dropped carer's left out.
    current = []
    for carer in working:
        route = rota["routes"].get(carer["id"], [])
        current.append(route[len(stay.get(carer["id"], [])) :])
    # Each visit's planned start, so the re-plan keeps times where moving them gains nothing.
    was = where(rota, world, day, {})
    planned_starts = [was[visit["id"]][1] for visit in planned]
    found = solve.route(today, vehicles, working, planned, allowed, preferred,
                        continuity, high_need, solutions, seconds, current,
                        planned_starts, moved_tenths)  # fmt: skip
    if found is None:
        raise RuntimeError(f"no re-plan found for day {day}")
    new_routes, search = found
    routes = {}
    for carer_id in sorted(set(stay) | set(new_routes)):
        route = stay.get(carer_id, []) + new_routes.get(carer_id, [])
        if route:
            routes[carer_id] = route
    # Every carer still working is held at the drop-out time before their first re-planned visit.
    holds = {carer["id"]: (len(stay.get(carer["id"], [])), at) for carer in working}
    on_route = {v for route in routes.values() for v in route}
    for visit in planned:
        if visit["id"] not in on_route:
            uncovered.append({"visit": visit["id"], "reason": why(visit, today, stay, at, by_id)})
    return {"routes": routes, "uncovered": uncovered, "holds": holds, "search": search}


def where(
    rota: dict[str, Any], world: dict[str, Any], day: int, holds: dict[str, tuple[int, int]]
) -> dict[str, tuple[str, int]]:
    """Each visit on a route, with its carer and start (minutes), as the checker times them."""
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    carers = {carer["id"]: carer for carer in world["carers"]}
    by_id = {visit["id"]: visit for visit in world["visits"] if visit["day"] == day}
    found = {}
    for carer_id, route in rota["routes"].items():
        hold = holds.get(carer_id)
        plan = timeline([by_id[v] for v in route], carers[carer_id], nodes, world, hold)
        for stop in plan["stops"]:
            found[stop["visit"]] = (carer_id, stop["starts"])
    return found


def work(
    route: list[str],
    carer: dict[str, Any],
    world: dict[str, Any],
    day: int,
    hold: tuple[int, int] | None = None,
) -> int:
    """A carer's working minutes on a route: care plus every drive, the journey home included."""
    if not route:
        return 0
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    by_id = {visit["id"]: visit for visit in world["visits"] if visit["day"] == day}
    plan = timeline([by_id[v] for v in route], carer, nodes, world, hold)
    care: int = sum(by_id[v]["duration"] for v in route)
    driving: int = sum(stop["drive"] for stop in plan["stops"]) + plan["home_drive"]
    return care + driving


def summarise(
    world: dict[str, Any],
    day: int,
    before: dict[str, Any],
    after: dict[str, Any],
    out: str,
    at: int,
) -> dict[str, Any]:
    """The re-plan as a coordinator reads it: every visit that changed carer or became uncovered
    (with its new start, or why), each carer whose working time changed, and the day's driving,
    continuity breaks and uncovered visits before and after."""
    holds = after["holds"]
    was = where(before, world, day, {})
    now = where(after, world, day, holds)
    reasons = {item["visit"]: item["reason"] for item in after["uncovered"]}
    changes = []
    retimed = []  # visits that keep their carer at a new time: their clients need telling too
    for visit_id, (carer, start) in sorted(was.items(), key=lambda item: (item[1][1], item[0])):
        moved = now.get(visit_id)
        if moved and moved[0] == carer:
            if moved[1] != start:
                retimed.append({"visit": visit_id, "carer": carer, "was": start,
                                "starts": moved[1]})  # fmt: skip
            continue
        changes.append({
            "visit": visit_id,
            "before": carer,
            "was": start,
            "after": moved[0] if moved else None,
            "starts": moved[1] if moved else None,
            "reason": None if moved else reasons[visit_id],
        })  # fmt: skip
    worked = []
    for carer in world["carers"]:
        then_route = before["routes"].get(carer["id"], [])
        now_route = after["routes"].get(carer["id"], [])
        then = work(then_route, carer, world, day)
        later = work(now_route, carer, world, day, holds.get(carer["id"]))
        # The visits a carer keeps, in the order they now come: a new order changes the driving
        # even when no visit changes carer, so it is said, not left unexplained.
        kept_then = [v for v in then_route if v in now_route]
        kept_now = [v for v in now_route if v in then_route]
        if then != later or kept_then != kept_now:
            worked.append({"carer": carer["id"], "before": then, "after": later,
                           "limit": day_limit(carer),
                           "reordered": kept_then != kept_now})  # fmt: skip
    base = check.check(before, world, day)
    checked = check.check(after, world, day, holds)
    totals = {}
    for key in ("driving_minutes", "continuity_breaks", "uncovered"):
        totals[key] = {"before": base[key], "after": checked[key]}
    # How far visits still to come moved from their planned times, in all: clients expect them.
    shifted = 0
    for visit_id, (_, start) in was.items():
        if visit_id in now and start >= at:
            shifted += abs(now[visit_id][1] - start)
    totals["minutes_moved"] = {"before": 0, "after": shifted}
    return {"day": day, "out": out, "at": at, "changes": changes, "retimed": retimed,
            "work": worked, "totals": totals, "search": after["search"]}  # fmt: skip
