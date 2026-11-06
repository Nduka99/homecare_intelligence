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


def edges(graph: Any) -> list[Edge]:
    """Plain (from, to, seconds) edges for `roads`, over the largest strongly connected part,
    so every place can reach every other.

    A road keeps its own speed tag. An untagged road takes the mean of the tagged roads of its
    type in this map (OSMnx's default): on the pinned map 82% of links are tagged, and 96% of
    tagged residential roads are 20 mph, so a 30 mph legal default would be too fast. A type
    with no tags at all takes 30 mph, the GOV.UK built-up limit."""
    graph = ox.truncate.largest_component(graph, strongly=True)
    graph = ox.add_edge_speeds(graph, fallback=BUILT_UP_KPH)
    graph = ox.add_edge_travel_times(graph)
    found = []
    for start, end, data in graph.edges(data=True):
        found.append((str(start), str(end), float(data["travel_time"])))
    return found
