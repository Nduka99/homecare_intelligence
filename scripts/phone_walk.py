"""Walk a running copy as a carer on an Android phone would, then as each office role.

    uv run python scripts/phone_walk.py https://care.example.org <screenshot folder>

The steps follow the operating guide's acceptance tests. The carer signs in, writes a note with no
signal, sends it once when signal returns, adds to a note and signs out. The lead finds each note
stored exactly once, and the manager's and coordinator's screens open without sideways scroll.
In demo mode every visitor gets their own copy of the demo, so the two notes it writes reach no
one else.

The last check fails on any console error, page error or failed request seen by the page. Only
requests cut off by the walk's own airplane mode (ERR_INTERNET_DISCONNECTED) are expected. Each
check prints PASS or FAIL; exits 1 if any fails. Laptop only (Playwright's Chromium).
"""

import sys
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    ConsoleMessage,
    Error,
    Page,
    Request,
    sync_playwright,
)

from pwa_check import LEFT_ON_PHONE, RECORD_VIOLATIONS, install_problems

CARER = "K08"
OFFLINE_NOTE = "Phone walk offline: tea taken, fridge stocked."
ONLINE_NOTE = "Phone walk: lunch made, ate well, all fine."
EXPECTED_OFFLINE = "ERR_INTERNET_DISCONNECTED"
NOTHING_LEFT = {"caches": 0, "databases": 0, "workers": 0}
OFFICE_PAGES = [
    ("Sign in as the manager", "/team", "08-manager"),
    ("Sign in as the coordinator", "/rota", "09-rota"),
    ("Sign in as the coordinator", "/rota/replan", "10-replan"),
    ("Sign in as the coordinator", "/imports", "11-imports"),
]
RESULTS: list[bool] = []


def report(ok: bool, name: str, detail: str = "") -> None:
    print("PASS" if ok else "FAIL", name, "|", detail[:300], flush=True)
    RESULTS.append(ok)


def no_sideways_scroll(page: Page) -> bool:
    return bool(page.evaluate("document.scrollingElement.scrollWidth <= window.innerWidth"))


def shot(page: Page, folder: Path, name: str) -> None:
    page.screenshot(path=folder / f"{name}.png", full_page=True)


def watch(page: Page, problems: list[str]) -> None:
    """Collect the console errors, page errors and failed requests the page sees."""

    def on_console(message: ConsoleMessage) -> None:
        if message.type == "error":
            problems.append(f"console at {page.url}: {message.text}")

    def on_failed(request: Request) -> None:
        problems.append(f"failed: {request.url} {request.failure}")

    def on_error(error: Error) -> None:
        problems.append(f"page error: {error}")

    page.on("console", on_console)
    page.on("requestfailed", on_failed)
    page.on("pageerror", on_error)


def carer(page: Page, context: BrowserContext, link: str, shots: Path) -> None:
    page.goto(link)
    report(page.url.endswith("/signin"), "the link opens the sign-in page", page.url)
    shot(page, shots, "01-signin")
    page.get_by_label("Carer").select_option(CARER)
    page.get_by_role("button", name="Sign in as this carer").tap()
    page.wait_for_url("**/today")
    page.wait_for_load_state("load")
    report("Your visits" in page.inner_text("body"), "the carer's day opens")
    report(no_sideways_scroll(page), "the carer's day: no sideways scroll")
    install = install_problems(page)
    report(install == [], "installable by Chrome's criteria", str(install))
    try:
        page.wait_for_selector(".route.is-live", timeout=15_000)
        report(True, "the live OpenStreetMap map shows with signal")
    except Error:
        report(False, "the live OpenStreetMap map shows with signal", "sketch only")
    shot(page, shots, "02-today")

    # No signal at the client's door: open the app, write the visit up, press Send.
    write = page.locator(".write-up a.primary")
    report(write.count() == 1, "a visit to write up is listed", f"{write.count()} links")
    form = write.first.get_attribute("href") or ""
    # The worker saves the day's pages and forms on its own meanwhile. Staying longer than a
    # signal check's 5 s limit means a check left open always shows as a failed request.
    page.wait_for_timeout(6000)
    context.set_offline(True)
    page.goto(link + "/today")
    report("Your visits" in page.inner_text("body"), "no signal: the day opens from the phone")
    page.locator(".write-up a.primary").first.tap()
    page.wait_for_url("**" + form)
    report("Your note" in page.inner_text("body"), "no signal: the note form opens", page.url)
    page.get_by_role("textbox", name="Your note").fill(OFFLINE_NOTE)
    page.get_by_role("button", name="Send the note").tap()
    try:
        page.wait_for_selector(".draft-state:has-text('No signal')", timeout=10_000)
    except Error:
        pass  # reported just below, with the text that was there instead
    state = page.text_content(".draft-state") or ""
    report("No signal" in state, "no signal: the note is kept on the phone", state)
    shot(page, shots, "03-offline-note")
    page.goto(link + "/today")
    page.wait_for_selector("[data-waiting]:not([hidden])", timeout=10_000)
    waiting = page.locator("[data-waiting]").inner_text()
    report(bool(waiting), "no signal: the day shows the note waiting", waiting)
    links = page.locator(f"main a[href='{form}']")
    offered = links.filter(has_text="Write the note").count()
    opened = links.filter(has_text="Open the note").count()
    report(
        offered == 0 and opened >= 1,
        "no signal: the waiting visit is only opened, not offered to write again",
        f"offered {offered}, opened {opened}",
    )
    shot(page, shots, "04-offline-today")

    # Signal back: send once.
    context.set_offline(False)
    page.get_by_role("button", name="Send now").tap()
    page.wait_for_url("**/today?*sent=1*")
    report("Your note was sent" in page.inner_text("body"), "signal back: Send now delivers")
    page.goto(link + form)
    report("Add to your note" in page.inner_text("body"), "the visit now has its note")

    # With signal: add to an earlier visit's note.
    page.goto(link + "/today")
    page.get_by_role("link", name="Add to your note").first.tap()
    page.wait_for_load_state("load")
    page.get_by_role("textbox", name="Your addition").fill(ONLINE_NOTE)
    page.get_by_role("button", name="Send the addition").tap()
    page.wait_for_url("**sent=1**")
    report("was sent" in page.inner_text("body"), "an addition with signal is sent and confirmed")
    shot(page, shots, "05-addition-sent")

    # Every note has been sent, so sign-out has no warning step.
    page.goto(link + "/today")
    page.get_by_role("button", name="Sign out").tap()
    page.wait_for_url("**/signin")
    left = page.evaluate(LEFT_ON_PHONE)
    report(left == NOTHING_LEFT, "sign-out leaves nothing on the phone", str(left))


def lead(page: Page, context: BrowserContext, link: str, shots: Path) -> None:
    """The lead, on the same phone and so the same demo visitor, finds each note once."""
    page.get_by_role("button", name="Sign in as the safeguarding lead").tap()
    page.wait_for_url("**/queue**")
    unread = page.locator("#band-unread").locator("xpath=..").inner_text()
    report("Not yet read" in unread, "the lead sees the carer's notes as not yet read", unread)
    report(no_sideways_scroll(page), "the register: no sideways scroll")
    shot(page, shots, "06-register")
    links = page.locator("ol[aria-labelledby='band-unread'] a")
    bodies = []
    for index in range(links.count()):
        detail = context.new_page()
        detail.goto(link + (links.nth(index).get_attribute("href") or ""))
        bodies.append(detail.inner_text("body"))
        detail.close()
    offline = sum(OFFLINE_NOTE in body for body in bodies)
    online = sum(ONLINE_NOTE in body for body in bodies)
    report(
        offline == 1 and online == 1,
        "each note is stored exactly once",
        f"offline {offline}, online {online}, unread {len(bodies)}",
    )
    page.goto(link + "/queue")
    urgent = page.locator("ol[aria-labelledby='band-1'] a")
    report(urgent.count() > 0, "the register lists notes with findings", f"{urgent.count()}")
    urgent.first.tap()
    page.wait_for_url("**/notes/**")
    report(no_sideways_scroll(page), "a note with findings: no sideways scroll", page.url)
    shot(page, shots, "07-note")


def offices(page: Page, link: str, shots: Path) -> None:
    for button, path, name in OFFICE_PAGES:
        page.goto(link + "/signin")
        page.get_by_role("button", name=button).tap()
        page.wait_for_url(lambda url: not url.endswith("/signin"))
        page.goto(link + path)
        body = page.inner_text("body")
        report(page.url.endswith(path) and "SYNTHETIC" in body.upper(), f"{path} opens")
        report(no_sideways_scroll(page), f"{path}: no sideways scroll")
        shot(page, shots, name)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    link = sys.argv[1].rstrip("/")
    shots = Path(sys.argv[2])
    shots.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(**playwright.devices["Pixel 7"])
        context.add_init_script(RECORD_VIOLATIONS)
        page = context.new_page()
        watch(page, problems)
        carer(page, context, link, shots)
        lead(page, context, link, shots)
        offices(page, link, shots)
        violations = page.evaluate("window.violations")
        report(violations == [], "no CSP violations on the last page", str(violations))
        unexpected = [problem for problem in problems if EXPECTED_OFFLINE not in problem]
        report(
            unexpected == [],
            "no console errors, page errors or failed requests while online",
            "; ".join(unexpected),
        )
        browser.close()
    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
