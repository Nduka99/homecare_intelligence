"""The FHIR R4 export of one client's record (`homecare fhir export`).

Every expected answer comes from outside the export code: the FHIR R4 specification (its
required codes, written out below), an independent FHIR library (fhir.resources, whose R4B models
match FHIR R4 for every resource and data type used here), direct SQL on the same database, or
decisions planted by hand in the test. Each protection was also switched off once, on purpose,
to see its test fail.
"""

import base64
import copy
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pydantic
import pytest
from fhir.resources.R4B.bundle import Bundle

from homecare import cli, fhir
from homecare.app import auditlog, db, seed
from homecare.app import main as app_main

CLIENT = "C45"
OTHER = "C01"
# HL7's security label for test data (v3-ActReason HTEST), as the specification writes it.
HTEST = ("http://terminology.hl7.org/CodeSystem/v3-ActReason", "HTEST")
# The codes the FHIR R4 specification's required bindings allow (hl7.org/fhir/R4, each page).
REQUIRED = {
    ("Bundle", "type"): {"document", "message", "transaction", "transaction-response", "batch",
                         "batch-response", "history", "searchset", "collection"},
    ("Encounter", "status"): {"planned", "arrived", "triaged", "in-progress", "onleave",
                              "finished", "cancelled", "entered-in-error", "unknown"},
    ("Flag", "status"): {"active", "inactive", "entered-in-error"},
    ("DocumentReference", "status"): {"current", "superseded", "entered-in-error"},
    ("Practitioner", "gender"): {"male", "female", "other", "unknown"},
}  # fmt: skip
RELATES_TO = {"replaces", "transforms", "signs", "appends"}
NOON = 1794139200.0  # 2026-11-08T12:00:00Z, converted with GNU date
ADDITION = "note-0123456789abcdef"
ADDITION_BODY = "Added after the visit: she asked me not to tell her son about the bruise."
# The concerns the planted decisions below leave standing, worked out by hand from them.
EXPECTED_FLAGS = {
    ("demo-week-0651", "physical abuse", "safety"),
    ("demo-week-0651", "psychological abuse", "safety"),
    ("demo-week-0010", "possible deterioration (soft sign)", "clinical"),
    ("demo-week-0035", "self-neglect", "safety"),
    ("demo-week-0179", "quality risk", None),
}


def finding(connection: sqlite3.Connection, note: str, position: int) -> int:
    row = connection.execute(
        "SELECT id FROM findings WHERE note = ? AND position = ?", (note, position)
    ).fetchone()
    return int(row["id"])


def decide(
    connection: sqlite3.Connection,
    note: str,
    position: int | None,
    label: str,
    reviewer: str,
    category: str | None = None,
) -> None:
    """One decision row, as the review screen stores it; a later row replaces an earlier one."""
    found = None if position is None else finding(connection, note, position)
    count = connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    connection.execute(
        "INSERT INTO decisions (note, finding, label, category, reviewer, visitor, route, decided)"
        " VALUES (?, ?, ?, ?, ?, NULL, 'an urgent finding', ?)",
        (note, found, label, category, reviewer, NOON + 60 * count),
    )


def plant(path: Path) -> sqlite3.Connection:
    """The demo seed, two reviewers, decisions of every kind and one app addition."""
    db.prepare(path, seed.load())
    connection = db.connect(path)
    connection.execute("INSERT INTO users (id, role) VALUES ('lead.one', 'lead')")
    connection.execute("INSERT INTO users (id, role) VALUES ('lead_two', 'lead')")
    decide(connection, "demo-week-0651", 0, "confirmed", "lead.one")  # physical abuse
    decide(connection, "demo-week-0651", 1, "wrong category", "lead.one", "psychological abuse")
    decide(connection, "demo-week-0651", 2, "not a concern", "lead.one")
    for position in range(3):  # one grouped decision is stored once per near-duplicate finding
        decide(connection, "demo-week-0010", position, "confirmed", "lead_two")
    decide(connection, "demo-week-0261", 0, "confirmed", "lead.one")
    decide(connection, "demo-week-0261", 0, "not a concern", "lead.one")  # a changed mind
    decide(connection, "demo-week-0179", 0, "not a concern", "lead_two")
    decide(connection, "demo-week-0179", 0, "confirmed", "lead_two")  # changed the other way
    decide(connection, "demo-week-0035", None, "missed concern", "lead.one", "self-neglect")
    decide(connection, "demo-week-0244", None, "not a concern", "lead.one")  # nothing missed
    other_note = connection.execute(
        "SELECT note FROM findings f JOIN notes n ON n.id = f.note WHERE n.client = ? LIMIT 1",
        (OTHER,),
    ).fetchone()["note"]
    decide(connection, other_note, 0, "confirmed", "lead.one")  # another client's concern
    connection.execute(
        "INSERT INTO notes (id, visit, client, carer, day, body, tamper, tamper_probability, "
        "source, amends, idempotency_key, written) VALUES (?, 'V03690', ?, 'K01', 26, ?, 0, 0, "
        "'app', 'demo-week-0651', ?, ?)",
        (ADDITION, CLIENT, ADDITION_BODY, str(uuid.uuid4()), NOON),
    )
    connection.commit()
    return connection


@pytest.fixture
def exported(tmp_path: Path) -> tuple[sqlite3.Connection, dict[str, Any]]:
    connection = plant(tmp_path / "h.db")
    return connection, fhir.bundle(connection, seed.load(), CLIENT)


def resources(found: dict[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
    every = [item["resource"] for item in found["entry"]]
    return [item for item in every if kind is None or item["resourceType"] == kind]


def references(value: Any) -> list[str]:
    """Every `reference` anywhere in a JSON value."""
    if isinstance(value, dict):
        found = [value["reference"]] if isinstance(value.get("reference"), str) else []
        for item in value.values():
            found += references(item)
        return found
    if isinstance(value, list):
        return [ref for item in value for ref in references(item)]
    return []


def reference_problems(found: dict[str, Any]) -> list[str]:
    urls = [item["fullUrl"] for item in found["entry"]]
    problems = [f"duplicate fullUrl {url}" for url in set(urls) if urls.count(url) > 1]
    problems += [f"dangling {ref}" for ref in references(found) if ref not in urls]
    return problems


def binding_problems(found: dict[str, Any]) -> list[str]:
    problems = []
    if found["type"] not in REQUIRED[("Bundle", "type")]:
        problems.append(f"Bundle.type {found['type']}")
    for item in resources(found):
        # A code that is present must be allowed; a missing 1..1 element fails the validator.
        for (kind, field), allowed in REQUIRED.items():
            if item["resourceType"] == kind and field in item and item[field] not in allowed:
                problems.append(f"{kind}.{field} {item.get(field)!r}")
        for link in item.get("relatesTo", []):
            if link["code"] not in RELATES_TO:
                problems.append(f"relatesTo.code {link['code']!r}")
    return problems


def labelled(meta: dict[str, Any]) -> bool:
    return any((code["system"], code["code"]) == HTEST for code in meta.get("security", []))


def by_id(found: dict[str, Any], kind: str, ident: str) -> dict[str, Any]:
    return next(item for item in found["entry"] if item["resource"]["resourceType"] == kind
                and item["resource"]["id"] == ident)  # fmt: skip


def test_the_bundle_is_valid_fhir_and_a_broken_resource_is_caught(
    exported: tuple[sqlite3.Connection, dict[str, Any]],
) -> None:
    _, found = exported
    Bundle.model_validate(found)
    broken = copy.deepcopy(found)
    del resources(broken, "Encounter")[0]["status"]  # 1..1 in the specification
    with pytest.raises(pydantic.ValidationError):
        Bundle.model_validate(broken)


def test_every_code_with_a_required_binding_is_allowed(
    exported: tuple[sqlite3.Connection, dict[str, Any]],
) -> None:
    _, found = exported
    assert binding_problems(found) == []
    broken = copy.deepcopy(found)
    resources(broken, "Flag")[0]["status"] = "maybe"
    assert binding_problems(broken) == ["Flag.status 'maybe'"]


def test_every_reference_resolves_to_one_entry(
    exported: tuple[sqlite3.Connection, dict[str, Any]],
) -> None:
    _, found = exported
    assert reference_problems(found) == []
    broken = copy.deepcopy(found)
    resources(broken, "Flag")[0]["subject"] = {"reference": "urn:uuid:" + str(uuid.uuid4())}
    assert len(reference_problems(broken)) == 1


def test_the_records_match_the_database(
    exported: tuple[sqlite3.Connection, dict[str, Any]],
) -> None:
    connection, found = exported
    notes = {row["id"]: row for row in connection.execute(
        "SELECT id, visit, carer, body FROM notes WHERE client = ?", (CLIENT,)
    )}  # fmt: skip
    documents = {item["id"]: item for item in resources(found, "DocumentReference")}
    assert set(documents) == set(notes)
    for ident, item in documents.items():
        text = base64.b64decode(item["content"][0]["attachment"]["data"]).decode("utf-8")
        assert text == notes[ident]["body"]
    assert {item["id"] for item in resources(found, "Encounter")} == {
        row["visit"] for row in notes.values()
    }
    practitioners = resources(found, "Practitioner")
    carers = {item["id"] for item in practitioners if "gender" in item}
    assert carers == {row["carer"] for row in notes.values()}
    reviewers = {item["name"][0]["text"] for item in practitioners if "name" in item}
    assert reviewers == {"lead.one", "lead_two"}
    assert [item["id"] for item in resources(found, "Patient")] == [CLIENT]


def test_nothing_from_another_client_is_exported(
    exported: tuple[sqlite3.Connection, dict[str, Any]],
) -> None:
    connection, found = exported
    patient = by_id(found, "Patient", CLIENT)["fullUrl"]
    for item in resources(found):
        for field in ("subject", "patient"):
            if field in item:
                assert item[field]["reference"] == patient
    text = fhir.dumps(found)
    others = connection.execute(
        "SELECT n.id, c.name FROM notes n JOIN clients c ON c.id = n.client WHERE n.client != ?",
        (CLIENT,),
    ).fetchall()
    assert others
    for row in others:
        assert row["id"] not in text
        assert row["name"] not in text


def test_only_a_persons_newest_decisions_become_flags(
    exported: tuple[sqlite3.Connection, dict[str, Any]],
) -> None:
    _, found = exported
    flags = set()
    for item in resources(found, "Flag"):
        note = item["extension"][0]["valueReference"]["reference"]
        ident = next(entry["resource"]["id"] for entry in found["entry"]
                     if entry["fullUrl"] == note)  # fmt: skip
        category = item["category"][0]["coding"][0]["code"] if "category" in item else None
        flags.add((ident, item["code"]["text"], category))
    assert flags == EXPECTED_FLAGS


def test_no_model_text_or_score_is_exported(
    exported: tuple[sqlite3.Connection, dict[str, Any]],
) -> None:
    connection, found = exported
    text = fhir.dumps(found)
    rows = connection.execute(
        "SELECT f.reason, f.quote FROM findings f JOIN notes n ON n.id = f.note WHERE n.client = ?",
        (CLIENT,),
    ).fetchall()
    assert rows
    for row in rows:
        assert row["reason"] not in text
        assert row["quote"] not in text
    assert "tamper" not in text


def test_the_bundle_and_every_resource_say_they_are_test_data(
    exported: tuple[sqlite3.Connection, dict[str, Any]],
) -> None:
    _, found = exported
    assert labelled(found["meta"])
    for item in resources(found):
        assert labelled(item["meta"]), item["resourceType"]


def test_uk_core_profiles_are_claimed_only_where_uk_core_has_one(
    exported: tuple[sqlite3.Connection, dict[str, Any]],
) -> None:
    _, found = exported
    base = "https://fhir.hl7.org.uk/StructureDefinition/UKCore-"
    for item in resources(found):
        kind = item["resourceType"]
        if kind in ("Patient", "Practitioner", "Encounter"):
            assert item["meta"]["profile"] == [base + kind]
        else:
            assert "profile" not in item["meta"]


def test_visit_times_carry_the_uk_offset_of_their_day(
    exported: tuple[sqlite3.Connection, dict[str, Any]],
) -> None:
    _, found = exported
    # V03000 is planned for 07:50 on day 21 (2 Nov, after British Summer Time) for 45 minutes.
    period = by_id(found, "Encounter", "V03000")["resource"]["period"]
    assert period == {"start": "2026-11-02T07:50:00+00:00", "end": "2026-11-02T08:35:00+00:00"}
    # Day 12 is Saturday 24 October, the last day of British Summer Time in 2026.
    assert fhir.local_time("2026-10-12", 12, 9 * 60) == "2026-10-24T09:00:00+01:00"


def test_an_addition_appends_to_its_note_and_the_export_repeats_exactly(
    exported: tuple[sqlite3.Connection, dict[str, Any]],
) -> None:
    connection, found = exported
    addition = by_id(found, "DocumentReference", ADDITION)["resource"]
    first = by_id(found, "DocumentReference", "demo-week-0651")["fullUrl"]
    assert addition["relatesTo"] == [{"code": "appends", "target": {"reference": first}}]
    assert addition["date"] == "2026-11-08T12:00:00+00:00"
    assert "date" not in by_id(found, "DocumentReference", "demo-week-0651")["resource"]
    again = fhir.bundle(connection, seed.load(), CLIENT)
    assert fhir.dumps(again) == fhir.dumps(found)


def accounts(monkeypatch: pytest.MonkeyPatch, folder: Path) -> bytes:
    key = bytes(range(32))
    monkeypatch.setattr(app_main, "ACCOUNTS_FOLDER", folder)
    monkeypatch.setenv("SIGN_IN", "accounts")
    monkeypatch.setenv("AUDIT_KEY", key.hex())
    return key


def test_the_command_writes_the_file_and_audits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = accounts(monkeypatch, tmp_path)
    plant(tmp_path / "homecare.db").close()
    out = tmp_path / "c45.json"
    cli.main(["fhir", "export", "--client", CLIENT, "--out", str(out)])
    data = out.read_bytes()
    connection = db.connect(tmp_path / "homecare.db")
    assert json.loads(data) == fhir.bundle(connection, seed.load(), CLIENT)
    chain = auditlog.chain(connection)
    assert [link["entry"]["event"] for link in chain] == ["fhir export"]
    entry = chain[0]["entry"]
    assert entry["client"] == CLIENT
    assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    assert entry["resources"] == len(json.loads(data)["entry"])
    assert auditlog.problems(connection, key, tmp_path / "audit.anchor") == []


def test_the_command_refuses_demo_mode_and_an_unknown_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out.json"
    monkeypatch.setenv("SIGN_IN", "demo")
    with pytest.raises(SystemExit, match="SIGN_IN=accounts"):
        cli.main(["fhir", "export", "--client", CLIENT, "--out", str(out)])
    accounts(monkeypatch, tmp_path)
    with pytest.raises(SystemExit, match="no client 'C99'"):
        cli.main(["fhir", "export", "--client", "C99", "--out", str(out)])
    assert not out.exists()
    connection = db.connect(tmp_path / "homecare.db")
    assert auditlog.chain(connection) == []
