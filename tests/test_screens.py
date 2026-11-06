"""The platform screens on the committed SYNTHETIC demo week: who can see what, escaping, no note
text in logs, decisions stored as labels, and demo visitors kept apart. Each attack test passes
only when the attack fails, and each was checked by breaking its protection on purpose."""

import logging
import re
import secrets
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homecare.app import db, main, screens

LAST_DAY = 27


def make(tmp_path: Path) -> tuple[TestClient, dict[str, Any]]:
    settings = {
        "mode": "demo",
        "database": tmp_path / "demo.db",
        "anchor": tmp_path / "demo.anchor",
        "key": secrets.token_bytes(32),
    }
    return TestClient(main.create_app(settings), base_url="https://testserver"), settings


def csrf_of(html: str) -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', html)
    assert found
    return found.group(1)


def sign_in(client: TestClient, user: str) -> None:
    fields = {"csrf": csrf_of(client.get("/signin").text), "user": user}
    client.post("/signin", data=fields, follow_redirects=False)


def first_queued(tmp_path: Path) -> dict[str, Any]:
    connection = db.connect(tmp_path / "demo.db")
    item = screens.day_queue(connection, LAST_DAY)[0]
    note = connection.execute("SELECT * FROM notes WHERE id = ?", (item["note"],)).fetchone()
    return dict(note)


def another_carer(tmp_path: Path, carer: str) -> str:
    connection = db.connect(tmp_path / "demo.db")
    row = connection.execute("SELECT id FROM carers WHERE id != ? ORDER BY id", (carer,)).fetchone()
    return str(row["id"])


def test_the_register_has_one_row_per_note_in_review_order(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    sign_in(client, "lead")
    page = client.get(f"/queue?day={LAST_DAY}").text
    connection = db.connect(tmp_path / "demo.db")
    queue = screens.day_queue(connection, LAST_DAY)
    links = re.findall(r'href="/notes/([^"]+)"', page)
    assert links == [item["note"] for item in queue]
    assert len(links) == len(set(links))
    assert "Act today" in page and "SYNTHETIC" in page


def test_a_carer_cannot_open_another_carers_note_or_the_register(tmp_path: Path) -> None:
    """Access is checked on the server, whatever the URL."""
    client, _ = make(tmp_path)
    note = first_queued(tmp_path)
    sign_in(client, another_carer(tmp_path, note["carer"]))
    assert client.get(f"/notes/{note['id']}").status_code == 403
    assert client.get("/queue").status_code == 403
    token = csrf_of(client.get("/signin").text)
    posted = client.post(f"/notes/{note['id']}/decisions", data={"csrf": token})
    assert posted.status_code == 403


def test_a_carer_sees_their_own_note_but_not_the_findings(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    note = first_queued(tmp_path)
    sign_in(client, note["carer"])
    page = client.get(f"/notes/{note['id']}")
    assert page.status_code == 200
    assert "Findings and your decision" not in page.text
    assert client.get("/today").status_code == 200


def test_markup_in_a_note_is_shown_as_text(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    connection = db.connect(tmp_path / "demo.db")
    visit = connection.execute("SELECT * FROM visits WHERE carer IS NOT NULL LIMIT 1").fetchone()
    connection.execute(
        "INSERT INTO notes VALUES ('evil-1', ?, ?, ?, ?, ?, 0, 0.0)",
        (visit["id"], visit["client"], visit["carer"], visit["day"],
         'Visit done. <script>alert("x")</script><img src=x onerror=alert(1)>'),
    )  # fmt: skip
    connection.commit()
    sign_in(client, "lead")
    page = client.get("/notes/evil-1").text
    assert "<script>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page


def test_a_decision_is_kept_as_a_label_with_its_route_and_opens_the_next_note(
    tmp_path: Path,
) -> None:
    client, _ = make(tmp_path)
    note = first_queued(tmp_path)
    sign_in(client, "lead")
    page = client.get(f"/notes/{note['id']}").text
    finding = re.search(r'name="finding-(\d+)"', page)
    assert finding
    fields = {"csrf": csrf_of(page), f"finding-{finding.group(1)}": "not a concern"}
    response = client.post(f"/notes/{note['id']}/decisions", data=fields, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/notes/")
    assert response.headers["location"] != f"/notes/{note['id']}"
    connection = db.connect(tmp_path / "demo.db")
    row = connection.execute("SELECT * FROM decisions").fetchone()
    assert (row["label"], row["route"], row["reviewer"]) == ("not a concern", "finding", "lead")


def test_an_unknown_label_or_category_is_not_stored(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    note = first_queued(tmp_path)
    sign_in(client, "lead")
    page = client.get(f"/notes/{note['id']}").text
    finding = re.search(r'name="finding-(\d+)"', page)
    assert finding
    fields = {"csrf": csrf_of(page), f"finding-{finding.group(1)}": "delete everything",
              "missed": "<b>made up</b>"}  # fmt: skip
    client.post(f"/notes/{note['id']}/decisions", data=fields, follow_redirects=False)
    connection = db.connect(tmp_path / "demo.db")
    assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_demo_visitors_never_see_each_others_decisions(tmp_path: Path) -> None:
    first, settings = make(tmp_path)
    second = TestClient(first.app, base_url="https://testserver")
    note = first_queued(tmp_path)
    sign_in(first, "lead")
    sign_in(second, "lead")
    page = first.get(f"/notes/{note['id']}").text
    finding = re.search(r'name="finding-(\d+)"', page)
    assert finding
    fields = {"csrf": csrf_of(page), f"finding-{finding.group(1)}": "confirmed"}
    first.post(f"/notes/{note['id']}/decisions", data=fields, follow_redirects=False)
    assert "Your last decision" in first.get(f"/notes/{note['id']}").text
    assert "Your last decision" not in second.get(f"/notes/{note['id']}").text


def test_a_full_review_logs_no_note_text(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Whatever logs during sign-in, reading and deciding, the note's words never appear."""
    caplog.set_level(logging.DEBUG)
    client, _ = make(tmp_path)
    note = first_queued(tmp_path)
    sign_in(client, "lead")
    page = client.get(f"/notes/{note['id']}").text
    fields = {"csrf": csrf_of(page)}
    client.post(f"/notes/{note['id']}/decisions", data=fields, follow_redirects=False)
    words = [w for w in re.findall(r"[A-Za-z]{6,}", note["body"])][:5]
    assert words
    for word in words:
        assert word not in caplog.text


def test_overlapping_quotes_merge_and_each_number_shows_once() -> None:
    text = "He shouted at her and grabbed her wrist. She is fine."
    runs = screens.segments(text, ["shouted at her and grabbed her wrist", "grabbed her wrist"])
    assert "".join(run["text"] for run in runs) == text
    shown = [number for run in runs for number in run["ends"]]
    assert sorted(shown) == [1, 2]
    marked = "".join(run["text"] for run in runs if run["findings"])
    assert marked == "shouted at her and grabbed her wrist"
