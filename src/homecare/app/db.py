"""The platform's SQLite database: plain SQL migrations, and the demo week loaded from the seed.

One connection per request (`connect`); SQLite opens in well under a millisecond. Lists are
stored as JSON text. Every query passes its values as parameters, never in the SQL text.
"""

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

MIGRATIONS = Path(__file__).parent / "migrations"
# How long a write waits for another to finish before giving up (Python's default, stated).
BUSY_SECONDS = 5.0
# One write at a time in this process: every write holds this from its first change to its
# commit. SQLite allows one writer, and a writer that finds its lock taken polls for it with no
# queue, so under load some waited out the whole busy timeout; waiting here queues them instead
# (one process serves the app). Reentrant, as the audit log's append runs inside a request's
# write.
WRITE = threading.RLock()


def connect(path: Path, budget: int | None = None) -> sqlite3.Connection:
    """A connection to the database at `path`. With a `budget` in bytes, a write that would grow
    the file past it fails with SQLITE_FULL and its transaction is rolled back whole. The cap is
    a setting of each connection (`max_page_count`), so every connection must be given it."""
    connection = sqlite3.connect(path, timeout=BUSY_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if budget is not None:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        connection.execute(f"PRAGMA max_page_count = {budget // page_size}")  # ints only
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


def load_referrals(connection: sqlite3.Connection, data: dict[str, Any]) -> None:
    """Load the demo referrals and their reads into an empty table; a filled one is left alone."""
    if connection.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]:
        return
    for referral in data["referrals"]:
        connection.execute(
            "INSERT INTO referrals VALUES (?, ?, ?, ?, ?)",
            (referral["id"], referral["name"], referral["text"], json.dumps(referral["claims"]),
             json.dumps(referral["dropped"])),
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
