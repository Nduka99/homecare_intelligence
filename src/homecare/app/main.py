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

import datetime
import hashlib
import mimetypes
import os
import re
import secrets
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
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from homecare.app import auditlog, auth, db, importer, maps, screens, seed

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC = Path(__file__).parent / "static"
# Windows does not know the web app manifest's type; browsers expect it.
mimetypes.add_type("application/manifest+json", ".webmanifest")
# The commit the image was built from (scripts/cluster.py passes it in), so the live site can be
# matched to a build; "local" when it is not given.
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
VISITOR = "__Host-visitor"
# A Send's idempotency key: a version 4 UUID, made by the server or the carer's phone.
KEY = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
LONGEST_NOTE = 4000
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
        known = Request(scope).cookies.get(VISITOR)
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


async def form_fields(request: Request) -> dict[str, str]:
    """A posted form's fields (first value of each), parsed with the standard library."""
    fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {name: values[0] for name, values in fields.items()}


FormFields = Annotated[dict[str, str], Depends(form_fields)]


def create_app(settings: dict[str, Any]) -> FastAPI:
    demo = settings["mode"] == "demo"
    path: Path = settings["database"]
    if demo:
        path.unlink(missing_ok=True)
        settings["anchor"].unlink(missing_ok=True)
    demo_week = seed.load()
    rota_data = seed.load(ROTA, ROTA.with_name("rota.json.sha256"))
    road_map = maps.load()
    plan = {visit["id"]: visit for visit in demo_week["visits"]}
    junctions = {client["id"]: client["node"] for client in demo_week["clients"]}
    homes = {carer["id"]: carer["home"] for carer in demo_week["carers"]}
    demo_now = demo_week["now"]  # the demo's clock: a day and a minute
    db.prepare(path, demo_week)
    if demo:
        add_demo_users(path)
    connection = db.connect(path)
    found = auditlog.problems(connection, settings["key"], settings["anchor"])
    connection.close()
    if found:
        raise SystemExit("the audit log does not verify: " + "; ".join(found))
    if demo:
        # The committed SYNTHETIC export, through the same checks as any import.
        connection = db.connect(path)
        loaded = importer.store(connection, importer.sample(), importer.SAMPLE.name, "demo start",
                                importer.reference(demo_week))  # fmt: skip
        auditlog.record(connection, settings["key"], settings["anchor"],
                        {"at": time.time(), "event": "import", "user": "demo start",
                         "sha256": loaded["sha256"], "kept": loaded["kept"],
                         "quarantined": loaded["quarantined"]})  # fmt: skip
        connection.close()

    app = FastAPI(title="Home-care intelligence (synthetic data)", version="0.1.0")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    # The route sketch is up to about 100 KB of SVG; compressed, it suits a phone on a weak
    # signal. BREACH needs another site to make the browser send guesses with the user's
    # session; the session cookie is SameSite=Strict, so such requests arrive signed out.
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(security_headers, demo=demo)

    def log(connection: Any, entry: dict[str, Any]) -> None:
        auditlog.record(
            connection, settings["key"], settings["anchor"], {"at": time.time(), **entry}
        )

    def page(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
        # When the page was made, so a copy saved on a carer's phone can say how old it is.
        rendered = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        base = {"build": BUILD, "demo": demo, "rendered": rendered}
        return TEMPLATES.TemplateResponse(request, name, base | context)

    def to(url: str, token: str | None = None) -> RedirectResponse:
        response = RedirectResponse(url, status_code=303)
        if token is not None:
            set_session_cookie(response, token)
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "build": BUILD}

    @app.get("/")
    def home(request: Request) -> Response:
        connection = db.connect(path)
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
        connection = db.connect(path)
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

    @app.post("/signin")
    def signin(request: Request, fields: FormFields) -> Response:
        connection = db.connect(path)
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
        connection = db.connect(path)
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
            return page_403(request)
        return session, user

    def page_403(request: Request) -> HTMLResponse:
        response = page(request, "forbidden.html", {})
        response.status_code = 403
        return response

    def problem(request: Request, status: int, title: str, message: str, back: str) -> HTMLResponse:
        """A refusal as a page: what happened, and the way back, never bare text."""
        response = page(request, "problem.html", {"title": title, "message": message, "back": back})
        response.status_code = status
        return response

    def expired(request: Request, back: str) -> HTMLResponse:
        message = "Nothing was saved. Go back and the page will have a fresh form."
        return problem(request, 403, "This form has expired", message, back)

    def no_note(request: Request, back: str) -> HTMLResponse:
        message = "This note does not exist, or it is not yours to see."
        return problem(request, 404, "No such note", message, back)

    def visitor_of(request: Request) -> str | None:
        """In demo mode, the visitor's own record: their notes and decisions, kept from every
        other visitor across sign-ins and roles. Accounts mode keeps one shared record."""
        if not demo:
            return None
        return hashlib.sha256(request.state.visitor.encode("ascii")).hexdigest()

    def visible_note(connection: Any, note_id: str, request: Request) -> Any:
        """A note's author, or None if there is no such note or another demo visitor wrote it."""
        return connection.execute(
            f"SELECT n.carer FROM notes n WHERE n.id = ? AND {screens.VISIBLE}",
            (note_id, visitor_of(request)),
        ).fetchone()

    def chosen_day(raw: str | None) -> int:
        days: list[int] = demo_week["days"]
        return int(raw) if raw and raw.isdigit() and int(raw) in days else days[-1]

    @app.get("/queue", response_class=HTMLResponse)
    def queue(request: Request, day: str | None = None) -> Response:
        connection = db.connect(path)
        try:
            found = signed_in(request, connection, ("lead",))
            if isinstance(found, Response):
                return found
            session, user = found
            today = chosen_day(day)
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
        connection = db.connect(path)
        try:
            found = signed_in(request, connection, ("lead", "carer"))
            if isinstance(found, Response):
                return found
            session, user = found
            row = visible_note(connection, note_id, request)
            if row is None:
                return no_note(request, HOME[user["role"]])
            if user["role"] == "carer" and row["carer"] != user["carer"]:
                return page_403(request)
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
                "extraction": demo_week["extraction"],
                "long_date": screens.week(demo_week["start"], [detail["day"]])[0]["long"],
                "choose": choose == "1",
                "nothing_missed": screens.NOTHING_MISSED,
            }
            return page(request, "note.html", context)
        finally:
            connection.close()

    @app.post("/notes/{note_id}/decisions")
    def decide(request: Request, note_id: str, fields: FormFields) -> Response:
        connection = db.connect(path)
        try:
            found = signed_in(request, connection, ("lead",))
            if isinstance(found, Response):
                return found
            session, user = found
            if not auth.csrf_ok(session, fields.get("csrf", "")):
                return expired(request, f"/notes/{note_id}")
            if visible_note(connection, note_id, request) is None:
                return no_note(request, HOME[user["role"]])
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
                label = "missed concern" if missed in screens.CATEGORIES_SET else "not a concern"
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
        connection = db.connect(path)
        try:
            found = signed_in(request, connection, ("manager",))
            if isinstance(found, Response):
                return found
            session, user = found
            log(connection, {"event": "view team", "user": user["id"]})
            context = {
                "user": user["id"],
                "csrf": session["csrf"],
                "team": screens.team(demo_week["retention"]),
            }
            return page(request, "team.html", context)
        finally:
            connection.close()

    @app.get("/today", response_class=HTMLResponse)
    def today(request: Request, day: str | None = None, sent: str | None = None) -> Response:
        connection = db.connect(path)
        try:
            found = signed_in(request, connection, ("carer",))
            if isinstance(found, Response):
                return found
            session, user = found
            chosen = chosen_day(day)
            carer = user["carer"]
            sheet = screens.carer_day(
                connection, carer, chosen, visitor_of(request), plan, demo_now
            )
            sketch = None
            live = None
            if sheet["visits"]:
                stops = [junctions[visit["client_id"]] for visit in sheet["visits"]]
                sketch = maps.sketch(road_map, homes[carer], stops)
                live = maps.live(road_map, homes[carer], stops)  # the live map, with signal
            for visit in sheet["visits"]:
                visit["directions"] = maps.directions(road_map, junctions[visit["client_id"]])
            context = {
                "user": user["id"],
                "csrf": session["csrf"],
                "week": screens.week(demo_week["start"], demo_week["days"]),
                "day": chosen,
                "is_today": chosen == demo_week["now"]["day"],
                "now": screens.clock(demo_week["now"]["minute"]),
                "sheet": sheet,
                "sketch": sketch,
                "live": live,
                "sent": sent == "1",
                "link": "/today",
            }
            return page(request, "today.html", context)
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
        context = {
            "user": user["id"],
            "csrf": session["csrf"],
            # A note records a visit that happened: before it begins there is no form.
            "from": None
            if screens.started(planned, demo_now)
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
        return page(request, "write.html", context)

    @app.get("/visits/{visit_id}/note", response_class=HTMLResponse)
    def write(request: Request, visit_id: str) -> Response:
        connection = db.connect(path)
        try:
            found = signed_in(request, connection, ("carer",))
            if isinstance(found, Response):
                return found
            session, user = found
            visit = own_visit(connection, user["carer"], visit_id)
            if visit is None:
                return page_403(request)
            return write_form(request, connection, session, user, visit)
        finally:
            connection.close()

    @app.post("/visits/{visit_id}/notes")
    def send(request: Request, visit_id: str, fields: FormFields) -> Response:
        """Store a carer's note. The carer comes from the session, never the form. A Send carries
        an idempotency key: sent again with the same words it stores nothing new; with other
        words it is refused (422), as the IETF Idempotency-Key draft advises."""
        connection = db.connect(path)
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
                return page_403(request)
            sheet = f"/today?day={visit['day']}"
            if not screens.started(plan[visit_id], demo_now):
                message = "A note records a visit that happened. Nothing was saved."
                return problem(request, 400, "This visit has not begun", message, sheet)
            visitor = visitor_of(request)
            asked = fields.get("amends", "") or None
            own = screens.own_notes(connection, carer, visit_id, visitor)
            if asked is not None and asked not in {note["id"] for note in own}:
                return page_403(request)
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
            sent = f"{sheet}&sent=1"
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
        connection = db.connect(path)
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
            sketch = None
            if the_day["chosen"]:
                stops = [junctions[visit["client_id"]] for visit in the_day["route"]]
                sketch = maps.sketch(road_map, homes[the_day["chosen"]], stops)
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
        connection = db.connect(path)
        try:
            found = signed_in(request, connection, ("coordinator",))
            if isinstance(found, Response):
                return found
            session, user = found
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
        connection = db.connect(path)
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
