"""The carer app's checks in a real browser, with the app's own CSP enforced (nothing bypassed):

1. Installable, by Chrome's published criteria (web.dev/articles/install-criteria): a manifest
   that parses without error, with a name, start_url, a standalone-type display, no preferred
   related app, and 192 px and 512 px icons that load at those sizes, on a secure page. (Chrome's
   own Page.getInstallabilityErrors returned no error even for a page with no manifest, so it
   could not fail.) Positive control: the register, which has no manifest, fails.
2. Offline: with the service worker in control, the visit sheet and a note form open without
   signal; a note sent without signal waits on the phone, then reaches the server once when the
   carer taps "Send now".
3. Sign-out: saved pages, drafts and the service worker are deleted, so without signal the visit
   sheet no longer opens. Sabotage: sign out with neither the app script nor the Clear-Site-Data
   header, and the same check must find the copy left behind.
4. Trusted Types: script on a carer page cannot write HTML or run an inline script. Positive
   control: the same calls succeed in a browser told to ignore the CSP.
5. The live map: tiles come only from OSM with an origin-only Referer and are never stored;
   without signal the sketch shows. Sabotage: no per-tile referrer policy; a stored tile.
6. A signal check leaves no request open past its 5 s limit. Sabotage: the app without its body
   read.
7. The phone keeps the current day only: pages saved a demo day earlier are gone once the
   clock moves on and the start page loads.
8. Signed out by the server (as when a session expires): the next load removes every saved page,
   so none opens without signal, but keeps the carer's drafts.
9. Measured, not checked: the pages a carer's phone keeps after opening today's sheet, after
   opening each day's sheet once, and after a week of use (each day's sheet opened on that
   day), read back from the browser's own storage, and how many clients they name.

A sabotage switches one protection off, and the same check must then fail, so a check that
cannot fail is caught.

Laptop only (the Chromium build Playwright 1.63 uses): `uv run python scripts/pwa_check.py`.
Exits 1 if any check fails.
"""

import re
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import Browser, BrowserContext, Error, Page, Route, sync_playwright

from homecare.app import db, screens, seed
from ui_check import DAY, NO_OSM, TILE_HOST, serve_tiles, start_app

WRITE_HTML = """() => { try { document.body.innerHTML = '<b>x</b>'; return 'wrote'; }
    catch (error) { return error.name; } }"""
INLINE_SCRIPT = """() => { try { const s = document.createElement('script');
    s.textContent = 'window.pwned = 1'; document.body.append(s); }
    catch (error) { return error.name; } return String(window.pwned); }"""
# Every page records the browser's own CSP and Trusted Types violation events, so a check can
# read them (a violation the page catches never reaches the console).
RECORD_VIOLATIONS = """window.violations = [];
document.addEventListener('securitypolicyviolation',
    (event) => window.violations.push(event.violatedDirective));"""
PNG = b"\x89PNG\r\n\x1a\n"
DISPLAYS = {"fullscreen", "standalone", "minimal-ui", "window-controls-overlay"}
LEFT_ON_PHONE = """async () => ({
    caches: (await caches.keys()).length,
    databases: (await indexedDB.databases()).length,
    workers: (await navigator.serviceWorker.getRegistrations()).length })"""
NOTHING_LEFT = {"caches": 0, "databases": 0, "workers": 0}
# How many drafts the carer app keeps on the phone (its IndexedDB store).
DRAFTS = """() => new Promise((resolve) => { const opening = indexedDB.open('carer-drafts');
    opening.onerror = () => resolve(-1);
    opening.onsuccess = () => { const database = opening.result;
      if (!database.objectStoreNames.contains('drafts')) { resolve(0); return; }
      const counting = database.transaction('drafts').objectStore('drafts').count();
      counting.onsuccess = () => resolve(counting.result); }; })"""
SAVED_PAGES = """async () => { const found = {};
    for (const name of await caches.keys()) {
      const cache = await caches.open(name);
      for (const request of await cache.keys()) {
        if (!request.url.includes('/static/')) {
          found[request.url] = await (await cache.match(request)).text(); } } }
    return found; }"""


def carers_with_work(folder: Path) -> list[tuple[str, str]]:
    """Carers with a visit begun but not written up on the demo clock's day, each with that
    visit, in carer order. Each check sequence takes its own carer, so none sees another's notes."""
    demo = seed.load()
    planned = {visit["id"]: visit for visit in demo["visits"]}
    connection = db.connect(folder / "ui.db")
    rows = connection.execute(
        "SELECT v.carer, v.id FROM visits v WHERE v.day = ? AND v.carer IS NOT NULL AND NOT "
        "EXISTS (SELECT 1 FROM notes n WHERE n.visit = v.id) ORDER BY v.carer, v.position",
        (DAY,),
    ).fetchall()
    connection.close()
    found: dict[str, str] = {}
    for row in rows:
        if row["carer"] not in found and screens.started(planned[row["id"]], demo["now"]):
            found[row["carer"]] = row["id"]
    return list(found.items())


def signed_in(
    browser: Browser,
    url: str,
    carer: str,
    warm: bool = True,
    tiles: list[dict[str, str]] | None = None,
    **options: Any,
) -> tuple[BrowserContext, Page]:
    """A carer signed in on a phone-sized page. With `warm`, the sheet is reloaded once the
    worker is ready, so the checks that follow start from a saved page; without it, the page is
    exactly as a carer's first open leaves it. Map tile requests are answered locally and
    recorded in `tiles`."""
    context = browser.new_context(viewport={"width": 390, "height": 844}, **options)
    serve_tiles(context, tiles if tiles is not None else [])
    context.add_init_script(RECORD_VIOLATIONS)
    page = context.new_page()
    page.goto(url + "/signin")
    page.get_by_label("Carer").select_option(carer)
    page.get_by_role("button", name="Sign in as this carer").click()
    page.goto(url + "/today")
    if warm:
        page.evaluate("navigator.serviceWorker.ready.then(() => true)")
        page.reload()  # now the worker controls the page, so this load is saved
    return context, page


def map_is_live(page: Page) -> bool:
    """Whether OpenStreetMap's map has taken the sketch's place on the visit sheet."""
    try:
        page.wait_for_selector(".route.is-live", timeout=10_000)
        return True
    except Error:
        return False


def tiles_in_storage(page: Page) -> list[str]:
    """Every address in the page's Cache Storage that is an OSM tile."""
    found: list[str] = page.evaluate(
        """async (host) => { const found = [];
        for (const name of await caches.keys()) {
          for (const request of await (await caches.open(name)).keys()) {
            if (request.url.startsWith(host)) found.push(request.url); } }
        return found; }""",
        TILE_HOST,
    )
    return found


def sheet_opens_offline(context: BrowserContext, page: Page, url: str) -> bool:
    """Whether the visit sheet opens with no signal (from a copy saved on the phone)."""
    context.set_offline(True)
    try:
        page.goto(url + "/today")
        return "Your visits" in page.content()
    except Error:
        return False  # the browser's own "no internet" error: nothing saved
    finally:
        context.set_offline(False)


def install_problems(page: Page) -> list[str]:
    """What stops the page meeting Chrome's install criteria; empty when it meets them."""
    found = page.context.new_cdp_session(page).send("Page.getAppManifest")
    if not found["url"]:
        return ["no manifest"]
    problems = [error["message"] for error in found["errors"]]
    manifest = page.request.get(found["url"]).json()
    if not (manifest.get("name") or manifest.get("short_name")):
        problems.append("no name")
    if not manifest.get("start_url"):
        problems.append("no start_url")
    if manifest.get("display") not in DISPLAYS:
        problems.append(f"display {manifest.get('display')!r}")
    if manifest.get("prefer_related_applications"):
        problems.append("prefers a related app")
    for size in (192, 512):
        icons = [i for i in manifest.get("icons", []) if i.get("sizes") == f"{size}x{size}"]
        png = b""
        if icons:
            png = page.request.get(urljoin(found["url"], icons[0]["src"])).body()
        # A PNG's signature, then its width and height in the header chunk.
        if png[:8] != PNG or struct.unpack(">II", png[16:24]) != (size, size):
            problems.append(f"no {size} px icon")
    if not page.evaluate("isSecureContext"):
        problems.append("not a secure context")
    return problems


def signal_checks(context: BrowserContext, url: str) -> tuple[int, list[str]]:
    """Open the visit sheet in a new page and stay past a signal check's 5 s limit. Returns how
    many checks finished, and how each failed one failed."""
    page = context.new_page()
    finished: list[str] = []
    failed: list[str] = []
    page.on("requestfinished", lambda r: finished.append(r.url) if "/healthz" in r.url else None)
    page.on(
        "requestfailed", lambda r: failed.append(r.failure or "") if "/healthz" in r.url else None
    )
    page.goto(url + "/today")
    page.wait_for_timeout(6000)
    page.close()
    return len(finished), failed


def saved_pages(page: Page) -> dict[str, str]:
    """Every page saved on the phone, by address, once the worker has stopped saving."""
    kept: dict[str, str] = {}
    for _ in range(20):
        page.wait_for_timeout(1000)
        now: dict[str, str] = page.evaluate(SAVED_PAGES)
        if now.keys() == kept.keys():
            break
        kept = now
    return kept


def earlier_days(pages: dict[str, str], demo: dict[str, Any]) -> list[str]:
    """The saved pages that belong to a day before the demo clock's: a note form for an earlier
    visit, or an earlier day's sheet."""
    day_of = {visit["id"]: visit["day"] for visit in demo["visits"]}
    today = demo["now"]["day"]
    found = []
    for address in pages:
        form = re.search(r"/visits/([^/]+)/note$", address)
        sheet = re.search(r"/today\?day=(\d+)$", address)
        if (form and day_of.get(form.group(1), today) < today) or (
            sheet and int(sheet.group(1)) < today
        ):
            found.append(address)
    return found


def on_the_phone(page: Page) -> str:
    """How many pages the phone keeps, and how many clients they name."""
    kept = saved_pages(page)
    names = [client["name"] for client in seed.load()["clients"]]
    named = [name for name in names if any(name in text for text in kept.values())]
    return f"{len(kept)} pages, naming {len(named)} clients"


def app_notes(folder: Path) -> list[str]:
    connection = db.connect(folder / "ui.db")
    rows = connection.execute("SELECT body FROM notes WHERE source = 'app'").fetchall()
    connection.close()
    return [row["body"] for row in rows]


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    measures: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory() as name:
        folder = Path(name)
        url, server, settings = start_app(folder)
        (carer, visit), (second, _) = carers_with_work(folder)[:2]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=NO_OSM)

            # 1. Installable, with the register as the positive control.
            context, page = signed_in(browser, url, carer)
            install = install_problems(page)
            results.append(("installable", install == [], str(install)))
            lead = browser.new_context().new_page()
            lead.goto(url + "/signin")
            lead.get_by_role("button", name="Sign in as the safeguarding lead").click()
            control = install_problems(lead)
            results.append(("installable: control fails", control != [], str(control)))

            # 1b. As a carer uses it: first open, write a note, then no signal and open the
            # app's start page. Nothing is reloaded by hand; the worker must have saved the
            # sheet and the forms itself.
            fresh, fresh_page = signed_in(browser, url, second, warm=False)
            fresh_page.locator(".write-up a.primary").first.click()
            fresh_page.get_by_role("textbox", name="Your note").fill("Lunch made; all well.")
            fresh_page.get_by_role("button", name="Send the note").click()
            fresh_page.wait_for_url("**sent=1**")
            fresh_page.wait_for_function("navigator.serviceWorker.controller !== null")
            fresh_page.wait_for_timeout(1500)  # the worker's own saving of the day's pages
            landed = fresh_page.url  # the address the Send landed on, with its one-time message
            fresh.set_offline(True)
            fresh_page.goto(url + "/today")
            start_text = fresh_page.inner_text("body")
            opens = "Your visits" in start_text and "Your note was sent" not in start_text
            results.append(("offline as used: start page opens, clean", opens, start_text[:80]))
            fresh_page.goto(landed)
            landed_text = fresh_page.inner_text("body")
            clean = "Your visits" in landed_text and "Your note was sent" not in landed_text
            results.append(("offline as used: the Send's own address shows a clean sheet", clean,
                            landed_text[:80]))  # fmt: skip
            fresh_form = False
            form_text = "the start page did not open, so no form could be reached"
            if opens:
                written = fresh_page.locator(".visits li").first.get_attribute("data-visit")
                fresh_page.goto(f"{url}/visits/{written}/note")
                form_text = fresh_page.inner_text("body")
                fresh_form = "Add to your note" in form_text
            results.append(("offline as used: a written visit's form is current", fresh_form,
                            form_text[:60]))  # fmt: skip
            # A page the phone never saved (an earlier day) shows the app's own page, styled
            # and with a way back, not the browser's error or bare text.
            fresh_page.goto(f"{url}/today?day=20")
            kind = fresh_page.evaluate("() => document.contentType")
            heading = fresh_page.locator("h1").all_inner_texts()
            links = fresh_page.get_by_role("link", name="Your visits")
            back = links.first.get_attribute("href") if links.count() == 1 else None
            not_saved = kind == "text/html" and heading == ["No signal"] and back == "/today"
            results.append(("offline: a page not saved shows the app's page", not_saved,
                            f"{kind} {heading} {back}"))  # fmt: skip
            fresh.set_offline(False)
            fresh.close()

            # 2. Offline: save the note form, write without signal, then send once.
            page.goto(f"{url}/visits/{visit}/note")
            page.goto(url + "/today")
            form_links = f"main a[href='/visits/{visit}/note']"
            offered_before = page.locator(form_links, has_text="Write the note").count()
            context.set_offline(True)
            page.goto(f"{url}/visits/{visit}/note")
            page.get_by_role("textbox", name="Your note").fill(
                "Offline note: tea taken, fridge stocked."
            )
            page.get_by_role("button", name="Send the note").click()
            # Typing shows "Draft saved" first; the Send's answer replaces it, so wait for that
            # answer (a timeout leaves `kept` false and the check fails).
            try:
                page.wait_for_function(
                    "document.querySelector('.draft-state').textContent.includes('No signal')",
                    timeout=10_000,
                )
            except Error:
                pass
            kept = "No signal" in (page.text_content(".draft-state") or "")
            page.goto(url + "/today")
            page.wait_for_selector("[data-offline]:not([hidden])", timeout=10_000)
            offline_line = page.locator("[data-offline]").is_visible()
            page.wait_for_selector("[data-waiting]:not([hidden])", timeout=10_000)
            waiting = page.locator("[data-waiting]").inner_text()
            results.append(("offline: sheet and form open", offline_line and kept, waiting))
            # A waiting note is not offered for writing again anywhere on the sheet, only opened
            # (the control: it was offered, before).
            offered_after = page.locator(form_links, has_text="Write the note").count()
            opened_after = page.locator(form_links, has_text="Open the note").count()
            results.append(("offline: a waiting note is not offered to write again",
                            offered_before >= 1 and offered_after == 0 and opened_after >= 1,
                            f"offered before {offered_before}, after {offered_after}; "
                            f"opened after {opened_after}"))  # fmt: skip
            context.set_offline(False)
            page.get_by_role("button", name="Send now").click()
            page.wait_for_url("**/today?*sent=1*")
            stored = [note for note in app_notes(folder) if note.startswith("Offline")]
            sent_once = stored == ["Offline note: tea taken, fridge stocked."]
            results.append(("offline: sent once on reconnect", sent_once, str(stored)))

            # 3. Sign-out deletes everything kept on the phone, even with an unsent draft.
            page.goto(f"{url}/visits/{visit}/note")
            page.get_by_role("textbox", name="Your addition").fill("A draft never sent.")
            page.goto(url + "/today")
            page.get_by_role("button", name="Sign out").click()
            warned = page.get_by_role("alert").inner_text()
            page.get_by_role("button", name="Sign out and delete").click()
            page.wait_for_url("**/signin")
            left = page.evaluate(LEFT_ON_PHONE)
            cleared = left == {"caches": 0, "databases": 0, "workers": 0}
            results.append(("sign-out: nothing left", cleared, f"{left}; warned: {warned}"))
            gone = not sheet_opens_offline(context, page, url)
            results.append(("sign-out: no sheet offline", gone, ""))
            seen = []
            for path in ["/today", f"/visits/{visit}/note"]:
                checked, checked_page = signed_in(browser, url, carer)
                checked_page.goto(url + path)
                checked_page.wait_for_timeout(1000)  # the app's script has run
                if path == "/today" and not map_is_live(checked_page):
                    seen.append("the live map never showed, so Leaflet went unchecked")
                seen += checked_page.evaluate("window.violations")
                checked.close()
            results.append(
                ("carer pages break no CSP or Trusted Types rule", seen == [], str(seen))
            )

            # Sabotage: sign out with no app script and no Clear-Site-Data header.
            sabotaged, sabotaged_page = signed_in(browser, url, carer)

            def without_clearing(route: Route) -> None:
                response = route.fetch(max_redirects=0)
                headers = {k: v for k, v in response.headers.items() if k != "clear-site-data"}
                route.fulfill(response=response, headers=headers)

            sabotaged.route("**/signout", without_clearing)
            sabotaged_page.evaluate("document.querySelector('form[action=\"/signout\"]').submit()")
            sabotaged_page.wait_for_url("**/signin")
            caught = sheet_opens_offline(sabotaged, sabotaged_page, url)
            results.append(("sign-out sabotaged: leftover found", caught, ""))

            # 3b. Sign out while the worker is still saving the day. A save that follows the
            # sign-out's redirect asks for the sign-in page with no cookie, which starts a second
            # signed-out session and replaces the cookie of the form on screen: the next person's
            # sign-in is refused as expired (found on the live link, 12 Nov). The worker's save
            # is held until the carer has signed out, then let go.
            context, page = signed_in(browser, url, carer)
            held: list[Route] = []

            def hold_worker_saves(route: Route) -> None:
                if route.request.service_worker:
                    held.append(route)
                else:
                    route.continue_()

            context.route("**/today", hold_worker_saves)
            page.evaluate("navigator.serviceWorker.controller.postMessage({save: ['/today']})")
            page.wait_for_timeout(1000)
            page.get_by_role("button", name="Sign out").click()
            page.wait_for_url("**/signin")
            context.unroute("**/today")
            followed: list[str] = []
            context.on("request", lambda r: followed.append(r.url) if r.service_worker else None)
            for route in held:
                route.continue_()
            page.wait_for_timeout(1500)
            results.append(("sign-out while saving: no save follows a redirect",
                            bool(held) and not any(u.endswith("/signin") for u in followed),
                            f"{len(held)} saves held; worker asked for {followed}"))  # fmt: skip
            # The held saves came back as sign-in redirects after the sign-out cleared the phone;
            # handling them must not leave storage behind (seen on the live link, 19 Nov).
            left = page.evaluate(LEFT_ON_PHONE)
            results.append(("sign-out while saving: nothing left after the saves return",
                            left == NOTHING_LEFT, str(left)))  # fmt: skip
            page.get_by_role("button", name="Sign in as the safeguarding lead").click()
            page.wait_for_url("**/queue", timeout=10_000)
            context.close()

            # 4. Trusted Types and the CSP, with a CSP-bypassing browser as the control.
            context, page = signed_in(browser, url, carer)
            blocked = [page.evaluate(WRITE_HTML), page.evaluate(INLINE_SCRIPT)]
            # The control for the rule check above: the same recorder sees these two.
            heard = page.evaluate("window.violations")
            results.append(("violation recorder: control sees both", len(heard) >= 2, str(heard)))
            results.append(("Trusted Types block HTML and inline script", "wrote" not in blocked
                            and "1" not in blocked, str(blocked)))  # fmt: skip
            open_context, open_page = signed_in(browser, url, carer, bypass_csp=True)
            allowed = [open_page.evaluate(WRITE_HTML), open_page.evaluate(INLINE_SCRIPT)]
            results.append(("Trusted Types: control succeeds", allowed == ["wrote", "1"],
                            str(allowed)))  # fmt: skip

            # 5. The live map, with every tile answered here, never by OSM.
            # With signal: tiles from OSM's tile server only, each with the site's origin as its
            # Referer (OSM's policy asks for one) and never a page path. No worker here, so the
            # sabotage below can change the app's script on its way to the page.
            tiles: list[dict[str, str]] = []
            context, page = signed_in(browser, url, carer, warm=False, tiles=tiles,
                                      service_workers="block")  # fmt: skip
            live = map_is_live(page)
            origin = url + "/"
            referers = sorted({tile["referer"] for tile in tiles})
            from_osm = bool(tiles) and all(tile["url"].startswith(TILE_HOST) for tile in tiles)
            results.append(("live map: with signal, tiles from OSM with an origin-only Referer",
                            live and from_osm and referers == [origin],
                            f"{len(tiles)} tiles; Referers {referers}"))  # fmt: skip
            credit = page.locator(".map-credit").inner_text() if live else ""
            results.append(("live map: the OSM credit shows on the map",
                            live and "© OpenStreetMap contributors" in credit, credit))  # fmt: skip
            context.close()

            # Sabotage: the app without its per-tile referrer policy. The page's own
            # no-referrer policy then strips the Referer, and the check must see it.
            sabotaged_tiles: list[dict[str, str]] = []
            sabotaged, sabotaged_page = signed_in(
                browser, url, carer, tiles=sabotaged_tiles, service_workers="block", warm=False
            )

            def without_tile_referrer(route: Route) -> None:
                script = route.fetch().text()
                script = script.replace('referrerPolicy: "strict-origin-when-cross-origin"', "")
                route.fulfill(status=200, content_type="text/javascript", body=script)

            sabotaged.route("**/static/app.js", without_tile_referrer)
            sabotaged_tiles.clear()
            sabotaged_page.reload()
            sabotaged_live = map_is_live(sabotaged_page)
            leaked = sorted({tile["referer"] for tile in sabotaged_tiles})
            results.append(("live map sabotaged: a missing Referer is caught",
                            sabotaged_live and bool(sabotaged_tiles) and leaked != [origin],
                            f"Referers {leaked}"))  # fmt: skip
            sabotaged.close()

            # Without signal: the sketch, and no tile asked for. With the worker in control,
            # no tile is ever stored in Cache Storage.
            tiles = []
            context, page = signed_in(browser, url, carer, tiles=tiles)
            map_is_live(page)
            stored = tiles_in_storage(page)
            results.append(("live map: no tile stored by the worker", bool(tiles) and stored == [],
                            f"{len(tiles)} tiles seen, {len(stored)} stored"))  # fmt: skip
            context.set_offline(True)
            asked = len(tiles)
            page.goto(url + "/today")
            page.wait_for_timeout(3000)  # the app has asked whether there is signal
            sketch = page.locator(".route > svg").is_visible()
            offline_live = page.locator(".route.is-live").count() > 0
            results.append(("live map: no signal shows the sketch and asks for no tile",
                            sketch and not offline_live and len(tiles) == asked,
                            f"sketch {sketch}, live {offline_live}, "
                            f"tiles asked offline {len(tiles) - asked}"))  # fmt: skip
            context.set_offline(False)
            # The control for "no tile stored": a tile put in Cache Storage is found.
            page.evaluate(
                "async (url) => (await caches.open('control')).put(url, new Response('x'))",
                TILE_HOST + "1/1/1.png",
            )
            results.append(("live map control: a stored tile is found",
                            tiles_in_storage(page) != [], ""))  # fmt: skip
            context.close()

            # 6. A signal check leaves no request open. The app asks /healthz whether there is
            # signal; an answer whose body is never read stays open until the check's 5 s limit
            # aborts it, so every page showed a failed request. The sabotage serves the
            # app without its body read, with no worker so the route reaches the page.
            context, _ = signed_in(browser, url, carer)
            done, aborted = signal_checks(context, url)
            results.append(("signal check: no request left open", done >= 1 and aborted == [],
                            f"{done} finished, failed {aborted}"))  # fmt: skip
            context.close()
            sabotaged, _ = signed_in(browser, url, carer, warm=False, service_workers="block")

            def without_body_read(route: Route) -> None:
                script = route.fetch().text().replace("await answer.text();", "")
                route.fulfill(status=200, content_type="text/javascript", body=script)

            sabotaged.route("**/static/app.js", without_body_read)
            done, aborted = signal_checks(sabotaged, url)
            results.append(("signal check sabotaged: a request left open is caught", aborted != [],
                            f"{done} finished, failed {aborted}"))  # fmt: skip
            sabotaged.close()

            # 7. The phone keeps the current day only. The carer's pages are saved with the demo
            # clock a day earlier; the clock moves on and the carer loads the start page. Which
            # day a saved page belongs to is read from its address and the seed, never from the
            # app's own mark on it.
            demo = seed.load()
            now = demo["now"]
            settings["clock"] = {"day": DAY - 1, "minute": now["minute"]}
            context, page = signed_in(browser, url, carer)
            before = earlier_days(saved_pages(page), demo)
            settings["clock"] = dict(now)
            page.goto(url + "/today")
            after = earlier_days(saved_pages(page), demo)
            results.append(("phone: an earlier day's pages are gone after the next load",
                            bool(before) and not after,
                            f"earlier pages before {len(before)}, after {len(after)}"))  # fmt: skip
            context.close()

            # 8. The server signs the carer out (the session ends there, as on expiry). The next
            # load meets the sign-in page; the phone then keeps no saved page, and opens none
            # without signal, but the draft written before is kept.
            context, page = signed_in(browser, url, carer)
            page.goto(f"{url}/visits/{visit}/note")
            page.locator("#body").fill("A draft kept through sign-out.")
            page.wait_for_function("document.querySelector('.draft-state').textContent.length > 0")
            page.goto(url + "/today")
            saved_pages(page)
            connection = sqlite3.connect(folder / "ui.db")
            connection.execute("DELETE FROM sessions")
            connection.commit()
            connection.close()
            page.goto(url + "/today")
            page.wait_for_url("**/signin")
            left = saved_pages(page)
            drafts = page.evaluate(DRAFTS)
            # Without signal the worker answers; its own "No signal" page has a "Your visits"
            # button, so the page's heading tells the sheet from it.
            context.set_offline(True)
            page.goto(url + "/today")
            heading = page.locator("h1").all_inner_texts()
            context.set_offline(False)
            results.append(("phone: signed out by the server, saved pages go and drafts stay",
                            not left and drafts >= 1 and heading == ["No signal"],
                            f"{len(left)} pages, {drafts} drafts, offline {heading}"))  # fmt: skip
            context.close()

            # 9. What the phone keeps (measured): today's sheet; each day's sheet opened once
            # with the clock fixed; a week of use, each day's sheet opened on that day.
            context, page = signed_in(browser, url, carer)
            measures.append(("phone: today's sheet opened", on_the_phone(page)))
            for day in demo["days"]:
                page.goto(f"{url}/today?day={day}")
                saved_pages(page)  # the worker has finished saving this day's pages
            measures.append(("phone: each day's sheet opened once", on_the_phone(page)))
            context.close()
            context, page = signed_in(browser, url, carer)
            for day in demo["days"]:
                settings["clock"] = {"day": day, "minute": now["minute"]}
                page.goto(f"{url}/today?day={day}")  # named: the sheet the app opens that day
                saved_pages(page)
            settings["clock"] = dict(now)
            measures.append(("phone: a week of use, each day's sheet on that day",
                             on_the_phone(page)))  # fmt: skip
            context.close()
            browser.close()
        server.should_exit = True
    for check, passed, detail in results:
        print(f"{'pass' if passed else 'FAIL'}  {check}  {detail}")
    for label, value in measures:
        print(f"measured  {label}  {value}")
    return 0 if all(passed for _, passed, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
