"""The laptop's model reading app notes (`homecare platform read`), with a stand-in model and the
real v2 extraction: a concern found moves the note from "not yet read" into the register; a
quote not in the note is dropped; a failed read leaves the note listed as unread. Each test
was checked by breaking its protection on purpose."""

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from homecare import llm
from homecare.app import auditlog, db, reader
from homecare.notes import extract
from test_screens import LAST_DAY, csrf_of, make, sign_in, visit_to_write

HEADER = {"model": "stand-in", "sha256": "0" * 64, "prompt": extract.PROMPT_HASH,
          "pipeline": extract.PIPELINE}  # fmt: skip
CONCERN = "There was a fresh bruise on her upper arm that she would not explain."


def written(tmp_path: Path, body: str) -> tuple[Any, str]:
    """A demo app with one app note from a carer; the client and the note's ID."""
    client, _ = make(tmp_path)
    connection = db.connect(tmp_path / "demo.db")
    visit = visit_to_write(tmp_path)
    sign_in(client, visit["carer"])
    token = csrf_of(client.get(f"/visits/{visit['id']}/note").text)
    fields = {"csrf": token, "key": str(uuid.uuid4()), "body": body, "amends": ""}
    client.post(f"/visits/{visit['id']}/notes", data=fields)
    note = connection.execute("SELECT id FROM notes WHERE source = 'app'").fetchone()["id"]
    return client, note


def stand_in(quote: str) -> Any:
    """A model that reports physical abuse with `quote` whenever the text mentions a bruise."""

    def chat(url: str, system: str, user: str, schema: object, **options: Any) -> object:
        found = [{"category": "physical abuse", "quote": quote, "reason": "unexplained bruise"}]
        return {"findings": found if "bruise" in user else []}

    return chat


def read(tmp_path: Path) -> dict[str, list[str]]:
    connection = db.connect(tmp_path / "demo.db")

    def model(text: str) -> dict[str, Any]:
        return extract.extract("http://model", text)

    return reader.read_all(connection, model, HEADER, lambda note: None)


def test_a_concern_found_moves_the_note_into_the_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, note = written(tmp_path, f"Lunch made. {CONCERN} Left her comfortable.")
    sign_in(client, "lead")
    before = client.get(f"/queue?day={LAST_DAY}").text
    assert "Not checked by AI yet" in before
    monkeypatch.setattr(llm, "chat", stand_in("fresh bruise on her upper arm"))
    assert read(tmp_path) == {"read": [note], "failed": []}
    after = client.get(f"/queue?day={LAST_DAY}").text
    assert "Not checked by AI yet" not in after
    act_today = after.split("Act today", 1)[1].split("</section>", 1)[0]
    assert f"/notes/{note}" in act_today
    page = client.get(f"/notes/{note}").text
    assert "<mark>fresh bruise on her upper arm</mark>" in page and "stand-in" not in page
    # Which model read the note is kept with the reading for audit, never shown on the screen.
    connection = db.connect(tmp_path / "demo.db")
    stored = connection.execute("SELECT extraction FROM readings WHERE note = ?", (note,))
    assert json.loads(stored.fetchone()["extraction"])["model"] == "stand-in"


def test_a_quote_not_in_the_note_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, note = written(tmp_path, f"Tea made. {CONCERN}")
    monkeypatch.setattr(llm, "chat", stand_in("she said her son hit her"))
    assert read(tmp_path)["read"] == [note]
    connection = db.connect(tmp_path / "demo.db")
    assert (
        connection.execute("SELECT COUNT(*) FROM findings WHERE note = ?", (note,)).fetchone()[0]
        == 0
    )


def test_a_failed_read_leaves_the_note_unread_and_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, note = written(tmp_path, f"Breakfast. {CONCERN}")

    def broken(*args: Any, **options: Any) -> object:
        raise llm.ModelServerError("the model server stopped")

    monkeypatch.setattr(llm, "chat", broken)
    assert read(tmp_path) == {"read": [], "failed": [note]}
    sign_in(client, "lead")
    register = client.get(f"/queue?day={LAST_DAY}").text
    unread_section = register.split("Not checked by AI yet", 1)[1]
    assert re.search(f"/notes/{note}", unread_section)


def test_each_model_call_carries_one_note_and_each_read_is_logged_before_the_next(
    tmp_path: Path,
) -> None:
    """The model is given one note's text per call, never two, and each read is stored with its
    audit entry before the next note is read, so a batch stopped part-way leaves no read
    unlogged."""
    client, settings = make(tmp_path)
    for number in range(3):
        visit = visit_to_write(tmp_path)
        sign_in(client, visit["carer"])
        token = csrf_of(client.get(f"/visits/{visit['id']}/note").text)
        fields = {"csrf": token, "key": str(uuid.uuid4()), "body": f"Visit {number}: all well.",
                  "amends": ""}  # fmt: skip
        client.post(f"/visits/{visit['id']}/notes", data=fields)
    connection = db.connect(tmp_path / "demo.db")
    bodies = [row["body"] for row in reader.unread(connection)]
    assert len(bodies) == 3
    calls: list[tuple[str, int]] = []

    def model_reads_logged() -> int:
        committed = db.connect(tmp_path / "demo.db")  # what another process would see
        count = sum(
            1 for link in auditlog.chain(committed) if link["entry"]["event"] == "model read"
        )
        committed.close()
        return count

    def model(text: str) -> dict[str, Any]:
        calls.append((text, model_reads_logged()))
        return {"findings": []}

    def logged(note: str) -> None:
        entry = {"event": "model read", "user": "command line", "note": note}
        auditlog.record(connection, settings["key"], settings["anchor"], entry)

    done = reader.read_all(connection, model, HEADER, logged)
    assert [text for text, _ in calls] == bodies
    assert [count for _, count in calls] == [0, 1, 2]
    assert model_reads_logged() == 3 and len(done["read"]) == 3


def test_a_read_whose_audit_entry_fails_is_not_stored(tmp_path: Path) -> None:
    _, note = written(tmp_path, "Lunch made; all well.")
    connection = db.connect(tmp_path / "demo.db")

    def broken(note: str) -> None:
        raise RuntimeError("the audit log could not be written")

    with pytest.raises(RuntimeError):
        reader.read_all(connection, lambda text: {"findings": []}, HEADER, broken)
    connection.close()
    assert [row["id"] for row in reader.unread(db.connect(tmp_path / "demo.db"))] == [note]
