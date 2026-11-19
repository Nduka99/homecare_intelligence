"""Have the laptop's model read the app notes it has not read yet (`homecare platform read`).

Each note goes through the same v2 extraction as the demo week: every quote checked against the
note, the band from the category in code. The model is given one note per call, and code chooses
which. Its findings are stored like the seed's, with a reading that records which model, prompt
and pipeline read it, and its audit entry, in one write before the next note is read. A read that
fails leaves the note unread, so it stays listed for a person and is tried again next time. The
model runs only on the laptop, outside the serving container, which never reads a note.
"""

import json
import sqlite3
import time
from collections.abc import Callable
from typing import Any


def unread(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT n.id, n.body FROM notes n WHERE n.source = 'app' AND NOT EXISTS "
        "(SELECT 1 FROM readings r WHERE r.note = n.id) ORDER BY n.written"
    ).fetchall()


def store(
    connection: sqlite3.Connection, note: str, result: dict[str, Any], header: dict[str, Any]
) -> None:
    """One note's findings and its reading, in the caller's open write."""
    for position, finding in enumerate(result["findings"]):
        connection.execute(
            "INSERT INTO findings (note, position, category, band, quote, reason, found_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (note, position, finding["category"], finding["band"], finding["quote"],
             finding["reason"], finding["read"]),
        )  # fmt: skip
    connection.execute(
        "INSERT INTO readings (note, extraction, read) VALUES (?, ?, ?)",
        (note, json.dumps(header, sort_keys=True), time.time()),
    )


def read_all(
    connection: sqlite3.Connection,
    extract: Callable[[str], dict[str, Any]],
    header: dict[str, Any],
    logged: Callable[[str], None],
) -> dict[str, list[str]]:
    """Read every unread app note, one per call; return the notes read and the notes whose read
    failed. `logged` writes a read's audit entry, committed with its findings and reading."""
    done: dict[str, list[str]] = {"read": [], "failed": []}
    for note in unread(connection):
        result = extract(note["body"])
        if result.get("error"):
            done["failed"].append(note["id"])
            continue
        store(connection, note["id"], result, header)
        logged(note["id"])
        connection.commit()
        done["read"].append(note["id"])
    return done
