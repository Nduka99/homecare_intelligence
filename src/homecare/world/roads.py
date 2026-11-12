"""Driving times between the places that matter (carers' homes and clients), from a road graph
given as plain edges.

The graph comes from OpenStreetMap through OSMnx on the laptop; `homecare world
build` turns it into a list of (from, to, seconds) edges, so this module needs no map library
and can be tested on a hand-made graph. Roads can be one-way, so a time from a to b need not
equal the time from b to a.
"""

import heapq

Edge = tuple[str, str, float]
Matrix = dict[str, dict[str, int]]


def neighbours(edges: list[Edge]) -> dict[str, list[tuple[str, float]]]:
    found: dict[str, list[tuple[str, float]]] = {}
    for start, end, seconds in edges:
        found.setdefault(start, []).append((end, seconds))
    return found


def times_from(source: str, graph: dict[str, list[tuple[str, float]]]) -> dict[str, float]:
    """Dijkstra: the shortest driving time from `source` to every node it can reach."""
    best = {source: 0.0}
    queue = [(0.0, source)]
    while queue:
        so_far, node = heapq.heappop(queue)
        if so_far > best[node]:
            continue
        for following, seconds in graph.get(node, []):
            through = so_far + seconds
            if through < best.get(following, float("inf")):
                best[following] = through
                heapq.heappush(queue, (through, following))
    return best


def path(source: str, target: str, graph: dict[str, list[tuple[str, float]]]) -> list[str]:
    """The junctions on the quickest route from `source` to `target`, both included: Dijkstra
    again, keeping each node's predecessor and stopping at the target."""
    best = {source: 0.0}
    before: dict[str, str] = {}
    queue = [(0.0, source)]
    while queue:
        so_far, node = heapq.heappop(queue)
        if node == target:
            break
        if so_far > best[node]:
            continue
        for following, seconds in graph.get(node, []):
            through = so_far + seconds
            if through < best.get(following, float("inf")):
                best[following] = through
                before[following] = node
                heapq.heappush(queue, (through, following))
    if target not in best:
        raise ValueError(f"no route from {source} to {target}")
    found = [target]
    while found[-1] != source:
        found.append(before[found[-1]])
    return found[::-1]


def matrix(edges: list[Edge], places: list[str]) -> Matrix:
    """Whole-second driving times between every pair of places. A pair with no route is an
    error: places are chosen from the graph's largest strongly connected part, so it should
    never happen."""
    graph = neighbours(edges)
    found: Matrix = {}
    for source in places:
        reach = times_from(source, graph)
        found[source] = {}
        for target in places:
            if target not in reach:
                raise ValueError(f"no route from {source} to {target}")
            found[source][target] = round(reach[target])
    return found
