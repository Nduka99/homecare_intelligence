"""The FHIR export checked by HL7's own validator against FHIR R4 and UK Core 2.0.2, offline.

Laptop only: it needs the pinned validator and Java runtime from
`uv run homecare fhir install-validator` (Windows), and is skipped where they are absent. One
validator run reads four files: the export, and three controls that show the check can fail.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from homecare import fhir, fhir_check
from homecare.app import seed
from test_fhir import CLIENT, plant, resources

pytestmark = pytest.mark.skipif(
    not fhir_check.installed(), reason="run `uv run homecare fhir install-validator` first"
)
# The only messages the export may raise, each read: no resource carries a human-readable
# narrative (a best-practice warning), and without a terminology server the validator cannot look
# up the notes' MIME type.
EXPECTED = (
    "Constraint failed: dom-6: 'A resource should have narrative",
    "The value provided ('text/plain; charset=utf-8') could not be validated",
)
# Two NHS numbers: base FHIR allows any number of identifiers, UK Core at most one NHS number.
TWO_NHS_NUMBERS = {
    "resourceType": "Patient",
    "identifier": [
        {"system": "https://fhir.nhs.uk/Id/nhs-number", "value": "9000000009"},
        {"system": "https://fhir.nhs.uk/Id/nhs-number", "value": "9000000017"},
    ],
}


def write(folder: Path, name: str, value: dict[str, Any]) -> Path:
    path = folder / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[dict[str, str]]]:
    folder = tmp_path_factory.mktemp("fhir")
    found = fhir.bundle(plant(folder / "h.db"), seed.load(), CLIENT, None)
    broken = copy.deepcopy(found)
    del resources(broken, "Encounter")[0]["status"]
    claimed = TWO_NHS_NUMBERS | {"meta": {"profile": [fhir.UK_CORE["Patient"]]}}
    paths = [
        write(folder, "export.json", found),
        write(folder, "no-status.json", broken),
        write(folder, "two-nhs-numbers-uk-core.json", claimed),
        write(folder, "two-nhs-numbers-base.json", TWO_NHS_NUMBERS),
    ]
    by_file: dict[str, list[dict[str, str]]] = {path.name: [] for path in paths}
    for issue in fhir_check.validate(paths):
        by_file[issue["file"]].append(issue)
    return by_file


def errors(found: list[dict[str, str]]) -> list[dict[str, str]]:
    return [issue for issue in found if issue["severity"] in ("error", "fatal")]


def test_the_export_meets_fhir_r4_and_uk_core(results: dict[str, list[dict[str, str]]]) -> None:
    assert errors(results["export.json"]) == []
    for issue in results["export.json"]:
        assert issue["message"].startswith(EXPECTED), issue


def test_the_validator_catches_a_missing_required_element(
    results: dict[str, list[dict[str, str]]],
) -> None:
    assert any("status" in issue["message"] for issue in errors(results["no-status.json"]))


def test_uk_core_is_applied_only_where_it_is_claimed(
    results: dict[str, list[dict[str, str]]],
) -> None:
    assert errors(results["two-nhs-numbers-uk-core.json"])
    assert errors(results["two-nhs-numbers-base.json"]) == []
