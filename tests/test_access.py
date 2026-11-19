"""Who may open which record on the SYNTHETIC demo week, and what is logged when they do.

Every read of a signed-in page and every refusal adds exactly one audit entry, holding IDs and
labels only, each value at most 64 characters; a signed-out request adds none; saving an account
on the command line is logged too. The pages come from the app's own route table, so a new page
with no case here fails.

A carer opens their own visits' day sheet, note forms and notes for the demo clock's day; a note
form also for the day before, so a late note or an addition can still be written; a Send on any
day. Any other day opens only after "Open another day" with a reason, which stores an alert for
the manager and an audit entry in one write, for that demo visitor and that day only, and never
opens another carer's records. Only the manager marks an alert reviewed.

Each attack test passes only when the attack fails, and each was seen to fail with its protection
switched off. The expected answers are the rule as written above, never read from the app's
code."""

import getpass
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from homecare import cli
from homecare.app import auditlog, db, main, seed
from test_screens import LAST_DAY, csrf_of, make, sign_in

# Pages open without signing in, or that only redirect.
OPEN = {"/", "/signin", "/guide", "/healthz", "/sw.js", "/map-data"}
LONGEST = 64  # the most characters of any one value an audit entry keeps


def entries(settings: dict[str, Any]) -> list[dict[str, Any]]:
    connection = db.connect(settings["database"])
    found = [link["entry"] for link in auditlog.chain(connection)]
    connection.close()
    return found


def browser(client: TestClient, user: str) -> TestClient:
    """Another browser on the same app, signed in as `user`."""
    other = TestClient(client.app, base_url="https://testserver")
    sign_in(other, user)
    return other


def signed_in_pages(client: TestClient) -> set[str]:
    found = set()
    for route in client.app.routes:  # type: ignore[attr-defined]
        if isinstance(route, APIRoute) and "GET" in (route.methods or set()):
            found.add(route.path)
    return found - OPEN


def cases() -> dict[str, list[tuple[str, str, str]]]:
    """Each signed-in page: an allowed read and each kind of refusal, as (who, address, kind).
    The carer's records are from the demo clock's day."""
    demo = seed.load()
    carer_of = {visit["id"]: visit["carer"] for visit in demo["visits"]}
    note = next(n for n in demo["notes"] if n["day"] == LAST_DAY)
    carer = carer_of[note["visit"]]
    other = next(n for n in demo["notes"] if carer_of[n["visit"]] != carer)
    return {
        "/queue": [("lead", f"/queue?day={LAST_DAY}", "read"), (carer, "/queue", "refused")],
        "/notes/{note_id}": [
            ("lead", f"/notes/{note['id']}", "read"),
            (carer, f"/notes/{other['id']}", "refused"),
            ("lead", "/notes/no-such-note", "refused"),
            ("coordinator", f"/notes/{note['id']}", "refused"),
        ],
        "/team": [("manager", "/team", "read"), ("lead", "/team", "refused")],
        "/today": [(carer, f"/today?day={LAST_DAY}", "read"), ("lead", "/today", "refused")],
        "/visits/{visit_id}/note": [
            (carer, f"/visits/{note['visit']}/note", "read"),
            (carer, f"/visits/{other['visit']}/note", "refused"),
            ("lead", f"/visits/{note['visit']}/note", "refused"),
        ],
        "/rota": [("coordinator", f"/rota?day={LAST_DAY}", "read"), (carer, "/rota", "refused")],
        "/rota/replan": [
            ("coordinator", "/rota/replan", "read"),
            ("lead", "/rota/replan", "refused"),
        ],
        "/imports": [("coordinator", "/imports", "read"), (carer, "/imports", "refused")],
        "/exports": [("lead", "/exports", "read"), (carer, "/exports", "refused")],
        "/referrals": [("coordinator", "/referrals", "read"), ("lead", "/referrals", "refused")],
        "/referrals/{referral_id}": [
            ("coordinator", "/referrals/referral-1", "read"),
            ("coordinator", "/referrals/no-such-referral", "refused"),
            ("lead", "/referrals/referral-1", "refused"),
        ],
    }


def test_every_read_and_every_refusal_of_a_signed_in_page_adds_one_entry(
    tmp_path: Path,
) -> None:
    client, settings = make(tmp_path)
    found = cases()
    assert set(found) == signed_in_pages(client), "a signed-in page with no case here"
    browsers: dict[str, TestClient] = {}
    for requests in found.values():
        for who, url, kind in requests:
            if who not in browsers:
                browsers[who] = browser(client, who)
            before = len(entries(settings))
            answer = browsers[who].get(url, follow_redirects=False)
            assert answer.status_code == (200 if kind == "read" else answer.status_code)
            added = entries(settings)[before:]
            assert len(added) == 1, f"{who} {url}: {added}"
            assert added[0]["user"] == who
            if kind == "read":
                assert added[0]["event"].startswith("view"), (url, added)
            else:
                assert answer.status_code in (403, 404), url
                assert added[0]["event"] == "refused" and added[0]["status"] == answer.status_code


def test_no_entry_holds_note_text_or_a_value_longer_than_the_cap(tmp_path: Path) -> None:
    """Addresses carry whatever a visitor types; an entry keeps at most 64 characters of any
    value, and never the words of a note."""
    client, settings = make(tmp_path)
    demo = seed.load()
    carer = next(visit["carer"] for visit in demo["visits"] if visit["carer"])
    long = "x" * 300
    lead = browser(client, "lead")
    coordinator = browser(client, "coordinator")
    browser(client, carer).get(f"/visits/{long}/note")
    lead.get(f"/notes/{long}")
    lead.get(f"/queue?day={long}")
    coordinator.get(f"/rota?carer={long}&day={long}")
    coordinator.get(f"/referrals/{long}")
    for note in demo["notes"][:20]:
        lead.get(f"/notes/{note['id']}")
    texts = [note["text"] for note in demo["notes"]]
    values = [v for entry in entries(settings) for v in entry.values() if isinstance(v, str)]
    # The long addresses were logged, cut to the cap (the check below can see them).
    assert any(len(value) == LONGEST and value.endswith("x" * 32) for value in values)
    for value in values:
        assert len(value) <= LONGEST, value
        assert len(value) < 16 or not any(value in text for text in texts), value


def test_a_signed_out_request_adds_no_entry(tmp_path: Path) -> None:
    """A visitor who is not signed in is sent to sign in; logging each of those would let anyone
    fill the log without an account."""
    client, settings = make(tmp_path)
    before = entries(settings)
    for path in signed_in_pages(client):
        answer = client.get(re.sub(r"\{[^}]+\}", "x", path), follow_redirects=False)
        assert answer.headers["location"] == "/signin"
    assert entries(settings) == before


def test_saving_an_account_on_the_command_line_is_logged_without_its_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = bytes(range(32))
    password = "a long enough password"  # noqa: S105 - a test account's password
    monkeypatch.setattr(main, "ACCOUNTS_FOLDER", tmp_path)
    monkeypatch.setenv("SIGN_IN", "accounts")
    monkeypatch.setenv("AUDIT_KEY", key.hex())
    monkeypatch.setattr(getpass, "getpass", lambda prompt: password)
    cli.main(["platform", "user", "lead-2", "--role", "lead"])
    connection = db.connect(tmp_path / "homecare.db")
    chain = auditlog.chain(connection)
    assert [link["entry"]["event"] for link in chain] == ["save account"]
    assert chain[0]["entry"]["account"] == "lead-2" and chain[0]["entry"]["role"] == "lead"
    assert password not in str(chain)
    assert auditlog.problems(connection, key, tmp_path / "audit.anchor") == []
    connection.close()


# A carer's records: today's, yesterday's note forms, another day only with a reason.

CARER = "K01"  # has their own visits and notes on days 25, 26 and 27
EARLIER = LAST_DAY - 2


def own(day: int, carer: str = CARER) -> tuple[str, str]:
    """One of the carer's visits on `day` that has a note, and that note."""
    demo = seed.load()
    visits = set()
    for visit in demo["visits"]:
        if visit["carer"] == carer and visit["day"] == day:
            visits.add(visit["id"])
    note = next(n for n in demo["notes"] if n["visit"] in visits)
    return note["visit"], note["id"]


def another_carers(day: int) -> tuple[str, str]:
    """A visit with a note on `day` by another carer, and that note."""
    demo = seed.load()
    carer_of = {visit["id"]: visit["carer"] for visit in demo["visits"]}
    note = next(n for n in demo["notes"] if n["day"] == day and carer_of[n["visit"]] != CARER)
    return note["visit"], note["id"]


def table(settings: dict[str, Any], name: str) -> list[dict[str, Any]]:
    connection = db.connect(settings["database"])
    query = f"SELECT * FROM {name} ORDER BY id"  # noqa: S608 - a table name from this file
    rows = [dict(row) for row in connection.execute(query)]
    connection.close()
    return rows


def open_day(carer: TestClient, day: int, reason: str) -> Any:
    token = csrf_of(carer.get(f"/today?day={LAST_DAY}").text)
    fields = {"csrf": token, "day": str(day), "reason": reason, "back": f"/today?day={day}"}
    return carer.post("/openings", data=fields, follow_redirects=False)


def test_a_carer_opens_todays_records_and_yesterdays_forms_and_nothing_older(
    tmp_path: Path,
) -> None:
    client, _ = make(tmp_path)
    carer = browser(client, CARER)
    expected = {}
    # (day, its sheet, a note form, a note page)
    for day, sheet, form, note_page in [
        (LAST_DAY, 200, 200, 200),
        (LAST_DAY - 1, 403, 200, 403),
        (EARLIER, 403, 403, 403),
    ]:
        visit, note = own(day)
        expected[f"/today?day={day}"] = sheet
        expected[f"/visits/{visit}/note"] = form
        expected[f"/notes/{note}"] = note_page
    found = {url: carer.get(url).status_code for url in expected}
    assert found == expected
    closed = carer.get(f"/today?day={EARLIER}").text
    assert "Open another day" in closed and "/visits/" not in closed


def test_another_day_opens_only_with_a_reason_that_leaves_an_alert(tmp_path: Path) -> None:
    client, settings = make(tmp_path)
    carer = browser(client, CARER)
    visit, note = own(EARLIER)
    for reason in ["", "   ", "x" * 501]:
        answer = open_day(carer, EARLIER, reason)
        assert answer.status_code == 400 and "Open another day" in answer.text
    assert table(settings, "alerts") == []
    assert carer.get(f"/visits/{visit}/note").status_code == 403
    reason = "Checking what I wrote on Saturday before the review."
    assert open_day(carer, EARLIER, reason).status_code == 303
    alerts = table(settings, "alerts")
    assert [(a["user"], a["day"], a["reason"], a["clock_day"]) for a in alerts] == [
        (CARER, EARLIER, reason, LAST_DAY)
    ]
    logged = [e for e in entries(settings) if e["event"] == "open another day"]
    assert len(logged) == 1 and logged[0]["alert"] == alerts[0]["id"]
    assert reason not in str(entries(settings))
    for url in [f"/today?day={EARLIER}", f"/visits/{visit}/note", f"/notes/{note}"]:
        assert carer.get(url).status_code == 200, url


def test_an_alert_and_its_audit_entry_are_stored_together_or_not_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = make(tmp_path)
    carer = TestClient(client.app, base_url="https://testserver", raise_server_exceptions=False)
    sign_in(carer, CARER)
    token = csrf_of(carer.get(f"/today?day={LAST_DAY}").text)

    def broken(*args: Any) -> None:
        raise RuntimeError("the audit log could not be written")

    monkeypatch.setattr(auditlog, "record", broken)
    fields = {"csrf": token, "day": str(EARLIER), "reason": "Covering a note.", "back": ""}
    assert carer.post("/openings", data=fields, follow_redirects=False).status_code == 500
    monkeypatch.undo()
    assert table(settings, "alerts") == []
    assert carer.get(f"/today?day={EARLIER}").status_code == 403


def test_a_reason_never_opens_another_carers_records(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    carer = browser(client, CARER)
    assert open_day(carer, EARLIER, "Covering for a colleague.").status_code == 303
    visit, note = another_carers(EARLIER)
    assert carer.get(f"/visits/{visit}/note").status_code == 403
    assert carer.get(f"/notes/{note}").status_code == 403


def test_an_opening_lasts_its_day_and_belongs_to_its_demo_visitor(tmp_path: Path) -> None:
    client, settings = make(tmp_path)
    carer = browser(client, CARER)
    assert open_day(carer, EARLIER, "Checking an old note.").status_code == 303
    assert carer.get(f"/today?day={EARLIER}").status_code == 200
    another_visitor = browser(client, CARER)
    assert another_visitor.get(f"/today?day={EARLIER}").status_code == 403
    settings["clock"] = {"day": LAST_DAY + 1, "minute": 480}
    assert carer.get(f"/today?day={EARLIER}").status_code == 403


def test_a_send_is_never_limited_by_day(tmp_path: Path) -> None:
    """A note written on an old form, or kept on the phone without signal, is still stored."""
    client, settings = make(tmp_path)
    carer = browser(client, CARER)
    visit, _ = own(EARLIER)
    token = csrf_of(carer.get(f"/today?day={LAST_DAY}").text)
    fields = {"csrf": token, "key": str(uuid.uuid4()), "body": "Late, from my phone.", "amends": ""}
    answer = carer.post(f"/visits/{visit}/notes", data=fields, follow_redirects=False)
    assert answer.status_code == 303
    stored = [note["body"] for note in table(settings, "notes") if note["source"] == "app"]
    assert stored == [fields["body"]]


def test_only_the_manager_marks_an_alert_reviewed_and_its_reason_shows_as_text(
    tmp_path: Path,
) -> None:
    """One demo visitor tries every role, so only the role can stop the review."""
    client, settings = make(tmp_path)
    sign_in(client, CARER)
    assert open_day(client, EARLIER, "<b>bold</b> & more").status_code == 303
    alert = table(settings, "alerts")[0]["id"]
    for who in ["lead", "coordinator", CARER]:
        sign_in(client, who)
        token = csrf_of(client.get("/", follow_redirects=True).text)
        answer = client.post(
            f"/alerts/{alert}/reviewed", data={"csrf": token}, follow_redirects=False
        )
        assert answer.status_code == 403, who
    assert table(settings, "alert_reviews") == []
    sign_in(client, "manager")
    manager = client
    page = manager.get("/team").text
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; more" in page and "<b>bold</b>" not in page
    for _ in range(2):
        answer = manager.post(
            f"/alerts/{alert}/reviewed", data={"csrf": csrf_of(page)}, follow_redirects=False
        )
        assert answer.status_code == 303
    reviews = table(settings, "alert_reviews")
    assert [(review["alert"], review["reviewer"]) for review in reviews] == [(alert, "manager")]


def test_an_alert_and_its_review_cannot_be_changed_or_deleted(tmp_path: Path) -> None:
    client, settings = make(tmp_path)
    sign_in(client, CARER)
    open_day(client, EARLIER, "Checking an old note.")
    sign_in(client, "manager")
    page = client.get("/team").text
    client.post("/alerts/1/reviewed", data={"csrf": csrf_of(page)}, follow_redirects=False)
    connection = db.connect(settings["database"])
    for statement in [
        "UPDATE alerts SET reason = 'nothing'",
        "DELETE FROM alerts",
        "UPDATE alert_reviews SET reviewer = 'lead'",
        "DELETE FROM alert_reviews",
    ]:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement)
    connection.close()
