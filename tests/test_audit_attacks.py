"""Attack tests on the audit chain: each edit, deletion or rewrite must be detected, and each
`test_sabotage_*` shows the check that catches it is needed."""

import copy
from typing import Any

from homecare import audit

KEY = b"key held outside the database"


def chain_of(count: int) -> tuple[list[dict[str, Any]], str]:
    chain: list[dict[str, Any]] = []
    for number in range(count):
        entry = {"who": "lead-1", "did": "viewed", "note": f"n-{number}"}
        anchor = audit.append(chain, entry, KEY)["mac"]
    return chain, anchor


def test_an_untouched_chain_verifies() -> None:
    chain, anchor = chain_of(5)
    assert audit.problems(chain, KEY, anchor) == []


def test_editing_one_record_is_detected() -> None:
    chain, anchor = chain_of(5)
    chain[2]["entry"]["did"] = "dismissed"
    assert audit.problems(chain, KEY, anchor) == [
        "record 2 was changed, or the chain was rebuilt without the key"
    ]


def test_deleting_the_newest_records_is_detected() -> None:
    chain, anchor = chain_of(5)
    del chain[3:]
    assert audit.problems(chain, KEY, anchor) == [
        "the newest record is not the anchored one: records were removed or added"
    ]


def test_rebuilding_the_whole_chain_without_the_key_is_detected() -> None:
    chain, _ = chain_of(5)
    forged: list[dict[str, Any]] = []
    for record in chain:
        entry = copy.deepcopy(record["entry"])
        entry["did"] = "dismissed"
        forged_anchor = audit.append(forged, entry, b"the attacker's own key")["mac"]
    # Even with the forged anchor written, the real key rejects the first record.
    assert audit.problems(forged, KEY, forged_anchor) == [
        "record 0 was changed, or the chain was rebuilt without the key"
    ]


def test_sabotage_without_the_anchor_a_deletion_passes() -> None:
    chain, _ = chain_of(5)
    del chain[3:]
    assert audit.problems(chain, KEY, chain[-1]["mac"]) == []


def test_sabotage_with_the_attackers_key_a_rebuilt_chain_passes() -> None:
    attacker = b"the attacker's own key"
    forged: list[dict[str, Any]] = []
    for number in range(3):
        anchor = audit.append(forged, {"did": "dismissed", "note": f"n-{number}"}, attacker)["mac"]
    assert audit.problems(forged, attacker, anchor) == []


def test_key_order_in_an_entry_does_not_change_its_mac() -> None:
    assert audit.canonical({"a": 1, "b": 2}) == audit.canonical({"b": 2, "a": 1})
