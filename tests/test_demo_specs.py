"""The demo week's note specs (DEMO only), on the hand-made grid world."""

from collections import Counter
from typing import Any

from homecare.matching import baseline
from homecare.world import writer
from test_world import build

DAYS = range(21, 28)


def specs(seed: int = 909) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    world = build()
    rotas = [baseline.plan_day(world, day) for day in DAYS]
    return world, writer.demo_specs(world, rotas, "demo-week", seed)


def test_one_spec_per_visit_made_with_the_clients_own_details() -> None:
    world, found = specs()
    rotas = [baseline.plan_day(world, day) for day in DAYS]
    made = [v for rota in rotas for route in rota["routes"].values() for v in route]
    assert sorted(spec["visit_id"] for spec in found) == sorted(made)
    clients = {client["id"]: client for client in world["clients"]}
    for spec in found:
        assert spec["client"] == clients[spec["client_id"]]["name"]
        assert spec["client_details"] == clients[spec["client_id"]]["details"]


def test_each_carer_keeps_one_writing_style() -> None:
    _, found = specs()
    styles: dict[str, set[str]] = {}
    for spec in found:
        styles.setdefault(spec["carer_id"], set()).add(spec["persona"])
    assert all(len(persona) == 1 for persona in styles.values())


def test_labels_come_from_the_template_and_fit_the_client() -> None:
    world, found = specs()
    by_id = {t["id"]: t for t in writer.templates("A")}
    sexes = {client["id"]: client["sex"] for client in world["clients"]}
    for spec in found:
        template = by_id[spec["templates"][0]]
        assert spec["labels"] == ([template["category"]] if template["category"] else [])
        assert template.get("sex") in (None, sexes[spec["client_id"]])


def test_concerns_appear_at_about_the_fixed_rate_and_repeat_by_seed() -> None:
    _, found = specs()
    share = Counter(bool(spec["labels"]) for spec in found)[True] / len(found)
    assert abs(share - writer.DEMO_CONCERN) < 0.05
    assert specs()[1] == found
    assert specs(seed=1)[1] != found


def test_a_template_that_names_a_visit_is_used_only_at_that_visit() -> None:
    world, found = specs()
    by_id = {t["id"]: t for t in writer.templates("A")}
    slots = {visit["id"]: visit["slot"] for visit in world["visits"]}
    for spec in found:
        named = by_id[spec["templates"][0]].get("slots")
        assert named is None or slots[spec["visit_id"]] in named


def test_every_kind_has_a_template_for_every_visit_and_client() -> None:
    pool = writer.templates("A")
    for note_kind in writer.MIX:
        for sex in ("F", "M"):
            for slot in writer.WRITER_VISIT:
                assert any(writer.fits(t, note_kind, sex, slot) for t in pool), (
                    note_kind,
                    sex,
                    slot,
                )
