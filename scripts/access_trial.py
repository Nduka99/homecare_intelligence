"""Who can open which client's record, and when, measured on the running app.

A client's record here is any page that shows the client by name with their visits, needs,
details or notes. The script measures three things and writes them to a JSON file:

1. Exposure. For each role, the records each person can open on the demo week's last day, in
   record-minutes (one record open to one person for one minute). Each access rule is stated as
   a formula over the week's data, in a few lines: the app as first built, the carers' rule now
   in the app (their own visits' records for the clock's day, and the note form for the day
   before), and a control. Separately, the app's own pages are asked for as each
   person at seeded random points (a person, a client, a minute). Exactly one formula must agree
   with the app at every point, and every other must disagree somewhere, or the comparison could
   not have told them apart.
2. Audit coverage. For every signed-in page in the app's route table, whether an allowed read
   and each kind of refusal add an entry to the audit log.
3. A working day, replayed. Every access the agency's people need in a day, at the time they
   need it: each carer's day sheet when they leave for the first visit, each visit on arrival,
   its note within the hour after the visit ends, an addition at the end of their day, and one
   note sent after midnight and added to the next morning; the safeguarding lead opening and
   deciding each note after it arrives, and for an urgent one the client's earlier notes; the
   coordinator's and the manager's pages. On the demo week each access goes through the real
   routes with the app's clock set to its time, and any refusal is listed. The carers' day is
   also built from a second agency's week (world seed 2027), which the app never loads. Each
   formula is checked against both days: a day sheet counts as opening every client on it.

Run: `uv run python scripts/access_trial.py <out.json>`. The second agency's week is rebuilt
with `homecare world build --seed 2027` and `homecare rota sweep --seed 2027`; files that differ
from the ones first measured are refused.
"""

import hashlib
import json
import random
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from homecare.app import main, seed
from homecare.matching.check import timeline
from homecare.notes.categories import ACT_THIS_VISIT, ACT_TODAY

ROOT = Path(__file__).resolve().parent.parent
FRESH_AGENCY = ROOT / "data" / "world" / "agency-seed-2027.json"
FRESH_ROTA = ROOT / "data" / "rota" / "sweep-seed-2027-days-0-6.json"
FRESH_SHA256 = {
    FRESH_AGENCY: "fd2165003790c5fea2d7dd2d39a557b34ee0046516a2a3c147b0253a97e801c3",
    FRESH_ROTA: "6a6c330dc8a213e848ca90209d2871590d23f8ed776abc1602bab8c2392c393b",
}
FRESH_DAYS = list(range(7))
ROTA_SETTING = "high need x2"  # the continuity weighting the demo week's own rota was planned with
MINUTES = 24 * 60
SAMPLE_SEED = "access-trial"
POINTS = {"carer": 60, "lead": 20, "coordinator": 20, "manager": 20}
# More carer points, each on a client of the carer's own week: a random client is rarely one,
# and only there do rules that differ by day disagree.
OWN_POINTS = 60
OFFICE = ("lead", "coordinator", "manager")
# When people act. The demo's notes are written up within the hour after a visit ends, so a
# note is sent half way through that hour; the lead opens it half an hour after it arrives.
SEND_AFTER = 30
REVIEW_AFTER = 30
AFTER_MIDNIGHT = 30  # 00:30
LEAD_STARTS = 8 * 60
# The coordinator's and manager's pages, and when each is opened. The saved re-plan is for a
# carer who drops out at 10:00, so it is read just after.
OFFICE_PAGES = [
    ("coordinator", "open rota", 7 * 60, "/rota?day={day}"),
    ("coordinator", "open imports", 9 * 60, "/imports"),
    ("manager", "open team", 9 * 60, "/team"),
    ("coordinator", "open re-plan", 10 * 60 + 5, "/rota/replan"),
    ("coordinator", "open referrals", 14 * 60, "/referrals"),
]
# Pages open without signing in, or that only redirect.
PUBLIC = {"/", "/signin", "/guide", "/healthz", "/sw.js", "/map-data"}

Rule = Callable[[dict[str, Any], str, str, int, int], set[str]]


# The weeks, as plain data.


def demo_week() -> dict[str, Any]:
    data = seed.load()
    notes = []
    for note in data["notes"]:
        bands = {finding["band"] for finding in note["findings"]}
        notes.append({
            "id": note["id"], "client": note["client"], "visit": note["visit"],
            "day": note["day"], "urgent": bool(bands & {ACT_TODAY, ACT_THIS_VISIT}),
        })  # fmt: skip
    carers = [carer["id"] for carer in data["carers"]]
    week = indexed(data["days"], carers, data["visits"], notes)
    week["names"] = {client["id"]: client["name"] for client in data["clients"]}
    return week


def fresh_week() -> dict[str, Any]:
    """The second agency's week, with each planned visit's arrival and drive worked out the
    same way as the demo's (`timeline`)."""
    for path, pinned in FRESH_SHA256.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != pinned:
            raise SystemExit(f"{path.name} differs from the file this measurement was fixed on")
    world = json.loads(FRESH_AGENCY.read_text(encoding="utf-8"))
    rotas = json.loads(FRESH_ROTA.read_text(encoding="utf-8"))["rotas"]
    by_id = {visit["id"]: visit for visit in world["visits"]}
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    carers = {carer["id"]: carer for carer in world["carers"]}
    visits = []
    for day in FRESH_DAYS:
        for carer, route in rotas[f"{ROTA_SETTING} day {day}"]["routes"].items():
            if not route:
                continue
            plan = timeline([by_id[v] for v in route], carers[carer], nodes, world)
            for position, stop in enumerate(plan["stops"]):
                visit = by_id[stop["visit"]]
                visits.append({
                    "id": visit["id"], "client": visit["client"], "day": day,
                    "slot": visit["slot"], "open": visit["open"], "close": visit["close"],
                    "duration": visit["duration"], "carer": carer, "position": position,
                    "starts": stop["starts"], "drive": stop["drive"],
                })  # fmt: skip
    return indexed(FRESH_DAYS, sorted(carers), visits, [])


def indexed(
    days: list[int],
    carers: list[str],
    visits: list[dict[str, Any]],
    notes: list[dict[str, Any]],
) -> dict[str, Any]:
    """A week with the lookups the formula and the replay need."""
    clients_of: dict[str, set[str]] = {carer: set() for carer in carers}
    clients_on: dict[tuple[str, int], set[str]] = {}
    visits_on: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for visit in visits:
        if visit["carer"] is not None:
            clients_of[visit["carer"]].add(visit["client"])
            clients_on.setdefault((visit["carer"], visit["day"]), set()).add(visit["client"])
            visits_on.setdefault((visit["carer"], visit["day"]), []).append(visit)
    return {
        "days": days,
        "carers": carers,
        "visits": visits,
        "notes": notes,
        "clients_of": clients_of,
        "clients_on": clients_on,
        "visits_on": visits_on,
        "noted": {note["client"] for note in notes},
        "visited": {visit["client"] for visit in visits},
    }


# The formula: the app's rules over the week's data. It never asks the app.


def as_built(week: dict[str, Any], role: str, person: str, day: int, minute: int) -> set[str]:
    """The clients whose record `person` can open at this time, as the app is built: a carer,
    every client of their own visits on any day of the week, at any time; the lead, every client
    with a note; the coordinator, every client with a visit (the rota shows each carer's route
    and every uncovered visit); the manager, none (the team page lists carers only)."""
    if role == "carer":
        return set(week["clients_of"][person])
    if role == "lead":
        return set(week["noted"])
    if role == "coordinator":
        return set(week["visited"])
    return set()


def current_day_only(
    week: dict[str, Any], role: str, person: str, day: int, minute: int
) -> set[str]:
    """The control: the rules as built with one changed, carers seeing only the current day's
    clients."""
    if role == "carer":
        return set(week["clients_on"].get((person, day), set()))
    return as_built(week, role, person, day, minute)


def todays_records(week: dict[str, Any], role: str, person: str, day: int, minute: int) -> set[str]:
    """The carers' rule in the app: a carer opens their own visits' records for the clock's day,
    and the note form, which names the client, for the day before too."""
    if role == "carer":
        today = week["clients_on"].get((person, day), set())
        return set(today | week["clients_on"].get((person, day - 1), set()))
    return as_built(week, role, person, day, minute)


def formulas() -> dict[str, Rule]:
    found: dict[str, Rule] = {
        "as built": as_built,
        "today's records": todays_records,
        "control: carers today only": current_day_only,
    }
    return found


def people(week: dict[str, Any], office: bool) -> dict[str, list[str]]:
    found = {"carer": week["carers"]}
    if office:
        found |= {role: [role] for role in OFFICE}
    return found


def exposure(
    week: dict[str, Any], rule: Rule, everyone: dict[str, list[str]], day: int
) -> dict[str, Any]:
    """Each role's record-minutes on `day`, and how many records its people can open."""
    found = {}
    for role, names in everyone.items():
        record_minutes = 0
        records = []
        for person in names:
            seen: set[str] = set()
            for minute in range(MINUTES):
                open_now = rule(week, role, person, day, minute)
                record_minutes += len(open_now)
                seen |= open_now
            records.append(len(seen))
        found[role] = {
            "people": len(names),
            "record_minutes": record_minutes,
            "records_per_person": {
                "mean": round(sum(records) / len(records), 2),
                "min": min(records),
                "max": max(records),
            },
        }
    return found


# The app, asked as each person.


def new_app(folder: Path) -> tuple[FastAPI, dict[str, Any]]:
    settings = {
        "mode": "demo",
        "database": folder / "trial.db",
        "anchor": folder / "trial.anchor",
        "key": secrets.token_bytes(32),
    }
    return main.create_app(settings), settings


def signed_in(app: FastAPI, person: str, visitor: str, number: int) -> TestClient:
    """A browser signed in as `person`. Every browser shares one visitor cookie, so in demo mode
    they all work on one agency's record, as its staff would; each has its own address, as each
    person's phone or desk does."""
    address = f"10.0.{number // 200}.{number % 200 + 1}"
    client = TestClient(app, base_url="https://testserver", headers={"cf-connecting-ip": address})
    client.cookies.set(main.VISITOR, visitor, domain="testserver.local")
    csrf = form_value(client.get("/signin").text, "csrf")
    answer = client.post("/signin", data={"csrf": csrf, "user": person}, follow_redirects=False)
    if answer.status_code != 303:
        raise SystemExit(f"could not sign in as {person}")
    return client


def browsers(app: FastAPI, everyone: dict[str, list[str]]) -> dict[str, TestClient]:
    visitor = secrets.token_urlsafe(32)
    names = [person for group in everyone.values() for person in group]
    return {person: signed_in(app, person, visitor, n) for n, person in enumerate(names)}


def form_value(page: str, name: str) -> str:
    found = re.search(rf'name="{name}" value="([^"]*)"', page)
    return found.group(1) if found else ""


def candidate_pages(week: dict[str, Any], client: str) -> list[str]:
    """Every page that could show this client's record, whoever asks; which of them a person
    may open is left to the app."""
    pages = [f"/notes/{note['id']}" for note in week["notes"] if note["client"] == client]
    routes = set()
    for visit in week["visits"]:
        if visit["client"] == client:
            pages.append(f"/visits/{visit['id']}/note")
            if visit["carer"] is not None:
                routes.add((visit["day"], visit["carer"]))
    for day in week["days"]:
        pages += [f"/today?day={day}", f"/queue?day={day}", f"/rota?day={day}"]
    pages += [f"/rota?day={day}&carer={carer}" for day, carer in sorted(routes)]
    return pages + ["/rota/replan", "/imports", "/team", "/referrals"]


def app_shows(browser: TestClient, pages: list[str], name: str) -> str | None:
    """The first page that shows this name to this browser, or None. A refusal that named the
    client would count too."""
    for page in pages:
        if name in browser.get(page, follow_redirects=False).text:
            return page
    return None


def probe(week: dict[str, Any], day: int, folder: Path) -> list[dict[str, Any]]:
    """Seeded random points, each asked of the app with its clock set to that minute. A
    record is found on a page by its client's name, so only the demo week is probed."""
    app, settings = new_app(folder)
    everyone = people(week, office=True)
    open_browsers = browsers(app, everyone)
    draw = random.Random(SAMPLE_SEED)
    clients = sorted(week["names"])
    points = []

    def ask(role: str, person: str, client: str, minute: int) -> None:
        settings["clock"] = {"day": day, "minute": minute}
        pages = candidate_pages(week, client)
        shown = app_shows(open_browsers[person], pages, week["names"][client])
        points.append({"role": role, "person": person, "client": client, "minute": minute,
                       "app_opens": shown is not None, "page": shown})  # fmt: skip

    for role, count in POINTS.items():
        for _ in range(count):
            person = draw.choice(everyone[role])
            client = draw.choice(clients)
            ask(role, person, client, draw.randrange(MINUTES))
    working = [carer for carer in week["carers"] if week["clients_of"][carer]]
    for _ in range(OWN_POINTS):
        carer = draw.choice(working)
        client = draw.choice(sorted(week["clients_of"][carer]))
        ask("carer", carer, client, draw.randrange(MINUTES))
    return points


def disagreements(
    week: dict[str, Any], rule: Rule, day: int, points: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    found = []
    for point in points:
        formula = point["client"] in rule(
            week, point["role"], point["person"], day, point["minute"]
        )
        if formula != point["app_opens"]:
            found.append(point | {"formula_opens": formula})
    return found


# Audit coverage.


def audit_requests(week: dict[str, Any], day: int) -> dict[str, list[tuple[str, str, str]]]:
    """For each signed-in page: who asks for what, and whether it is a read or which refusal."""
    carer_of = {visit["id"]: visit["carer"] for visit in week["visits"]}
    note = next(n for n in week["notes"] if n["day"] == day)
    own = note["visit"]
    carer = carer_of[own]
    others_note = next(n for n in week["notes"] if carer_of[n["visit"]] != carer)
    other = others_note["visit"]
    return {
        "/queue": [("lead", f"/queue?day={day}", "read"), (carer, "/queue", "wrong role")],
        "/notes/{note_id}": [
            ("lead", f"/notes/{note['id']}", "read"),
            (carer, f"/notes/{others_note['id']}", "not theirs"),
            ("lead", "/notes/no-such-note", "no such record"),
            ("coordinator", f"/notes/{note['id']}", "wrong role"),
        ],
        "/team": [("manager", "/team", "read"), ("lead", "/team", "wrong role")],
        "/today": [(carer, f"/today?day={day}", "read"), ("lead", "/today", "wrong role")],
        "/visits/{visit_id}/note": [
            (carer, f"/visits/{own}/note", "read"),
            (carer, f"/visits/{other}/note", "not theirs"),
            ("lead", f"/visits/{own}/note", "wrong role"),
        ],
        "/rota": [("coordinator", f"/rota?day={day}", "read"), (carer, "/rota", "wrong role")],
        "/rota/replan": [
            ("coordinator", "/rota/replan", "read"),
            ("lead", "/rota/replan", "wrong role"),
        ],
        "/imports": [("coordinator", "/imports", "read"), (carer, "/imports", "wrong role")],
        "/exports": [("lead", "/exports", "read"), (carer, "/exports", "wrong role")],
        "/referrals": [("coordinator", "/referrals", "read"), ("lead", "/referrals", "wrong role")],
        "/referrals/{referral_id}": [
            ("coordinator", "/referrals/referral-1", "read"),
            ("coordinator", "/referrals/no-such-referral", "no such record"),
            ("lead", "/referrals/referral-1", "wrong role"),
        ],
    }


def audit_rows(settings: dict[str, Any]) -> list[str]:
    connection = sqlite3.connect(settings["database"])
    rows = connection.execute("SELECT entry FROM audit ORDER BY position").fetchall()
    connection.close()
    return [row[0] for row in rows]


def audit_coverage(week: dict[str, Any], day: int, folder: Path) -> dict[str, Any]:
    app, settings = new_app(folder)
    signed = sorted(
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and "GET" in (route.methods or set())
        and route.path not in PUBLIC
    )  # fmt: skip
    asked = audit_requests(week, day)
    if signed != sorted(asked):
        raise SystemExit(f"pages not covered or gone: {sorted(set(signed) ^ set(asked))}")
    everyone = {"all": sorted({who for found in asked.values() for who, _, _ in found})}
    open_browsers = browsers(app, everyone)
    results = []
    for path, requests in asked.items():
        for who, url, kind in requests:
            before = audit_rows(settings)
            answer = open_browsers[who].get(url, follow_redirects=False)
            added = audit_rows(settings)[len(before) :]
            events = [json.loads(entry)["event"] for entry in added]
            results.append({"route": path, "who": who, "url": url, "kind": kind,
                            "status": answer.status_code, "logged": events})  # fmt: skip
    reads = [r for r in results if r["kind"] == "read"]
    refusals = [r for r in results if r["kind"] != "read"]
    return {
        "reads_logged": f"{sum(1 for r in reads if r['logged'])} of {len(reads)}",
        "refusals_logged": f"{sum(1 for r in refusals if r['logged'])} of {len(refusals)}",
        "requests": results,
    }


# The working day.


def at(day: int, minute: int) -> tuple[int, int]:
    """A day and minute, with minutes past midnight carried into the next day."""
    return day + minute // MINUTES, minute % MINUTES


def access(
    person: str, role: str, action: str, when: tuple[int, int], **record: Any
) -> dict[str, Any]:
    return {"person": person, "role": role, "action": action, "day": when[0],
            "minute": when[1]} | record  # fmt: skip


def carers_day(week: dict[str, Any], today: int) -> list[dict[str, Any]]:
    """Each carer's accesses on `today`, and one note from yesterday sent after midnight."""
    found = []
    sheet_at = {}
    for carer in week["carers"]:
        route = sorted(
            (v for v in week["visits"] if v["carer"] == carer and v["day"] == today),
            key=lambda visit: visit["position"],
        )
        if not route:
            continue
        sheet_at[carer] = route[0]["starts"] - route[0]["drive"]
        found.append(access(carer, "carer", "open day sheet", (today, sheet_at[carer]),
                            client=None, clients=sorted({v["client"] for v in route}),
                            visit_day=today))  # fmt: skip
        for visit in route:
            ends = visit["starts"] + visit["duration"]
            record = {"visit": visit["id"], "client": visit["client"], "visit_day": today}
            found.append(access(carer, "carer", "open visit", (today, visit["starts"]), **record))
            found.append(access(carer, "carer", "send note", at(today, ends + SEND_AFTER),
                                **record))  # fmt: skip
        last_ends = route[-1]["starts"] + route[-1]["duration"]
        first = {"visit": route[0]["id"], "client": route[0]["client"], "visit_day": today}
        found.append(access(carer, "carer", "add to note", at(today, last_ends + SEND_AFTER),
                            **first))  # fmt: skip
    # The latest bedtime visit yesterday whose carer works today is written up after midnight,
    # and added to when the carer sets off the next morning.
    late = max(
        (v for v in week["visits"] if v["day"] == today - 1 and v["slot"] == "bed"
         and v["carer"] in sheet_at),
        key=lambda visit: (visit["starts"] + visit["duration"], visit["id"]),
    )  # fmt: skip
    record = {"visit": late["id"], "client": late["client"], "visit_day": today - 1}
    carer = late["carer"]
    found.append(access(carer, "carer", "open visit", (today - 1, late["starts"]), **record))
    found.append(access(carer, "carer", "send note", (today, AFTER_MIDNIGHT), **record))
    found.append(access(carer, "carer", "add to note", (today, sheet_at[carer]), **record))
    return found


def office_day(week: dict[str, Any], today: int) -> list[dict[str, Any]]:
    """The lead reviews each of today's notes after it arrives, and for an urgent one opens the
    client's earlier notes too; the coordinator and manager open their pages."""
    found = [access("lead", "lead", "open register", (today, LEAD_STARTS), client=None)]
    urgent = {note["visit"] for note in week["notes"] if note["urgent"]}
    for visit in week["visits"]:
        if visit["day"] != today or visit["carer"] is None:
            continue
        when = at(today, visit["starts"] + visit["duration"] + SEND_AFTER + REVIEW_AFTER)
        found.append(access("lead", "lead", "review note", when, visit=visit["id"],
                            client=visit["client"], visit_day=today))  # fmt: skip
        if visit["id"] in urgent:
            for note in week["notes"]:
                if note["client"] == visit["client"] and note["day"] < today:
                    found.append(access("lead", "lead", "open earlier note", when,
                                        note=note["id"], client=note["client"],
                                        visit_day=note["day"]))  # fmt: skip
    for role, action, minute, url in OFFICE_PAGES:
        found.append(access(role, role, action, (today, minute), client=None,
                            url=url.format(day=today)))  # fmt: skip
    return found


def by_time(accesses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(accesses, key=lambda item: (item["day"], item["minute"]))


def first_note(settings: dict[str, Any], visit: str) -> str | None:
    """The visit's first note: the demo's own, or the one the replay sent; None if neither."""
    connection = sqlite3.connect(settings["database"])
    row = connection.execute(
        "SELECT id FROM notes WHERE visit = ? AND amends IS NULL ORDER BY written IS NOT NULL",
        (visit,),
    ).fetchone()
    connection.close()
    return str(row[0]) if row else None


def sent(answer: Any) -> bool:
    return bool(answer.status_code == 303 and not answer.headers["location"].startswith("/signin"))


def send(browser: TestClient, visit: str, form: dict[str, str], words: str) -> bool:
    fields = form | {"body": words}
    return sent(browser.post(f"/visits/{visit}/notes", data=fields, follow_redirects=False))


def note_form(browser: TestClient, visit: str) -> tuple[bool, dict[str, str]]:
    answer = browser.get(f"/visits/{visit}/note")
    fields = {name: form_value(answer.text, name) for name in ("csrf", "key", "amends")}
    return answer.status_code == 200 and bool(fields["key"]), fields


def review(browser: TestClient, note: str) -> bool:
    """Open a note and decide it: each finding confirmed, or nothing missed when it has none."""
    answer = browser.get(f"/notes/{note}")
    if answer.status_code != 200:
        return False
    fields = {"csrf": form_value(answer.text, "csrf")}
    groups = set(re.findall(r'name="finding-(\d+)"', answer.text))
    fields |= {f"finding-{group}": "confirmed" for group in groups}
    if not groups:
        fields["missed"] = "nothing"
    answer = browser.post(f"/notes/{note}/decisions", data=fields, follow_redirects=False)
    return sent(answer) and "choose=1" not in answer.headers["location"]  # asked to choose again


def replay(week: dict[str, Any], today: int, folder: Path) -> dict[str, Any]:
    """Each access made through the app's routes, with its clock at the access's time, and
    the audit entries the day wrote (rows and bytes, for the cost of logging)."""
    app, settings = new_app(folder)
    # A day of saves arrives in about a minute here; no person saves at that pace, so the
    # per-address save limit is lifted for the replay only.
    main.SAVES_PER_WINDOW = 1_000_000
    open_browsers = browsers(app, people(week, office=True))
    forms: dict[tuple[str, str], dict[str, str]] = {}
    results = []
    for item in by_time(carers_day(week, today) + office_day(week, today)):
        settings["clock"] = {"day": item["day"], "minute": item["minute"]}
        browser = open_browsers[item["person"]]
        action = item["action"]
        if action == "open day sheet":
            allowed = browser.get(f"/today?day={item['visit_day']}").status_code == 200
        elif action == "open visit":
            allowed, forms[(item["person"], item["visit"])] = note_form(browser, item["visit"])
        elif action == "send note":
            allowed = send(browser, item["visit"], forms[(item["person"], item["visit"])],
                           "Replayed visit: all well.")  # fmt: skip
        elif action == "add to note":
            allowed, form = note_form(browser, item["visit"])
            allowed = allowed and send(browser, item["visit"], form, "Replayed addition.")
        elif action == "open register":
            allowed = browser.get(f"/queue?day={item['day']}").status_code == 200
        elif action == "review note":
            note = first_note(settings, item["visit"])
            allowed = note is not None and review(browser, note)
        elif action == "open earlier note":
            allowed = browser.get(f"/notes/{item['note']}").status_code == 200
        else:
            allowed = browser.get(item["url"]).status_code == 200
        results.append(item | {"allowed": allowed})
    logged = audit_rows(settings)
    audit = {"rows": len(logged), "bytes": sum(len(entry.encode()) for entry in logged)}
    return summary(results) | {"audit": audit, "accesses": results}


def formula_replay(
    week: dict[str, Any], rule: Rule, accesses: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Each access checked against a formula: allowed when every client record it opens is open
    to that person at that time (a day sheet opens every client on it; a page about no one
    client is open to its role)."""
    results = []
    for item in accesses:
        needed = item.get("clients") or ([item["client"]] if item["client"] else [])
        open_now = rule(week, item["role"], item["person"], item["day"], item["minute"])
        results.append(item | {"allowed": all(client in open_now for client in needed)})
    return results


def compare_formulas(
    demo: dict[str, Any], fresh: dict[str, Any], points: list[dict[str, Any]],
    demo_day: list[dict[str, Any]], fresh_day: list[dict[str, Any]],
) -> dict[str, Any]:  # fmt: skip
    """Each formula's exposure, its disagreements with the app's pages, and the needed accesses
    it would refuse on each replayed day."""
    today = demo["days"][-1]
    found = {}
    for name, rule in formulas().items():
        demo_refused = formula_replay(demo, rule, demo_day)
        found[name] = {
            "demo_exposure": exposure(demo, rule, people(demo, office=True), today),
            "fresh_exposure": exposure(fresh, rule, people(fresh, office=False), fresh["days"][-1]),
            "app_disagrees": len(disagreements(demo, rule, today, points)),
            "demo_replay": summary(demo_refused)["by_role"],
            "demo_refused": summary(demo_refused)["refused"],
            "fresh_replay": summary(formula_replay(fresh, rule, fresh_day))["by_role"],
        }
    return found


def summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    roles: dict[str, dict[str, int]] = {}
    for item in results:
        counts = roles.setdefault(item["role"], {"accesses": 0, "refused": 0})
        counts["accesses"] += 1
        counts["refused"] += 0 if item["allowed"] else 1
    return {"by_role": roles, "refused": [item for item in results if not item["allowed"]]}


def main_run(out: Path) -> int:
    started = time.monotonic()
    timings = {}
    demo = demo_week()
    fresh = fresh_week()
    today = demo["days"][-1]
    fresh_today = fresh["days"][-1]
    report: dict[str, Any] = {"about": "SYNTHETIC data only."}
    with tempfile.TemporaryDirectory() as name:
        folder = Path(name)
        points = probe(demo, today, folder / "probe")
        timings["probe"] = round(time.monotonic() - started)
        report["audit"] = audit_coverage(demo, today, folder / "audit")
        timings["audit"] = round(time.monotonic() - started)
        report["demo_replay"] = replay(demo, today, folder / "replay")
        timings["replay"] = round(time.monotonic() - started)
    fresh_day = by_time(carers_day(fresh, fresh_today))
    report["fresh_replay"] = {"accesses": fresh_day}
    report["formulas"] = compare_formulas(
        demo, fresh, points, report["demo_replay"]["accesses"], fresh_day
    )
    matching = [name for name, found in report["formulas"].items() if not found["app_disagrees"]]
    report["agreement"] = {
        "points": len(points),
        "app_opens": sum(1 for point in points if point["app_opens"]),
        "formulas_agreeing": matching,
        "sample": points,
    }
    report["seconds_elapsed"] = timings
    out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8",
                   newline="\n")  # fmt: skip
    print(f"the app's pages agree with: {matching} ({len(points)} points)")
    print(f"audit: reads logged {report['audit']['reads_logged']}, "
          f"refusals logged {report['audit']['refusals_logged']}")  # fmt: skip
    print(f"the app, replayed day: {report['demo_replay']['by_role']}")
    print(f"audit entries in the replayed day: {report['demo_replay']['audit']}")
    for name, found in report["formulas"].items():
        carers = found["demo_exposure"]["carer"]["record_minutes"]
        lead = found["demo_exposure"]["lead"]["record_minutes"]
        refused = sum(row["refused"] for row in found["demo_replay"].values())
        fresh_refused = sum(row["refused"] for row in found["fresh_replay"].values())
        print(
            f"{name}: carers {carers:,}, lead {lead:,} record-minutes; disagrees with the app "
            f"at {found['app_disagrees']}; refuses {refused} (demo), {fresh_refused} (fresh)"
        )
    print(f"written to {out}; seconds {timings}")
    return 0 if len(matching) == 1 else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: access_trial.py <out.json>")
    sys.exit(main_run(Path(sys.argv[1])))
