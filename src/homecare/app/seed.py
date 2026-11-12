"""The SYNTHETIC demo week the platform shows, as one JSON file committed with the app.

`build` gathers it on the laptop from the world, the planned rota, the demo-week notes, the v2
extraction run and the PIGuard probabilities (`homecare platform seed`). The app loads it into
SQLite at start, after checking it against its pinned SHA-256. The labels the notes were written
from stay out, because a real platform never has them. Road data stays out too: it is derived
from OpenStreetMap, so it lives in its own file with the ODbL notice (`maps.py`); the seed
holds only which junction each client and carer's home is on, and the planned times.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from homecare.matching.check import timeline
from homecare.tamper import tagged

FILE = Path(__file__).parent / "data" / "demo.json"
PIN = FILE.with_name("demo.json.sha256")
FINDING_FIELDS = ("category", "band", "quote", "reason", "read")
# The demo's clock: 12:30 on the week's last day. On that day, visits that ended within the
# hour before it (or have not ended) have no note yet: carers often write up a little later. So a
# carer has visits done but not written up, and visits still to come.
NOW = 750
WRITE_UP_LAG = 60


def build(
    world: dict[str, Any],
    rotas: list[dict[str, Any]],
    days: list[int],
    notes: list[dict[str, Any]],
    run: list[dict[str, Any]],
    tamper: dict[str, Any],
    retention: dict[str, Any],
) -> dict[str, Any]:
    """The seed. `rotas` holds one planned day per entry of `days`; `run` is the extraction run
    file's records (header first); `tamper` is the PIGuard cache (`sha256`, `probabilities`);
    `retention` is the fitted model and each working carer's inputs for the manager's screen."""
    planned: dict[str, tuple[str, int]] = {}
    times: dict[str, dict[str, int]] = {}
    by_id = {visit["id"]: visit for visit in world["visits"]}
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    carers = {carer["id"]: carer for carer in world["carers"]}
    for rota in rotas:
        for carer_id, route in rota["routes"].items():
            for order, visit_id in enumerate(route):
                planned[visit_id] = (carer_id, order)
            if route:
                plan = timeline([by_id[v] for v in route], carers[carer_id], nodes, world)
                for stop in plan["stops"]:
                    times[stop["visit"]] = {"starts": stop["starts"], "drive": stop["drive"]}
    visits = []
    for visit in world["visits"]:
        if visit["day"] in days:
            carer: str | None = None
            position: int | None = None
            if visit["id"] in planned:
                carer, position = planned[visit["id"]]
            visits.append(
                {
                    "id": visit["id"],
                    "client": visit["client"],
                    "day": visit["day"],
                    "slot": visit["slot"],
                    "open": visit["open"],
                    "close": visit["close"],
                    "duration": visit["duration"],
                    "carer": carer,
                    "position": position,
                    **times.get(visit["id"], {"starts": None, "drive": None}),
                }
            )
    findings = {record["id"]: record["findings"] for record in run[1:]}
    probabilities = tamper["probabilities"]
    seed_notes = []
    for note in notes:
        visit = by_id[note["visit_id"]]
        ends = times[visit["id"]]["starts"] + visit["duration"]
        if note["day"] == days[-1] and ends > NOW - WRITE_UP_LAG:
            continue  # not written up yet by the demo's clock
        probability = probabilities[note["id"]]
        seed_notes.append(
            {
                "id": note["id"],
                "visit": note["visit_id"],
                "client": note["client_id"],
                "carer": note["carer_id"],
                "day": note["day"],
                "text": note["text"],
                "findings": [{k: f[k] for k in FINDING_FIELDS} for f in findings[note["id"]]],
                "tamper": tagged(probability),
                "tamper_probability": round(probability, 4),
            }
        )
    return {
        "synthetic": True,
        "about": "SYNTHETIC demo week: no real person, client, carer or provider.",
        "start": world["start"],
        "days": days,
        "now": {"day": days[-1], "minute": NOW},
        "extraction": run[0]["header"],
        "writer": {"model": notes[0]["writer"], "sha256": notes[0]["writer_sha256"]},
        "tamper": {"model": "piguard", "sha256": tamper["sha256"], "rule": "P(injection) > 0.5"},
        "clients": [
            {k: client[k] for k in ("id", "name", "details", "group", "needs", "node")}
            for client in world["clients"]
        ],
        "carers": [
            {k: carer[k] for k in ("id", "gender", "skills", "contract", "days_off", "home")}
            for carer in world["carers"]
        ],
        "visits": visits,
        "notes": seed_notes,
        "retention": retention,
    }


def dumps(seed: dict[str, Any]) -> str:
    """One stable text form, so the same inputs always give the same file and pin."""
    return json.dumps(seed, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load(path: Path = FILE, pin: Path = PIN) -> dict[str, Any]:
    """A committed data file (the seed by default), after checking it against its pin; a
    changed file stops the app."""
    text = path.read_text(encoding="utf-8")
    if sha256(text) != pin.read_text(encoding="utf-8").strip():
        raise SystemExit(f"{path.name} differs from its pin: the app will not start")
    seed: dict[str, Any] = json.loads(text)
    return seed
