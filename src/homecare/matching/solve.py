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
    today = world | {"carers": [c for c in world["carers"] if on_duty(c, day)]}
    carers = today["carers"]
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    planned, allowed, uncovered = split_visits(today, day)

    # Nodes: the planned visits, then each carer's home (start and end of their route).
    places = [nodes[visit["client"]] for visit in planned] + [carer["home"] for carer in carers]
    homes = [len(planned) + k for k in range(len(carers))]
    drive = [[minutes(world["travel"][a][b]) for b in places] for a in places]
    care = [visit["duration"] for visit in planned] + [0] * len(carers)
    busy = [[care[a] + drive[a][b] for b in range(len(places))] for a in range(len(places))]

    manager = pywrapcp.RoutingIndexManager(len(places), len(carers), homes, homes)
    routing = pywrapcp.RoutingModel(manager)
    busy_index = routing.RegisterTransitMatrix(busy)
    shift_start, shift_end = world["shift"]
    routing.AddDimension(busy_index, shift_end, shift_end, False, "time")
    routing.AddDimensionWithVehicleCapacity(
        busy_index, 0, [day_limit(carer) for carer in carers], True, "work"
    )
    clock = routing.GetDimensionOrDie("time")
    for node, visit in enumerate(planned):
        index = manager.NodeToIndex(node)
        clock.CumulVar(index).SetRange(visit["open"], visit["close"] - visit["duration"])
        # SetAllowedVehiclesForIndex rejects every list in the 9.15 Python binding, so the visit's
        # vehicle variable is limited instead; -1 keeps "uncovered" possible.
        routing.VehicleVar(index).SetValues([-1, *allowed[node]])
        routing.AddDisjunction([index], DROP)
    for k in range(len(carers)):
        clock.CumulVar(routing.Start(k)).SetRange(shift_start, shift_end)
        clock.CumulVar(routing.End(k)).SetRange(shift_start, shift_end)
    for k, carer in enumerate(carers):
        cost = [row[:] for row in drive]
        for node, visit in enumerate(planned):
            if world["usual"][visit["client"]] != carer["id"]:
                extra = continuity * (high_need if visit.get("tier", 1) > 1 else 1)
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
    solution = routing.SolveWithParameters(parameters)
    if solution is None:
        raise RuntimeError(f"no rota found for day {day}")

    routes = read_routes(solution, routing, manager, carers, planned)
    on_route = {visit_id for route in routes.values() for visit_id in route}
    for visit in planned:
        if visit["id"] not in on_route:
            uncovered.append({"visit": visit["id"], "reason": reason(visit, today)})
    stopped = "solution limit" if len(found) >= solutions else "time limit"
    return {
        "routes": routes,
        "uncovered": uncovered,
        "search": {"solutions": len(found), "stopped": stopped},
    }


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
