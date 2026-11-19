"""The web app. Every screen shows SYNTHETIC data.

Two sign-in modes, set by `SIGN_IN`:
- "demo" (the default, and the cloud): one button per role, no passwords. The database is rebuilt
  from the seed at every start, and the audit key is random per process.
- "accounts" (the laptop): usernames and scrypt passwords made with `homecare platform user`; the
  database persists in `data/platform/`, and `AUDIT_KEY` (64 hex characters) must be set.

Start-up refuses to run if the demo data differs from its pinned hash or the audit log has been
edited. Every request is checked on the server, so a carer cannot reach another carer's notes by
changing a URL.
"""

import asyncio
import datetime
import functools
import hashlib
import json
import mimetypes
import os
import re
import secrets
import signal
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from homecare import fhir
from homecare.app import access, auditlog, auth, db, importer, limits, maps, screens, seed

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC = Path(__file__).parent / "static"
# Windows does not know the web app manifest's type; browsers expect it.
mimetypes.add_type("application/manifest+json", ".webmanifest")
# The commit the image was built from (scripts/cluster.py passes it in). Only /healthz reports it,
# so the live site can be matched to a build; "local" when it is not given.
BUILD = os.environ.get("GIT_COMMIT", "local")[:12]
ACCOUNTS_FOLDER = Path("data") / "platform"
# Scripts only from this site (the carer app and its service worker), never inline; Trusted
# Types make writing HTML from script throw, and only the app's one policy may exist.
HEADERS = {
    # Map tiles come only from OpenStreetMap's tile server. The "default" Trusted Types
    # policy lets through exactly one string, Leaflet's load-time "<svg/>" test.
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; worker-src 'self'; "
    "img-src 'self' https://tile.openstreetmap.org; "
    "manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
    "require-trusted-types-for 'script'; trusted-types carer-app default",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
HOME = {"lead": "/queue", "carer": "/today", "manager": "/team", "coordinator": "/rota"}
# The rota data the coordinator sees: the week's uncovered visits with the planner's reasons,
# and a saved, checked re-plan (`homecare rota replan`). Pinned like the seed.
ROTA = seed.FILE.with_name("rota.json")
# The coordinator's demo referrals, each with the laptop model's read (`homecare referrals demo`).
REFERRALS = seed.REFERRALS
VISITOR = "__Host-visitor"
# A Send's idempotency key: a version 4 UUID, made by the server or the carer's phone.
KEY = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
# A visitor cookie the server made: `secrets.token_urlsafe(32)`. Any other value is replaced.
VISITOR_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
LONGEST_NOTE = 4000
# The most characters of any one value an audit entry keeps: enough for every ID the app makes,
# while an address typed by a visitor cannot fill the log.
LONGEST_LOGGED = 64
# A carer's reason for opening another day: enough for a sentence or two.
LONGEST_REASON = 500
# The demo day a carer's page was served on, so the phone keeps the current day's pages only.
SERVED_DAY = "Care-Day"
# How long a coordinator has to approve a request for a client's record: a working day.
EXPORT_SECONDS = 24 * 60 * 60
# Why an export step is refused: its status, the page's title and its message.
EXPORT_REFUSALS = {
    "missing": (404, "No such request", "This request does not exist."),
    "own": (
        403,
        "Someone else must approve",
        "You asked for this file, so another person must approve it.",
    ),
    "approved": (409, "Already approved", "This request was approved already."),
    "late": (
        410,
        "Past its deadline",
        "This request waited more than a day. The lead can ask again.",
    ),
    "not yours": (403, "Not your request", "Only the person who asked can download this file."),
    "unapproved": (403, "Not approved yet", "A coordinator must approve this request first."),
    "downloaded": (
        409,
        "Already downloaded",
        "This file was downloaded already. Ask again for a new copy.",
    ),
}
# Where a carer may be sent back to once another day is open: their own pages only.
BACK = re.compile(r"/(today\?day=\d{1,3}|visits/[A-Za-z0-9-]{1,64}/note|notes/[A-Za-z0-9-]{1,64})")
FORM_TYPE = "application/x-www-form-urlencoded"
# Limits on a posted form, checked before any other work. The longest note (4,000 characters of
# up to four bytes, each byte percent-encoded as three) is about 48 KB; the busiest forms (a
# referral's profile, a note's decisions) have fewer than 20 fields; a weak phone signal sends
# the largest form in well under the time allowed.
LARGEST_FORM = 64 * 1024
MOST_FIELDS = 200
FORM_SECONDS = 20.0
# Forms from one visitor's address (limits.py): this many at a time, and SAVES_PER_WINDOW in
# each window: five times the 15 forms one person sends following the guide through all four
# roles, rounded up.
FORMS_AT_ONCE = 4
SAVES_PER_WINDOW = 100
WINDOW_SECONDS = 600.0
# The demo's database may grow to this, inside the 64 MiB in-memory folder it lives in; at the
# budget the demo restarts with fresh data (`restart`).
DEMO_BUDGET = 40 * 1024 * 1024
# At most this many requests at once: the 40 worker threads that run the pages, and a queue half
# their size ("small queue lengths relative to the thread pool size (e.g., 50% or less)",
# Google SRE book, ch. 22). Past it, the busy page is sent at once.
IN_FLIGHT = 60
BUSY = ("The service is busy", "Nothing was saved. Try again in a moment.")
RESTARTING = (
    "The demo is restarting",
    "The demo has filled its storage and is starting again with fresh data. Nothing you just "
    "sent was saved. Try again in a minute.",
)
REFUSED_PROFILE = {
    "no visit": "Not saved: choose at least one visit. If the referral gives none, ask the "
    "referrer.",
    "not asked": "Not saved: options the referral does not state would be saved as not needed. "
    "Ask the referrer, then tick “I have asked the referrer”.",
}
HTTP_ERRORS = {
    400: ("This form could not be read", "Nothing was saved. Go back and send it again."),
    404: ("Page not found", "There is no page at this address."),
    405: ("This page cannot be opened this way", "Open it from a link or button in the app."),
    408: (
        "This form took too long to arrive",
        "Nothing was saved. Check your signal, then send it again.",
    ),
    413: (
        "This form is too long",
        f"Nothing was saved. A note can be up to {LONGEST_NOTE:,} characters.",
    ),
    429: (
        "Too many forms in a short time",
        "Nothing was saved. Many forms came from your connection at once: wait a few minutes, "
        "then try again.",
    ),
}
SIGNIN_MESSAGES = {
    "wrong": "Wrong username or password.",
    "locked": "This account is locked for 15 minutes after five failed sign-ins.",
}


def set_session_cookie(response: Response, token: str) -> None:
    """The session cookie: never readable by scripts, sent only over HTTPS and only to this site."""
    response.set_cookie(auth.COOKIE, token, httponly=True, secure=True, samesite="strict", path="/")


def settings_from_env() -> dict[str, Any]:
    mode = os.environ.get("SIGN_IN", "demo")
    if mode == "demo":
        folder = Path(tempfile.gettempdir())
        return {
            "mode": mode,
            "database": folder / "homecare-demo.db",
            "anchor": folder / "homecare-demo.anchor",
            "key": secrets.token_bytes(32),
        }
    if mode == "accounts":
        if len(os.environ.get("AUDIT_KEY", "")) != 64:
            raise SystemExit(
                "accounts mode needs AUDIT_KEY: 64 hex characters, kept outside the database"
            )
        return {
            "mode": mode,
            "database": ACCOUNTS_FOLDER / "homecare.db",
            "anchor": ACCOUNTS_FOLDER / "audit.anchor",
            "key": bytes.fromhex(os.environ["AUDIT_KEY"]),
        }
    raise SystemExit(f"SIGN_IN must be demo or accounts, not {mode!r}")


def add_demo_users(path: Path) -> None:
    connection = db.connect(path)
    connection.execute("INSERT INTO users (id, role) VALUES ('lead', 'lead')")
    connection.execute("INSERT INTO users (id, role) VALUES ('manager', 'manager')")
    connection.execute("INSERT INTO users (id, role) VALUES ('coordinator', 'coordinator')")
    connection.execute("INSERT INTO users (id, role, carer) SELECT id, 'carer', id FROM carers")
    connection.commit()
    connection.close()


def security_headers(app: ASGIApp, demo: bool) -> ASGIApp:
    """Every response gets the security headers; static files are kept by the phone and checked
    by ETag, pages are never stored. In demo mode a visitor without a visitor cookie gets one, so
    their notes and decisions stay theirs when they sign in as another role.

    Plain ASGI, not Starlette's BaseHTTPMiddleware: that one logs "No response returned" each
    time a browser abandons a request, as a carer's phone does when leaving a page mid-check."""

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        known = Request(scope).cookies.get(VISITOR, "")
        if not VISITOR_TOKEN.fullmatch(known):
            known = ""
        arriving = secrets.token_urlsafe(32) if demo and not known else None
        scope.setdefault("state", {})["visitor"] = known or arriving

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.update(HEADERS)
                if scope["path"].startswith("/static/"):
                    headers["Cache-Control"] = "no-cache"
                if arriving:
                    cookie = f"{VISITOR}={arriving}; HttpOnly; Path=/; SameSite=strict; Secure"
                    headers.append("Set-Cookie", cookie)
            await send(message)

        await app(scope, receive, send_with_headers)

    return middleware


def load_shedding(app: ASGIApp, busy_page: bytes) -> ASGIApp:
    """Past IN_FLIGHT requests at once, answer at once with the busy page instead of queueing,
    so an overload slows no one already being served. The health check is never turned away: a
    busy app the kubelet took for a dead one would be restarted, which wipes the demo."""
    in_flight = 0
    head = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(busy_page)).encode()),
        (b"retry-after", b"5"),
    ]

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal in_flight
        if scope["type"] != "http" or scope["path"] == "/healthz":
            await app(scope, receive, send)
            return
        if in_flight >= IN_FLIGHT:
            await send({"type": "http.response.start", "status": 503, "headers": head})
            await send({"type": "http.response.body", "body": busy_page})
            return
        in_flight += 1
        try:
            await app(scope, receive, send)
        finally:
            in_flight -= 1

    return middleware


def restart() -> None:
    """Stop the server gracefully: Kubernetes and compose start it again, and the demo always
    starts from a fresh database. Used when the demo's storage is full."""
    signal.raise_signal(signal.SIGTERM)


# The rest of a refused body is never read, so the connection cannot carry another request: it
# is closed (RFC 9110, 408 and 413).
CLOSE = {"Connection": "close"}


async def form_fields(request: Request) -> dict[str, str]:
    """A posted form's fields (first value of each), parsed with the standard library. One
    address may send FORMS_AT_ONCE forms at a time and SAVES_PER_WINDOW in each window. The body
    is read as it arrives, so a form over LARGEST_FORM bytes or FORM_SECONDS is refused before it
    is all held in memory; it must be UTF-8 with at most MOST_FIELDS fields. A refusal is a page,
    and nothing is saved."""
    state = request.app.state.limits
    # Only the tunnel can reach the app, so Cloudflare's header names the visitor's address.
    address = request.headers.get("cf-connecting-ip", "")
    if not address and request.client:
        address = request.client.host
    now = time.time()
    refused = limits.admit(state, address, now, SAVES_PER_WINDOW, WINDOW_SECONDS, FORMS_AT_ONCE)
    if refused == limits.AT_ONCE:
        raise StarletteHTTPException(429, headers=CLOSE | {"Retry-After": "5"})
    if refused == limits.WINDOW:
        wait = str(limits.seconds_left(state, now, WINDOW_SECONDS))
        raise StarletteHTTPException(429, headers=CLOSE | {"Retry-After": wait})
    try:
        return await read_form(request)
    finally:
        limits.arrived(state, address)


async def read_form(request: Request) -> dict[str, str]:
    body = bytearray()
    try:
        async with asyncio.timeout(FORM_SECONDS):
            async for chunk in request.stream():
                body += chunk
                if len(body) > LARGEST_FORM:
                    raise StarletteHTTPException(413, headers=CLOSE)
    except TimeoutError:
        raise StarletteHTTPException(408, headers=CLOSE) from None
    except ClientDisconnect:
        raise StarletteHTTPException(400) from None  # the sender has gone; nothing to answer
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=MOST_FIELDS)
    except ValueError:  # bytes that are not UTF-8, or too many fields
        raise StarletteHTTPException(400) from None
    return {name: values[0] for name, values in fields.items()}


FormFields = Annotated[dict[str, str], Depends(form_fields)]


def create_app(settings: dict[str, Any]) -> FastAPI:
    demo = settings["mode"] == "demo"
    path: Path = settings["database"]
    # The demo's storage budget; accounts mode keeps real records and is never capped.
    budget: int | None = settings.get("budget", DEMO_BUDGET if demo else None)
    if demo:
        path.unlink(missing_ok=True)
        settings["anchor"].unlink(missing_ok=True)
    demo_week = seed.load()
    rota_data = seed.load(ROTA, ROTA.with_name("rota.json.sha256"))
    referral_data = seed.load(REFERRALS, REFERRALS.with_name("referrals.json.sha256"))
    road_map = maps.load()
    plan = {visit["id"]: visit for visit in demo_week["visits"]}
    junctions = {client["id"]: client["node"] for client in demo_week["clients"]}
    homes = {carer["id"]: carer["home"] for carer in demo_week["carers"]}
    # The demo's clock, a day and a minute, read on every request; a caller that sets
    # settings["clock"] moves it without restarting the app (and losing its database).
    settings.setdefault("clock", dict(demo_week["now"]))
    db.prepare(path, demo_week)
    if demo:
        add_demo_users(path)
    connection = db.connect(path, budget)
    db.load_referrals(connection, referral_data)
    found = auditlog.problems(connection, settings["key"], settings["anchor"])
    connection.close()
    if found:
        raise SystemExit("the audit log does not verify: " + "; ".join(found))
    if demo:
        # The committed SYNTHETIC export, through the same checks as any import.
        connection = db.connect(path, budget)
        loaded = importer.store(connection, importer.sample(), importer.SAMPLE.name, "the system",
                                importer.reference(demo_week))  # fmt: skip
        auditlog.record(connection, settings["key"], settings["anchor"],
                        {"at": time.time(), "event": "import", "user": "the system",
                         "sha256": loaded["sha256"], "kept": loaded["kept"],
                         "quarantined": loaded["quarantined"]})  # fmt: skip
        connection.close()
        if budget is not None and path.stat().st_size > budget / 2:
            # A demo that started nearly full would fill and restart again and again.
            raise SystemExit("the demo's starting data fills more than half its storage budget")

    # No API documentation pages: the app is a website, and the route list is not for visitors.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.limits = limits.new()
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    # The route sketch is up to about 100 KB of SVG; compressed, it suits a phone on a weak
    # signal. BREACH needs another site to make the browser send guesses with the user's
    # session; the session cookie is SameSite=Strict, so such requests arrive signed out.
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    # Made once, so turning a request away costs no rendering and no database.
    busy = {
        "title": BUSY[0],
        "message": BUSY[1],
        "back": "/",
        "back_label": "Go to your start page",
    }
    busy_page = TEMPLATES.get_template("problem.html").render(busy | {"demo": demo}).encode()
    app.add_middleware(load_shedding, busy_page=busy_page)
    app.add_middleware(security_headers, demo=demo)

    def log(connection: Any, entry: dict[str, Any]) -> None:
        auditlog.record(
            connection, settings["key"], settings["anchor"], {"at": time.time(), **entry}
        )

    def page(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
        # When the page was made, so a copy saved on a carer's phone can say how old it is.
        rendered = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        base = {"demo": demo, "rendered": rendered}
        return TEMPLATES.TemplateResponse(request, name, base | context)

    def to(url: str, token: str | None = None) -> RedirectResponse:
        response = RedirectResponse(url, status_code=303)
        if token is not None:
            set_session_cookie(response, token)
        return response

    @functools.lru_cache(maxsize=256)
    def drawn(home: str, stops: tuple[str, ...]) -> tuple[dict[str, Any], dict[str, Any]]:
        """A route's sketch and its live-map data. Both come only from the pinned road map, so
        each route is drawn once (about 45 ms) and kept: at most one per carer and day. The
        templates only read them."""
        return maps.sketch(road_map, home, list(stops)), maps.live(road_map, home, list(stops))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Answered on the event loop, not by a worker thread, so it answers while every
        thread is busy with pages."""
        return {"status": "ok", "build": BUILD}

    @app.get("/")
    def home(request: Request) -> Response:
        connection = db.connect(path, budget)
        try:
            session = auth.find_session(
                connection, request.cookies.get(auth.COOKIE, ""), time.time()
            )
            if session is None or session["user"] is None:
                return to("/signin")
            user = connection.execute(
                "SELECT role FROM users WHERE id = ?", (session["user"],)
            ).fetchone()
            if user is None:
                return to("/signin")
            return to(HOME.get(user["role"], "/signin"))
        finally:
            connection.close()

    def signin_form(
        request: Request, connection: Any, session: dict[str, Any], message: str
    ) -> HTMLResponse:
        carers = [row["id"] for row in connection.execute("SELECT id FROM carers ORDER BY id")]
        context = {"csrf": session["csrf"], "carers": carers, "message": message}
        return page(request, "signin.html", context)

    @app.get("/signin", response_class=HTMLResponse)
    def signin_page(request: Request) -> Response:
        connection = db.connect(path, budget)
        try:
            now = time.time()
            token = request.cookies.get(auth.COOKIE, "")
            session = auth.find_session(connection, token, now)
            if session is None:
                token = auth.start_session(connection, None, now)
                session = auth.find_session(connection, token, now) or {}
            response = signin_form(request, connection, session, "")
            set_session_cookie(response, token)
            return response
        finally:
            connection.close()

    @app.get("/guide", response_class=HTMLResponse)
    def guide(request: Request) -> HTMLResponse:
        """How to use the demo, one section per role. Public, so a visitor reads it first."""
        return page(request, "guide.html", {})

    @app.post("/signin")
    def signin(request: Request, fields: FormFields) -> Response:
        connection = db.connect(path, budget)
        try:
            now = time.time()
            token = request.cookies.get(auth.COOKIE, "")
            session = auth.find_session(connection, token, now)
            if session is None or not auth.csrf_ok(session, fields.get("csrf", "")):
                return expired(request, "/signin")
            user_id = fields.get("user", "")
            known = connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
            if demo:
                outcome = "ok" if known else "wrong"
            else:
                outcome = auth.check_password(connection, user_id, fields.get("password", ""), now)
            if outcome != "ok":
                # Only a real account name is logged: people type passwords into the name box.
                named = user_id if known else "unknown account"
                log(connection, {"event": "failed sign in", "user": named, "outcome": outcome})
                return signin_form(request, connection, session, SIGNIN_MESSAGES[outcome])
            auth.end_session(connection, token)
            log(connection, {"event": "sign in", "user": user_id})
            return to("/", auth.start_session(connection, user_id, now))
        finally:
            connection.close()

    @app.post("/signout")
    def signout(request: Request, fields: FormFields) -> Response:
        connection = db.connect(path, budget)
        try:
            token = request.cookies.get(auth.COOKIE, "")
            session = auth.find_session(connection, token, time.time())
            if not auth.csrf_ok(session, fields.get("csrf", "")):
                return expired(request, "/")
            auth.end_session(connection, token)
            if session and session["user"]:
                log(connection, {"event": "sign out", "user": session["user"]})
            response = to("/signin")
            response.delete_cookie(
                auth.COOKIE, path="/", secure=True, httponly=True, samesite="strict"
            )
            # Cached pages and the phone's drafts go; the session cookie is deleted above. Not
            # "cookies": a demo visitor's cookie keeps their notes theirs after signing out.
            response.headers["Clear-Site-Data"] = '"cache", "storage"'
            return response
        finally:
            connection.close()

    def signed_in(
        request: Request, connection: Any, roles: tuple[str, ...]
    ) -> tuple[dict[str, Any], Any] | Response:
        """The session and user, or the response to send instead: the sign-in page, or 403."""
        session = auth.find_session(connection, request.cookies.get(auth.COOKIE, ""), time.time())
        if session is None or session["user"] is None:
            return to("/signin")
        user = connection.execute("SELECT * FROM users WHERE id = ?", (session["user"],)).fetchone()
        if user is None:
            return to("/signin")
        if user["role"] not in roles:
            return page_403(request, connection, user["id"])
        return session, user

    def refused(request: Request, connection: Any, user: str, status: int) -> None:
        """Every refusal of a signed-in person is logged with the address they asked for."""
        path = request.url.path[:LONGEST_LOGGED]
        log(connection, {"event": "refused", "user": user, "status": status, "path": path})

    def page_403(request: Request, connection: Any, user: str) -> HTMLResponse:
        refused(request, connection, user, 403)
        response = page(request, "forbidden.html", {})
        response.status_code = 403
        return response

    def problem(
        request: Request, status: int, title: str, message: str, back: str, back_label: str = ""
    ) -> HTMLResponse:
        """A refusal as a page: what happened, and the way back, never bare text."""
        context = {"title": title, "message": message, "back": back, "back_label": back_label}
        response = page(request, "problem.html", context)
        response.status_code = status
        return response

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, error: StarletteHTTPException) -> HTMLResponse:
        """An unknown address, a wrong method or a refused form gets a page like every other,
        never JSON."""
        title, message = HTTP_ERRORS.get(error.status_code, HTTP_ERRORS[404])
        response = problem(request, error.status_code, title, message, "/", "Go to your start page")
        response.headers.update(error.headers or {})
        return response

    @app.exception_handler(Exception)
    def server_error(request: Request, error: Exception) -> HTMLResponse:
        """A fault shows a page that names nothing inside the app. Starlette answers it outside
        the middleware, so the security headers are added here."""
        message = "This page could not be shown. Try again in a moment."
        title = "Something went wrong"
        response = problem(request, 500, title, message, "/", "Go to your start page")
        response.headers.update(HEADERS)
        return response

    @app.exception_handler(sqlite3.OperationalError)
    def database_error(request: Request, error: sqlite3.OperationalError) -> HTMLResponse:
        """The write that failed was rolled back whole. A full demo restarts with fresh data; a
        database still busy after its wait is an overload; anything else is a server fault."""
        code = error.sqlite_errorcode & 0xFF  # the primary result code under an extended one
        if code == sqlite3.SQLITE_FULL and demo:
            restart()
            response = problem(request, 503, *RESTARTING, "/", "Go to your start page")
            response.headers["Retry-After"] = "30"
            return response
        if code == sqlite3.SQLITE_BUSY:
            response = problem(request, 503, *BUSY, "/", "Go to your start page")
            response.headers["Retry-After"] = "5"
            return response
        return server_error(request, error)

    def expired(request: Request, back: str) -> HTMLResponse:
        message = "Nothing was saved. Go back and the page will have a fresh form."
        return problem(request, 403, "This form has expired", message, back)

    def no_note(request: Request, back: str, connection: Any, user: str) -> HTMLResponse:
        refused(request, connection, user, 404)
        message = "This note does not exist, or it is not yours to see."
        return problem(request, 404, "No such note", message, back)

    def visitor_of(request: Request) -> str | None:
        """In demo mode, the visitor's own record: their notes and decisions, kept from every
        other visitor across sign-ins and roles. Accounts mode keeps one shared record."""
        if not demo:
            return None
        return hashlib.sha256(request.state.visitor.encode("ascii")).hexdigest()

    def visible_note(connection: Any, note_id: str, request: Request) -> Any:
        """A note's author and day, or None if there is no such note or another demo visitor
        wrote it."""
        return connection.execute(
            f"SELECT n.carer, n.day FROM notes n WHERE n.id = ? AND {screens.VISIBLE}",
            (note_id, visitor_of(request)),
        ).fetchone()

    def week_day(raw: str | None) -> int | None:
        """The day asked for if it is one of the week's. Compared as text: `int()` refuses some
        characters `isdigit()` accepts ("²"), and very long numbers."""
        days: list[int] = demo_week["days"]
        for day in days:
            if raw == str(day):
                return day
        return None

    def chosen_day(raw: str | None) -> int:
        """The day asked for if it is one of the week's, else the clock's day (the last day when
        the clock is past the week)."""
        asked = week_day(raw)
        if asked is not None:
            return asked
        today: int = settings["clock"]["day"]
        return today if today in demo_week["days"] else demo_week["days"][-1]

    def carer_may_open(connection: Any, request: Request, user: Any, kind: str, day: int) -> bool:
        """Whether this carer may open their own `kind` of record from `day` now: the rule is
        `access.carer_may_open`; the days they opened with a reason today come from the alerts,
        kept to this demo visitor."""
        today = settings["clock"]["day"]
        rows = connection.execute(
            "SELECT day FROM alerts WHERE user = ? AND visitor IS ? AND clock_day = ?",
            (user["id"], visitor_of(request), today),
        )
        return access.carer_may_open(kind, day, today, {row["day"] for row in rows})

    def closed_day(
        request: Request, connection: Any, session: dict[str, Any], user: Any, day: int,
        back: str, reason: str = "", message: str = "",
    ) -> HTMLResponse:  # fmt: skip
        """Another day's records, refused in place with the way to open them: a reason. Shows
        the day only, never a client."""
        if not message:
            refused(request, connection, user["id"], 403)
        context = {
            "user": user["id"],
            "csrf": session["csrf"],
            "day": day,
            "long_date": screens.week(demo_week["start"], [day])[0]["long"],
            "back": back,
            "reason": reason,
            "message": message,
            "longest": LONGEST_REASON,
        }
        response = page(request, "opening.html", context)
        response.status_code = 400 if message else 403
        return response

    @app.get("/queue", response_class=HTMLResponse)
    def queue(request: Request, day: str | None = None) -> Response:
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("lead",))
            if isinstance(found, Response):
                return found
            session, user = found
            today = chosen_day(day)
            log(connection, {"event": "view register", "user": user["id"], "day": today})
            context = {
                "user": user["id"],
                "csrf": session["csrf"],
                "week": screens.week(demo_week["start"], demo_week["days"]),
                "day": today,
                "register": screens.register(connection, today, visitor_of(request)),
                "link": "/queue",
            }
            return page(request, "queue.html", context)
        finally:
            connection.close()

    @app.get("/notes/{note_id}", response_class=HTMLResponse)
    def note(request: Request, note_id: str, choose: str | None = None) -> Response:
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("lead", "carer"))
            if isinstance(found, Response):
                return found
            session, user = found
            row = visible_note(connection, note_id, request)
            if row is None:
                return no_note(request, HOME[user["role"]], connection, user["id"])
            if user["role"] == "carer":
                if row["carer"] != user["carer"]:
                    return page_403(request, connection, user["id"])
                if not carer_may_open(connection, request, user, access.NOTE, row["day"]):
                    back = f"/notes/{note_id}"
                    return closed_day(request, connection, session, user, row["day"], back)
            log(connection, {"event": "view note", "user": user["id"], "note": note_id})
            detail = screens.note_detail(connection, note_id, visitor_of(request))
            reviewer = user["role"] == "lead"
            context = {
                "user": user["id"],
                "csrf": session["csrf"],
                "note": detail,
                "reviewer": reviewer,
                # On a wide screen the lead's register sits beside the note (list-detail).
                "register": screens.register(connection, detail["day"], visitor_of(request))
                if reviewer
                else None,
                "long_date": screens.week(demo_week["start"], [detail["day"]])[0]["long"],
                "choose": choose == "1",
                "nothing_missed": screens.NOTHING_MISSED,
            }
            return page(request, "note.html", context)
        finally:
            connection.close()

    @app.post("/notes/{note_id}/decisions")
    def decide(request: Request, note_id: str, fields: FormFields) -> Response:
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("lead",))
            if isinstance(found, Response):
                return found
            session, user = found
            if not auth.csrf_ok(session, fields.get("csrf", "")):
                return expired(request, f"/notes/{note_id}")
            if visible_note(connection, note_id, request) is None:
                return no_note(request, HOME[user["role"]], connection, user["id"])
            detail = screens.note_detail(connection, note_id, visitor_of(request))
            missed = fields.get("missed", "")
            if not detail["findings"] and missed not in screens.CATEGORIES_SET | {
                screens.NOTHING_MISSED
            }:
                # With no finding to decide, only the person's own record of their read counts;
                # a save with nothing chosen would leave no trace that anyone read the note.
                return to(f"/notes/{note_id}?choose=1")
            route = detail["reasons"][0] if detail["reasons"] else "opened directly"
            if not detail["read"]:
                route = "not read by the model"
            now = time.time()
            with db.WRITE:
                for group in detail["findings"]:
                    label = fields.get(f"finding-{group['id']}", "")
                    if label not in screens.LABELS:
                        continue
                    category = fields.get(f"category-{group['id']}", "")
                    if label != "wrong category" or category not in screens.CATEGORIES_SET:
                        category = ""
                    # One decision on the screen labels every stored finding in the group.
                    for finding in group["members"]:
                        connection.execute(
                            "INSERT INTO decisions (note, finding, label, category, reviewer, "
                            "visitor, route, decided) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (note_id, finding["id"], label, category or None, user["id"],
                             visitor_of(request), route, now),
                        )  # fmt: skip
                        log(connection, {"event": "decision", "user": user["id"], "note": note_id,
                                         "finding": finding["id"], "label": label})  # fmt: skip
                if missed in screens.CATEGORIES_SET | {screens.NOTHING_MISSED}:
                    # The whole note's verdict, with no finding: a missed concern and its category,
                    # or "not a concern", meaning the person read it all and found nothing missed.
                    label = (
                        "missed concern" if missed in screens.CATEGORIES_SET else "not a concern"
                    )
                    connection.execute(
                        "INSERT INTO decisions (note, label, category, reviewer, visitor, route, "
                        "decided) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (note_id, label, missed if label == "missed concern" else None, user["id"],
                         visitor_of(request), route, now),
                    )  # fmt: skip
                    log(connection, {"event": "decision", "user": user["id"], "note": note_id,
                                     "label": label})  # fmt: skip
                connection.commit()
            following = screens.next_note(connection, note_id, visitor_of(request))
            return to(f"/notes/{following}" if following else f"/queue?day={detail['day']}")
        finally:
            connection.close()

    @app.get("/team", response_class=HTMLResponse)
    def team(request: Request) -> Response:
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("manager",))
            if isinstance(found, Response):
                return found
            session, user = found
            log(connection, {"event": "view team", "user": user["id"]})
            context = {
                "user": user["id"],
                "csrf": session["csrf"],
                "alerts": screens.alerts(connection, demo_week["start"], visitor_of(request)),
                "team": screens.team(demo_week["retention"]),
            }
            return page(request, "team.html", context)
        finally:
            connection.close()

    @app.post("/alerts/{alert_id}/reviewed")
    def review_alert(request: Request, alert_id: str, fields: FormFields) -> Response:
        """The manager marks a carer's opening of another day reviewed, once."""
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("manager",))
            if isinstance(found, Response):
                return found
            session, user = found
            if not auth.csrf_ok(session, fields.get("csrf", "")):
                return expired(request, "/team")
            alert = connection.execute(
                "SELECT id FROM alerts WHERE CAST(id AS TEXT) = ? AND visitor IS ?",
                (alert_id, visitor_of(request)),
            ).fetchone()
            if alert is None:
                refused(request, connection, user["id"], 404)
                message = "This alert does not exist."
                return problem(request, 404, "No such alert", message, "/team")
            with db.WRITE:
                cursor = connection.execute(
                    "INSERT INTO alert_reviews (alert, reviewer, reviewed) VALUES (?, ?, ?) "
                    "ON CONFLICT (alert) DO NOTHING",
                    (alert["id"], user["id"], time.time()),
                )
                if cursor.rowcount:
                    log(connection, {"event": "review opening", "user": user["id"],
                                     "alert": alert["id"]})  # fmt: skip
                connection.commit()
            return to("/team#openings")
        finally:
            connection.close()

    @app.post("/openings")
    def open_day(request: Request, fields: FormFields) -> Response:
        """A carer opens another day's records with a reason. The alert for the manager and its
        audit entry are one write; the reason is kept in the alert only, never the log."""
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("carer",))
            if isinstance(found, Response):
                return found
            session, user = found
            if not auth.csrf_ok(session, fields.get("csrf", "")):
                return expired(request, "/today")
            day = week_day(fields.get("day"))
            if day is None:
                message = "That day is not in this week. Nothing was opened."
                return problem(request, 400, "No such day", message, "/today")
            back = fields.get("back", "")
            if not BACK.fullmatch(back):
                back = f"/today?day={day}"
            if carer_may_open(connection, request, user, access.SHEET, day):
                return to(back)  # already open: nothing to record
            reason = fields.get("reason", "").strip()
            if not reason or len(reason) > LONGEST_REASON:
                message = f"Nothing was opened: write why in 1 to {LONGEST_REASON} characters."
                return closed_day(request, connection, session, user, day, back,
                                  fields.get("reason", ""), message)  # fmt: skip
            clock = settings["clock"]
            with db.WRITE:
                cursor = connection.execute(
                    "INSERT INTO alerts (user, visitor, day, reason, clock_day, clock_minute, "
                    "opened) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user["id"], visitor_of(request), day, reason, clock["day"], clock["minute"],
                     time.time()),
                )  # fmt: skip
                log(connection, {"event": "open another day", "user": user["id"], "day": day,
                                 "alert": cursor.lastrowid})  # fmt: skip
                connection.commit()
            return to(back)
        finally:
            connection.close()

    @app.get("/today", response_class=HTMLResponse)
    def today(request: Request, day: str | None = None, sent: str | None = None) -> Response:
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("carer",))
            if isinstance(found, Response):
                return found
            session, user = found
            chosen = chosen_day(day)
            carer = user["carer"]
            if not carer_may_open(connection, request, user, access.SHEET, chosen):
                back = f"/today?day={chosen}"
                return closed_day(request, connection, session, user, chosen, back)
            log(connection, {"event": "view day sheet", "user": user["id"], "day": chosen})
            sheet = screens.carer_day(
                connection, carer, chosen, visitor_of(request), plan, settings["clock"]
            )
            sketch = None
            live = None
            if sheet["visits"]:
                stops = tuple(junctions[visit["client_id"]] for visit in sheet["visits"])
                sketch, live = drawn(homes[carer], stops)  # live: the live map, with signal
            for visit in sheet["visits"]:
                visit["directions"] = maps.directions(road_map, junctions[visit["client_id"]])
            context = {
                "user": user["id"],
                "csrf": session["csrf"],
                "week": screens.week(demo_week["start"], demo_week["days"]),
                "day": chosen,
                "is_today": chosen == settings["clock"]["day"],
                "now": screens.clock(settings["clock"]["minute"]),
                "sheet": sheet,
                "sketch": sketch,
                "live": live,
                "sent": sent == "1",
                "link": "/today",
            }
            response = page(request, "today.html", context)
            response.headers[SERVED_DAY] = str(settings["clock"]["day"])
            return response
        finally:
            connection.close()

    def own_visit(connection: Any, carer: str, visit_id: str) -> Any:
        """One of this carer's visits with its client, or None for any other visit."""
        return connection.execute(
            "SELECT v.*, c.name, c.needs FROM visits v JOIN clients c ON c.id = v.client "
            "WHERE v.id = ? AND v.carer = ?",
            (visit_id, carer),
        ).fetchone()

    def write_form(
        request: Request, connection: Any, session: dict[str, Any], user: Any, visit: Any,
        body: str = "", message: str = "",
    ) -> HTMLResponse:  # fmt: skip
        """The note form for a visit, with a fresh idempotency key: an addition to the carer's
        first note once one exists, since a care record is never edited."""
        notes = screens.own_notes(connection, user["carer"], visit["id"], visitor_of(request))
        planned = plan[visit["id"]]
        sheet_open = carer_may_open(connection, request, user, access.SHEET, visit["day"])
        context = {
            "user": user["id"],
            "csrf": session["csrf"],
            # Yesterday's form opens, its day sheet does not: back to today's.
            "back": f"/today?day={visit['day']}" if sheet_open else "/today",
            # A note records a visit that happened: before it begins there is no form.
            "from": None
            if screens.started(planned, settings["clock"])
            else screens.clock(planned["starts"]),
            "visit": {
                "id": visit["id"],
                "day": visit["day"],
                "client": visit["name"],
                "visit": screens.SLOT_NAMES[visit["slot"]],
                "window": f"{screens.clock(visit['opens'])}–{screens.clock(visit['closes'])}",
                "long_date": screens.week(demo_week["start"], [visit["day"]])[0]["long"],
            },
            "notes": notes,
            "amends": notes[0]["id"] if notes else "",
            "key": str(uuid.uuid4()),
            "body": body,
            "message": message,
            "longest": LONGEST_NOTE,
        }
        response = page(request, "write.html", context)
        response.headers[SERVED_DAY] = str(settings["clock"]["day"])
        return response

    @app.get("/visits/{visit_id}/note", response_class=HTMLResponse)
    def write(request: Request, visit_id: str) -> Response:
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("carer",))
            if isinstance(found, Response):
                return found
            session, user = found
            visit = own_visit(connection, user["carer"], visit_id)
            if visit is None:
                return page_403(request, connection, user["id"])
            if not carer_may_open(connection, request, user, access.FORM, visit["day"]):
                back = f"/visits/{visit_id}/note"
                return closed_day(request, connection, session, user, visit["day"], back)
            log(connection, {"event": "view visit", "user": user["id"], "visit": visit_id})
            return write_form(request, connection, session, user, visit)
        finally:
            connection.close()

    @app.post("/visits/{visit_id}/notes")
    def send(request: Request, visit_id: str, fields: FormFields) -> Response:
        """Store a carer's note. The carer comes from the session, never the form. A Send carries
        an idempotency key: sent again with the same words it stores nothing new; with other
        words it is refused (422), as the IETF Idempotency-Key draft advises."""
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("carer",))
            if isinstance(found, Response):
                return found
            session, user = found
            if not auth.csrf_ok(session, fields.get("csrf", "")):
                return expired(request, "/today")
            carer = user["carer"]
            visit = own_visit(connection, carer, visit_id)
            if visit is None:
                return page_403(request, connection, user["id"])
            sheet = "/today"
            if not screens.started(plan[visit_id], settings["clock"]):
                message = "A note records a visit that happened. Nothing was saved."
                return problem(request, 400, "This visit has not begun", message, sheet)
            visitor = visitor_of(request)
            asked = fields.get("amends", "") or None
            own = screens.own_notes(connection, carer, visit_id, visitor)
            if asked is not None and asked not in {note["id"] for note in own}:
                return page_403(request, connection, user["id"])
            key = fields.get("key", "")
            body = fields.get("body", "").strip()
            if not KEY.fullmatch(key):
                message = "Nothing was saved. Open the visit again for a fresh form."
                return problem(request, 400, "This form is out of date", message, sheet)
            if not body or len(body) > LONGEST_NOTE:
                message = f"Not sent: write the note in 1 to {LONGEST_NOTE:,} characters."
                response = write_form(request, connection, session, user, visit, body, message)
                response.status_code = 400
                return response
            sent = "/today?sent=1"
            with db.WRITE:  # looking for an earlier copy and storing it are one step
                earlier = connection.execute(
                    "SELECT visit, body FROM notes WHERE carer = ? AND idempotency_key = ?",
                    (carer, key),
                ).fetchone()
                if earlier is not None:
                    if tuple(earlier) == (visit_id, body):
                        return to(sent)  # the same Send again: already stored
                    message = "This Send was used for different words, and that note is kept."
                    return problem(request, 422, "This note was already sent", message, sheet)
                # A record never holds two first notes: once the carer has written this visit up, a
                # Send is an addition to the first note, even from a form saved before it was sent.
                # Read again under the lock, so two Sends at once cannot both be first notes.
                own = screens.own_notes(connection, carer, visit_id, visitor)
                firsts = [note["id"] for note in own if note["amends"] is None]
                amends = firsts[0] if firsts else None
                note_id = "note-" + secrets.token_hex(8)
                connection.execute(
                    "INSERT INTO notes (id, visit, client, carer, day, body, tamper, "
                    "tamper_probability, source, amends, idempotency_key, written, visitor) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, 0, 'app', ?, ?, ?, ?)",
                    (note_id, visit_id, visit["client"], carer, visit["day"], body, amends, key,
                     time.time(), visitor),
                )  # fmt: skip
                log(connection, {"event": "write note", "user": user["id"], "note": note_id,
                                 "visit": visit_id, "amends": amends})  # fmt: skip
                connection.commit()
            return to(sent)
        finally:
            connection.close()

    @app.get("/rota", response_class=HTMLResponse)
    def rota(request: Request, day: str | None = None, carer: str | None = None) -> Response:
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("coordinator",))
            if isinstance(found, Response):
                return found
            session, user = found
            chosen = chosen_day(day)
            reasons = {
                item["visit"]: item["reason"] for item in rota_data["uncovered"][str(chosen)]
            }
            the_day = screens.rota_day(connection, chosen, plan, reasons, carer)
            log(connection, {"event": "view rota", "user": user["id"], "day": chosen,
                             "carer": the_day["chosen"]})  # fmt: skip
            sketch = None
            if the_day["chosen"]:
                stops = tuple(junctions[visit["client_id"]] for visit in the_day["route"])
                sketch = drawn(homes[the_day["chosen"]], stops)[0]
            context = {
                "user": user["id"],
                "csrf": session["csrf"],
                "week": screens.week(demo_week["start"], demo_week["days"]),
                "day": chosen,
                "rota": the_day,
                "sketch": sketch,
                "link": "/rota",
                "replan": rota_data["replan"],
            }
            return page(request, "rota.html", context)
        finally:
            connection.close()

    @app.get("/rota/replan", response_class=HTMLResponse)
    def rota_replan(request: Request) -> Response:
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("coordinator",))
            if isinstance(found, Response):
                return found
            session, user = found
            log(connection, {"event": "view re-plan", "user": user["id"]})
            view = screens.replan_view(connection, rota_data["replan"])
            context = {
                "user": user["id"],
                "csrf": session["csrf"],
                "plan": view,
                "long_date": screens.week(demo_week["start"], [view["day"]])[0]["long"],
            }
            return page(request, "replan.html", context)
        finally:
            connection.close()

    @app.get("/imports", response_class=HTMLResponse)
    def imports(request: Request) -> Response:
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("coordinator",))
            if isinstance(found, Response):
                return found
            session, user = found
            log(connection, {"event": "view imports", "user": user["id"]})
            context = {
                "user": user["id"],
                "csrf": session["csrf"],
                "report": screens.import_report(connection),
            }
            return page(request, "imports.html", context)
        finally:
            connection.close()

    def no_referral(request: Request, connection: Any, user: str) -> HTMLResponse:
        refused(request, connection, user, 404)
        message = "This referral does not exist."
        return problem(request, 404, "No such referral", message, "/referrals")

    @app.get("/referrals", response_class=HTMLResponse)
    def referrals(request: Request) -> Response:
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("coordinator",))
            if isinstance(found, Response):
                return found
            session, user = found
            log(connection, {"event": "view referrals", "user": user["id"]})
            context = {
                "user": user["id"],
                "csrf": session["csrf"],
                "referrals": screens.referral_list(connection, visitor_of(request)),
            }
            return page(request, "referrals.html", context)
        finally:
            connection.close()

    def referral_page(
        request: Request, connection: Any, session: dict[str, Any], user: Any, referral_id: str,
        posted: dict[str, Any] | None = None, message: str = "",
    ) -> HTMLResponse | None:  # fmt: skip
        """A referral's page, or None if there is no such referral. Matching uses the demo
        agency's carers and only a profile a person has confirmed."""
        visitor = visitor_of(request)
        detail = screens.referral_detail(
            connection, referral_id, visitor, demo_week["carers"], posted
        )
        if detail is None:
            return None
        context = {
            "user": user["id"],
            "csrf": session["csrf"],
            "referral": detail,
            "referrals": screens.referral_list(connection, visitor),
            "message": message,
        }
        return page(request, "referral.html", context)

    @app.get("/referrals/{referral_id}", response_class=HTMLResponse)
    def referral(request: Request, referral_id: str) -> Response:
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("coordinator",))
            if isinstance(found, Response):
                return found
            session, user = found
            response = referral_page(request, connection, session, user, referral_id)
            if response is None:
                return no_referral(request, connection, user["id"])
            log(connection, {"event": "view referral", "user": user["id"], "referral": referral_id})
            return response
        finally:
            connection.close()

    @app.post("/referrals/{referral_id}/decisions")
    def confirm_referral(request: Request, referral_id: str, fields: FormFields) -> Response:
        """Store the profile a coordinator confirmed. Who confirmed it comes from the session,
        never the form, and the profile only from the form's known fields and values."""
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("coordinator",))
            if isinstance(found, Response):
                return found
            session, user = found
            back = f"/referrals/{referral_id}"
            if not auth.csrf_ok(session, fields.get("csrf", "")):
                return expired(request, back)
            known = connection.execute(
                "SELECT claims FROM referrals WHERE id = ?", (referral_id,)
            ).fetchone()
            if known is None:
                return no_referral(request, connection, user["id"])
            read = [claim["claim"] for claim in json.loads(known["claims"])]
            profile, why = screens.profile_from_form(fields, read)
            if profile is None:
                message = "The form held a choice this page never offers. Nothing was saved."
                return problem(request, 400, "This form could not be saved", message, back)
            if why:
                message = REFUSED_PROFILE[why]
                response = referral_page(request, connection, session, user, referral_id, profile,
                                         message)  # fmt: skip
                if response is None:
                    return no_referral(request, connection, user["id"])
                response.status_code = 400
                return response
            with db.WRITE:
                cursor = connection.execute(
                    "INSERT INTO referral_decisions (referral, profile, decided_by, visitor, "
                    "decided) VALUES (?, ?, ?, ?, ?)",
                    (referral_id, json.dumps(profile, sort_keys=True), user["id"],
                     visitor_of(request), time.time()),
                )  # fmt: skip
                decision = cursor.lastrowid
                log(connection, {"event": "confirm referral", "user": user["id"],
                                 "referral": referral_id, "decision": decision})  # fmt: skip
                connection.commit()
            return to(f"{back}#who")
        finally:
            connection.close()

    def export_request(connection: Any, request: Request, request_id: str) -> Any:
        """An export request in view of this demo visitor, with who approved and downloaded it
        (None for each not yet done), or None if there is no such request."""
        return connection.execute(
            "SELECT r.*, a.approved_by, x.released_by FROM export_requests r "
            "LEFT JOIN export_approvals a ON a.request = r.id "
            "LEFT JOIN export_releases x ON x.request = r.id "
            "WHERE CAST(r.id AS TEXT) = ? AND r.visitor IS ?",
            (request_id, visitor_of(request)),
        ).fetchone()

    def export_refused(request: Request, connection: Any, user: Any, why: str) -> HTMLResponse:
        status, title, message = EXPORT_REFUSALS[why]
        refused(request, connection, user["id"], status)
        return problem(request, status, title, message, "/exports", "Back to exports")

    def exports_page(
        request: Request, connection: Any, session: dict[str, Any], user: Any,
        reason: str = "", message: str = "",
    ) -> HTMLResponse:  # fmt: skip
        clients = connection.execute("SELECT id, name FROM clients ORDER BY name").fetchall()
        context = {
            "user": user["id"],
            "role": user["role"],
            "csrf": session["csrf"],
            "clients": [dict(row) for row in clients],
            "requests": screens.exports(connection, visitor_of(request), time.time()),
            "reason": reason,
            "message": message,
            "longest": LONGEST_REASON,
        }
        response = page(request, "exports.html", context)
        response.status_code = 400 if message else 200
        return response

    @app.get("/exports", response_class=HTMLResponse)
    def exports(request: Request) -> Response:
        """Requests for a client's record as a file: the lead asks, a coordinator approves."""
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("lead", "coordinator"))
            if isinstance(found, Response):
                return found
            session, user = found
            log(connection, {"event": "view exports", "user": user["id"]})
            return exports_page(request, connection, session, user)
        finally:
            connection.close()

    @app.post("/exports")
    def ask_export(request: Request, fields: FormFields) -> Response:
        """The lead asks for one client's record, with a reason; it needs a coordinator's
        approval within a working day."""
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("lead",))
            if isinstance(found, Response):
                return found
            session, user = found
            if not auth.csrf_ok(session, fields.get("csrf", "")):
                return expired(request, "/exports")
            client = connection.execute(
                "SELECT id FROM clients WHERE id = ?", (fields.get("client", ""),)
            ).fetchone()
            reason = fields.get("reason", "").strip()
            if client is None or not reason or len(reason) > LONGEST_REASON:
                message = (
                    "Nothing was asked: choose a client and write why in 1 to "
                    f"{LONGEST_REASON} characters."
                )
                return exports_page(request, connection, session, user, fields.get("reason", ""),
                                    message)  # fmt: skip
            now = time.time()
            with db.WRITE:
                cursor = connection.execute(
                    "INSERT INTO export_requests (client, reason, requested_by, visitor, "
                    "requested, deadline) VALUES (?, ?, ?, ?, ?, ?)",
                    (client["id"], reason, user["id"], visitor_of(request), now,
                     now + EXPORT_SECONDS),
                )  # fmt: skip
                log(connection, {"event": "ask for export", "user": user["id"],
                                 "request": cursor.lastrowid, "client": client["id"]})  # fmt: skip
                connection.commit()
            return to("/exports")
        finally:
            connection.close()

    @app.post("/exports/{request_id}/approve")
    def approve_export(request: Request, request_id: str, fields: FormFields) -> Response:
        """A coordinator approves another person's request, once, before its deadline. Who
        approved comes from the session, never the form."""
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("coordinator",))
            if isinstance(found, Response):
                return found
            session, user = found
            if not auth.csrf_ok(session, fields.get("csrf", "")):
                return expired(request, "/exports")
            asked = export_request(connection, request, request_id)
            if asked is None:
                return export_refused(request, connection, user, "missing")
            if asked["requested_by"] == user["id"]:
                return export_refused(request, connection, user, "own")
            if asked["approved_by"] is not None:
                return export_refused(request, connection, user, "approved")
            if time.time() > asked["deadline"]:
                return export_refused(request, connection, user, "late")
            with db.WRITE:
                cursor = connection.execute(
                    "INSERT INTO export_approvals (request, approved_by, approved) "
                    "VALUES (?, ?, ?) ON CONFLICT (request) DO NOTHING",
                    (asked["id"], user["id"], time.time()),
                )
                if cursor.rowcount:
                    log(connection, {"event": "approve export", "user": user["id"],
                                     "request": asked["id"]})  # fmt: skip
                connection.commit()
            return to("/exports")
        finally:
            connection.close()

    @app.post("/exports/{request_id}/download")
    def download_export(request: Request, request_id: str, fields: FormFields) -> Response:
        """The person who asked downloads the approved record once, as a FHIR R4 file. The
        download is logged, with the file's SHA-256, before the file is sent."""
        connection = db.connect(path, budget)
        try:
            found = signed_in(request, connection, ("lead",))
            if isinstance(found, Response):
                return found
            session, user = found
            if not auth.csrf_ok(session, fields.get("csrf", "")):
                return expired(request, "/exports")
            asked = export_request(connection, request, request_id)
            if asked is None:
                return export_refused(request, connection, user, "missing")
            if asked["requested_by"] != user["id"]:
                return export_refused(request, connection, user, "not yours")
            if asked["approved_by"] is None:
                return export_refused(request, connection, user, "unapproved")
            if asked["released_by"] is not None:
                return export_refused(request, connection, user, "downloaded")
            record = fhir.bundle(connection, demo_week, asked["client"], visitor_of(request))
            text = fhir.dumps(record)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            with db.WRITE:
                cursor = connection.execute(
                    "INSERT INTO export_releases (request, released_by, released, resources, "
                    "sha256) VALUES (?, ?, ?, ?, ?) ON CONFLICT (request) DO NOTHING",
                    (asked["id"], user["id"], time.time(), len(record["entry"]), digest),
                )
                if not cursor.rowcount:
                    connection.rollback()
                    return export_refused(request, connection, user, "downloaded")
                log(connection, {"event": "fhir export", "user": user["id"],
                                 "request": asked["id"], "client": asked["client"],
                                 "resources": len(record["entry"]), "sha256": digest})  # fmt: skip
                connection.commit()
            name = f"{asked['client']}-record.json"
            disposition = {"Content-Disposition": f'attachment; filename="{name}"'}
            return Response(text, media_type="application/fhir+json", headers=disposition)
        finally:
            connection.close()

    @app.get("/sw.js")
    def service_worker() -> FileResponse:
        """The carer app's service worker, served from the root so it may look after /today."""
        return FileResponse(STATIC / "sw.js", media_type="text/javascript")

    @app.get("/map-data")
    def map_data() -> FileResponse:
        """The road data the map is drawn from, offered under the ODbL, as its licence asks."""
        return FileResponse(
            maps.FILE, media_type="application/json", filename="homecare-roads-odbl.json"
        )

    return app


def create_app_from_env() -> FastAPI:
    """The uvicorn factory: `uvicorn homecare.app.main:create_app_from_env --factory`."""
    return create_app(settings_from_env())
