"""The demo area's road graph, from OpenStreetMap (ODbL) through OSMnx. Laptop only: the serving
image has no OSMnx, to stay small enough for a free host.

The raw graph is downloaded once, saved and pinned by SHA-256 in `map.json`, because
OpenStreetMap changes; every build checks the hash first. Speeds and travel times are worked out
from the pinned file in code, so the rule for them can change without a new download.

Map data © OpenStreetMap contributors, under the Open Database License 1.0.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import osmnx as ox

from homecare import models
from homecare.world.roads import Edge

PIN = Path(__file__).with_name("map.json")
FILE = Path("data") / "maps" / "b12-drive.graphml"
# The mean of B12's three neighbourhoods as OpenStreetMap places them (Nominatim, 2026-11-05:
# Balsall Heath, Sparkbrook, Highgate), rounded to about 100 m. A bare "B12" query matched a
# building in B15, so it is not used.
CENTRE = (52.462, -1.882)
DIST = 3000  # metres from the centre to each side of the box
BUILT_UP_KPH = 30 * 1.609344


def download(path: Path = FILE) -> dict[str, Any]:
    """Download the drive network once and return its pin. Refuses to replace a saved file."""
    if path.exists():
        raise SystemExit(f"{path} exists; a pinned map is never replaced")
    ox.settings.use_cache = False
    graph = ox.graph_from_point(CENTRE, DIST, network_type="drive")
    path.parent.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(graph, path)
    return {
        "file": path.as_posix(),
        "sha256": models.sha256(path),
        "centre": list(CENTRE),
        "dist": DIST,
        "network_type": "drive",
        "osmnx": ox.__version__,
        "downloaded": date.today().isoformat(),
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "credit": "© OpenStreetMap contributors",
        "licence": "ODbL-1.0",
    }


def load(pin_path: Path = PIN) -> Any:
    """The pinned graph, after its SHA-256 matches the pin."""
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    path = Path(pin["file"])
    if not path.is_file():
        raise SystemExit(f"{path} is missing; run `homecare world map` first")
    if models.sha256(path) != pin["sha256"]:
        raise SystemExit(f"{path} differs from its pin in {pin_path}; refusing to use it")
    return ox.load_graphml(path)


def timed(graph: Any) -> Any:
    """The largest strongly connected part, so every place can reach every other, with a
    driving time on every link.

    A road keeps its own speed tag. An untagged road takes the mean of the tagged roads of its
    type in this map (OSMnx's default): on the pinned map 82% of links are tagged, and 96% of
    tagged residential roads are 20 mph, so a 30 mph legal default would be too fast. A type
    with no tags at all takes 30 mph, the GOV.UK built-up limit."""
    graph = ox.truncate.largest_component(graph, strongly=True)
    graph = ox.add_edge_speeds(graph, fallback=BUILT_UP_KPH)
    return ox.add_edge_travel_times(graph)


def edges(graph: Any) -> list[Edge]:
    """Plain (from, to, seconds) edges for `roads`."""
    found = []
    for start, end, data in timed(graph).edges(data=True):
        found.append((str(start), str(end), float(data["travel_time"])))
    return found


def road_file(graph: Any, map_sha256: str) -> dict[str, Any]:
    """The road data the carer's map draws and routes on, from the same links and times as
    `edges`: each junction's position, and each one-way link's seconds, street name and bends.
    It is a Derivative Database of OpenStreetMap, so it carries the ODbL notice and is offered
    for download beside the map's credit."""
    graph = timed(graph)
    nodes = {}
    for node, data in graph.nodes(data=True):
        nodes[str(node)] = [round(data["y"], 6), round(data["x"], 6)]
    links = []
    for start, end, data in graph.edges(data=True):
        name = data.get("name") or ""
        if isinstance(name, list):  # a link that joins two named streets
            name = " / ".join(name)
        bends = []
        if "geometry" in data:  # the end points are the junctions themselves
            bends = [[round(y, 6), round(x, 6)] for x, y in data["geometry"].coords[1:-1]]
        links.append([str(start), str(end), round(float(data["travel_time"]), 1), name, bends])
    links.sort()
    return {
        "licence": (
            "Contains OpenStreetMap data © OpenStreetMap contributors, available under the Open "
            "Database License 1.0 (https://opendatacommons.org/licenses/odbl/1-0/). Derived "
            "from the pinned extract below: its largest strongly connected part, with free-flow "
            "driving times from speed tags (an untagged road takes its type's mean)."
        ),
        "source": "https://www.openstreetmap.org/copyright",
        "map_sha256": map_sha256,
        "nodes": dict(sorted(nodes.items())),
        "links": links,
    }
