"""The carer's route sketch: quickest roads, one-way streets respected, every stop on the route in
order, and a changed road file stops the app."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from homecare.app import maps, seed
from homecare.world import roads


def square() -> dict[str, Any]:
    """Four junctions about 700 m apart. a to b directly is slow; a, d, c, b is quicker; b to a
    has no road (one-way); d to c bends north on the way."""
    nodes = {"a": (52.0, -1.0), "b": (52.0, -0.99), "c": (52.006, -0.99), "d": (52.006, -1.0)}
    links = [("a", "b", 100.0), ("a", "d", 10.0), ("d", "c", 10.0), ("c", "b", 10.0),
             ("b", "c", 10.0), ("c", "d", 10.0), ("d", "a", 10.0)]  # fmt: skip
    bends: dict[tuple[str, str], list[maps.Point]] = {(a, b): [] for a, b, _ in links}
    bends[("d", "c")] = [(52.007, -0.995)]
    bends[("c", "d")] = [(52.007, -0.995)]
    streets = [[nodes["a"], nodes["b"]], [nodes["a"], nodes["d"]], [nodes["c"], nodes["b"]],
               [nodes["d"], (52.007, -0.995), nodes["c"]]]  # fmt: skip
    return {"nodes": nodes, "bends": bends, "streets": streets, "graph": roads.neighbours(links)}


def test_the_quickest_roads_and_one_way_streets() -> None:
    graph = square()["graph"]
    assert roads.path("a", "b", graph) == ["a", "d", "c", "b"]
    assert roads.path("b", "a", graph) == ["b", "c", "d", "a"]  # never back along a to b
    with pytest.raises(ValueError, match="no route"):
        roads.path("a", "nowhere", graph)


def test_the_route_follows_bends_and_passes_each_stop_in_order() -> None:
    road_map = square()
    points = maps.route(road_map, ["a", "b", "a"])
    nodes = road_map["nodes"]
    assert points[:5] == [nodes["a"], nodes["d"], (52.007, -0.995), nodes["c"], nodes["b"]]
    assert points[-1] == nodes["a"]


def test_the_sketch_shows_the_stops_and_only_nearby_streets() -> None:
    road_map = square()
    road_map["streets"].append([(52.2, -1.0), (52.2, -0.99)])  # about 22 km north
    drawn = maps.sketch(road_map, "a", ["c", "b", "c"])
    left, top, width, height = map(int, drawn["view"].split())
    for stop in [drawn["home"], *drawn["stops"]]:
        assert left <= stop["x"] <= left + width and top <= stop["y"] <= top + height
    for stop in drawn["stops"]:  # every label box lies wholly inside the frame
        box = stop["box"]
        assert left <= box["x"] and box["x"] + box["width"] <= left + width
        assert top <= box["y"] and box["y"] + box["height"] <= top + height
    assert height >= 0.75 * width  # never a thin strip
    assert [stop["label"] for stop in drawn["stops"]] == ["1, 3", "2"]  # c is seen twice
    assert drawn["streets"].count("M") == 4  # the square's four streets, not the far one


def test_a_long_label_at_the_edge_stays_inside_the_frame() -> None:
    """Eight visits to the same client: the label is wider than the margin around the stop."""
    drawn = maps.sketch(square(), "a", ["b"] * 8)
    left, top, width, _ = map(int, drawn["view"].split())
    (stop,) = drawn["stops"]
    assert stop["label"] == "1, 2, 3, 4, 5, 6, 7, 8"
    assert left <= stop["box"]["x"] and stop["box"]["x"] + stop["box"]["width"] <= left + width


def test_a_route_on_the_edge_of_the_map_slides_the_frame_onto_the_map() -> None:
    """A flat route along the mapped area's southern edge: the 4:3 frame grows upwards over
    streets, not half above and half below over blank paper."""
    mapped = [(0, 0), (2000, 1000)]
    left, top, width, height = maps.frame([(0, 1000), (2000, 1000)], mapped)
    assert (width, height) == (2500, 1875)
    assert top + height == 1000 + maps.MARGIN  # the bottom stops at the route's margin
    assert left == -maps.MARGIN  # nothing to slide across


def test_parallel_roads_are_all_drawn_and_routing_takes_the_quickest(tmp_path: Path) -> None:
    """Two roads join the same junctions: both are drawn; the route bends along the quicker."""
    found = {
        "nodes": {"a": [52.0, -1.0], "b": [52.0, -0.99]},
        "links": [["a", "b", 50.0, "Slow Lane", [[52.002, -0.995]]],
                  ["a", "b", 20.0, "Quick Road", [[51.998, -0.995]]],
                  ["b", "a", 20.0, "Quick Road", [[51.998, -0.995]]]],
    }  # fmt: skip
    text = json.dumps(found)
    (tmp_path / "r.json").write_text(text, encoding="utf-8")
    (tmp_path / "r.sha256").write_text(seed.sha256(text), encoding="utf-8")
    road_map = maps.load(tmp_path / "r.json", tmp_path / "r.sha256")
    assert len(road_map["streets"]) == 2  # Quick Road both ways is one street
    assert maps.route(road_map, ["a", "b"])[1] == (51.998, -0.995)


def test_a_thinned_street_keeps_its_ends_and_drops_only_sub_pixel_points() -> None:
    """Read back independently: the relative moves add up to kept input points, the street still
    starts and ends at its junctions, and only points within one step of the last kept are gone."""
    points = [(100, 200), (101, 200), (103, 201), (110, 205), (111, 205), (140, 240)]
    path = maps.street_path(points, step=5)
    start = re.match(r"M(-?\d+) (-?\d+)l", path)
    assert start is not None
    at = (int(start.group(1)), int(start.group(2)))
    kept = [at]
    for dx, dy in re.findall(r"(-?\d+) (-?\d+)", path[start.end() :]):
        at = (at[0] + int(dx), at[1] + int(dy))
        kept.append(at)
    assert kept == [(100, 200), (110, 205), (140, 240)]
    assert maps.street_path([(5, 5), (6, 6)], step=50) == "M5 5l1 1"  # the end is never dropped


def test_the_live_map_shows_the_sketch_s_stops_route_and_home() -> None:
    """The live map and the sketch describe one day: the same labelled stops at the same
    junctions, a route that starts and ends at home, and coordinates read from the road file."""
    road_map = square()
    stops = ["c", "b", "c"]
    drawn = maps.sketch(road_map, "a", stops)
    live = maps.live(road_map, "a", stops)
    assert [stop["label"] for stop in live["stops"]] == [m["label"] for m in drawn["stops"]]
    assert [stop["label"] for stop in live["stops"]] == ["1, 3", "2"]
    nodes = road_map["nodes"]
    assert [(s["lat"], s["lon"]) for s in live["stops"]] == [nodes["c"], nodes["b"]]
    assert live["home"] == list(nodes["a"])
    assert live["route"][0] == live["route"][-1] == list(nodes["a"])
    route_points = [tuple(point) for point in live["route"]]
    assert all(nodes[stop] in route_points for stop in stops)


def test_directions_open_google_maps_at_the_junction() -> None:
    assert maps.directions(square(), "c") == (
        "https://www.google.com/maps/dir/?api=1&destination=52.006,-0.99&travelmode=driving"
    )


def test_a_real_day_routes_through_every_stop_in_order() -> None:
    """On the committed road file and seed: each stop appears on the drawn route, in route
    order, and the route starts and ends at home."""
    road_map = maps.load()
    demo = seed.load()
    homes = {carer["id"]: carer["home"] for carer in demo["carers"]}
    nodes = {client["id"]: client["node"] for client in demo["clients"]}
    day = [v for v in demo["visits"] if v["carer"] == "K01" and v["day"] == demo["days"][-1]]
    stops = [nodes[v["client"]] for v in sorted(day, key=lambda v: v["position"])]
    drawn = maps.sketch(road_map, homes["K01"], stops)
    pairs = re.findall(r"[ML](-?\d+) (-?\d+)", drawn["route"])
    route = [(int(x), int(y)) for x, y in pairs]
    places = {stop["label"]: (stop["x"], stop["y"]) for stop in drawn["stops"]}
    at = 0
    for number in range(1, len(stops) + 1):
        place = next(xy for label, xy in places.items() if str(number) in label.split(", "))
        at = route.index(place, at)
    assert route[0] == route[-1] == (drawn["home"]["x"], drawn["home"]["y"])


def test_a_changed_road_file_stops_the_app(tmp_path: Path) -> None:
    text = maps.FILE.read_text(encoding="utf-8")
    (tmp_path / "roads.json").write_text(text, encoding="utf-8", newline="\n")
    (tmp_path / "roads.json.sha256").write_text(seed.sha256(text), encoding="utf-8")
    assert maps.load(tmp_path / "roads.json", tmp_path / "roads.json.sha256")["nodes"]
    changed = json.loads(text)
    changed["links"][0][2] = 0.1  # one road made instant
    (tmp_path / "roads.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(SystemExit, match="differs from its pin"):
        maps.load(tmp_path / "roads.json", tmp_path / "roads.json.sha256")


def test_the_road_file_carries_the_odbl_notice() -> None:
    found = json.loads(maps.FILE.read_text(encoding="utf-8"))
    assert "© OpenStreetMap contributors" in found["licence"]
    assert "Open Database License" in found["licence"]
