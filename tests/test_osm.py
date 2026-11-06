"""The OSMnx edge: speeds and travel times on a hand-made graph, and the map's pinned hash."""

import json
from pathlib import Path

import networkx as nx
import osmnx as ox
import pytest

from homecare import models
from homecare.world import osm

MILE = 1609.344


def small_graph() -> "nx.MultiDiGraph[int]":
    """1, 2 and 3 reach each other; 4 is only reachable one way, so it is left out."""
    graph: nx.MultiDiGraph[int] = nx.MultiDiGraph(crs="epsg:4326")
    for node in (1, 2, 3, 4):
        graph.add_node(node, x=-1.88 + node / 1000, y=52.46)
    # A tagged 20 mph residential mile: 180 s.
    graph.add_edge(1, 2, osmid=10, highway="residential", length=MILE, maxspeed="20 mph")
    # Untagged residential: takes the mean of the tagged residential roads, 20 mph.
    graph.add_edge(2, 1, osmid=11, highway="residential", length=MILE)
    # A type with no tags anywhere: the 30 mph built-up fallback, 120 s.
    graph.add_edge(2, 3, osmid=12, highway="unclassified", length=MILE)
    graph.add_edge(3, 1, osmid=13, highway="unclassified", length=MILE)
    graph.add_edge(1, 4, osmid=14, highway="residential", length=MILE, maxspeed="20 mph")
    return graph


def test_speeds_and_times_follow_the_rule() -> None:
    found = {(start, end): seconds for start, end, seconds in osm.edges(small_graph())}
    # OSMnx converts mph with 1.60934, so tagged roads differ from the exact mile in the 6th digit.
    expected = {("1", "2"): 180.0, ("2", "1"): 180.0, ("2", "3"): 120.0, ("3", "1"): 120.0}
    assert found == pytest.approx(expected, rel=1e-5)


def pinned(tmp_path: Path) -> Path:
    path = tmp_path / "map.graphml"
    ox.save_graphml(small_graph(), path)
    pin = tmp_path / "map.json"
    pin.write_text(json.dumps({"file": str(path), "sha256": models.sha256(path)}))
    return pin


def test_an_unchanged_map_loads(tmp_path: Path) -> None:
    assert osm.load(pinned(tmp_path)).number_of_nodes() == 4


def test_a_changed_map_is_refused(tmp_path: Path) -> None:
    pin = pinned(tmp_path)
    path = Path(json.loads(pin.read_text())["file"])
    path.write_text(path.read_text(encoding="utf-8").replace("residential", "motorway", 1))
    with pytest.raises(SystemExit, match="differs from its pin"):
        osm.load(pin)
