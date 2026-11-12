"""The carer app's server side on the committed SYNTHETIC demo week: a Send stores a note once,
a carer reaches only their own visits and notes, notes are escaped and never logged, demo
visitors stay apart, and the day sheet shows the next visit and the route. Each attack test
passes only when the attack fails, and each was checked by breaking its protection on purpose."""

import logging
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homecare.app import db, maps
from test_screens import LAST_DAY, csrf_of, make, sign_in, visit_to_write


def unwritten_visit(tmp_path: Path) -> dict[str, Any]:
    return visit_to_write(tmp_path)


def other_carer_visit(tmp_path: Path, carer: str) -> dict[str, Any]:
    connection = db.connect(tmp_path / "demo.db")
    row = connection.execute(
        "SELECT * FROM visits WHERE day = ? AND carer IS NOT NULL AND carer != ? LIMIT 1",
        (LAST_DAY, carer),
    ).fetchone()
    return dict(row)


def send(client: TestClient, visit: str, body: str, key: str = "", amends: str = "") -> Any:
    token = csrf_of(client.get(f"/visits/{visit}/note").text)
    fields = {"csrf": token, "key": key or str(uuid.uuid4()), "body": body, "amends": amends}
    return client.post(f"/visits/{visit}/notes", data=fields, follow_redirects=False)


def app_notes(tmp_path: Path) -> list[dict[str, Any]]:
    connection = db.connect(tmp_path / "demo.db")
    rows = connection.execute("SELECT * FROM notes WHERE source = 'app' ORDER BY written")
    return [dict(row) for row in rows]


def test_a_replayed_send_stores_the_note_once(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    sign_in(client, visit["carer"])
    key = str(uuid.uuid4())
    first = send(client, visit["id"], "Mrs A ate well. All tasks done.", key)
    again = send(client, visit["id"], "Mrs A ate well. All tasks done.", key)
    assert first.status_code == again.status_code == 303
    assert first.headers["location"] == again.headers["location"]
    assert len(app_notes(tmp_path)) == 1


def test_a_key_reused_for_other_words_is_refused_and_the_note_kept(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    sign_in(client, visit["carer"])
    key = str(uuid.uuid4())
    send(client, visit["id"], "Mrs A ate well.", key)
    reused = send(client, visit["id"], "Nothing happened at all.", key)
    assert reused.status_code == 422
    assert [note["body"] for note in app_notes(tmp_path)] == ["Mrs A ate well."]


def test_a_carer_cannot_write_to_or_add_to_another_carers_visit(tmp_path: Path) -> None:
    """Access is checked on the server: the visit must be the signed-in carer's, and so must
    the note an addition points at."""
    client, _ = make(tmp_path)
    mine = unwritten_visit(tmp_path)
    theirs = other_carer_visit(tmp_path, mine["carer"])
    sign_in(client, mine["carer"])
    assert client.get(f"/visits/{theirs['id']}/note").status_code == 403
    token = csrf_of(client.get(f"/visits/{mine['id']}/note").text)
    fields = {"csrf": token, "key": str(uuid.uuid4()), "body": "Written in.", "amends": ""}
    assert client.post(f"/visits/{theirs['id']}/notes", data=fields).status_code == 403
    connection = db.connect(tmp_path / "demo.db")
    their_note = connection.execute(
        "SELECT id FROM notes WHERE carer = ? LIMIT 1", (theirs["carer"],)
    ).fetchone()["id"]
    fields |= {"key": str(uuid.uuid4()), "amends": their_note}
    assert client.post(f"/visits/{mine['id']}/notes", data=fields).status_code == 403
    assert app_notes(tmp_path) == []


def test_the_author_comes_from_the_session_not_the_form(tmp_path: Path) -> None:
    """A draft left on a shared phone by carer A, sent while B is signed in: A's visit is
    refused, and a carer named in the form is ignored."""
    client, _ = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    other = other_carer_visit(tmp_path, visit["carer"])
    sign_in(client, other["carer"])
    token = csrf_of(client.get(f"/visits/{other['id']}/note").text)
    fields = {"csrf": token, "key": str(uuid.uuid4()), "body": "A's draft.", "amends": ""}
    assert client.post(f"/visits/{visit['id']}/notes", data=fields).status_code == 403
    fields |= {"key": str(uuid.uuid4()), "carer": visit["carer"]}
    client.post(f"/visits/{other['id']}/notes", data=fields, follow_redirects=False)
    assert [note["carer"] for note in app_notes(tmp_path)] == [other["carer"]]


def test_a_send_with_a_missing_or_wrong_csrf_token_is_refused(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    sign_in(client, visit["carer"])
    for token in ["", "guessed"]:
        fields = {"csrf": token, "key": str(uuid.uuid4()), "body": "Hello.", "amends": ""}
        assert client.post(f"/visits/{visit['id']}/notes", data=fields).status_code == 403
    assert app_notes(tmp_path) == []


def test_an_empty_or_overlong_note_or_a_bad_key_stores_nothing(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    sign_in(client, visit["carer"])
    assert send(client, visit["id"], "   ").status_code == 400
    assert send(client, visit["id"], "x" * 4001).status_code == 400
    assert send(client, visit["id"], "Fine.", key="not-a-uuid").status_code == 400
    assert app_notes(tmp_path) == []


def test_an_addition_is_a_new_note_and_the_original_is_unchanged(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    sign_in(client, visit["carer"])
    send(client, visit["id"], "Ate lunch.")
    first = app_notes(tmp_path)[0]
    page = client.get(f"/visits/{visit['id']}/note").text
    assert f'name="amends" value="{first["id"]}"' in page and "Add to your note" in page
    send(client, visit["id"], "Forgot: the fridge was empty.", amends=first["id"])
    notes = app_notes(tmp_path)
    assert [note["body"] for note in notes] == ["Ate lunch.", "Forgot: the fridge was empty."]
    assert notes[1]["amends"] == first["id"]
    assert "Added since" in client.get(f"/notes/{first['id']}").text


def test_markup_in_an_app_note_is_shown_as_text(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    sign_in(client, visit["carer"])
    send(client, visit["id"], 'Done. <script>alert("x")</script><img src=x onerror=alert(1)>')
    note = app_notes(tmp_path)[0]
    for user in [visit["carer"], "lead"]:
        sign_in(client, user)
        page = client.get(f"/notes/{note['id']}").text
        assert "<script>alert" not in page and "<img src=x" not in page
        assert "&lt;script&gt;" in page


def test_sending_and_adding_log_no_note_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    caplog.set_level(logging.DEBUG)
    sign_in(client, visit["carer"])
    send(client, visit["id"], "Zanzibar marmalade quietly.")
    send(client, visit["id"], "Quokka lanterns.", amends=app_notes(tmp_path)[0]["id"])
    connection = db.connect(tmp_path / "demo.db")
    audit = " ".join(row["entry"] for row in connection.execute("SELECT entry FROM audit"))
    for word in ["Zanzibar", "marmalade", "Quokka", "lanterns"]:
        assert word not in caplog.text and word not in audit
    assert audit.count('"write note"') == 2


def test_demo_visitors_never_see_each_others_notes(tmp_path: Path) -> None:
    first, settings = make(tmp_path)
    second = TestClient(first.app, base_url="https://testserver")
    visit = unwritten_visit(tmp_path)
    sign_in(first, visit["carer"])
    send(first, visit["id"], "Seen only by the first visitor.")
    note = app_notes(tmp_path)[0]["id"]
    sign_in(second, visit["carer"])
    assert second.get(f"/notes/{note}").status_code == 404
    assert note not in second.get(f"/today?day={LAST_DAY}").text
    sign_in(second, "lead")
    assert note not in second.get(f"/queue?day={LAST_DAY}").text
    token = csrf_of(second.get(f"/queue?day={LAST_DAY}").text)
    decided = second.post(f"/notes/{note}/decisions", data={"csrf": token, "missed": "neglect"})
    assert decided.status_code == 404


def test_a_visitors_note_waits_for_the_lead_as_not_yet_read(tmp_path: Path) -> None:
    """Signed in as the lead, the same visitor sees their carer note listed as unread: a note
    the model has not read is never silently left out of the register."""
    client, _ = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    sign_in(client, visit["carer"])
    send(client, visit["id"], "Unread for now.")
    note = app_notes(tmp_path)[0]["id"]
    sign_in(client, "lead")
    register = client.get(f"/queue?day={LAST_DAY}").text
    assert "Not yet read by the model" in register and f"/notes/{note}" in register
    assert "Not read by the model yet" in client.get(f"/notes/{note}").text


def test_the_day_sheet_shows_the_next_visit_progress_and_the_route(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    sign_in(client, visit["carer"])
    page = client.get(f"/today?day={LAST_DAY}").text
    assert "Demo clock" in page and "visits written up" in page
    write_up = page.split('class="card write-up"', 1)[1].split("</section>", 1)[0]
    assert f'href="/visits/{visit["id"]}/note"' in write_up
    after_next = page.split('class="card next-card"', 1)[1].split("</section>", 1)[0]
    assert "Leave by" in after_next or "Be there by" in after_next  # the same place: "Be there"
    assert "/note" not in after_next and "once the visit begins" in after_next
    assert 'class="stop is-next"' in page  # the map marks the next stop as the list does
    assert re.search(r"google\.com/maps/dir/\?api=1&amp;destination=[\d.]+,-[\d.]+", page)
    assert "openstreetmap.org/copyright" in page and 'href="/map-data"' in page
    assert '<svg viewBox="' in page and 'class="path" d="M' in page
    send(client, visit["id"], "Done.")
    after = client.get(f"/today?day={LAST_DAY}&sent=1").text
    assert "Your note was sent." in after
    written = re.search(r'<span class="figure">(\d+)</span> of', page)
    written_after = re.search(r'<span class="figure">(\d+)</span> of', after)
    assert written and written_after
    assert int(written_after.group(1)) == int(written.group(1)) + 1


def test_a_visit_not_yet_begun_cannot_have_a_note(tmp_path: Path) -> None:
    """A note records a visit that happened: before the visit begins there is no form, and a
    Send is refused."""
    client, _ = make(tmp_path)
    later = visit_to_write(tmp_path, begun=False)
    sign_in(client, later["carer"])
    page = client.get(f"/visits/{later['id']}/note").text
    assert "begins at" in page and 'name="body"' not in page
    token = csrf_of(page)
    fields = {"csrf": token, "key": str(uuid.uuid4()), "body": "Too early.", "amends": ""}
    assert client.post(f"/visits/{later['id']}/notes", data=fields).status_code == 400
    assert app_notes(tmp_path) == []


def test_only_carer_pages_load_the_app_and_the_worker_is_served_from_the_root(
    tmp_path: Path,
) -> None:
    """The review screens stay script-free; the carer's pages load one script from this site
    (the map's route is a JSON data block, which never runs), and the service worker is served
    at /sw.js so it can look after /today."""
    client, _ = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    sign_in(client, visit["carer"])
    for url in [f"/today?day={LAST_DAY}", f"/visits/{visit['id']}/note"]:
        page = client.get(url).text
        tags = re.findall(r"<script[^>]*>", page)
        runs = [tag for tag in tags if tag != '<script type="application/json" id="route-data">']
        assert runs == ['<script type="module" src="/static/app.js">']
        assert 'rel="manifest" href="/static/manifest.webmanifest"' in page
    worker = client.get("/sw.js")
    assert worker.status_code == 200 and worker.headers["content-type"].startswith(
        "text/javascript"
    )
    manifest = client.get("/static/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert manifest.json()["start_url"] == "/today"
    sign_in(client, "lead")
    assert "<script" not in client.get(f"/queue?day={LAST_DAY}").text


def test_the_map_data_is_offered_under_the_odbl(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    response = client.get("/map-data")
    assert response.status_code == 200
    assert response.content == maps.FILE.read_bytes()
    assert "Open Database License" in response.text


def test_a_stale_form_for_a_written_visit_becomes_an_addition(tmp_path: Path) -> None:
    """A form saved on the phone before the first note went still says "first note". The record
    never holds two first notes: the server makes such a Send an addition to the first."""
    client, _ = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    sign_in(client, visit["carer"])
    send(client, visit["id"], "First note.")
    send(client, visit["id"], "Sent from an old form.", amends="")
    notes = app_notes(tmp_path)
    assert [note["amends"] for note in notes] == [None, notes[0]["id"]]


def test_every_refusal_is_a_page_with_a_way_back(tmp_path: Path) -> None:
    """A carer who meets a refusal sees what happened and where to go, never bare text."""
    client, _ = make(tmp_path)
    later = visit_to_write(tmp_path, begun=False)
    visit = unwritten_visit(tmp_path)
    sign_in(client, visit["carer"])
    token = csrf_of(client.get(f"/visits/{visit['id']}/note").text)
    key = str(uuid.uuid4())
    send(client, visit["id"], "Words.", key)
    answers = [
        client.post(f"/visits/{later['id']}/notes",
                    data={"csrf": token, "key": str(uuid.uuid4()), "body": "x", "amends": ""}),
        client.post(f"/visits/{visit['id']}/notes", data={"csrf": "wrong"}),
        client.post(f"/visits/{visit['id']}/notes",
                    data={"csrf": token, "key": key, "body": "Other words.", "amends": ""}),
        client.get("/notes/nope"),
    ]  # fmt: skip
    assert [answer.status_code for answer in answers] == [400, 403, 422, 404]
    for answer in answers:
        assert answer.headers["content-type"].startswith("text/html")
        assert 'href="/today' in answer.text and "SYNTHETIC" in answer.text


def test_a_carer_back_from_a_long_visit_signs_in_again_and_nothing_is_lost(
    tmp_path: Path,
) -> None:
    """After the idle timeout the session is over, but the phone's drafts are its own: the carer
    signs in again, and the same Send then stores the note once."""
    client, settings = make(tmp_path)
    visit = unwritten_visit(tmp_path)
    sign_in(client, visit["carer"])
    key = str(uuid.uuid4())
    connection = db.connect(tmp_path / "demo.db")
    connection.execute("UPDATE sessions SET seen = seen - 3600")
    connection.commit()
    token = csrf_of(client.get("/signin").text)  # the old form's token is dead with its session
    fields = {"csrf": token, "key": key, "body": "Written during a long visit.", "amends": ""}
    refused = client.post(f"/visits/{visit['id']}/notes", data=fields, follow_redirects=False)
    assert refused.status_code == 303 and refused.headers["location"] == "/signin"
    assert app_notes(tmp_path) == []
    sign_in(client, visit["carer"])
    first = send(client, visit["id"], "Written during a long visit.", key)
    again = send(client, visit["id"], "Written during a long visit.", key)
    assert first.status_code == again.status_code == 303
    assert [note["body"] for note in app_notes(tmp_path)] == ["Written during a long visit."]
