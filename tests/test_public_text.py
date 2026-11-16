"""Nothing from inside the app reaches a screen. No page a person can open shows a hash or commit
id, a model or model-file name, a command-line command, or a word about how the app is built and
run ("pipeline", "laptop", "SHA-256"). Unknown addresses and server faults answer with a page,
never JSON or a traceback, and the API documentation pages do not exist.

The screens come from the app's own route table, so a new screen this test does not open fails
the coverage check. Each check was seen to fail with the old text or setting put back."""

import html
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from homecare import models
from homecare.app import db, main, screens
from homecare.notes import routing
from test_carer_app import send
from test_referral_screens import FIRST, PROFILE, confirm
from test_screens import LAST_DAY, make, sign_in, visit_to_write

COMMIT = "0123456789abcdef0123456789abcdef01234567"  # stands in for the build's commit
HEX_RUN = re.compile(r"[0-9a-f]{12,}", re.IGNORECASE)  # hashes and commit ids
# `homecare` is the command-line program (`cli.py`); the screens write the product "Home-care".
COMMAND = re.compile(r"\bhomecare [a-z]")
# Words about how the app is built, deployed and run, which no screen needs.
INTERNAL = re.compile(r"SHA-256|\b[Pp]ipeline|\b[Ll]aptop|\bUTC\b|online copy|demo start|\bBuild\b")
SERVICE_WORKER = Path(__file__).resolve().parents[1] / "web" / "sw" / "sw.ts"


def visible(page: str) -> str:
    """What a person can read or hear: the text, the page title and the labels read aloud."""
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.DOTALL)
    labels = re.findall(r'\b(?:aria-label|alt|title)="([^"]*)"', page)
    text = re.sub(r"<[^>]+>", " ", page)
    return html.unescape(" ".join([text, *labels]))


def leaks(text: str) -> list[str]:
    found = HEX_RUN.findall(text) + COMMAND.findall(text) + INTERNAL.findall(text)
    lowered = text.lower()
    for item in models.read():
        for key in ("name", "file", "folder", "source"):  # an encoder has a folder, not a file
            value = item.get(key)
            if value and value.lower() in lowered:
                found.append(value)
    return found


def html_routes(client: TestClient) -> set[str]:
    """Every page a person can open: the app's GET routes that answer with a page."""
    found = set()
    for route in client.app.routes:  # type: ignore[attr-defined]
        if isinstance(route, APIRoute) and "GET" in (route.methods or set()):
            if route.response_class is HTMLResponse:
                found.add(route.path)
    return found


def note_ids(tmp_path: Path) -> dict[str, str]:
    """One note of each kind the lead can open: the first in the register, a random check, a
    note tagged for possible tampering, and one whose urgent findings all came from sentences
    read alone."""
    connection = db.connect(tmp_path / "demo.db")
    queue = screens.day_queue(connection, LAST_DAY, None)
    register = screens.register(connection, LAST_DAY, None)
    rows = [row for section in register["sections"] for row in section["rows"]]
    tampered = connection.execute("SELECT id FROM notes WHERE tamper = 1").fetchone()
    return {
        "first": queue[0]["note"],
        "random check": next(i["note"] for i in queue if i["section"] == routing.RANDOM_CHECK),
        "tampering": tampered["id"],
        "sentence only": next(row["note"] for row in rows if "check the context" in row["flags"]),
    }


def every_page(client: TestClient, tmp_path: Path) -> tuple[dict[str, str], set[str]]:
    """Every screen in its main states, as a visitor reaches them, and the routes opened."""
    pages: dict[str, str] = {}
    opened: set[str] = set()

    def get(name: str, route: str, url: str) -> None:
        pages[name] = client.get(url).text
        opened.add(route)

    get("guide", "/guide", "/guide")
    get("sign-in", "/signin", "/signin")
    later = visit_to_write(tmp_path, begun=False)
    sign_in(client, later["carer"])
    get("note not begun", "/visits/{visit_id}/note", f"/visits/{later['id']}/note")
    visit = visit_to_write(tmp_path)
    sign_in(client, visit["carer"])
    get("carer's day", "/today", f"/today?day={LAST_DAY}")
    get("write a note", "/visits/{visit_id}/note", f"/visits/{visit['id']}/note")
    send(client, visit["id"], "Lunch made and eaten.")  # waits for the lead, not read by AI
    get("no access", "/queue", "/queue")
    sign_in(client, "lead")
    get("register", "/queue", f"/queue?day={LAST_DAY}")
    connection = db.connect(tmp_path / "demo.db")
    unread = connection.execute("SELECT id FROM notes WHERE source = 'app'").fetchone()["id"]
    get("note not checked by AI", "/notes/{note_id}", f"/notes/{unread}")
    for kind, note in note_ids(tmp_path).items():
        get(f"note, {kind}", "/notes/{note_id}", f"/notes/{note}")
    get("no such note", "/notes/{note_id}", "/notes/no-such-note")
    sign_in(client, "manager")
    get("team", "/team", "/team")
    sign_in(client, "coordinator")
    get("rota", "/rota", f"/rota?day={LAST_DAY}&carer={visit['carer']}")
    get("re-plan", "/rota/replan", "/rota/replan")
    get("visit import", "/imports", "/imports")
    get("referrals", "/referrals", "/referrals")
    get("referral draft", "/referrals/{referral_id}", f"/referrals/{FIRST}")
    unasked = {name: value for name, value in PROFILE.items() if name != "asked"}
    pages["referral refused"] = confirm(client, FIRST, unasked).text
    confirm(client, FIRST, PROFILE)
    get("referral confirmed", "/referrals/{referral_id}", f"/referrals/{FIRST}")
    get("page not found", "", "/no-such-page")
    get("wrong method", "", "/signout")
    worker = SERVICE_WORKER.read_text(encoding="utf-8")
    found = re.search(r"const NOT_SAVED = `(.*?)`;", worker, re.DOTALL)
    assert found, "the service worker's offline page was not found"
    pages["offline page"] = found.group(1)
    return pages, opened


def test_the_scan_catches_the_old_screen_text() -> None:
    """The text these screens used to show is caught, so a clean scan means something."""
    old = (
        "Read on the laptop by gemma-4-26b-a4b, pipeline v2. Prompt SHA-256 "
        "c24f097bd2f451d4f933e8bc7d8d539f. On the laptop: homecare platform import FILE. "
        "16 November 2026 10:44 UTC, by demo start. Build 0574e0f902f5"
    )
    found = leaks(old)
    for expected in ["gemma-4-26b-a4b", "pipeline", "SHA-256", "Build", "UTC", "demo start"]:
        assert expected in found
    assert "homecare p" in found and "0574e0f902f5" in found
    assert leaks("Dutch tea at 16:30 on Sunday 8 November 2026") == []


def test_no_screen_shows_anything_from_inside_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "BUILD", COMMIT[:12])
    client, _ = make(tmp_path)
    assert COMMIT[:12] in client.get("/healthz").text  # the build id is set, so a leak would show
    pages, opened = every_page(client, tmp_path)
    assert html_routes(client) - opened == set(), "screens this test never opens"
    found = {name: leaks(visible(page)) for name, page in pages.items()}
    assert {name: words for name, words in found.items() if words} == {}


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/no-such-page", 404),
        ("/signout", 405),
        ("/docs", 404),
        ("/redoc", 404),
        ("/openapi.json", 404),
    ],
)
def test_unknown_addresses_answer_with_a_page_and_there_are_no_api_docs(
    tmp_path: Path, path: str, status: int
) -> None:
    client, _ = make(tmp_path)
    response = client.get(path)
    assert response.status_code == status
    assert response.headers["content-type"].startswith("text/html")
    assert "SYNTHETIC" in response.text and 'href="/"' in response.text
    assert response.headers["content-security-policy"] == main.HEADERS["Content-Security-Policy"]


def test_a_server_fault_shows_a_page_without_its_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any) -> None:
        raise RuntimeError("internal detail C:/srv/homecare/app.py line 1")

    client, _ = make(tmp_path)
    faulty = TestClient(client.app, base_url="https://testserver", raise_server_exceptions=False)
    sign_in(faulty, "manager")
    monkeypatch.setattr(screens, "team", broken)
    response = faulty.get("/team")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/html")
    assert "Something went wrong" in response.text and "SYNTHETIC" in response.text
    assert "Traceback" not in response.text and "internal detail" not in response.text
    assert response.headers["content-security-policy"] == main.HEADERS["Content-Security-Policy"]
