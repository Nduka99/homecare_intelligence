"""Store a checked visit export in the platform database.

`homecare platform import FILE` does this on the laptop; demo mode imports the committed SYNTHETIC
sample at start through the same function. A refused file stores nothing. The same file twice
(the same SHA-256) adds nothing.
"""

import datetime
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from homecare.imports import validate

SAMPLE = Path(__file__).parent / "data" / "visit-export.csv"
SAMPLE_PIN = SAMPLE.with_name("visit-export.csv.sha256")


def reference(seed: dict[str, Any]) -> dict[str, Any]:
    """The plan an export is checked against: the seed's planned visits, clients and carers."""
    start = datetime.date.fromisoformat(seed["start"])
    visits = {}
    for visit in seed["visits"]:
        if visit["carer"] is not None:
            visits[visit["id"]] = {
                "client": visit["client"],
                "carer": visit["carer"],
                "date": (start + datetime.timedelta(days=visit["day"])).isoformat(),
                "starts": visit["starts"],
                "duration": visit["duration"],
            }
    return {
        "visits": visits,
        "clients": {client["id"] for client in seed["clients"]},
        "carers": {carer["id"] for carer in seed["carers"]},
    }


def sample() -> bytes:
    """The committed SYNTHETIC export, after checking it against its pin."""
    data = SAMPLE.read_bytes()
    if hashlib.sha256(data).hexdigest() != SAMPLE_PIN.read_text(encoding="utf-8").strip():
        raise SystemExit(f"{SAMPLE.name} differs from its pin: the app will not start")
    return data


def store(
    connection: sqlite3.Connection, data: bytes, name: str, by: str, reference: dict[str, Any]
) -> dict[str, Any]:
    """Check and store one export; return its counts. Raises validate.Refused for a file that
    cannot be used at all, before anything is stored."""
    digest = hashlib.sha256(data).hexdigest()
    earlier = connection.execute("SELECT * FROM imports WHERE sha256 = ?", (digest,)).fetchone()
    if earlier is not None:
        return {**dict(earlier), "new": False}
    rows, blank = validate.read(data)
    result = validate.check(rows, reference)
    flagged = sum(1 for row in result["kept"] if row["flags"])
    cursor = connection.execute(
        "INSERT INTO imports (sha256, name, imported, by, kept, quarantined, flagged, blank) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (digest, name, time.time(), by, len(result["kept"]), len(result["quarantined"]),
         flagged, blank),
    )  # fmt: skip
    number = cursor.lastrowid
    for row in result["kept"]:
        connection.execute(
            "INSERT INTO actuals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (number, row["line"], row["visit"], row["carer"], row["date"], row["check_in"],
             row["check_out"], row["note"], json.dumps(row["flags"])),
        )  # fmt: skip
    for row in result["quarantined"]:
        connection.execute(
            "INSERT INTO quarantine VALUES (?, ?, ?, ?, ?)",
            (number, row["line"], json.dumps(row["reasons"]), json.dumps(row["related"]),
             json.dumps(row["cells"], ensure_ascii=False)),
        )  # fmt: skip
    connection.commit()
    found = connection.execute("SELECT * FROM imports WHERE id = ?", (number,)).fetchone()
    return {**dict(found), "new": True}
