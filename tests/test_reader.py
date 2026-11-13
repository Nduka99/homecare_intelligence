"""The laptop's model reading app notes (`homecare platform read`), with a stand-in model and the
real v2 extraction: a concern found moves the note from "not yet read" into the register; a
quote not in the note is dropped; a failed read leaves the note listed as unread. Each test
was checked by breaking its protection on purpose."""

import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from homecare import llm
from homecare.app import db, reader
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
    return reader.read_all(connection, lambda text: extract.extract("http://model", text), HEADER)


def test_a_concern_found_moves_the_note_into_the_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, note = written(tmp_path, f"Lunch made. {CONCERN} Left her comfortable.")
    sign_in(client, "lead")
    before = client.get(f"/queue?day={LAST_DAY}").text
    assert "Not yet read by the model" in before
    monkeypatch.setattr(llm, "chat", stand_in("fresh bruise on her upper arm"))
    assert read(tmp_path) == {"read": [note], "failed": []}
    after = client.get(f"/queue?day={LAST_DAY}").text
    assert "Not yet read by the model" not in after
    act_today = after.split("Act today", 1)[1].split("</section>", 1)[0]
    assert f"/notes/{note}" in act_today
    page = client.get(f"/notes/{note}").text
    assert "<mark>fresh bruise on her upper arm</mark>" in page and "stand-in" in page


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
    unread_section = register.split("Not yet read by the model", 1)[1]
    assert re.search(f"/notes/{note}", unread_section)
