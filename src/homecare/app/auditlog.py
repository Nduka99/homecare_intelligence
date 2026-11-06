"""Where the platform keeps its audit log, so that any edit to it can be detected. The records
live in the database; the signing key and the newest MAC (the anchor) live outside it. The
chain itself is `homecare.audit`.

Entries hold IDs and labels only, never the text of a note. Appends are serialized by a lock, so
the anchor file always names the newest record.
"""

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from homecare import audit

LOCK = threading.Lock()


def chain(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT entry, mac FROM audit ORDER BY position").fetchall()
    return [{"entry": json.loads(row["entry"]), "mac": row["mac"]} for row in rows]


def record(connection: sqlite3.Connection, key: bytes, anchor: Path, entry: dict[str, Any]) -> None:
    with LOCK:
        last = connection.execute("SELECT mac FROM audit ORDER BY position DESC LIMIT 1").fetchone()
        mac = audit.mac(key, last["mac"] if last else audit.GENESIS, entry)
        connection.execute(
            "INSERT INTO audit (entry, mac) VALUES (?, ?)", (json.dumps(entry, sort_keys=True), mac)
        )
        connection.commit()
        anchor.write_text(mac, encoding="utf-8")


def problems(connection: sqlite3.Connection, key: bytes, anchor: Path) -> list[str]:
    """Why the stored chain cannot be trusted; an empty chain needs no anchor yet."""
    found = chain(connection)
    if not found and not anchor.exists():
        return []
    stored = anchor.read_text(encoding="utf-8").strip() if anchor.exists() else ""
    return audit.problems(found, key, stored)
