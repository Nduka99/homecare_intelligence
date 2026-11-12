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

from homecare.app import db, main, screens, seed
from homecare.notes import routing

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


def visit_to_write(tmp_path: Path, begun: bool = True) -> dict[str, Any]:
    """A planned visit on the demo clock's day with no note yet: one that has begun (so its
    carer may write it up now), or, with `begun=False`, one still to come."""
    demo = seed.load()
    starts = {visit["id"]: visit for visit in demo["visits"]}
    connection = db.connect(tmp_path / "demo.db")
    rows = connection.execute(
        "SELECT v.* FROM visits v WHERE v.day = ? AND v.carer IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM notes n WHERE n.visit = v.id) ORDER BY v.carer, v.position",
        (LAST_DAY,),
    ).fetchall()
    for row in rows:
        if screens.started(starts[row["id"]], demo["now"]) == begun:
            return dict(row)
    raise LookupError("no such visit in the demo week")


def first_queued(tmp_path: Path) -> dict[str, Any]:
    connection = db.connect(tmp_path / "demo.db")
    item = screens.day_queue(connection, LAST_DAY, None)[0]
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
    queue = screens.day_queue(connection, LAST_DAY, None)
    register = page.split('aria-label="The day\'s register"', 1)[1]
    links = re.findall(r'href="/notes/([^"]+)"', register)
    assert links == [item["note"] for item in queue]
    assert f'href="/notes/{queue[0]["note"]}">Open the first note' in page
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
        "INSERT INTO notes (id, visit, client, carer, day, body, tamper, tamper_probability) "
        "VALUES ('evil-1', ?, ?, ?, ?, ?, 0, 0.0)",
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


def test_only_the_manager_sees_the_team_and_the_scores_are_the_models(tmp_path: Path) -> None:
    """The retention screen is the manager's alone, and every chance shown is the model's own
    plain-Python score of that carer."""
    from homecare.app import seed
    from homecare.retention import model as retention_model

    client, _ = make(tmp_path)
    sign_in(client, "lead")
    assert client.get("/team").status_code == 403
    client, _ = make(tmp_path / "manager")
    sign_in(client, "manager")
    page = client.get("/team")
    assert page.status_code == 200
    assert "never decides anything about a person" in page.text
    retention = seed.load()["retention"]
    rows = screens.team(retention)
    for row in rows:
        expected = retention_model.score(retention["model"], retention["carers"][row["carer"]])
        assert row["chance"] == expected
        assert f"{100 * expected:.1f}</span> in 100" in page.text
    assert sum(row["suggested"] for row in rows) == max(1, round(0.1 * len(rows)))


def test_a_note_with_no_finding_needs_an_explicit_record_of_the_read(tmp_path: Path) -> None:
    """A random check has no finding to decide. Saving with nothing chosen stores nothing and
    asks again; "I read the whole note: nothing missed" is kept as the note's own decision, so
    the random sample has a denominator."""
    client, _ = make(tmp_path)
    connection = db.connect(tmp_path / "demo.db")
    queue = screens.day_queue(connection, LAST_DAY, None)
    note = next(item["note"] for item in queue if item["section"] == routing.RANDOM_CHECK)
    sign_in(client, "lead")
    token = csrf_of(client.get(f"/notes/{note}").text)
    skipped = client.post(f"/notes/{note}/decisions", data={"csrf": token, "missed": ""},
                          follow_redirects=False)  # fmt: skip
    assert skipped.headers["location"] == f"/notes/{note}?choose=1"
    assert "Not saved" in client.get(skipped.headers["location"]).text
    assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    client.post(f"/notes/{note}/decisions",
                data={"csrf": token, "missed": screens.NOTHING_MISSED})  # fmt: skip
    row = connection.execute("SELECT * FROM decisions WHERE note = ?", (note,)).fetchone()
    assert (row["label"], row["finding"], row["category"]) == ("not a concern", None, None)
    assert row["route"] == routing.RANDOM_CHECK


def test_unread_notes_come_first_numbered_through_and_counted(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    visit = visit_to_write(tmp_path)
    sign_in(client, visit["carer"])
    token = csrf_of(client.get(f"/visits/{visit['id']}/note").text)
    client.post(f"/visits/{visit['id']}/notes",
                data={"csrf": token, "key": "6f0e1c2a-1b2c-4d3e-8f40-5a6b7c8d9e0f",
                      "body": "Lunch made.", "amends": ""})  # fmt: skip
    sign_in(client, "lead")
    page = client.get(f"/queue?day={LAST_DAY}").text
    numbers = [int(n) for n in re.findall(r'<span class="num">(\d+)</span>', page)]
    assert numbers == list(range(1, len(numbers) + 1))  # numbered through, unread first
    assert page.index("Not yet read by the model") < page.index("Act today")
    tally = re.search(r"<strong>(\d+)</strong> notes? to decide", page)
    assert tally and int(tally.group(1)) == len(numbers)
