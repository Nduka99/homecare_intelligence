"""Where the platform keeps its audit log, so that any edit to it can be detected. The records
live in the database; the signing key and the newest MAC (the anchor) live outside it. The
chain itself is `homecare.audit`.

Entries hold IDs and labels only, never the text of a note. An append holds the app's one write
lock (`db.WRITE`) and SQLite's write lock from reading the newest MAC to writing the anchor, so
appends never overlap, even from two processes, and the anchor file always names the newest
record. When a request logs part-way through its own write, it already holds both locks, and the
append joins its transaction.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

from homecare import audit
from homecare.app import db


def chain(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT entry, mac FROM audit ORDER BY position").fetchall()
    return [{"entry": json.loads(row["entry"]), "mac": row["mac"]} for row in rows]


def record(connection: sqlite3.Connection, key: bytes, anchor: Path, entry: dict[str, Any]) -> None:
    """Append one entry. `db.WRITE` orders appends within this process; SQLite's write lock
    orders them across processes (a command-line tool appending while the server runs). That
    lock is taken before the newest MAC is read and held until the commit, and the anchor is
    written inside it, so two appends can never chain onto the same record or leave the anchor
    on an older one. A request already part-way through its own write holds that lock already."""
    with db.WRITE:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        last = connection.execute("SELECT mac FROM audit ORDER BY position DESC LIMIT 1").fetchone()
        mac = audit.mac(key, last["mac"] if last else audit.GENESIS, entry)
        connection.execute(
            "INSERT INTO audit (entry, mac) VALUES (?, ?)", (json.dumps(entry, sort_keys=True), mac)
        )
        before = anchor.read_text(encoding="utf-8") if anchor.exists() else None
        anchor.write_text(mac, encoding="utf-8")
        try:
            connection.commit()
        except sqlite3.Error:
            # The record is not stored, so the anchor goes back to the newest one that is.
            connection.rollback()
            if before is None:
                anchor.unlink()
            else:
                anchor.write_text(before, encoding="utf-8")
            raise


def problems(connection: sqlite3.Connection, key: bytes, anchor: Path) -> list[str]:
    """Why the stored chain cannot be trusted; an empty chain needs no anchor yet."""
    found = chain(connection)
    if not found and not anchor.exists():
        return []
    stored = anchor.read_text(encoding="utf-8").strip() if anchor.exists() else ""
    return audit.problems(found, key, stored)
