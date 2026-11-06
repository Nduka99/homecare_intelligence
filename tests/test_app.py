"""The web app's sign-in, sessions and audit log. Each attack test passes only when the attack
fails, and each was checked by breaking its protection on purpose and watching the test fail."""

import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homecare.app import auditlog, auth, db, main


def demo_settings(tmp_path: Path) -> dict[str, Any]:
    return {
        "mode": "demo",
        "database": tmp_path / "demo.db",
        "anchor": tmp_path / "demo.anchor",
        "key": secrets.token_bytes(32),
    }


def accounts_settings(tmp_path: Path, key: bytes) -> dict[str, Any]:
    return {
        "mode": "accounts",
        "database": tmp_path / "accounts.db",
        "anchor": tmp_path / "accounts.anchor",
        "key": key,
    }


def client_for(settings: dict[str, Any]) -> TestClient:
    # https, so the client returns the Secure session cookie as a browser would.
    return TestClient(main.create_app(settings), base_url="https://testserver")


def csrf_of(html: str) -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', html)
    assert found, "no CSRF field on the page"
    return found.group(1)


def sign_in(client: TestClient, user: str, password: str = "") -> Any:
    token = csrf_of(client.get("/signin").text)
    fields = {"csrf": token, "user": user, "password": password}
    return client.post("/signin", data=fields, follow_redirects=False)


def test_health_reports_ok_and_build(tmp_path: Path) -> None:
    response = client_for(demo_settings(tmp_path)).get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_every_page_says_synthetic_and_sends_the_security_headers(tmp_path: Path) -> None:
    response = client_for(demo_settings(tmp_path)).get("/signin")
    assert "SYNTHETIC" in response.text
    assert "script-src 'none'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_the_session_cookie_is_host_only_secure_httponly_and_strict(tmp_path: Path) -> None:
    cookie = client_for(demo_settings(tmp_path)).get("/signin").headers["set-cookie"]
    assert cookie.startswith("__Host-session=")
    for part in ("HttpOnly", "Path=/", "SameSite=strict", "Secure"):
        assert part.lower() in cookie.lower()
    assert "domain=" not in cookie.lower()


def test_signed_out_and_forged_sessions_go_to_sign_in(tmp_path: Path) -> None:
    client = client_for(demo_settings(tmp_path))
    assert client.get("/", follow_redirects=False).headers["location"] == "/signin"
    client.cookies.set(auth.COOKIE, "forged-token", domain="testserver")
    assert client.get("/", follow_redirects=False).headers["location"] == "/signin"


def test_sign_in_issues_a_new_session_and_the_old_one_dies(tmp_path: Path) -> None:
    """Session fixation: the token from before sign-in must not be the signed-in one."""
    client = client_for(demo_settings(tmp_path))
    client.get("/signin")
    before = client.cookies.get(auth.COOKIE)
    assert sign_in(client, "lead").headers["location"] == "/"
    after = client.cookies.get(auth.COOKIE)
    assert before and after and before != after
    connection = db.connect(tmp_path / "demo.db")
    assert auth.find_session(connection, before, 0.0) is None


def test_a_post_with_a_missing_or_wrong_csrf_token_is_refused(tmp_path: Path) -> None:
    client = client_for(demo_settings(tmp_path))
    client.get("/signin")
    for fields in ({"user": "lead"}, {"user": "lead", "csrf": "guess"}):
        assert client.post("/signin", data=fields, follow_redirects=False).status_code == 403
    sign_in(client, "lead")
    assert client.post("/signout", data={"csrf": "guess"}).status_code == 403


def test_idle_and_absolute_timeouts_end_a_session(tmp_path: Path) -> None:
    db.prepare(tmp_path / "t.db", {"clients": [], "carers": [], "visits": [], "notes": []})
    connection = db.connect(tmp_path / "t.db")
    token = auth.start_session(connection, None, now=0.0)
    assert auth.find_session(connection, token, auth.IDLE_SECONDS - 1) is not None
    assert auth.find_session(connection, token, 2 * auth.IDLE_SECONDS) is None
    token = auth.start_session(connection, None, now=0.0)
    for minute in range(1, auth.ABSOLUTE_SECONDS // 60 + 2, 20):  # active all day
        found = auth.find_session(connection, token, minute * 60.0)
    assert found is None


def test_signing_out_ends_the_session_on_the_server(tmp_path: Path) -> None:
    client = client_for(demo_settings(tmp_path))
    sign_in(client, "lead")
    token = client.cookies.get(auth.COOKIE)
    page = client.get("/signin").text  # signed in: the form still carries this session's token
    response = client.post("/signout", data={"csrf": csrf_of(page)}, follow_redirects=False)
    assert response.headers["clear-site-data"]
    connection = db.connect(tmp_path / "demo.db")
    assert token and auth.find_session(connection, token, 0.0) is None


def test_five_wrong_passwords_lock_the_account(tmp_path: Path) -> None:
    client_for(accounts_settings(tmp_path, secrets.token_bytes(32)))
    connection = db.connect(tmp_path / "accounts.db")
    connection.execute(
        "INSERT INTO users (id, role, password) VALUES ('lead', 'lead', ?)",
        (auth.hash_password("correct horse battery staple"),),
    )
    connection.commit()
    outcomes = [auth.check_password(connection, "lead", "wrong", now=1.0) for _ in range(5)]
    assert outcomes == ["wrong"] * 4 + ["locked"]
    assert auth.check_password(connection, "lead", "correct horse battery staple", 2.0) == "locked"
    later = 2.0 + auth.LOCKOUT_SECONDS
    assert auth.check_password(connection, "lead", "correct horse battery staple", later) == "ok"


def test_sign_ins_are_audited_without_secrets(tmp_path: Path) -> None:
    settings = demo_settings(tmp_path)
    client = client_for(settings)
    sign_in(client, "nobody")
    sign_in(client, "lead")
    connection = db.connect(tmp_path / "demo.db")
    entries = [record["entry"] for record in auditlog.chain(connection)]
    assert [entry["event"] for entry in entries] == ["failed sign in", "sign in"]
    assert entries[0]["user"] == "unknown account"  # what was typed is not kept
    assert auditlog.problems(connection, settings["key"], settings["anchor"]) == []


def test_an_edited_audit_log_stops_the_app_at_start(tmp_path: Path) -> None:
    """Someone with write access to the database edits an audit record (dropping the append-only
    trigger first, as such a person could); the next start refuses."""
    key = secrets.token_bytes(32)
    settings = accounts_settings(tmp_path, key)
    client = client_for(settings)
    client.get("/signin")
    connection = db.connect(tmp_path / "accounts.db")
    connection.execute("INSERT INTO users (id, role) VALUES ('lead', 'lead')")
    connection.commit()
    sign_in(client, "lead", "anything")  # a failed sign-in: one audit record
    connection.execute("DROP TRIGGER audit_append_only_update")
    connection.execute("UPDATE audit SET entry = replace(entry, 'lead', 'someone')")
    connection.commit()
    with pytest.raises(SystemExit, match="audit log does not verify"):
        main.create_app(accounts_settings(tmp_path, key))


def test_the_audit_table_refuses_edits(tmp_path: Path) -> None:
    client = client_for(demo_settings(tmp_path))
    sign_in(client, "lead")
    connection = db.connect(tmp_path / "demo.db")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM audit")
