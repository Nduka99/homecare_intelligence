"""The demo's user guide (`/guide`) stays true to the app it describes:
- every screen a person can open has a step in the guide, and every step names a real screen, so
  a new screen added without a guide step fails here;
- every word the guide puts in bold is shown by the app (its other templates or the carer app's
  script), so renaming a button without updating the guide fails here;
- the guide opens without signing in, and the sign-in page and the footer link to it.
The screens come from the app's own route table, never from the guide.
"""

import re
import secrets
from pathlib import Path

from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from homecare.app import main

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "homecare" / "app" / "templates"
CARER_SCRIPT = ROOT / "web" / "app" / "app.ts"


def make(tmp_path: Path) -> TestClient:
    settings = {
        "mode": "demo",
        "database": tmp_path / "demo.db",
        "anchor": tmp_path / "demo.anchor",
        "key": secrets.token_bytes(32),
    }
    return TestClient(main.create_app(settings), base_url="https://testserver")


def guide_main(client: TestClient) -> str:
    """The guide's own content, without the app bar and footer every page shares."""
    html = client.get("/guide").text
    return html.split("<main", 1)[1].split("</main>", 1)[0]


def screens(client: TestClient) -> set[str]:
    """Every page a person can open: the app's GET routes that answer with a page."""
    found = set()
    for route in client.app.routes:  # type: ignore[attr-defined]
        if isinstance(route, APIRoute) and "GET" in (route.methods or set()):
            if route.response_class is HTMLResponse:
                found.add(route.path)
    found.discard("/guide")  # the guide does not describe itself
    return found


def test_every_screen_has_a_guide_step_and_every_step_a_real_screen(tmp_path: Path) -> None:
    client = make(tmp_path)
    named = set(re.findall(r'data-screen="([^"]+)"', guide_main(client)))
    expected = screens(client)
    assert len(expected) >= 11  # the route table was read: 11 screens when the guide was written
    assert expected - named == set(), "screens with no step in the guide"
    assert named - expected == set(), "guide steps naming no screen"


def test_every_bold_word_in_the_guide_is_shown_by_the_app(tmp_path: Path) -> None:
    client = make(tmp_path)
    bold = re.findall(r"<strong>(.*?)</strong>", guide_main(client))
    assert len(bold) > 30
    shown = [CARER_SCRIPT.read_text(encoding="utf-8")]
    for template in sorted(TEMPLATES.glob("*.html")):
        if template.name != "guide.html":
            shown.append(template.read_text(encoding="utf-8"))
    # Templates may capitalise a word ("confirmed" shows as "Confirmed"), so case is ignored.
    everything = "\n".join(shown).lower()
    missing = [word for word in bold if word.lower() not in everything]
    assert missing == []


def test_the_guide_opens_signed_out_and_is_linked_from_sign_in_and_the_footer(
    tmp_path: Path,
) -> None:
    client = make(tmp_path)
    guide = client.get("/guide", follow_redirects=False)
    assert guide.status_code == 200
    assert "How to use this demo" in guide.text and "SYNTHETIC" in guide.text
    signin = client.get("/signin").text
    assert 'href="/guide"' in signin.split("<footer", 1)[0]  # in the page itself, not only below
    csrf = re.search(r'name="csrf" value="([^"]+)"', signin)
    assert csrf
    client.post("/signin", data={"csrf": csrf.group(1), "user": "lead"}, follow_redirects=False)
    queue = client.get("/queue").text
    assert 'href="/guide"' in queue.split("<footer", 1)[1]


def test_every_link_in_the_guide_lands_on_exactly_one_place(tmp_path: Path) -> None:
    """A jump link must find its target, and an id used twice breaks it, and any label that
    points at that id."""
    client = make(tmp_path)
    guide = client.get("/guide").text
    signin = client.get("/signin").text
    links = re.findall(r'href="(/signin)?#([^"]+)"', guide)
    assert len(links) >= 10
    for page, target in links:
        html = signin if page else guide
        assert html.count(f'id="{target}"') == 1, target
    # The other screens link into the guide too ("About the AI").
    into_guide = set()
    for template in TEMPLATES.glob("*.html"):
        into_guide |= set(
            re.findall(r'href="/guide#([^"]+)"', template.read_text(encoding="utf-8"))
        )
    assert into_guide
    for target in into_guide:
        assert guide.count(f'id="{target}"') == 1, target
