"""The accessibility check for the platform screens: axe-core's WCAG 2.0, 2.1 and 2.2 A
and AA rules in Chromium through Playwright, at desktop, tablet and phone widths, in light and
dark.

Laptop only (needs the Chromium build Playwright 1.63 uses): `uv run python scripts/ui_check.py`.
It starts the app in demo mode on a free local port, signs in through the real pages as the
safeguarding lead, the manager, the coordinator and a carer (with the carer app's offline
state), runs axe on every screen, and saves each screenshot into
`.impeccable/review/`. It exits 1 if axe finds any violation.

axe-core 4.13.0 (MPL-2.0) is in `tests/ui/axe.min.js`, copied unchanged from its npm package and
checked against its SHA-256 here. axe is injected with CSP bypassed for the test browser only;
the app's own CSP is checked with nothing bypassed in `scripts/pwa_check.py`.
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
import zlib
from pathlib import Path
from typing import Any, Literal

import uvicorn
from playwright.sync_api import Browser, BrowserContext, Page, Route, ViewportSize, sync_playwright

from homecare.app import db, main, screens, seed
from homecare.notes import routing

AXE = Path("tests/ui/axe.min.js")
AXE_SHA256 = "c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1"
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
SIZES = {"desktop": (1280, 900), "tablet": (768, 1024), "phone": (390, 844)}
SCHEMES: tuple[Literal["light", "dark"], ...] = ("light", "dark")
OUT = Path(".impeccable/review")
DAY = 27
CARER = "K01"  # has visits still to write up by the demo clock


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


TILE_HOST = "https://tile.openstreetmap.org/"
# The browser can never reach OSM's tile server from a check: its usage policy forbids automated
# use, so its name resolves to this machine and every tile request is answered below.
NO_OSM = ["--host-resolver-rules=MAP tile.openstreetmap.org 127.0.0.1"]


def blank_tile() -> bytes:
    """A one-pixel transparent PNG, built from its parts so no image file is needed."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        size = len(data).to_bytes(4, "big")
        return size + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    header = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
    pixels = zlib.compress(bytes(5))
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def serve_tiles(context: BrowserContext, seen: list[dict[str, str]]) -> None:
    """Answer every OSM tile request with a blank tile, recording its address and Referer."""
    tile = blank_tile()

    def answer(route: Route) -> None:
        referer = route.request.headers.get("referer", "")
        seen.append({"url": route.request.url, "referer": referer})
        route.fulfill(status=200, content_type="image/png", body=tile)

    context.route(TILE_HOST + "**", answer)


def new_page(browser: Browser, width: int, height: int, scheme: Literal["light", "dark"]) -> Page:
    """A fresh signed-out browser at one size and colour scheme; scripts may be injected (axe)."""
    viewport: ViewportSize = {"width": width, "height": height}
    context = browser.new_context(bypass_csp=True, viewport=viewport, color_scheme=scheme)
    serve_tiles(context, [])
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


def settled(page: Page) -> None:
    """Wait until a page change's view transition (and any other animation) has finished, so a
    capture or an axe run sees one page, not two cross-fading. Right after a click the old page
    can still be showing with nothing animating yet, so the new page must load first and the
    check waits past the transition's start before it asks."""
    page.wait_for_load_state("load")
    page.wait_for_timeout(300)
    page.wait_for_function("document.getAnimations().length === 0", timeout=10_000)


def sideways(page: Page) -> list[dict[str, Any]]:
    """Reflow (WCAG 1.4.10), which axe does not test: the page must not scroll sideways."""
    extra: int = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    if extra <= 0:
        return []
    help_text = f"the page scrolls sideways by {extra} px"
    return [{"id": "reflow", "impact": "serious", "help": help_text, "nodes": []}]


def check(page: Page, name: str, size: str, scheme: str, report: list[dict[str, Any]]) -> None:
    settled(page)
    page.screenshot(path=str(OUT / f"{name}-{size}-{scheme}.png"), full_page=True)
    for violation in axe(page) + sideways(page):
        report.append({"screen": name, "size": size, "scheme": scheme, **violation})


def driving_next() -> str:
    """A carer whose next visit by the demo clock needs a drive, so the sheet shows "Leave by"."""
    demo = seed.load()
    now = demo["now"]
    routes: dict[str, list[dict[str, Any]]] = {}
    for visit in demo["visits"]:
        if visit["carer"] and visit["day"] == now["day"]:
            routes.setdefault(visit["carer"], []).append(visit)
    for carer, visits in sorted(routes.items()):
        later = [
            v for v in sorted(visits, key=lambda v: v["position"]) if v["starts"] > now["minute"]
        ]
        if later and later[0]["drive"] > 0:
            return carer
    raise SystemExit("no carer has a next visit that needs a drive")


def carer_app(
    browser: Browser,
    url: str,
    width: int,
    height: int,
    scheme: Literal["light", "dark"],
    size: str,
    report: list[dict[str, Any]],
) -> None:
    """The carer app's screens and states: the note form and its error, a sent note, the sheet
    offline with a note waiting, then, as the same demo visitor, the lead's register with the
    note not yet read and the note itself."""
    page = new_page(browser, width, height, scheme)
    page.goto(url + "/signin")
    page.get_by_label("Carer").select_option(CARER)
    page.get_by_role("button", name="Sign in as this carer").click()
    page.evaluate("navigator.serviceWorker.ready.then(() => true)")
    page.goto(url + "/today")
    demo = seed.load()
    today = [v for v in demo["visits"] if v["carer"] == CARER and v["day"] == demo["now"]["day"]]
    later = next(v["id"] for v in today if not screens.started(v, demo["now"]))
    page.goto(f"{url}/visits/{later}/note")
    check(page, "write-not-begun", size, scheme, report)
    page.goto(url + "/today")
    write = page.locator(".write-up a.primary").first.get_attribute("href")
    page.goto(url + str(write))
    check(page, "write", size, scheme, report)
    page.get_by_role("textbox", name="Your note").fill(" ")
    page.evaluate("document.querySelector('form.compose').submit()")  # the server's own check
    check(page, "write-error", size, scheme, report)
    page.goto(url + str(write))
    page.get_by_role("textbox", name="Your note").fill("Tea made; she ate well. All tasks done.")
    page.get_by_role("button", name="Send the note").click()
    page.wait_for_url("**sent=1**")
    check(page, "today-sent", size, scheme, report)
    page.goto(url + str(write))  # now an addition, written offline below
    page.goto(url + "/today")
    page.context.set_offline(True)
    page.goto(url + str(write))
    page.get_by_role("textbox", name="Your addition").fill("Written with no signal.")
    page.get_by_role("button", name="Send the addition").click()
    page.wait_for_function("document.querySelector('.draft-state').textContent.length > 0")
    page.goto(url + "/today")
    page.wait_for_selector("[data-waiting]:not([hidden])")
    page.wait_for_selector("[data-offline]:not([hidden])")
    check(page, "today-offline", size, scheme, report)
    page.context.set_offline(False)
    page.goto(url + "/signin")
    page.get_by_role("button", name="Sign in as the safeguarding lead").click()
    page.goto(f"{url}/queue?day={DAY}")
    check(page, "register-unread", size, scheme, report)
    unread = page.locator("ol[aria-labelledby='band-unread'] a").first.get_attribute("href")
    page.goto(url + str(unread))
    check(page, "note-unread", size, scheme, report)


def first_view(page: Page, name: str, size: str, scheme: str) -> None:
    """The viewport only, once the page change has settled."""
    settled(page)
    page.screenshot(path=str(OUT / f"first-{name}-{size}-{scheme}.png"))


def first_views(browser: Browser, url: str) -> None:
    """What a person sees without scrolling (full-page captures stitch the page, which draws the
    sticky action bar mid-page): the carer's day and note form, the register, a note and the note
    scrolled to its decisions, at phone and desktop sizes in both themes. Each waits for the page
    change to settle. One more capture is taken on purpose during the change, with animations
    slowed twentyfold, as evidence of the view transition."""
    for size in ("phone", "desktop"):
        for scheme in SCHEMES:
            page = new_page(browser, *SIZES[size], scheme)
            page.goto(url + "/signin")
            page.get_by_label("Carer").select_option(CARER)
            page.get_by_role("button", name="Sign in as this carer").click()
            page.goto(f"{url}/today?day={DAY}")
            first_view(page, "today", size, scheme)
            page.locator(".write-up a.primary").first.click()
            page.wait_for_url("**/visits/**")
            first_view(page, "write", size, scheme)
            page.goto(url + "/signin")
            page.get_by_role("button", name="Sign in as the safeguarding lead").click()
            page.goto(f"{url}/queue?day={DAY}")
            first_view(page, "register", size, scheme)
            if size == "phone" and scheme == "light":
                cdp = page.context.new_cdp_session(page)
                cdp.send("Animation.enable")
                cdp.send("Animation.setPlaybackRate", {"playbackRate": 0.05})
                page.get_by_role("link", name="Open the first note to decide").click()
                page.wait_for_timeout(1200)
                page.screenshot(path=str(OUT / "first-page-change-mid-transition.png"))
                cdp.send("Animation.setPlaybackRate", {"playbackRate": 1})
            else:
                page.get_by_role("link", name="Open the first note to decide").click()
            page.wait_for_url("**/notes/**")
            first_view(page, "note", size, scheme)
            findings = page.get_by_role("heading", name="Findings and your decision")
            findings.scroll_into_view_if_needed()
            first_view(page, "note-decisions", size, scheme)
            page.context.close()


def control(browser: Browser, url: str) -> set[str]:
    """The positive control: the sign-in page with its text made faint, its language removed and
    an element too wide for a phone. Every check must report its fault, or a run of zero
    violations would prove nothing."""
    page = new_page(browser, *SIZES["phone"], "light")
    page.goto(url + "/signin")
    page.add_style_tag(
        content="body, body * { color: #c8c8c8 !important; } main::after { content: '';"
        " display: block; width: 40rem; height: 1px; }"
    )
    page.evaluate("document.documentElement.removeAttribute('lang')")
    found = {violation["id"] for violation in axe(page) + sideways(page)}
    page.context.close()
    return found


CONTROL_EXPECTS = {"color-contrast", "html-has-lang", "reflow"}
# What marks "you are here": the chosen day, the coordinator's current page, the open row.
CURRENT = ('.days [aria-current="date"]', '.switch [aria-current="page"]', ".row[aria-current]")


def forced_colours(browser: Browser, url: str, carer: str, note: int) -> list[dict[str, Any]]:
    """High-contrast modes (Windows contrast themes) drop backgrounds and shadows, so a current
    day, page or row marked only by those disappears; each must keep an outline. Each screen
    must show at least one such mark, so the check cannot pass on a page that has none."""
    context = browser.new_context(forced_colors="active", viewport={"width": 1280, "height": 900})
    serve_tiles(context, [])
    page = context.new_page()
    page.goto(url + "/signin")
    page.get_by_label("Carer").select_option(carer)
    page.get_by_role("button", name="Sign in as this carer").click()
    found = current_marks(page, "today", f"{url}/today?day={DAY}")
    page.goto(url + "/signin")
    page.get_by_role("button", name="Sign in as the safeguarding lead").click()
    found += current_marks(page, "note", f"{url}/notes/{note}")
    page.goto(url + "/signin")
    page.get_by_role("button", name="Sign in as the coordinator").click()
    found += current_marks(page, "rota-carer", f"{url}/rota?day={DAY}&carer=K08")
    page.screenshot(path=str(OUT / "rota-carer-forced-colours.png"))
    context.close()
    return found


def current_marks(page: Page, name: str, address: str) -> list[dict[str, Any]]:
    page.goto(address)
    settled(page)
    styles: list[str] = page.evaluate(
        "(selectors) => selectors.flatMap(s => [...document.querySelectorAll(s)])"
        ".map(e => getComputedStyle(e).outlineStyle)",
        list(CURRENT),
    )
    row = {"screen": name, "size": "desktop", "scheme": "forced colours", "impact": "serious"}
    if not styles:
        return [{**row, "id": "current-missing", "help": "no current mark found", "nodes": []}]
    hidden = [style for style in styles if style == "none"]
    if hidden:
        text = f"{len(hidden)} of {len(styles)} current marks vanish in high-contrast mode"
        return [{**row, "id": "current-not-marked", "help": text, "nodes": []}]
    return []


def main_check() -> int:
    if hashlib.sha256(AXE.read_bytes()).hexdigest() != AXE_SHA256:
        raise SystemExit("tests/ui/axe.min.js differs from its pin")
    OUT.mkdir(parents=True, exist_ok=True)
    report: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as folder:
        url, server = start_app(Path(folder))
        connection = db.connect(Path(folder) / "ui.db")
        queue = screens.day_queue(connection, DAY, None)
        first = queue[0]["note"]
        random_check = next(i["note"] for i in queue if i["section"] == routing.RANDOM_CHECK)
        tampered = connection.execute("SELECT id FROM notes WHERE tamper = 1").fetchone()[0]
        carer = driving_next()
        connection.close()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=NO_OSM)
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
                    manager = new_page(browser, width, height, scheme)
                    manager.goto(url + "/signin")
                    manager.get_by_role("button", name="Sign in as the manager").click()
                    manager.goto(f"{url}/team")
                    check(manager, "team", size, scheme, report)
                    worker = new_page(browser, width, height, scheme)
                    worker.goto(url + "/signin")
                    worker.get_by_label("Carer").select_option(carer)
                    worker.get_by_role("button", name="Sign in as this carer").click()
                    worker.goto(f"{url}/today?day={DAY}")
                    check(worker, "today", size, scheme, report)
                    worker.goto(f"{url}/queue")
                    check(worker, "forbidden", size, scheme, report)
                    carer_app(browser, url, width, height, scheme, size, report)
                    coordinator = new_page(browser, width, height, scheme)
                    coordinator.goto(url + "/signin")
                    coordinator.get_by_role("button", name="Sign in as the coordinator").click()
                    check(coordinator, "rota", size, scheme, report)
                    coordinator.goto(f"{url}/rota?day={DAY}&carer=K08")
                    check(coordinator, "rota-carer", size, scheme, report)
                    coordinator.goto(url + "/rota/replan")
                    check(coordinator, "replan", size, scheme, report)
                    coordinator.goto(url + "/imports")
                    check(coordinator, "imports", size, scheme, report)
            report += forced_colours(browser, url, carer, first)
            first_views(browser, url)
            caught = control(browser, url)
            browser.close()
        server.should_exit = True
    (OUT / "axe.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    for row in report:
        print(f"{row['screen']} {row['size']} {row['scheme']}: {row['id']} ({row['impact']}) "
              f"{row['help']} {row['nodes']}")  # fmt: skip
    screens_checked = 22 * len(SIZES) * len(SCHEMES)
    print(f"axe: {len(report)} violations on {screens_checked} screen captures")
    missed = CONTROL_EXPECTS - caught
    print(f"control (faint text, no language, too wide): caught {sorted(caught & CONTROL_EXPECTS)}")
    if missed:
        print(f"control FAILED: axe did not report {sorted(missed)}, so a clean run proves nothing")
        return 1
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main_check())
