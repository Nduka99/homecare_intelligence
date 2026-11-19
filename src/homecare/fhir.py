"""One client's record as a FHIR R4 Bundle of plain JSON, for the two-person export (`/exports`).

Only records a person made leave the system: the client, the carers who wrote notes, the visits
those notes belong to, the notes themselves, and the concerns a reviewer confirmed. A model
finding becomes a Flag only through a person's newest decision on it, and no model quote, reason
or score is exported, because the model is never trusted to write a care record.

Resource choices follow the FHIR R4 specification (https://hl7.org/fhir/R4/):
- a confirmed concern is a Flag, "a warning or notification of some sort presented to the user";
  NHS England's Patient Flags API carries its child-protection flag as a Flag too;
- a carer's note is a DocumentReference, which indexes "a document, clinical note";
- a visit is an Encounter of class HH, "home health".

Every resource and the Bundle carry HL7's security label for test data (HTEST: "simulated or
synthetic health data"), because every record here is synthetic. Entries have `urn:uuid:` full
URLs, as the specification asks for resources with no server identity. The same database always
gives the same file: the UUIDs are derived from the app's IDs, and the Bundle has no timestamp.
"""

import base64
import datetime
import hashlib
import json
import sqlite3
import uuid
import zoneinfo
from typing import Any

from homecare.app import screens
from homecare.notes.categories import SAFEGUARDING

TEST_DATA = {
    "system": "http://terminology.hl7.org/CodeSystem/v3-ActReason",
    "code": "HTEST",
    "display": "test health data",
}
HOME_HEALTH = {
    "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
    "code": "HH",
    "display": "home health",
}
# UK Core (HL7 UK, release 2.0.2 on FHIR 4.0.1) profiles three of the five resource types; it has
# no Flag or DocumentReference profile. Each claim is checked by HL7's validator.
UK_CORE = {
    "Patient": "https://fhir.hl7.org.uk/StructureDefinition/UKCore-Patient",
    "Practitioner": "https://fhir.hl7.org.uk/StructureDefinition/UKCore-Practitioner",
    "Encounter": "https://fhir.hl7.org.uk/StructureDefinition/UKCore-Encounter",
}
FLAG_CATEGORY = "http://terminology.hl7.org/CodeSystem/flag-category"
FLAG_DETAIL = "http://hl7.org/fhir/StructureDefinition/flag-detail"
DETERIORATION = "possible deterioration (soft sign)"
NAMESPACE = "homecare-intelligence"
# The agency is in Birmingham; FHIR needs a UTC offset on every time, and the demo weeks span the
# end of British Summer Time.
LONDON = zoneinfo.ZoneInfo("Europe/London")


def full_url(kind: str, ident: str) -> str:
    return "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"{NAMESPACE}/{kind}/{ident}"))


def reference(kind: str, ident: str) -> dict[str, str]:
    return {"reference": full_url(kind, ident)}


def resource(kind: str, ident: str, fields: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {"security": [dict(TEST_DATA)]}
    if kind in UK_CORE:
        meta["profile"] = [UK_CORE[kind]]
    return {"resourceType": kind, "id": ident, "meta": meta} | fields


def reviewer_id(name: str) -> str:
    """A sign-in name may hold characters a FHIR id forbids, so the id is a hash of it."""
    return "user-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def local_time(start: str, day: int, minute: int) -> str:
    """A time on a day of the synthetic week, in UK time with its offset."""
    date = screens.date_of(start, day)
    hour, rest = divmod(minute, 60)
    moment = datetime.datetime(date.year, date.month, date.day, hour, rest, tzinfo=LONDON)
    return moment.isoformat(timespec="seconds")


def instant(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, datetime.UTC).isoformat(timespec="seconds")


def patient(client: sqlite3.Row) -> dict[str, Any]:
    return resource("Patient", client["id"], {"name": [{"text": client["name"]}]})


def carer(row: sqlite3.Row) -> dict[str, Any]:
    return resource("Practitioner", row["id"], {"gender": row["gender"]})


def reviewer(name: str) -> dict[str, Any]:
    return resource("Practitioner", reviewer_id(name), {"name": [{"text": name}]})


def encounter(
    visit: sqlite3.Row, client: str, carers: list[str], start: str, planned: int
) -> dict[str, Any]:
    """A visit with a note. The synthetic visits happen as planned, so the period is the rota's."""
    fields = {
        "status": "finished",
        "class": dict(HOME_HEALTH),
        "type": [{"text": f"{screens.SLOT_NAMES[visit['slot']]} visit"}],
        "subject": reference("Patient", client),
        "participant": [{"individual": reference("Practitioner", name)} for name in carers],
        "period": {
            "start": local_time(start, visit["day"], planned),
            "end": local_time(start, visit["day"], planned + visit["duration"]),
        },
    }
    return resource("Encounter", visit["id"], fields)


def document(note: sqlite3.Row) -> dict[str, Any]:
    """A carer's note, word for word. Only a note sent from the app knows when it was written."""
    text = base64.b64encode(note["body"].encode("utf-8")).decode("ascii")
    fields: dict[str, Any] = {
        "status": "current",
        "type": {"text": "Care visit note"},
        "subject": reference("Patient", note["client"]),
        "author": [reference("Practitioner", note["carer"])],
        "content": [{"attachment": {"contentType": "text/plain; charset=utf-8", "data": text}}],
        "context": {"encounter": [reference("Encounter", note["visit"])]},
    }
    if note["written"] is not None:
        fields["date"] = instant(note["written"])
    if note["amends"] is not None:
        target = reference("DocumentReference", note["amends"])
        fields["relatesTo"] = [{"code": "appends", "target": target}]
    return resource("DocumentReference", note["id"], fields)


def concern_category(decision: sqlite3.Row) -> str | None:
    """The concern a person's decision records, or None when it records no concern."""
    if decision["label"] == "confirmed":
        return str(decision["found"])
    if decision["label"] in ("wrong category", "missed concern"):
        return str(decision["category"])
    return None


def concerns(decisions: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """One decision per confirmed concern: each finding's (and each note's) newest decision
    counts, and a decision on near-duplicate findings, stored once per finding, is one concern."""
    newest: dict[tuple[str, int | None], sqlite3.Row] = {}
    for row in decisions:  # in the order they were made, so a later one replaces an earlier one
        newest[(row["note"], row["finding"])] = row
    found: dict[tuple[str, str], sqlite3.Row] = {}
    for row in newest.values():
        category = concern_category(row)
        if category is None:
            continue
        key = (row["note"], category)
        if key not in found or row["id"] > found[key]["id"]:
            found[key] = row
    return sorted(found.values(), key=lambda row: row["id"])


def flag(decision: sqlite3.Row, client: str, visit: str) -> dict[str, Any]:
    category = concern_category(decision)
    fields: dict[str, Any] = {
        "extension": [
            {"url": FLAG_DETAIL, "valueReference": reference("DocumentReference", decision["note"])}
        ],
        "status": "active",
        "code": {"text": category},
        "subject": reference("Patient", client),
        "period": {"start": instant(decision["decided"])},
        "encounter": reference("Encounter", visit),
        "author": reference("Practitioner", reviewer_id(decision["reviewer"])),
    }
    if category in SAFEGUARDING:
        fields["category"] = [{"coding": [{"system": FLAG_CATEGORY, "code": "safety"}]}]
    elif category == DETERIORATION:
        fields["category"] = [{"coding": [{"system": FLAG_CATEGORY, "code": "clinical"}]}]
    return resource("Flag", f"flag-{decision['id']}", fields)


def entry(item: dict[str, Any]) -> dict[str, Any]:
    return {"fullUrl": full_url(item["resourceType"], item["id"]), "resource": item}


# A note or decision in view: one with no visitor (the seed's, or any in accounts mode), or the
# demo visitor's own. Each demo visitor's notes and decisions are theirs alone.
IN_VIEW = "(visitor IS NULL OR visitor = ?)"


def bundle(
    connection: sqlite3.Connection, seed: dict[str, Any], client_id: str, visitor: str | None
) -> dict[str, Any]:
    """The client's record: Patient, Practitioners, Encounters, DocumentReferences and Flags,
    from the notes and decisions in view of `visitor` (None in accounts mode)."""
    client = connection.execute(
        "SELECT id, name FROM clients WHERE id = ?", (client_id,)
    ).fetchone()
    if client is None:
        raise ValueError(f"no client {client_id!r}")
    seen = f"SELECT visit, carer FROM notes WHERE client = ? AND {IN_VIEW}"
    notes = connection.execute(
        "SELECT id, visit, client, carer, body, written, amends FROM notes WHERE client = ? "
        f"AND {IN_VIEW} ORDER BY id",
        (client_id, visitor),
    ).fetchall()
    visits = connection.execute(
        f"SELECT id, day, slot, duration FROM visits WHERE id IN (SELECT visit FROM ({seen})) "
        "ORDER BY day, id",
        (client_id, visitor),
    ).fetchall()
    carers = connection.execute(
        f"SELECT id, gender FROM carers WHERE id IN (SELECT carer FROM ({seen})) ORDER BY id",
        (client_id, visitor),
    ).fetchall()
    decisions = connection.execute(
        "SELECT d.id, d.note, d.finding, d.label, d.category, d.reviewer, d.decided, "
        "f.category AS found FROM decisions d JOIN notes n ON n.id = d.note "
        "LEFT JOIN findings f ON f.id = d.finding WHERE n.client = ? "
        "AND (n.visitor IS NULL OR n.visitor = ?) AND (d.visitor IS NULL OR d.visitor = ?) "
        "ORDER BY d.id",
        (client_id, visitor, visitor),
    ).fetchall()
    confirmed = concerns(decisions)
    planned = {visit["id"]: visit["starts"] for visit in seed["visits"]}
    visit_of = {note["id"]: note["visit"] for note in notes}
    writers: dict[str, list[str]] = {}
    for note in notes:
        names = writers.setdefault(note["visit"], [])
        if note["carer"] not in names:
            names.append(note["carer"])
    items = [patient(client)]
    items += [carer(row) for row in carers]
    items += [reviewer(name) for name in sorted({row["reviewer"] for row in confirmed})]
    for visit in visits:
        names = sorted(writers[visit["id"]])
        items.append(encounter(visit, client_id, names, seed["start"], planned[visit["id"]]))
    items += [document(note) for note in notes]
    items += [flag(row, client_id, visit_of[row["note"]]) for row in confirmed]
    return {
        "resourceType": "Bundle",
        "meta": {"security": [dict(TEST_DATA)]},
        "type": "collection",
        "entry": [entry(item) for item in items],
    }


def dumps(found: dict[str, Any]) -> str:
    return json.dumps(found, indent=1, ensure_ascii=False) + "\n"
