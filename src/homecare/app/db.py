"""The platform's SQLite database: plain SQL migrations, and the demo week loaded from the seed.

One connection per request (`connect`); SQLite opens in well under a millisecond. Lists are
stored as JSON text. Every query passes its values as parameters, never in the SQL text.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

MIGRATIONS = Path(__file__).parent / "migrations"


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    """Apply each `NNN_*.sql` file newer than the database's `user_version`, in order."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    for path in sorted(MIGRATIONS.glob("*.sql")):
        number = int(path.name.split("_")[0])
        if number > version:
            connection.executescript(path.read_text(encoding="utf-8"))
            connection.execute(f"PRAGMA user_version = {number}")  # an int from a file name
    connection.commit()


def load_seed(connection: sqlite3.Connection, seed: dict[str, Any]) -> None:
    """Load the demo week into an empty database; a database that has notes is left alone."""
    if connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]:
        return
    for client in seed["clients"]:
        connection.execute(
            "INSERT INTO clients VALUES (?, ?, ?, ?, ?)",
            (client["id"], client["name"], client["details"], client["group"],
             json.dumps(client["needs"])),
        )  # fmt: skip
    for carer in seed["carers"]:
        connection.execute(
            "INSERT INTO carers VALUES (?, ?, ?, ?, ?)",
            (carer["id"], carer["gender"], json.dumps(carer["skills"]), carer["contract"],
             json.dumps(carer["days_off"])),
        )  # fmt: skip
    for visit in seed["visits"]:
        connection.execute(
            "INSERT INTO visits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (visit["id"], visit["client"], visit["day"], visit["slot"], visit["open"],
             visit["close"], visit["duration"], visit["carer"], visit["position"]),
        )  # fmt: skip
    for note in seed["notes"]:
        connection.execute(
            "INSERT INTO notes (id, visit, client, carer, day, body, tamper, tamper_probability)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (note["id"], note["visit"], note["client"], note["carer"], note["day"], note["text"],
             int(note["tamper"]), note["tamper_probability"]),
        )  # fmt: skip
        for position, finding in enumerate(note["findings"]):
            connection.execute(
                "INSERT INTO findings (note, position, category, band, quote, reason, found_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (note["id"], position, finding["category"], finding["band"], finding["quote"],
                 finding["reason"], finding["read"]),
            )  # fmt: skip
    connection.commit()


def prepare(path: Path, seed: dict[str, Any]) -> None:
    """Create or migrate the database at `path` and load the demo week if it is empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        migrate(connection)
        load_seed(connection, seed)
    finally:
        connection.close()
