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

import os
import secrets
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from homecare.app import auditlog, auth, db, screens, seed

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
# Render sets RENDER_GIT_COMMIT, so the live site can be matched to a local build.
BUILD = os.environ.get("RENDER_GIT_COMMIT", "local")[:12]
ACCOUNTS_FOLDER = Path("data") / "platform"
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'none'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
HOME = {"lead": "/queue", "carer": "/today"}
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
    connection.execute("INSERT INTO users (id, role, carer) SELECT id, 'carer', id FROM carers")
    connection.commit()
    connection.close()


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
    db.prepare(path, demo_week)
    if demo:
        add_demo_users(path)
    connection = db.connect(path)
    found = auditlog.problems(connection, settings["key"], settings["anchor"])
    connection.close()
    if found:
        raise SystemExit("the audit log does not verify: " + "; ".join(found))

    app = FastAPI(title="Home-care intelligence (synthetic data)", version="0.1.0")
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[..., Any]) -> Response:
        response: Response = await call_next(request)
        response.headers.update(HEADERS)
        return response

    def log(connection: Any, entry: dict[str, Any]) -> None:
        auditlog.record(
            connection, settings["key"], settings["anchor"], {"at": time.time(), **entry}
        )

    def page(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
        base = {"build": BUILD, "demo": demo}
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
                return Response("The form expired. Go back and try again.", status_code=403)
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
                return Response("The form expired. Go back and try again.", status_code=403)
            auth.end_session(connection, token)
            if session and session["user"]:
                log(connection, {"event": "sign out", "user": session["user"]})
            response = to("/signin")
            response.delete_cookie(
                auth.COOKIE, path="/", secure=True, httponly=True, samesite="strict"
            )
            response.headers["Clear-Site-Data"] = '"cache", "cookies", "storage"'
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

    def visitor_of(session: dict[str, Any]) -> str | None:
        """Demo visitors see only their own decisions; accounts share one record."""
        return str(session["token_hash"]) if demo else None

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
                "register": screens.register(connection, today, visitor_of(session)),
                "link": "/queue",
            }
            return page(request, "queue.html", context)
        finally:
            connection.close()

    @app.get("/notes/{note_id}", response_class=HTMLResponse)
    def note(request: Request, note_id: str) -> Response:
        connection = db.connect(path)
        try:
            found = signed_in(request, connection, ("lead", "carer"))
            if isinstance(found, Response):
                return found
            session, user = found
            row = connection.execute("SELECT carer FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                return Response("No such note.", status_code=404)
            if user["role"] == "carer" and row["carer"] != user["carer"]:
                return page_403(request)
            log(connection, {"event": "view note", "user": user["id"], "note": note_id})
            detail = screens.note_detail(connection, note_id, visitor_of(session))
            context = {
                "user": user["id"],
                "csrf": session["csrf"],
                "note": detail,
                "reviewer": user["role"] == "lead",
                "extraction": demo_week["extraction"],
                "long_date": screens.week(demo_week["start"], [detail["day"]])[0]["long"],
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
                return Response("The form expired. Go back and try again.", status_code=403)
            detail = screens.note_detail(connection, note_id, visitor_of(session))
            route = detail["reasons"][0] if detail["reasons"] else "opened directly"
            now = time.time()
            for finding in detail["findings"]:
                label = fields.get(f"finding-{finding['id']}", "")
                if label not in screens.LABELS:
                    continue
                category = fields.get(f"category-{finding['id']}", "")
                if label != "wrong category" or category not in screens.CATEGORIES_SET:
                    category = ""
                connection.execute(
                    "INSERT INTO decisions (note, finding, label, category, reviewer, visitor, "
                    "route, decided) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (note_id, finding["id"], label, category or None, user["id"],
                     visitor_of(session), route, now),
                )  # fmt: skip
                log(connection, {"event": "decision", "user": user["id"], "note": note_id,
                                 "finding": finding["id"], "label": label})  # fmt: skip
            missed = fields.get("missed", "")
            if missed in screens.CATEGORIES_SET:
                connection.execute(
                    "INSERT INTO decisions (note, label, category, reviewer, visitor, route, "
                    "decided) VALUES (?, 'missed concern', ?, ?, ?, ?, ?)",
                    (note_id, missed, user["id"], visitor_of(session), route, now),
                )
                log(connection, {"event": "decision", "user": user["id"], "note": note_id,
                                 "label": "missed concern"})  # fmt: skip
            connection.commit()
            following = screens.next_note(connection, note_id, visitor_of(session))
            return to(f"/notes/{following}" if following else f"/queue?day={detail['day']}")
        finally:
            connection.close()

    @app.get("/today", response_class=HTMLResponse)
    def today(request: Request, day: str | None = None) -> Response:
        connection = db.connect(path)
        try:
            found = signed_in(request, connection, ("carer",))
            if isinstance(found, Response):
                return found
            session, user = found
            chosen = chosen_day(day)
            context = {
                "user": user["id"],
                "csrf": session["csrf"],
                "week": screens.week(demo_week["start"], demo_week["days"]),
                "day": chosen,
                "visits": screens.visit_sheet(connection, user["carer"], chosen),
                "link": "/today",
            }
            return page(request, "today.html", context)
        finally:
            connection.close()

    return app


def create_app_from_env() -> FastAPI:
    """The uvicorn factory: `uvicorn homecare.app.main:create_app_from_env --factory`."""
    return create_app(settings_from_env())
