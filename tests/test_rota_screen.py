"""The coordinator's rota and saved re-plan, checked against the seed and the rota
file rather than the screen's own data."""

import re
import secrets
import shutil
from pathlib import Path

import pytest

from homecare.app import main, seed
from test_screens import LAST_DAY, make, sign_in

ROTA = seed.FILE.with_name("rota.json")


def test_the_coordinator_starts_on_the_rota(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    sign_in(client, "coordinator")
    assert client.get("/", follow_redirects=False).headers["location"] == "/rota"


def test_the_day_shows_every_carer_working_and_every_visit_to_arrange_with_its_reason(
    tmp_path: Path,
) -> None:
    client, _ = make(tmp_path)
    sign_in(client, "coordinator")
    page = client.get(f"/rota?day={LAST_DAY}").text
    demo = seed.load()
    day = [visit for visit in demo["visits"] if visit["day"] == LAST_DAY]
    working = sorted({visit["carer"] for visit in day if visit["carer"]})
    unplanned = [visit for visit in day if visit["carer"] is None]
    team = re.findall(r'href="/rota\?day=27&amp;carer=([^"]+)"', page)
    assert team == working
    # One reason stated once, with every visit it covers listed under it.
    arrange = page.split('id="arrange-title"', 1)[1].split("</section>", 1)[0]
    assert arrange.count("Needs two carers: the planner does not pair carers yet") == 1
    assert arrange.count("<li>") == len(unplanned) == 8
    assert f'<span class="count">{len(unplanned)}</span>' in arrange
    assert f"{len(day) - len(unplanned)} visits planned" in page


def test_a_chosen_carer_s_day_is_their_route_in_order(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    sign_in(client, "coordinator")
    page = client.get(f"/rota?day={LAST_DAY}&carer=K08").text
    demo = seed.load()
    clients = {client["id"]: client["name"] for client in demo["clients"]}
    route = sorted(
        (v for v in demo["visits"] if v["day"] == LAST_DAY and v["carer"] == "K08"),
        key=lambda v: v["position"],
    )
    section = page.split('id="carer-title"', 1)[1].split("</section>", 1)[0]
    shown = re.findall(r"<strong>([^<]+)</strong>", section)
    assert shown == [clients[visit["client"]] for visit in route]
    assert "<svg viewBox=" in section and 'aria-current="page"' in page
    unknown = client.get(f"/rota?day={LAST_DAY}&carer=K99").text
    assert 'id="carer-title"' not in unknown


def test_the_re_plan_shows_every_change_and_who_gains_work(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    sign_in(client, "coordinator")
    page = client.get("/rota/replan").text
    saved = seed.load(ROTA, ROTA.with_name("rota.json.sha256"))["replan"]
    assert page.count("</strong> at <span") == sum(1 for c in saved["changes"] if c["after"])
    for change in saved["changes"]:
        assert f"<strong>{change['after']}</strong>" in page
    near = [row for row in saved["work"] if row["carer"] != saved["out"]]
    near = [row for row in near if row["limit"] - row["after"] <= 15]
    for row in near:
        left = row["limit"] - row["after"]
        assert f"Near the daily limit</span> {left} minutes under" in page
    for item in saved["retimed"]:
        assert f"{item['carer']} at <span" in page
    # Every minute the summary says moved is in a row the coordinator can read.
    listed = sum(abs(c["starts"] - c["was"]) for c in saved["changes"] if c["after"])
    listed += sum(abs(item["starts"] - item["was"]) for item in saved["retimed"])
    assert listed == saved["totals"]["minutes_moved"]["after"]
    driving = saved["totals"]["driving_minutes"]
    assert f"{driving['before']} → {driving['after']}" in page


def test_only_the_coordinator_opens_the_rota(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    for user in ("lead", "manager", "K08"):
        sign_in(client, user)
        assert client.get("/rota").status_code == 403
        assert client.get("/rota/replan").status_code == 403
    client.cookies.clear()
    assert client.get("/rota", follow_redirects=False).headers["location"] == "/signin"


def test_a_changed_rota_file_stops_the_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    changed = tmp_path / "rota.json"
    shutil.copy(ROTA, changed)
    shutil.copy(ROTA.with_name("rota.json.sha256"), tmp_path / "rota.json.sha256")
    changed.write_text(changed.read_text(encoding="utf-8").replace("K16", "K17"), encoding="utf-8")
    monkeypatch.setattr(main, "ROTA", changed)
    settings = {"mode": "demo", "database": tmp_path / "demo.db",
                "anchor": tmp_path / "demo.anchor", "key": secrets.token_bytes(32)}  # fmt: skip
    with pytest.raises(SystemExit, match="differs from its pin"):
        main.create_app(settings)
