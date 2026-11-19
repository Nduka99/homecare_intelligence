"""A client's record leaves the app only with two people, on the SYNTHETIC demo week.

The safeguarding lead asks for one client's record, with a reason; a coordinator approves it in
their own session within 24 hours; the person who asked then downloads it once, as a file. No
one approves their own request, no approval counts for another request or after its deadline,
and who did each step comes from the session, never the form. A demo file holds the seed's notes
and that visitor's own, never another visitor's. The download is logged, with the file's
SHA-256, before the file is sent.

Each attack test passes only when the attack fails, and each was seen to fail with its
protection switched off. Who may release a file is decided in this file by `released_by_rule`,
written from the rule above, never read from the app's code."""

import hashlib
import itertools
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homecare.app import auditlog, db, main
from test_screens import csrf_of, make, sign_in, visit_to_write

CLIENT = "C45"
ROLES = ("lead", "coordinator", "manager", "K01")  # one person per role; K01 is a carer
DAY = 24 * 60 * 60


def released_by_rule(acting: set[str]) -> bool:
    """The rule, independently of the app: a file leaves only when the lead asked and a
    different person who is a coordinator approved."""
    return "lead" in acting and "coordinator" in acting


def at_least_one(acting: set[str]) -> bool:
    """A wrong rule, as a control: any one person can release a file."""
    return len(acting) >= 1


def as_user(client: TestClient, user: str) -> str:
    """Sign this browser in as `user`; its form token."""
    sign_in(client, user)
    return csrf_of(client.get("/", follow_redirects=True).text)


def ask(client: TestClient, client_id: str = CLIENT) -> Any:
    token = as_user(client, "lead")
    fields = {"csrf": token, "client": client_id, "reason": "A hospital admission needs it."}
    return client.post("/exports", data=fields, follow_redirects=False)


def act(client: TestClient, user: str, step: str, request: int, **extra: str) -> Any:
    token = as_user(client, user)
    fields = {"csrf": token, **extra}
    return client.post(f"/exports/{request}/{step}", data=fields, follow_redirects=False)


def rows(settings: dict[str, Any], table: str) -> list[dict[str, Any]]:
    connection = db.connect(settings["database"])
    query = f"SELECT * FROM {table} ORDER BY id"  # noqa: S608 - a table name from this file
    found = [dict(row) for row in connection.execute(query)]
    connection.close()
    return found


def released(answer: Any) -> bool:
    return bool(
        answer.status_code == 200
        and "attachment" in answer.headers.get("content-disposition", "")
        and json.loads(answer.content)["resourceType"] == "Bundle"
    )


def attempt(tmp_path: Path, acting: set[str]) -> bool:
    """Every person in `acting` tries every step, as one demo visitor; whether a file left."""
    client, _ = make(tmp_path)
    for user in ROLES:
        if user in acting:
            token = as_user(client, user)
            client.post("/exports", data={"csrf": token, "client": CLIENT, "reason": "Needed."})
    out = False
    for step in ("approve", "download"):
        for user in ROLES:
            if user in acting:
                out = released(act(client, user, step, 1)) or out
    return out


def test_a_file_leaves_only_when_the_lead_asks_and_a_coordinator_approves(
    tmp_path: Path,
) -> None:
    """All 16 sets of people acting, against the rule written in this file; the wrong rule
    must disagree somewhere, or the comparison could not tell them apart."""
    found = {}
    for size in range(len(ROLES) + 1):
        for acting in itertools.combinations(ROLES, size):
            found[acting] = attempt(tmp_path / "-".join(acting or ("none",)), set(acting))
    assert len(found) == 16
    assert {acting: released_by_rule(set(acting)) for acting in found} == found
    assert any(at_least_one(set(acting)) != outcome for acting, outcome in found.items())


def test_who_approved_comes_from_the_session_not_the_form(tmp_path: Path) -> None:
    client, settings = make(tmp_path)
    ask(client)
    act(client, "coordinator", "approve", 1, approved_by="manager")
    assert [row["approved_by"] for row in rows(settings, "export_approvals")] == ["coordinator"]


def test_an_approval_counts_for_its_own_request_only(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    ask(client)
    ask(client)
    assert act(client, "coordinator", "approve", 1).status_code == 303
    assert not released(act(client, "lead", "download", 2))
    assert released(act(client, "lead", "download", 1))


def test_a_file_is_downloaded_once_and_logged_first_with_its_hash(tmp_path: Path) -> None:
    client, settings = make(tmp_path)
    ask(client)
    act(client, "coordinator", "approve", 1)
    first = act(client, "lead", "download", 1)
    assert released(first)
    assert not released(act(client, "lead", "download", 1))
    assert len(rows(settings, "export_releases")) == 1
    connection = db.connect(settings["database"])
    logged = [link["entry"] for link in auditlog.chain(connection)]
    connection.close()
    exported = [entry for entry in logged if entry["event"] == "fhir export"]
    assert len(exported) == 1 and exported[0]["client"] == CLIENT
    assert exported[0]["sha256"] == hashlib.sha256(first.content).hexdigest()


def test_an_approval_after_the_deadline_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = make(tmp_path)
    ask(client)
    later = time.time() + DAY + 60
    monkeypatch.setattr(time, "time", lambda: later)
    assert act(client, "coordinator", "approve", 1).status_code >= 400
    monkeypatch.undo()
    assert rows(settings, "export_approvals") == []
    assert not released(act(client, "lead", "download", 1))


def test_no_one_approves_their_own_request(tmp_path: Path) -> None:
    """A person who holds both jobs is planted as the one who asked, as this browser's demo
    visitor; their approval is refused."""
    client, settings = make(tmp_path)
    client.get("/signin")  # the browser's demo visitor cookie
    visitor = hashlib.sha256(client.cookies[main.VISITOR].encode("ascii")).hexdigest()
    connection = sqlite3.connect(settings["database"])
    connection.execute(
        "INSERT INTO export_requests (client, reason, requested_by, visitor, requested, deadline)"
        " VALUES (?, 'Planted.', 'coordinator', ?, ?, ?)",
        (CLIENT, visitor, time.time(), time.time() + DAY),
    )
    connection.commit()
    connection.close()
    assert act(client, "coordinator", "approve", 1).status_code == 403
    assert rows(settings, "export_approvals") == []


@pytest.mark.parametrize("user", ["manager", "K01", "coordinator"])
def test_only_the_lead_asks_for_a_file(tmp_path: Path, user: str) -> None:
    client, settings = make(tmp_path)
    token = as_user(client, user)
    fields = {"csrf": token, "client": CLIENT, "reason": "Needed."}
    assert client.post("/exports", data=fields, follow_redirects=False).status_code == 403
    assert rows(settings, "export_requests") == []


def test_asking_for_no_client_or_with_no_reason_stores_nothing(tmp_path: Path) -> None:
    client, settings = make(tmp_path)
    token = as_user(client, "lead")
    for fields in [{"client": "C99", "reason": "Needed."}, {"client": CLIENT, "reason": "  "}]:
        answer = client.post("/exports", data={"csrf": token, **fields}, follow_redirects=False)
        assert answer.status_code == 400
    assert rows(settings, "export_requests") == []


def test_only_the_person_who_asked_downloads_the_file(tmp_path: Path) -> None:
    client, settings = make(tmp_path)
    connection = sqlite3.connect(settings["database"])
    connection.execute("INSERT INTO users (id, role) VALUES ('lead-2', 'lead')")
    connection.commit()
    connection.close()
    ask(client)
    act(client, "coordinator", "approve", 1)
    assert not released(act(client, "lead-2", "download", 1))
    assert released(act(client, "lead", "download", 1))


def test_a_demo_file_never_holds_another_visitors_notes(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    other = TestClient(client.app, base_url="https://testserver")  # another demo visitor
    visit = visit_to_write(tmp_path)
    words = "Another visitor's note: " + secrets.token_hex(8)
    token = as_user(other, visit["carer"])
    page = other.get(f"/visits/{visit['id']}/note").text
    fields = {"csrf": token, "key": form(page, "key"), "body": words, "amends": ""}
    answer = other.post(f"/visits/{visit['id']}/notes", data=fields, follow_redirects=False)
    assert answer.status_code == 303
    settings = {"database": tmp_path / "demo.db"}
    note = next(row["id"] for row in rows(settings, "notes") if row["body"] == words)
    for browser, request, expected in [(client, 1, False), (other, 2, True)]:
        ask(browser, client_id=visit["client"])
        act(browser, "coordinator", "approve", request)
        answer = act(browser, "lead", "download", request)
        assert released(answer)
        found = json.loads(answer.content)["entry"]
        assert any(item["resource"]["id"] == note for item in found) is expected


def form(page: str, name: str) -> str:
    found = re.search(rf'name="{name}" value="([^"]*)"', page)
    return found.group(1) if found else ""
