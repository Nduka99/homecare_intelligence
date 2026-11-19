"""No input makes the app fail with a server error. For each generated input, every route in the
app's own route table is called with it, as a signed-out visitor or as one of the four roles: odd
text in the address and the query, form fields with odd names and values (sometimes with the
session's real form token, so the input reaches past the token check), raw bytes as a form body,
and an odd visitor cookie. Every answer must be a page or a redirect, never a 500.

The inputs come from Hypothesis's general strategies for text and bytes, not from a list of
inputs known to break the app. Run once on the app before its input checks were fixed, the fuzzer
found the crashes those checks now prevent."""

import re
from typing import Any
from urllib.parse import quote, urlencode

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from homecare.app import auth, main
from test_screens import make, sign_in, visit_to_write

ROLES = ["visitor", "carer", "lead", "manager", "coordinator"]
PARAMETERS = ["day", "choose", "sent", "carer"]
FIELDS = ["csrf", "user", "password", "body", "key", "amends", "missed", "gender", "asked",
          "day", "reason", "back", "client"]  # fmt: skip
TEXT = st.text(max_size=40)
# A cookie travels in a header, whose bytes are read as Latin-1; ";" and "," end a cookie.
COOKIE = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0xFF, exclude_characters=";,\x7f"),
    max_size=60,
)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One demo app, a client for each role with its form token, and every route."""
    folder = tmp_path_factory.mktemp("fuzz")
    first, _ = make(folder)
    app = first.app
    carer = visit_to_write(folder)["carer"]
    clients = {}
    for role in ROLES:
        client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
        if role == "visitor":
            page = client.get("/signin").text
        else:
            sign_in(client, carer if role == "carer" else role)
            page = client.get("/").text
        token = re.findall(r'name="csrf" value="([^"]+)"', page)[0]
        clients[role] = (client, token)
    routes = []
    for route in app.routes:  # type: ignore[attr-defined]
        if isinstance(route, APIRoute):
            routes.append((route.path, sorted(route.methods or set())))
    return {"clients": clients, "routes": routes}


@given(data=st.data())
def test_no_input_causes_a_server_error(world: dict[str, Any], data: st.DataObject) -> None:
    role = data.draw(st.sampled_from(ROLES), label="role")
    client, token = world["clients"][role]
    part = data.draw(TEXT, label="address part")
    query = data.draw(st.dictionaries(st.sampled_from(PARAMETERS) | TEXT, TEXT, max_size=4))
    form = data.draw(st.dictionaries(st.sampled_from(FIELDS) | TEXT, TEXT, max_size=6))
    if data.draw(st.booleans(), label="real form token"):
        form["csrf"] = token
    raw = data.draw(st.none() | st.binary(max_size=300), label="raw body")
    visitor = data.draw(COOKIE, label="visitor cookie")
    session = client.cookies.get(auth.COOKIE) or ""
    cookie = f"{auth.COOKIE}={session}; {main.VISITOR}=".encode() + visitor.encode("latin-1")
    headers = {"cookie": cookie, "content-type": "application/x-www-form-urlencoded"}
    for path, methods in world["routes"]:
        url = re.sub(r"\{[^}]+\}", quote(part, safe="") or "x", path)
        for method in methods:
            if method == "GET":
                answer = client.get(url, params=query, headers=headers, follow_redirects=False)
            elif path == "/signout":
                # Signing out with the real token would end the role's session for later inputs.
                wrong = urlencode({"csrf": "not the token"})
                answer = client.post(url, content=wrong, headers=headers, follow_redirects=False)
            else:
                body = raw if raw is not None else urlencode(form).encode()
                answer = client.post(url, content=body, headers=headers, follow_redirects=False)
            assert answer.status_code < 500, f"{method} {url} as {role}: {answer.status_code}"
            if answer.status_code >= 400:
                assert answer.headers["content-type"].startswith("text/html"), f"{method} {url}"
