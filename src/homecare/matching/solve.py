"""The optimised rota for one day, by OR-Tools vehicle routing. Laptop only: the serving image
has no OR-Tools, to stay small enough for a free host.

Each carer working that day is a vehicle that starts and ends at home:
- a time dimension holds the clock: each visit starts inside its window, and waiting is allowed;
- a work dimension counts care plus driving, with no waiting, up to the carer's daily limit;
- a visit may go only to carers able to do it alone (skills, and gender where the client asks);
- leaving a visit uncovered costs DROP, far above any driving it saves, so the solver covers
  every visit it can;
- the cost of a route is its driving minutes, plus `continuity` minutes for each visit to a client
  who is not usually this carer's, times `high_need` for high-need clients (tier above 1).

Visits no carer may do alone, and visits needing two carers, are uncovered before the solve, with
their reason. This matters because an empty allowed list in `SetAllowedVehiclesForIndex` allows
every carer; limiting the vehicle variable, as here, forbids everyone instead.
The 20-minute break rule is not in the model: the checker tests every rota for it.
`check.py` is the oracle.

The search stops after a fixed number of solutions, so a run repeats exactly. A time limit is only
a safety net: a day with few possible rotas can run out of new solutions, and guided local search
would then never stop. The result says which limit stopped it; only a run stopped by the solution
limit is sure to repeat.
"""

from typing import Any

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from homecare.matching.baseline import able, day_visits, reason
from homecare.matching.check import day_limit, minutes
from homecare.world.agency import on_duty

DROP = 100_000
SHIFT_SCALE = 10  # a re-plan's cost units per driving minute (see `route`)
# On day 0 of the built world, 1,000 solutions took 40 s; 3,000 took 143 s and cut driving by 1%.
SOLUTIONS = 1_000
SECONDS = 300


def solve(
    world: dict[str, Any],
    day: int,
    continuity: int,
    high_need: int,
    solutions: int = SOLUTIONS,
    seconds: int = SECONDS,
) -> dict[str, Any]:
    """The day's rota: every carer working starts at home when the shift starts, with the whole
    daily limit, and a visit away from the client's usual carer costs continuity."""
    today = world | {"carers": [c for c in world["carers"] if on_duty(c, day)]}
    carers = today["carers"]
    planned, allowed, uncovered = split_visits(today, day)
    vehicles = []
    for carer in carers:
        vehicles.append({"start": carer["home"], "home": carer["home"],
                         "ready": world["shift"][0], "work": day_limit(carer)})  # fmt: skip
    preferred = [world["usual"][visit["client"]] for visit in planned]
    found = route(world, vehicles, carers, planned, allowed, preferred,
                  continuity, high_need, solutions, seconds)  # fmt: skip
    if found is None:
        raise RuntimeError(f"no rota found for day {day}")
    routes, search = found
    on_route = {visit_id for route in routes.values() for visit_id in route}
    for visit in planned:
        if visit["id"] not in on_route:
            uncovered.append({"visit": visit["id"], "reason": reason(visit, today)})
    return {"routes": routes, "uncovered": uncovered, "search": search}


def route(
    world: dict[str, Any],
    vehicles: list[dict[str, Any]],
    carers: list[dict[str, Any]],
    planned: list[dict[str, Any]],
    allowed: list[list[int]],
    preferred: list[str],
    continuity: int,
    high_need: int,
    solutions: int,
    seconds: int,
    current: list[list[str]] | None = None,
    planned_starts: list[int] | None = None,
    moved_tenths: int = 1,
) -> tuple[dict[str, list[str]], dict[str, Any]] | None:
    """The routing model both the day's rota and a re-plan solve. Carer k (`carers[k]`) leaves
    `vehicles[k]["start"]` no earlier than its `ready` minute, ends at its `home`, and may work
    `work` more minutes. A visit may go only to its `allowed` carers (by position), and costs
    `continuity` (times `high_need` above tier 1) with any carer but its `preferred` one.
    Returns each carer's visits in order and how the search stopped, or None if none is found."""
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    # Nodes: the planned visits, then each carer's start, then the home of each carer who starts
    # elsewhere. A carer starting at home starts and ends on one node, as the day's rota always
    # has, so its model (and so its search) is unchanged.
    places = [nodes[visit["client"]] for visit in planned]
    places += [vehicle["start"] for vehicle in vehicles]
    starts = [len(planned) + k for k in range(len(vehicles))]
    ends = []
    for k, vehicle in enumerate(vehicles):
        if vehicle["start"] == vehicle["home"]:
            ends.append(starts[k])
        else:
            ends.append(len(places))
            places.append(vehicle["home"])
    drive = [[minutes(world["travel"][a][b]) for b in places] for a in places]
    care = [visit["duration"] for visit in planned] + [0] * (len(places) - len(planned))
    busy = [[care[a] + drive[a][b] for b in range(len(places))] for a in range(len(places))]

    manager = pywrapcp.RoutingIndexManager(len(places), len(vehicles), starts, ends)
    routing = pywrapcp.RoutingModel(manager)
    busy_index = routing.RegisterTransitMatrix(busy)
    shift_start, shift_end = world["shift"]
    routing.AddDimension(busy_index, shift_end, shift_end, False, "time")
    work = [vehicle["work"] for vehicle in vehicles]
    routing.AddDimensionWithVehicleCapacity(busy_index, 0, work, True, "work")
    clock = routing.GetDimensionOrDie("time")
    # A re-plan also prices moving a visit from its `planned_starts` time, at `moved_tenths`
    # tenths of a minute of driving per minute moved (`homecare rota replan-sweep` measures the
    # weight). Costs are whole numbers, so everything else is scaled by ten.
    scale = SHIFT_SCALE if planned_starts is not None else 1
    for node, visit in enumerate(planned):
        index = manager.NodeToIndex(node)
        clock.CumulVar(index).SetRange(visit["open"], visit["close"] - visit["duration"])
        # SetAllowedVehiclesForIndex rejects every list in the 9.15 Python binding, so the visit's
        # vehicle variable is limited instead; -1 keeps "uncovered" possible.
        routing.VehicleVar(index).SetValues([-1, *allowed[node]])
        routing.AddDisjunction([index], DROP * scale)
        if planned_starts is not None and moved_tenths > 0:
            clock.SetCumulVarSoftUpperBound(index, planned_starts[node], moved_tenths)
            clock.SetCumulVarSoftLowerBound(index, planned_starts[node], moved_tenths)
    for k, vehicle in enumerate(vehicles):
        clock.CumulVar(routing.Start(k)).SetRange(vehicle["ready"], shift_end)
        clock.CumulVar(routing.End(k)).SetRange(shift_start, shift_end)
    for k, carer in enumerate(carers):
        cost = [[value * scale for value in row] for row in drive]
        for node, visit in enumerate(planned):
            if preferred[node] != carer["id"]:
                extra = continuity * (high_need if visit.get("tier", 1) > 1 else 1) * scale
                for row in cost:
                    row[node] += extra
        routing.SetArcCostEvaluatorOfVehicle(routing.RegisterTransitMatrix(cost), k)

    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    parameters.solution_limit = solutions
    parameters.time_limit.seconds = seconds
    found: list[int] = []
    routing.AddAtSolutionCallback(lambda: found.append(1))
    solution = None
    began = "nothing"
    if current is not None:
        # A re-plan searches from the plan as it stands (`current`: each carer's planned visits
        # still to come), so a change survives only if it lowers the cost: a new order that
        # merely ties is never chosen over the order carers and clients already expect.
        position = {visit["id"]: node for node, visit in enumerate(planned)}
        indices = []
        for route in current:
            indices.append([manager.NodeToIndex(position[v]) for v in route if v in position])
        start = routing.ReadAssignmentFromRoutes(indices, True)
        if start is not None:
            solution = routing.SolveFromAssignmentWithParameters(start, parameters)
        if solution is not None:
            began = "the current plan"
    if solution is None:
        solution = routing.SolveWithParameters(parameters)
    if solution is None:
        return None
    routes = read_routes(solution, routing, manager, carers, planned)
    stopped = "solution limit" if len(found) >= solutions else "time limit"
    return routes, {"solutions": len(found), "stopped": stopped, "from": began}


def split_visits(
    today: dict[str, Any], day: int
) -> tuple[list[dict[str, Any]], list[list[int]], list[dict[str, str]]]:
    """The visits to plan with the carers (by position) allowed each, and those uncovered now."""
    planned = []
    allowed = []
    uncovered = []
    for visit in day_visits(today, day):
        able_ids = {carer["id"] for carer in able(visit, today)}
        vehicles = [k for k, carer in enumerate(today["carers"]) if carer["id"] in able_ids]
        if visit.get("two_carers") or not vehicles:
            uncovered.append({"visit": visit["id"], "reason": reason(visit, today)})
        else:
            planned.append(visit)
            allowed.append(vehicles)
    return planned, allowed, uncovered


def read_routes(
    solution: Any, routing: Any, manager: Any, carers: list[dict[str, Any]], planned: list[Any]
) -> dict[str, list[str]]:
    """Each carer's visits in order, from the solver's next-node choices."""
    routes = {}
    for k, carer in enumerate(carers):
        index = solution.Value(routing.NextVar(routing.Start(k)))
        route = []
        while not routing.IsEnd(index):
            route.append(planned[manager.IndexToNode(index)]["id"])
            index = solution.Value(routing.NextVar(index))
        if route:
            routes[carer["id"]] = route
    return routes
