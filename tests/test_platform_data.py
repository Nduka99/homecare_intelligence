"""The platform's data: the seed is stable and pinned, and notes are append-only."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from homecare.app import db, seed


def tiny_inputs() -> tuple[Any, ...]:
    world = {
        "start": "2026-10-12",
        "clients": [
            {"id": "C01", "name": "Mrs A Test", "details": "80, frailty", "group": "general",
             "needs": ["personal care"], "node": "123"},
        ],
        "carers": [
            {"id": "K01", "gender": "female", "skills": ["personal care"], "contract": "full-time",
             "days_off": [5, 6], "home": "456"},
        ],
        "visits": [
            {"id": "V1", "client": "C01", "day": 0, "slot": "morning", "open": 420, "close": 600,
             "duration": 45},
            {"id": "V2", "client": "C01", "day": 21, "slot": "morning", "open": 420, "close": 600,
             "duration": 45},
        ],
    }  # fmt: skip
    rotas = [{"routes": {"K01": ["V2"]}, "uncovered": []}]
    notes = [
        {"id": "n1", "visit_id": "V2", "client_id": "C01", "carer_id": "K01", "day": 21,
         "text": "Morning visit.", "labels": ["missed task"], "writer": "w", "writer_sha256": "s"},
    ]  # fmt: skip
    run = [{"header": {"model": "m"}}, {"id": "n1", "dropped": 0, "findings": []}]
    tamper = {"sha256": "t", "probabilities": {"n1": 0.9}}
    return world, rotas, [21], notes, run, tamper


def test_the_seed_is_stable_and_leaves_out_labels_and_places() -> None:
    first = seed.dumps(seed.build(*tiny_inputs()))
    assert first == seed.dumps(seed.build(*tiny_inputs()))
    built = seed.build(*tiny_inputs())
    assert built["synthetic"] is True
    assert [v["id"] for v in built["visits"]] == ["V2"]  # only the demo days
    assert built["visits"][0]["carer"] == "K01"
    assert built["notes"][0]["tamper"] is True
    for banned in ('"labels"', '"node"', '"home"'):
        assert banned not in first


def test_a_changed_seed_stops_the_app(tmp_path: Path) -> None:
    """One changed character in the seed, and loading refuses."""
    text = seed.dumps(seed.build(*tiny_inputs()))
    (tmp_path / "demo.json").write_text(text, encoding="utf-8")
    (tmp_path / "demo.json.sha256").write_text(seed.sha256(text), encoding="utf-8")
    assert seed.load(tmp_path / "demo.json", tmp_path / "demo.json.sha256")["synthetic"]
    (tmp_path / "demo.json").write_text(text.replace("Morning", "Evening"), encoding="utf-8")
    with pytest.raises(SystemExit, match="differs from its pin"):
        seed.load(tmp_path / "demo.json", tmp_path / "demo.json.sha256")


def test_the_committed_seed_matches_its_pin_and_says_synthetic() -> None:
    loaded = seed.load()
    assert loaded["synthetic"] is True
    assert len(loaded["notes"]) == 910


def test_the_database_loads_the_seed_once(tmp_path: Path) -> None:
    built = seed.build(*tiny_inputs())
    db.prepare(tmp_path / "h.db", built)
    db.prepare(tmp_path / "h.db", built)
    connection = db.connect(tmp_path / "h.db")
    assert connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_notes_cannot_be_edited_or_deleted(tmp_path: Path) -> None:
    """An amendment must be a new entry; the database refuses to change or drop a note."""
    db.prepare(tmp_path / "h.db", seed.build(*tiny_inputs()))
    connection = db.connect(tmp_path / "h.db")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE notes SET body = 'nothing happened' WHERE id = 'n1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM notes WHERE id = 'n1'")
