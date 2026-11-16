"""The app stays up and loses nothing when visitors send more, larger, slower or stranger requests
than a person would. Each test makes the attack and passes only when it fails: a refused form is
the app's own page and stores nothing, no input causes a server error, concurrent writes neither
stall nor break the audit log, an overload is answered at once while the health check keeps
answering, the demo's storage stays inside its budget and restarts clean, and one address cannot
take every save for itself. Each test was seen to fail with its protection switched off.

Some tests run the app in a real server on a free local port, since timeouts, keep-alive and
requests in flight exist only there."""

import http.client
import re
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from homecare import cli
from homecare.app import auditlog, auth, db, main
from test_screens import LAST_DAY, make, sign_in, visit_to_write

# The largest legal note: 4,000 characters of four bytes each, percent-encoded three bytes per
# byte by the browser, about 48 KB.
LONGEST_NOTE = "😀" * main.LONGEST_NOTE
CF = "cf-connecting-ip"  # the visitor's address, as Cloudflare passes it to the app


def post_routes(app: FastAPI) -> list[str]:
    """Every POST address in the app's route table, with a stand-in for each path part."""
    found = []
    for route in app.routes:
        if isinstance(route, APIRoute) and "POST" in (route.methods or set()):
            found.append(re.sub(r"\{[^}]+\}", "x", route.path))
    return found


def is_page(response: Any, status: int) -> bool:
    """The app's own page with this status: HTML, marked SYNTHETIC, with a way back."""
    return (
        response.status_code == status
        and response.headers["content-type"].startswith("text/html")
        and "SYNTHETIC" in response.text
        and 'href="/' in response.text
    )


def app_notes(path: Path) -> int:
    connection = db.connect(path)
    count = connection.execute("SELECT COUNT(*) FROM notes WHERE source = 'app'").fetchone()[0]
    connection.close()
    return int(count)


def note_form(client: TestClient, visit: str) -> dict[str, str]:
    page = client.get(f"/visits/{visit}/note").text
    fields = dict(re.findall(r'name="(csrf|key|amends)" value="([^"]*)"', page))
    return fields


# Forms: size, encoding, fields, time.


def test_an_oversized_form_is_refused_with_a_page_on_every_post_route(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    big = b"body=" + b"a" * (64 * 1024)  # past the promised limit of 64 KiB, set here, not read
    routes = post_routes(client.app)  # type: ignore[arg-type]
    assert routes, "the route table lists no POST address"
    for route in routes:
        told = client.post(route, content=big, headers={"content-type": main.FORM_TYPE})
        assert is_page(told, 413), f"{route} with Content-Length: {told.status_code}"
        chunks = (big[start : start + 8192] for start in range(0, len(big), 8192))
        streamed = client.post(route, content=chunks, headers={"content-type": main.FORM_TYPE})
        assert is_page(streamed, 413), f"{route} sent in chunks: {streamed.status_code}"


def test_the_longest_note_a_carer_can_write_still_saves(tmp_path: Path) -> None:
    client, settings = make(tmp_path)
    visit = visit_to_write(tmp_path)
    sign_in(client, visit["carer"])
    fields = note_form(client, visit["id"]) | {"body": LONGEST_NOTE}
    sent = client.post(f"/visits/{visit['id']}/notes", data=fields, follow_redirects=False)
    assert sent.status_code == 303 and "sent=1" in sent.headers["location"]
    assert app_notes(settings["database"]) == 1


def test_a_form_that_is_not_utf8_is_refused_with_a_page(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    faulty = TestClient(client.app, base_url="https://testserver", raise_server_exceptions=False)
    faulty.get("/signin")
    refused = faulty.post(
        "/signin", content=b"csrf=\xff\xfe&user=lead", headers={"content-type": main.FORM_TYPE}
    )
    assert is_page(refused, 400)


def test_a_form_with_too_many_fields_is_refused_with_a_page(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    client.get("/signin")
    many = "&".join(f"f{number}=1" for number in range(201))  # past the promised 200
    refused = client.post("/signin", content=many, headers={"content-type": main.FORM_TYPE})
    assert is_page(refused, 400)


# Values no screen offers.


@pytest.mark.parametrize("day", ["²", "1" * 5000, "٢٧", "-1", ""])
def test_a_day_that_is_not_in_the_week_opens_the_last_day(tmp_path: Path, day: str) -> None:
    """`²` counts as a digit for `isdigit` but not for `int`; 5,000 digits pass `isdigit` but are
    past Python's limit for turning text into a number."""
    client, _ = make(tmp_path)
    faulty = TestClient(client.app, base_url="https://testserver", raise_server_exceptions=False)
    visit = visit_to_write(tmp_path)
    for user, route in [(visit["carer"], "/today"), ("lead", "/queue"), ("coordinator", "/rota")]:
        sign_in(faulty, user)
        opened = faulty.get(route, params={"day": day})
        assert opened.status_code == 200, f"{route}?day={day[:10]}: {opened.status_code}"


@pytest.mark.parametrize("token", ["é", "x" * 43 + "é", "\x00"])
def test_a_csrf_token_with_any_characters_is_refused_with_a_page(
    tmp_path: Path, token: str
) -> None:
    client, _ = make(tmp_path)
    faulty = TestClient(client.app, base_url="https://testserver", raise_server_exceptions=False)
    faulty.get("/signin")
    refused = faulty.post("/signin", data={"csrf": token, "user": "lead"})
    assert is_page(refused, 403)


def test_a_visitor_cookie_the_server_did_not_make_is_replaced(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    faulty = TestClient(client.app, base_url="https://testserver", raise_server_exceptions=False)
    sign_in(faulty, "lead")
    session = faulty.cookies.get(auth.COOKIE)
    cookie = f"{auth.COOKIE}={session}; {main.VISITOR}=".encode() + "é".encode()
    opened = faulty.get(f"/queue?day={LAST_DAY}", headers={b"cookie": cookie})
    assert opened.status_code == 200
    assert f"{main.VISITOR}=" in opened.headers.get("set-cookie", "")


# Sessions.


def test_expired_sessions_are_removed_when_a_session_starts(tmp_path: Path) -> None:
    client, settings = make(tmp_path)
    connection = db.connect(settings["database"])
    old = time.time() - auth.IDLE_SECONDS - 60
    for number in range(1000):
        connection.execute(
            "INSERT INTO sessions VALUES (?, NULL, 'x', ?, ?)", (f"old-{number}", old, old)
        )
    connection.commit()
    client.get("/signin")
    left = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    connection.close()
    assert left == 1


# The audit log under concurrent writes.


def test_a_write_and_a_logged_view_at_the_same_moment_wait_in_line_not_in_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One request is part-way through a write when another request logs a page view. The view
    must wait its turn in the app, never inside SQLite, whose waiting writers keep no order and
    can wait out the whole busy timeout under load. The view's connection is given no busy
    timeout at all, so meeting the first write inside SQLite fails at once. (Before the fix, the
    two waited on each other until the database gave up: a deadlock.)"""
    path = tmp_path / "audit.db"
    anchor = tmp_path / "audit.anchor"
    key = b"k" * 32
    connection = db.connect(path)
    db.migrate(connection)
    connection.close()
    failures: list[BaseException] = []

    def log_a_view() -> None:
        viewer = db.connect(path)
        viewer.execute("PRAGMA busy_timeout = 0")
        try:
            auditlog.record(viewer, key, anchor, {"event": "view", "user": "lead"})
        except BaseException as error:  # noqa: BLE001 - the test reports any failure
            failures.append(error)
        finally:
            viewer.close()

    started = time.monotonic()
    writer = db.connect(path)
    view = threading.Thread(target=log_a_view)
    with db.WRITE:  # as every write in the app does, from its first change to its commit
        writer.execute("INSERT INTO sessions VALUES ('t', NULL, 'c', 0, 0)")
        view.start()
        time.sleep(0.3)  # the view arrives while the write is open
        auditlog.record(writer, key, anchor, {"event": "write note", "user": "K08"})
    view.join(timeout=10)
    took = time.monotonic() - started
    assert not failures, failures
    assert took < 1.5, f"took {took:.1f} s"
    check = db.connect(path)
    assert auditlog.problems(check, key, anchor) == []
    events = [item["entry"]["event"] for item in auditlog.chain(check)]
    assert sorted(events) == ["view", "write note"]
    writer.close()
    check.close()


# Storage budget and restart.


def test_a_full_demo_refuses_the_write_whole_and_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restarts: list[bool] = []
    monkeypatch.setattr(main, "restart", lambda: restarts.append(True), raising=False)
    client, settings = make(tmp_path)
    budget = 2 * settings["database"].stat().st_size + 512 * 1024
    settings["budget"] = budget
    client = TestClient(main.create_app(settings), base_url="https://testserver")
    visit = visit_to_write(tmp_path)
    sign_in(client, visit["carer"])
    # A long note needs a new page of the database file, which a full database cannot add.
    fields = note_form(client, visit["id"]) | {"body": "Lunch made. " + "x" * 3900}
    # Fill the budget the way many visitors would, then send one more note.
    filler = db.connect(settings["database"], budget)
    filled = False
    for number in range(budget // 4000 + 10):
        try:
            filler.execute(
                "INSERT INTO sessions VALUES (?, NULL, ?, 1e12, 1e12)", (f"f{number}", "x" * 4000)
            )
            filler.commit()
        except sqlite3.OperationalError:
            filled = True
            break
    filler.close()
    assert filled, "the database grew past its budget"
    before = app_notes(settings["database"])
    sent = client.post(f"/visits/{visit['id']}/notes", data=fields, follow_redirects=False)
    assert is_page(sent, 503) and "restarting" in sent.text
    assert sent.headers.get("retry-after")
    assert restarts == [True]
    assert app_notes(settings["database"]) == before
    connection = db.connect(settings["database"])
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_a_send_pressed_twice_at_once_is_stored_once(tmp_path: Path) -> None:
    """A carer taps Send twice on a slow signal, and two identical forms arrive together. Both
    must be told the note was sent, and it must be stored once. Looking for an earlier copy and
    storing the note are one step under the write lock; split, both would find no copy, and the
    second would break the one-note-per-key rule with a server error."""
    client, settings = make(tmp_path)
    faulty = TestClient(client.app, base_url="https://testserver", raise_server_exceptions=False)
    visit = visit_to_write(tmp_path)
    sign_in(faulty, visit["carer"])
    fields = note_form(faulty, visit["id"]) | {"body": "Lunch made."}
    answers: list[int] = []

    def send() -> None:
        sent = faulty.post(f"/visits/{visit['id']}/notes", data=fields, follow_redirects=False)
        answers.append(sent.status_code)

    both = [threading.Thread(target=send) for _ in range(2)]
    with db.WRITE:  # hold every write back until both forms have arrived
        for thread in both:
            thread.start()
        time.sleep(0.5)
    for thread in both:
        thread.join()
    assert answers == [303, 303]
    assert app_notes(settings["database"]) == 1


# Saves per address.


def test_one_address_cannot_save_past_its_limit_but_another_still_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "SAVES_PER_WINDOW", 4, raising=False)
    monkeypatch.setattr(main, "WINDOW_SECONDS", 1.0, raising=False)
    client, settings = make(tmp_path)
    visit = visit_to_write(tmp_path)
    one = {CF: "203.0.113.5"}
    token = re.findall(r'name="csrf" value="([^"]+)"', client.get("/signin").text)[0]
    client.post("/signin", data={"csrf": token, "user": visit["carer"]}, headers=one)  # save 1
    for words in ["One.", "Two.", "Three."]:  # saves 2 to 4
        fields = note_form(client, visit["id"]) | {"body": words}
        client.post(f"/visits/{visit['id']}/notes", data=fields, headers=one)
    fields = note_form(client, visit["id"]) | {"body": "Four."}
    refused = client.post(f"/visits/{visit['id']}/notes", data=fields, headers=one)
    assert is_page(refused, 429) and refused.headers.get("retry-after")
    assert app_notes(settings["database"]) == 3
    other = client.post(
        f"/visits/{visit['id']}/notes", data=fields, headers={CF: "198.51.100.7"},
        follow_redirects=False,
    )  # fmt: skip
    assert other.status_code == 303
    time.sleep(1.1)  # the next window
    fields = note_form(client, visit["id"]) | {"body": "Five."}
    again = client.post(
        f"/visits/{visit['id']}/notes", data=fields, headers=one, follow_redirects=False
    )
    assert again.status_code == 303
    assert app_notes(settings["database"]) == 5


# A real server: timeouts, keep-alive and requests in flight.


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """The demo app in a real server with the live settings, on a free local port."""
    _, settings = make(tmp_path)
    app = main.create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                            **cli.server_config())  # fmt: skip
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield port, settings
    server.should_exit = True
    thread.join(timeout=15)


def raw_request(port: int, head: bytes, body: bytes, wait: float) -> bytes:
    """Send a request's head and part of its body, then keep the connection open; the answer."""
    with socket.create_connection(("127.0.0.1", port), timeout=wait) as connection:
        connection.sendall(head + body)
        answer = b""
        try:
            while chunk := connection.recv(65536):
                answer += chunk
        except TimeoutError:
            pass
        return answer


def slow_form(path: str, length: int, address: str = "127.0.0.1") -> bytes:
    return (
        f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: {main.FORM_TYPE}\r\n"
        f"Content-Length: {length}\r\n{CF}: {address}\r\n\r\n"
    ).encode()


def test_a_form_that_stops_arriving_is_refused_in_time(
    served: tuple[int, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "FORM_SECONDS", 1.0)
    port, _ = served
    started = time.monotonic()
    answer = raw_request(port, slow_form("/signin", 1000), b"csrf=abc", wait=6)
    assert answer.startswith(b"HTTP/1.1 408"), answer[:80]
    assert time.monotonic() - started < 4


def test_one_address_cannot_hold_more_than_its_share_of_slow_forms(
    served: tuple[int, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "FORM_SECONDS", 5.0)
    port, _ = served
    held = []
    for _ in range(main.FORMS_AT_ONCE):
        connection = socket.create_connection(("127.0.0.1", port), timeout=10)
        connection.sendall(slow_form("/signin", 1000, "203.0.113.9") + b"csrf=")
        held.append(connection)
    time.sleep(0.3)
    started = time.monotonic()
    answer = raw_request(port, slow_form("/signin", 1000, "203.0.113.9"), b"csrf=", wait=3)
    assert answer.startswith(b"HTTP/1.1 429"), answer[:80]
    assert time.monotonic() - started < 1
    for connection in held:
        connection.close()


def test_a_post_on_a_connection_left_idle_is_still_served(
    served: tuple[int, dict[str, Any]],
) -> None:
    """cloudflared keeps idle connections to the app for 90 s (its documented default) and sends
    the next request on one; the app must not have closed it first."""
    port, _ = served
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", "/healthz")
    connection.getresponse().read()
    time.sleep(6)  # longer than uvicorn's default of 5 s
    try:
        connection.request(
            "POST", "/signin", body="csrf=x", headers={"content-type": main.FORM_TYPE}
        )
        status = connection.getresponse().status
    except ConnectionError as error:
        pytest.fail(f"the app closed the idle connection: {error!r}")
    assert status == 403
    connection.close()
    assert cli.server_config()["timeout_keep_alive"] > 90


def slow_signin_page(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Make every new sign-in page take `seconds`, standing in for a slow page."""
    start = auth.start_session

    def slow(*args: Any) -> str:
        time.sleep(seconds)
        return start(*args)

    monkeypatch.setattr(auth, "start_session", slow)


def get(port: int, path: str, timeout: float = 30) -> tuple[int, float]:
    started = time.monotonic()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    connection.request("GET", path)
    status = connection.getresponse().status
    connection.close()
    return status, time.monotonic() - started


def test_past_the_limit_a_request_gets_the_busy_page_at_once(
    served: tuple[int, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "IN_FLIGHT", 3)
    slow_signin_page(monkeypatch, 1.5)
    port, _ = served
    slow = [threading.Thread(target=get, args=(port, "/signin")) for _ in range(3)]
    for thread in slow:
        thread.start()
    time.sleep(0.3)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    started = time.monotonic()
    connection.request("GET", "/guide")
    answer = connection.getresponse()
    page = answer.read().decode()
    assert answer.status == 503 and "SYNTHETIC" in page and answer.getheader("retry-after")
    assert time.monotonic() - started < 0.2
    for thread in slow:
        thread.join()


def test_the_health_check_answers_while_every_worker_thread_is_busy(
    served: tuple[int, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kubelet restarts a container whose health check misses three times, which would wipe
    the demo; an overload must never look like a dead app."""
    slow_signin_page(monkeypatch, 2.0)
    port, _ = served
    slow = [threading.Thread(target=get, args=(port, "/signin")) for _ in range(45)]
    for thread in slow:
        thread.start()
    time.sleep(0.5)  # the 40 worker threads are all busy
    status, took = get(port, "/healthz")
    assert status == 200 and took < 0.5, f"{status} after {took:.2f} s"
    for thread in slow:
        thread.join()


def test_the_seed_leaves_most_of_the_budget_free(tmp_path: Path) -> None:
    """A demo that started nearly full would restart again and again."""
    _, settings = make(tmp_path)
    assert settings["database"].stat().st_size < main.DEMO_BUDGET / 2
