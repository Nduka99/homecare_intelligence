"""The coordinator's referral screens on the committed SYNTHETIC demo referrals.

Each attack test passes only when the attack fails, and each was checked by breaking its
protection on purpose and watching the test fail. The tests cover:
- only the coordinator, and only with a valid CSRF token;
- only known values are stored, and the confirmer comes from the session;
- carers are listed only from a saved profile;
- decisions are append-only, and markup in a referral is shown as text;
- demo visitors are kept apart, no referral text is logged, and the file is pinned.
"""

import json
import logging
import re
import secrets
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homecare.app import db, main, screens, seed
from homecare.referrals import fields

DEMO = json.loads(seed.REFERRALS.read_text(encoding="utf-8"))
FIRST = DEMO["referrals"][0]["id"]
CARERS = seed.load()["carers"]
# A profile a person might confirm: two needs, two visits, women only for personal care.
PROFILE = {
    "need: personal care": "yes",
    "need: medication": "yes",
    "visit: morning": "yes",
    "visit: bed": "yes",
    "gender": "female",
    "asked": "yes",  # options the referral leaves unstated were asked about
}


def make(tmp_path: Path) -> TestClient:
    settings = {
        "mode": "demo",
        "database": tmp_path / "demo.db",
        "anchor": tmp_path / "demo.anchor",
        "key": secrets.token_bytes(32),
    }
    return TestClient(main.create_app(settings), base_url="https://testserver")


def csrf_of(html: str) -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', html)
    assert found, "no CSRF field on the page"
    return found.group(1)


def sign_in(client: TestClient, user: str) -> None:
    form = {"csrf": csrf_of(client.get("/signin").text), "user": user}
    client.post("/signin", data=form, follow_redirects=False)


def confirm(client: TestClient, referral_id: str, form: dict[str, str]) -> Any:
    page = client.get(f"/referrals/{referral_id}").text
    data = {"csrf": csrf_of(page), **form}
    return client.post(f"/referrals/{referral_id}/decisions", data=data, follow_redirects=False)


def stored(tmp_path: Path) -> list[sqlite3.Row]:
    connection = db.connect(tmp_path / "demo.db")
    return connection.execute("SELECT * FROM referral_decisions ORDER BY id").fetchall()


def test_only_the_coordinator_reaches_the_referrals(tmp_path: Path) -> None:
    client = make(tmp_path)
    for user in ("lead", "manager", "K01"):
        sign_in(client, user)
        assert client.get("/referrals").status_code == 403
        assert client.get(f"/referrals/{FIRST}").status_code == 403
        token = csrf_of(client.get("/").text)  # the user's own valid token, from their home page
        data = {"csrf": token, **PROFILE}
        save = client.post(f"/referrals/{FIRST}/decisions", data=data, follow_redirects=False)
        assert save.status_code == 403
        client.cookies.clear()
    assert client.get("/referrals", follow_redirects=False).headers["location"] == "/signin"
    save = client.post(f"/referrals/{FIRST}/decisions", data=PROFILE, follow_redirects=False)
    assert save.headers["location"] == "/signin"
    assert stored(tmp_path) == []


def test_a_save_with_a_missing_or_wrong_csrf_token_is_refused(tmp_path: Path) -> None:
    client = make(tmp_path)
    sign_in(client, "coordinator")
    for token in ("", "not-the-token"):
        data = {"csrf": token, **PROFILE}
        save = client.post(f"/referrals/{FIRST}/decisions", data=data, follow_redirects=False)
        assert save.status_code == 403
    assert stored(tmp_path) == []


def test_only_known_values_are_stored_and_a_profile_needs_a_visit(tmp_path: Path) -> None:
    client = make(tmp_path)
    sign_in(client, "coordinator")
    assert confirm(client, FIRST, PROFILE | {"gender": "other"}).status_code == 400
    assert confirm(client, FIRST, PROFILE | {"need: medication": "1"}).status_code == 400
    no_visit = confirm(client, FIRST, {"need: personal care": "yes", "gender": "none"})
    assert no_visit.status_code == 400
    assert "choose at least one visit" in no_visit.text
    # The refused form comes back as it was posted: the ticked need stays ticked.
    assert 'name="need: personal care" value="yes" checked' in no_visit.text
    assert 'name="need: medication" value="yes" checked' not in no_visit.text
    assert stored(tmp_path) == []
    # An invented claim name is ignored; the rest is stored (expected profile written by hand).
    form = {"visit: morning": "yes", "gender": "none", "need: cat sitting": "yes", "asked": "yes"}
    assert confirm(client, FIRST, form).status_code == 303
    expected = {"needs": [], "slots": ["morning"], "gender_preference": None,
                "time_critical": False, "insulin": False, "hoist": False}  # fmt: skip
    assert json.loads(stored(tmp_path)[0]["profile"]) == expected


def test_an_unstated_option_is_saved_as_not_needed_only_once_the_referrer_was_asked(
    tmp_path: Path,
) -> None:
    """The first referral never mentions dementia, learning disability, end of life or a hoist
    (read by hand from the committed file), so the list asks about four, and a profile leaving
    them unticked is refused until the person says they asked the referrer."""
    client = make(tmp_path)
    sign_in(client, "coordinator")
    assert "9 claims read · 4 to ask the referrer" in client.get("/referrals").text
    unasked = {name: value for name, value in PROFILE.items() if name != "asked"}
    refused = confirm(client, FIRST, unasked)
    assert refused.status_code == 400
    assert "I have asked the referrer" in refused.text and "Not saved" in refused.text
    assert stored(tmp_path) == []
    assert confirm(client, FIRST, PROFILE).status_code == 303


def test_the_confirmer_is_the_signed_in_coordinator_never_the_form(tmp_path: Path) -> None:
    client = make(tmp_path)
    sign_in(client, "coordinator")
    form = PROFILE | {"decided_by": "lead", "confirmed_by": "lead"}
    assert confirm(client, FIRST, form).status_code == 303
    assert [row["decided_by"] for row in stored(tmp_path)] == ["coordinator"]


def allowed(skills: list[str], gender: str | None) -> list[str]:
    """The demo carers who may do a visit, worked out here without the planner's code: every
    skill, and the gender when one is required."""
    found = []
    for carer in CARERS:
        if set(skills) <= set(carer["skills"]) and gender in (None, carer["gender"]):
            found.append(carer["id"])
    return found


def test_carers_are_listed_only_from_a_saved_profile(tmp_path: Path) -> None:
    """Before a save, no carers; after a save that differs from the model's read, exactly the
    carers the saved profile allows."""
    client = make(tmp_path)
    sign_in(client, "coordinator")
    read = [claim["claim"] for claim in DEMO["referrals"][0]["claims"]]
    confirmed = ["need: personal care", "need: medication", "visit: morning", "visit: bed",
                 "carer gender: female"]  # fmt: skip
    assert fields.profile(read) != fields.profile(confirmed)  # the save changes the read
    before = client.get(f"/referrals/{FIRST}").text
    assert "Confirm the profile to see who may go" in before
    assert "carers may go" not in before
    assert confirm(client, FIRST, PROFILE).status_code == 303
    after = client.get(f"/referrals/{FIRST}").text
    # Morning and bed are personal care visits, so the women-only rule applies to both.
    expected = allowed(["personal care", "medication"], "female")
    assert expected
    assert after.count(f"{len(expected)} of {len(CARERS)} carers may go") == 2
    assert after.count(", ".join(expected)) == 2
    assert after.count("Women only: personal care is given at this visit") == 2
    assert "Matching below uses this profile" in after


def test_saved_profiles_and_referrals_cannot_be_edited_or_deleted(tmp_path: Path) -> None:
    client = make(tmp_path)
    sign_in(client, "coordinator")
    confirm(client, FIRST, PROFILE)
    connection = db.connect(tmp_path / "demo.db")
    for statement in (
        "UPDATE referral_decisions SET decided_by = 'lead'",
        "DELETE FROM referral_decisions",
        "UPDATE referrals SET body = 'changed'",
        "DELETE FROM referrals",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement)


def plant(tmp_path: Path, body: str, claims: list[dict[str, str]]) -> None:
    connection = db.connect(tmp_path / "demo.db")
    connection.execute(
        "INSERT INTO referrals VALUES ('planted', 'Planted Referral', ?, ?, '[]')",
        (body, json.dumps(claims)),
    )
    connection.commit()


def test_markup_in_a_referral_is_shown_as_text(tmp_path: Path) -> None:
    client = make(tmp_path)
    body = 'Calls each morning. <script>alert("x")</script><mark>forged quote</mark>'
    plant(tmp_path, body, [{"claim": "visit: morning", "quote": "Calls each morning"}])
    sign_in(client, "coordinator")
    page = client.get("/referrals/planted").text
    assert "<script>" not in page and "<mark>forged quote</mark>" not in page
    assert "&lt;script&gt;" in page and "&lt;mark&gt;forged quote" in page


def test_a_shared_quote_has_one_number_shown_once_in_reading_order(tmp_path: Path) -> None:
    make(tmp_path)
    body = "Calls each morning and at night. Insulin is given by the carers."
    # The model listed insulin first; the numbers still follow the text.
    claims = [
        {"claim": "insulin by carers", "quote": "Insulin is given by the carers"},
        {"claim": "visit: morning", "quote": "Calls each morning and at night"},
        {"claim": "visit: bed", "quote": "Calls each morning and at night"},
    ]
    plant(tmp_path, body, claims)
    connection = db.connect(tmp_path / "demo.db")
    detail = screens.referral_detail(connection, "planted", None, CARERS)
    assert detail is not None
    numbers = {c["name"]: c["quote"] for c in detail["visits"] + detail["flags"]}
    assert (numbers["visit: morning"], numbers["visit: bed"]) == (1, 1)
    assert numbers["insulin by carers"] == 2
    assert numbers["visit: lunch"] is None
    shown = [number for run in detail["segments"] for number in run["ends"]]
    assert shown == [1, 2]


def test_demo_visitors_never_see_each_others_confirmations(tmp_path: Path) -> None:
    first = make(tmp_path)
    second = TestClient(first.app, base_url="https://testserver")
    sign_in(first, "coordinator")
    sign_in(second, "coordinator")
    assert confirm(first, FIRST, PROFILE).status_code == 303
    assert "carers may go" in first.get(f"/referrals/{FIRST}").text
    other = second.get(f"/referrals/{FIRST}").text
    assert "carers may go" not in other and "a draft to confirm" in other


def test_viewing_and_confirming_logs_no_referral_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client = make(tmp_path)
    sign_in(client, "coordinator")
    client.get("/referrals")
    assert confirm(client, FIRST, PROFILE).status_code == 303
    connection = db.connect(tmp_path / "demo.db")
    audit = " ".join(row["entry"] for row in connection.execute("SELECT entry FROM audit"))
    assert "confirm referral" in audit and FIRST in audit
    words = DEMO["referrals"][0]["text"].split()
    spans = [" ".join(words[i : i + 5]) for i in range(len(words) - 4)]
    assert spans
    for span in spans:
        assert span not in audit
        assert span not in caplog.text


def test_a_changed_referrals_file_stops_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed = tmp_path / "referrals.json"
    shutil.copy(seed.REFERRALS, changed)
    shutil.copy(
        seed.REFERRALS.with_name("referrals.json.sha256"), tmp_path / "referrals.json.sha256"
    )
    text = changed.read_text(encoding="utf-8")
    changed.write_text(text.replace("visit: morning", "visit: lunch", 1), encoding="utf-8")
    monkeypatch.setattr(main, "REFERRALS", changed)
    with pytest.raises(SystemExit, match="differs from its pin"):
        make(tmp_path)


def test_the_demo_file_holds_no_answer_key() -> None:
    """A real platform never has the profile a referral was written from."""
    assert set(DEMO) == {"synthetic", "about", "writer", "reader", "referrals"}
    assert DEMO["synthetic"] is True
    for referral in DEMO["referrals"]:
        assert set(referral) == {"id", "name", "text", "claims", "dropped"}
