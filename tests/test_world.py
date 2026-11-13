"""The demo agency and its driving times, on hand-made graphs (no map library needed)."""

import math
import random
from typing import Any

import pytest

from homecare.world import agency, roads

# A small one-way grid: from a to d is quick, from d to a goes the long way round.
EDGES = [
    ("a", "b", 60.0),
    ("b", "c", 60.0),
    ("c", "d", 60.0),
    ("a", "d", 200.0),
    ("d", "e", 30.0),
    ("e", "a", 400.0),
    ("b", "e", 500.0),
]


def floyd_warshall(edges: list[roads.Edge], places: list[str]) -> dict[str, dict[str, float]]:
    """Expected distances worked out another way: every pair at once, not by Dijkstra."""
    nodes = sorted({u for u, _, _ in edges} | {v for _, v, _ in edges})
    best = {u: {v: (0.0 if u == v else float("inf")) for v in nodes} for u in nodes}
    for start, end, seconds in edges:
        best[start][end] = min(best[start][end], seconds)
    for middle in nodes:
        for start in nodes:
            for end in nodes:
                through = best[start][middle] + best[middle][end]
                if through < best[start][end]:
                    best[start][end] = through
    return {u: {v: best[u][v] for v in places} for u in places}


def test_the_matrix_matches_an_independent_all_pairs_check() -> None:
    places = ["a", "c", "d", "e"]
    found = roads.matrix(EDGES, places)
    expected = floyd_warshall(EDGES, places)
    assert found == {u: {v: round(expected[u][v]) for v in places} for u in places}
    assert found["a"]["d"] == 180  # through b and c, not the 200-second road
    assert found["d"]["a"] == 430  # one-way roads make the return longer


def test_a_place_with_no_route_is_an_error() -> None:
    with pytest.raises(ValueError, match="no route"):
        roads.matrix([("a", "b", 10.0)], ["a", "b"])


def grid(size: int) -> list[roads.Edge]:
    edges = []
    for row in range(size):
        for col in range(size):
            here = f"{row}-{col}"
            for d_row, d_col in ((0, 1), (1, 0), (0, -1), (-1, 0)):
                r, c = row + d_row, col + d_col
                if 0 <= r < size and 0 <= c < size:
                    edges.append((here, f"{r}-{c}", 60.0))
    return edges


EDGES_GRID = grid(12)
NODES = sorted({u for u, _, _ in EDGES_GRID})


def build(seed: int = agency.SEED) -> dict[str, Any]:
    return agency.build(NODES, lambda places: roads.matrix(EDGES_GRID, places), seed)


def test_the_same_seed_builds_the_same_agency() -> None:
    assert build() == build()
    assert build() != build(seed=7)


def test_everyone_sits_on_a_distinct_graph_node() -> None:
    world = build()
    places = [c["node"] for c in world["clients"]] + [c["home"] for c in world["carers"]]
    assert set(places) <= set(NODES)
    assert len(places) == len(set(places))
    assert world["synthetic"] is True


def test_a_usual_carer_can_do_every_visit_of_the_client() -> None:
    world = build()
    carers = {c["id"]: c for c in world["carers"]}
    for client in world["clients"]:
        plan = [agency.visit_rules(client, slot) for slot in client["slots"]]
        usual = world["usual"][client["id"]]
        able = [c for c in world["carers"] if all(agency.can_do(c, r) for r in plan)]
        if usual is None:
            assert not able
        else:
            assert all(agency.can_do(carers[usual], rules) for rules in plan)


def test_every_visit_fits_its_window_and_repeats_daily() -> None:
    world = build()
    for visit in world["visits"]:
        assert visit["open"] + visit["duration"] <= visit["close"]
        assert agency.SHIFT[0] <= visit["open"] and visit["close"] <= agency.SHIFT[1]
    per_day = sum(len(client["slots"]) for client in world["clients"])
    assert len(world["visits"]) == per_day * agency.DAYS


def test_sabotage_a_matrix_from_the_wrong_graph_disagrees_with_the_oracle() -> None:
    shuffled = [(u, v, random.Random(1).uniform(1, 600)) for u, v, _ in EDGES]
    places = ["a", "c", "d", "e"]
    expected = floyd_warshall(EDGES, places)
    found = roads.matrix(shuffled, places)
    assert found != {u: {v: round(expected[u][v]) for v in places} for u in places}


def client(**profile: Any) -> dict[str, Any]:
    base = {"needs": ["personal care"], "slots": ["morning", "lunch", "bed"], "tier": 1}
    return base | profile


def test_time_critical_medicine_narrows_the_medicine_rounds_only() -> None:
    rules = {slot: agency.visit_rules(client(time_critical=True), slot) for slot in agency.SLOTS}
    assert (rules["morning"]["open"], rules["morning"]["close"]) == (450, 510)
    assert (rules["bed"]["open"], rules["bed"]["close"]) == (1200, 1260)
    assert (rules["lunch"]["open"], rules["lunch"]["close"]) == agency.SLOTS["lunch"][:2]


def test_overlays_set_the_right_rules_on_the_right_visits() -> None:
    profile = client(insulin=True, hoist=True, gender_preference="female")
    morning = agency.visit_rules(profile, "morning")
    lunch = agency.visit_rules(profile, "lunch")
    assert "insulin" in morning["skills"] and "insulin" not in lunch["skills"]
    assert morning["two_carers"] and not lunch["two_carers"]
    assert morning["gender"] == "female" and lunch["gender"] is None


def test_can_do_needs_every_skill_and_the_preferred_gender() -> None:
    rules = agency.visit_rules(client(gender_preference="female"), "morning")
    assert agency.can_do({"skills": ["personal care"], "gender": "female"}, rules)
    assert not agency.can_do({"skills": ["personal care"], "gender": "male"}, rules)
    assert not agency.can_do({"skills": [], "gender": "female"}, rules)


def test_every_client_has_a_known_group_and_its_tier() -> None:
    for item in build()["clients"]:
        share, tier, skill = agency.GROUPS[item["group"]]
        assert item["tier"] == tier
        assert skill is None or skill in item["needs"]


def test_each_carer_works_five_days_and_every_weekday_is_staffed() -> None:
    carers = build()["carers"]
    for carer in carers:
        first, second = carer["days_off"]
        assert second == (first + 1) % 7
        assert sum(agency.on_duty(carer, day) for day in range(7)) == 5
    # Each weekday loses the carers whose two days off cover it: at most two staggered groups.
    most_off = 2 * math.ceil(len(carers) / 7)
    for day in range(7):
        assert sum(agency.on_duty(carer, day) for carer in carers) >= len(carers) - most_off


def test_every_client_has_a_synthetic_name_and_a_description() -> None:
    clients = build()["clients"]
    assert len({client["name"] for client in clients}) == len(clients)
    for client in clients:
        assert client["name"].startswith("Mrs " if client["sex"] == "F" else "Mr ")
        assert client["details"].startswith(f"{client['age']}, ")


def test_describing_clients_changes_nothing_else() -> None:
    plain = build()
    for client in plain["clients"]:
        for field in ("name", "sex", "age", "details"):
            del client[field]
    rng = random.Random(agency.SEED)
    picked = rng.sample(sorted(NODES), agency.CLIENTS + agency.CARERS)
    assert plain["clients"] == agency.make_clients(rng, picked[: agency.CLIENTS])
