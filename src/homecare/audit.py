"""The audit log as a hash chain, so that any edit, deletion or rewrite is detected.

Each record's MAC is HMAC-SHA256(key, previous MAC + the entry as canonical JSON). The key is
held outside the database, so someone who can only write to the database cannot build a chain
that verifies. The newest MAC (the anchor) is also kept outside the database, so deleting the
newest records is caught too. This module is storage-free: the platform stores the records and
the anchor.
"""

import hashlib
import hmac
import json
from typing import Any

GENESIS = "0" * 64


def canonical(entry: dict[str, Any]) -> bytes:
    """One byte string per entry, whatever the order of its keys."""
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def mac(key: bytes, previous: str, entry: dict[str, Any]) -> str:
    return hmac.new(key, previous.encode() + canonical(entry), hashlib.sha256).hexdigest()


def append(chain: list[dict[str, Any]], entry: dict[str, Any], key: bytes) -> dict[str, Any]:
    """Add an entry to the chain; the new record's MAC is the new anchor."""
    previous = chain[-1]["mac"] if chain else GENESIS
    record = {"entry": entry, "mac": mac(key, previous, entry)}
    chain.append(record)
    return record


def problems(chain: list[dict[str, Any]], key: bytes, anchor: str) -> list[str]:
    """Why the chain cannot be trusted: the first record that does not follow from the one
    before it, or a newest record that is not the anchored one."""
    previous = GENESIS
    for index, record in enumerate(chain):
        if not hmac.compare_digest(record["mac"], mac(key, previous, record["entry"])):
            return [f"record {index} was changed, or the chain was rebuilt without the key"]
        previous = record["mac"]
    if not hmac.compare_digest(previous, anchor):
        return ["the newest record is not the anchored one: records were removed or added"]
    return []
