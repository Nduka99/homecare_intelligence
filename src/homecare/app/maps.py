"""The carer's route sketch: the day's streets and route, drawn as SVG by the server from the
committed road file (© OpenStreetMap contributors, ODbL).

There are no map tiles and no scripts, so the sketch works offline and needs no map service.
Positions are projected to metres on a flat local grid: across a few kilometres that is accurate
to well under 1%, plenty for a sketch.
"""

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from homecare.app import seed
from homecare.world import roads

FILE = Path(__file__).parent / "data" / "roads.json"
PIN = FILE.with_name("roads.json.sha256")
MARGIN = 250  # metres of streets shown around the day's route
ASPECT = 0.75  # never flatter than 4:3, or the sketch is a thin strip on a phone
SPAN = 360  # nominal pixels across, for sizing markers in the sketch's own metres
METRES_PER_DEGREE = 111_320
DIRECTIONS = "https://www.google.com/maps/dir/?api=1&destination={lat},{lon}&travelmode=driving"

Point = tuple[float, float]


def load(path: Path = FILE, pin: Path = PIN) -> dict[str, Any]:
    """The road file, after checking it against its pin, ready to route on and draw.

    Two junctions can be joined by more than one road: routing follows the quickest one's bends,
    and every road is drawn, each two-way road once."""
    found = seed.load(path, pin)
    nodes = {node: (lat, lon) for node, (lat, lon) in found["nodes"].items()}
    quickest: dict[tuple[str, str], tuple[float, list[Point]]] = {}
    streets: list[list[Point]] = []
    seen = set()
    for start, end, seconds, _, points in found["links"]:
        bends = [(lat, lon) for lat, lon in points]
        if (start, end) not in quickest or seconds < quickest[(start, end)][0]:
            quickest[(start, end)] = (seconds, bends)
        street = [nodes[start], *bends, nodes[end]]
        if tuple(street[::-1]) not in seen:
            seen.add(tuple(street))
            streets.append(street)
    edges = [(start, end, seconds) for start, end, seconds, _, _ in found["links"]]
    return {
        "nodes": nodes,
        "bends": {link: bends for link, (_, bends) in quickest.items()},
        "streets": streets,
        "graph": roads.neighbours(edges),
    }


def directions(road_map: dict[str, Any], node: str) -> str:
    """A Google Maps link that opens turn-by-turn directions to a junction (no key needed)."""
    lat, lon = road_map["nodes"][node]
    return DIRECTIONS.format(lat=lat, lon=lon)


def route(road_map: dict[str, Any], stops: list[str]) -> list[Point]:
    """Every point along the quickest roads through the stops in order, bends included."""
    nodes = road_map["nodes"]
    points = [nodes[stops[0]]]
    for start, end in zip(stops, stops[1:], strict=False):
        junctions = roads.path(start, end, road_map["graph"])
        for a, b in zip(junctions, junctions[1:], strict=False):
            points += road_map["bends"][(a, b)]
            points.append(nodes[b])
    return points


def projector(points: list[Point]) -> Callable[[Point], tuple[int, int]]:
    """A function from (lat, lon) to whole metres east and south of the points' north-west
    corner, scaled for the points' mean latitude."""
    north = max(lat for lat, _ in points)
    west = min(lon for _, lon in points)
    across = math.cos(math.radians(sum(lat for lat, _ in points) / len(points)))

    def project(point: Point) -> tuple[int, int]:
        lat, lon = point
        x = (lon - west) * across * METRES_PER_DEGREE
        y = (north - lat) * METRES_PER_DEGREE
        return round(x), round(y)

    return project


def line(points: list[tuple[int, int]]) -> str:
    """An SVG path through the points, every one kept at its exact position (the route)."""
    first, *rest = points
    return f"M{first[0]} {first[1]}" + "".join(f"L{x} {y}" for x, y in rest)


def street_path(points: list[tuple[int, int]], step: float) -> str:
    """A background street's SVG path: the start, then each move relative to the last, which
    writes shorter numbers than absolute positions. A point closer than `step` to the last one
    kept is left out, since the sketch cannot show it; the last point always stays, so streets
    still meet at their junctions."""
    first, *rest = points
    kept = [first]
    for index, point in enumerate(rest):
        if index == len(rest) - 1 or math.dist(point, kept[-1]) >= step:
            kept.append(point)
    moves = [f"{x - px} {y - py}" for (px, py), (x, y) in zip(kept, kept[1:], strict=False)]
    return f"M{first[0]} {first[1]}" + ("l" + " ".join(moves) if moves else "")


def inside(start: int, size: int, low: int, high: int, need: tuple[int, int]) -> int:
    """Slide a window of `size` from `start` back inside the mapped span [low, high] where it
    can go, without losing the span it must show, `need`."""
    if start < low:
        start = min(low, need[0])
    if start + size > high:
        start = max(high - size, need[1] - size)
    return start


def frame(drawn: list[tuple[int, int]], mapped: list[tuple[int, int]]) -> tuple[int, int, int, int]:
    """Left, top, width and height around the drawn points with a margin, widened or heightened
    to at least 4:3, then slid back inside the mapped area where the points allow: a route near
    the edge of the map's extract would otherwise show half a frame of blank paper."""
    xs = [x for x, _ in drawn]
    ys = [y for _, y in drawn]
    need_x = (min(xs) - MARGIN, max(xs) + MARGIN)
    need_y = (min(ys) - MARGIN, max(ys) + MARGIN)
    width = max(need_x[1] - need_x[0], round((need_y[1] - need_y[0]) / ASPECT))
    height = max(need_y[1] - need_y[0], round(width * ASPECT))
    left = need_x[0] - (width - (need_x[1] - need_x[0])) // 2
    top = need_y[0] - (height - (need_y[1] - need_y[0])) // 2
    left = inside(left, width, min(x for x, _ in mapped), max(x for x, _ in mapped), need_x)
    top = inside(top, height, min(y for _, y in mapped), max(y for _, y in mapped), need_y)
    return left, top, width, height


def places(stops: list[str]) -> dict[str, list[int]]:
    """Each place the day visits, with its visit numbers in route order (a client seen twice
    gets [1, 6]), so the sketch, the live map and the visit list share one numbering."""
    found: dict[str, list[int]] = {}
    for number, stop in enumerate(stops, start=1):
        found.setdefault(stop, []).append(number)
    return found


def live(road_map: dict[str, Any], home: str, stops: list[str]) -> dict[str, Any]:
    """The same day for the live map, in latitude and longitude rounded to about a metre: the
    route through every bend, home, and one stop per place with the sketch's label."""
    nodes = road_map["nodes"]
    markers = []
    for stop, numbers in places(stops).items():
        lat, lon = nodes[stop]
        label = ", ".join(map(str, numbers))
        markers.append({"lat": round(lat, 5), "lon": round(lon, 5), "label": label})
    path = route(road_map, [home, *stops, home])
    home_lat, home_lon = nodes[home]
    return {
        "route": [[round(lat, 5), round(lon, 5)] for lat, lon in path],
        "home": [round(home_lat, 5), round(home_lon, 5)],
        "stops": markers,
    }


def sketch(road_map: dict[str, Any], home: str, stops: list[str]) -> dict[str, Any]:
    """What the template draws for a day that starts and ends at `home` and visits `stops`: a
    view box, the streets inside it, the route, and one marker per place listing its visit
    numbers as in the visit list (a client seen twice gets "1, 6"). `unit` is the sketch's
    metres per nominal pixel, for sizing markers and text."""
    nodes = road_map["nodes"]
    path = route(road_map, [home, *stops, home])
    project = projector(path)
    drawn = [project(point) for point in path]
    left, top, width, height = frame(drawn, [project(point) for point in nodes.values()])
    unit = width / SPAN
    streets = []
    for street in road_map["streets"]:
        points = [project(point) for point in street]
        if any(left <= x <= left + width and top <= y <= top + height for x, y in points):
            streets.append(street_path(points, unit))  # detail under a nominal pixel is not drawn
    markers = []
    for stop, numbers in places(stops).items():
        x, y = project(nodes[stop])
        label = ", ".join(map(str, numbers))
        box_width = (len(label) * 7 + 12) * unit
        box_height = 22 * unit
        # Centred on the stop, but slid back inside the frame so no label is cut off.
        box_left = min(max(x - box_width / 2, left), left + width - box_width)
        box_top = min(max(y - box_height / 2, top), top + height - box_height)
        markers.append({
            "label": label,
            "x": x,
            "y": y,
            "box": {"x": round(box_left), "y": round(box_top), "width": round(box_width),
                    "height": round(box_height)},
            "text": {"x": round(box_left + box_width / 2), "y": round(box_top + 15.5 * unit)},
        })  # fmt: skip
    home_x, home_y = project(nodes[home])
    return {
        "view": f"{left} {top} {width} {height}",
        "font": round(13 * unit),
        "streets": "".join(streets),
        "route": line(drawn),
        "home": {"x": home_x, "y": home_y, "corner_x": round(home_x - 7 * unit),
                 "corner_y": round(home_y - 7 * unit), "size": round(14 * unit),
                 "stroke": round(2.5 * unit)},
        "stops": markers,
    }  # fmt: skip
