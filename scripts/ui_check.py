"""The accessibility check for the platform screens: axe-core's WCAG 2.0, 2.1 and 2.2 A
and AA rules in Chromium through Playwright, at desktop and phone widths, in light and dark.

Laptop only (needs the Chromium build Playwright 1.63 uses): `uv run python scripts/ui_check.py`.
It starts the app in demo mode on a free local port, signs in through the real pages as the
safeguarding lead and as a carer, runs axe on every screen, and saves each screenshot into
`.impeccable/review/`. It exits 1 if axe finds any violation.

axe-core 4.13.0 (MPL-2.0) is in `tests/ui/axe.min.js`, copied unchanged from its npm package and
checked against its SHA-256 here. Scripts are injected with CSP bypassed for the test browser
only; the app itself still sends `script-src 'none'`.
"""

import hashlib
import json
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Literal

import uvicorn
from playwright.sync_api import Browser, Page, ViewportSize, sync_playwright

from homecare.app import db, main, screens
from homecare.notes import routing

AXE = Path("tests/ui/axe.min.js")
AXE_SHA256 = "c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1"
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
SIZES = {"desktop": (1280, 900), "phone": (390, 844)}
SCHEMES: tuple[Literal["light", "dark"], ...] = ("light", "dark")
OUT = Path(".impeccable/review")
DAY = 27


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_app(folder: Path) -> tuple[str, uvicorn.Server]:
    settings = {
        "mode": "demo",
        "database": folder / "ui.db",
        "anchor": folder / "ui.anchor",
        "key": secrets.token_bytes(32),
    }
    port = free_port()
    config = uvicorn.Config(
        main.create_app(settings), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/healthz", timeout=1)
            return url, server
        except OSError:
            time.sleep(0.1)
    raise SystemExit("the app did not start")


def new_page(browser: Browser, width: int, height: int, scheme: Literal["light", "dark"]) -> Page:
    """A fresh signed-out browser at one size and colour scheme; scripts may be injected (axe)."""
    viewport: ViewportSize = {"width": width, "height": height}
    context = browser.new_context(bypass_csp=True, viewport=viewport, color_scheme=scheme)
    return context.new_page()


def axe(page: Page) -> list[dict[str, Any]]:
    page.add_script_tag(path=str(AXE))
    found: list[dict[str, Any]] = page.evaluate(
        "async (tags) => (await axe.run(document, {runOnly: {type: 'tag', values: tags}}))"
        ".violations.map(v => ({id: v.id, impact: v.impact, help: v.help,"
        " nodes: v.nodes.slice(0, 3).map(n => n.target.join(' '))}))",
        TAGS,
    )
    return found


def check(page: Page, name: str, size: str, scheme: str, report: list[dict[str, Any]]) -> None:
    page.screenshot(path=str(OUT / f"{name}-{size}-{scheme}.png"), full_page=True)
    for violation in axe(page):
        report.append({"screen": name, "size": size, "scheme": scheme, **violation})


def main_check() -> int:
    if hashlib.sha256(AXE.read_bytes()).hexdigest() != AXE_SHA256:
        raise SystemExit("tests/ui/axe.min.js differs from its pin")
    OUT.mkdir(parents=True, exist_ok=True)
    report: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as folder:
        url, server = start_app(Path(folder))
        connection = db.connect(Path(folder) / "ui.db")
        queue = screens.day_queue(connection, DAY)
        first = queue[0]["note"]
        random_check = next(i["note"] for i in queue if i["section"] == routing.RANDOM_CHECK)
        tampered = connection.execute("SELECT id FROM notes WHERE tamper = 1").fetchone()[0]
        carer = connection.execute("SELECT carer FROM notes WHERE id = ?", (first,)).fetchone()[0]
        connection.close()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for scheme in SCHEMES:
                for size, (width, height) in SIZES.items():
                    lead = new_page(browser, width, height, scheme)
                    lead.goto(url + "/signin")
                    check(lead, "signin", size, scheme, report)
                    lead.get_by_role("button", name="Sign in as the safeguarding lead").click()
                    if "/queue" not in lead.url:
                        raise SystemExit(f"sign-in did not reach the register: {lead.url}")
                    lead.goto(f"{url}/queue?day={DAY}")
                    check(lead, "register", size, scheme, report)
                    lead.goto(f"{url}/notes/{first}")
                    check(lead, "note", size, scheme, report)
                    lead.get_by_role("radio", name="Wrong category").first.check()
                    check(lead, "note-wrong-category", size, scheme, report)
                    lead.get_by_role("radio", name="Confirmed").first.check()
                    lead.get_by_role("button", name="Save and open the next note").click()
                    lead.goto(f"{url}/queue?day={DAY}")
                    check(lead, "register-decided", size, scheme, report)
                    lead.goto(f"{url}/notes/{first}")
                    check(lead, "note-decided", size, scheme, report)
                    lead.goto(f"{url}/notes/{random_check}")
                    check(lead, "note-random-check", size, scheme, report)
                    lead.goto(f"{url}/notes/{tampered}")
                    check(lead, "note-tampering", size, scheme, report)
                    worker = new_page(browser, width, height, scheme)
                    worker.goto(url + "/signin")
                    worker.get_by_label("Carer").select_option(carer)
                    worker.get_by_role("button", name="Sign in as this carer").click()
                    worker.goto(f"{url}/today?day={DAY}")
                    check(worker, "today", size, scheme, report)
                    worker.goto(f"{url}/queue")
                    check(worker, "forbidden", size, scheme, report)
            browser.close()
        server.should_exit = True
    (OUT / "axe.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    for row in report:
        print(f"{row['screen']} {row['size']} {row['scheme']}: {row['id']} ({row['impact']}) "
              f"{row['help']} {row['nodes']}")  # fmt: skip
    screens_checked = 10 * len(SIZES) * len(SCHEMES)
    print(f"axe: {len(report)} violations on {screens_checked} screen captures")
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main_check())
