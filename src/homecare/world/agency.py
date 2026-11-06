"""The demo agency: SYNTHETIC clients, carers, care plans and four weeks of visits, from a seed.

Clients and carers sit on road-graph nodes (junctions), never real addresses. A care plan is the
same each day: 1 to 4 visits, each in a time window, needing the client's care skills. Each
client has a usual carer, the nearest one able to do all their visits, so the rota can measure
continuity. Carers work split shifts on five days a week, with two consecutive days off, so a
usual carer is away on two days of each week.

Each client has a profile: a main group, which sets how much continuity matters (its tier),
and overlays, which set hard rules:
- time-critical medicine: the morning and bed visits fall within 30 minutes of the prescribed time;
- insulin: a delegated task, so the morning visit needs a carer trained for it;
- a hoist: the morning and bed visits need two carers at once (pairing is not built yet);
- a gender preference for intimate care, on the morning and bed visits.

The shares below are this demo's own assumptions, not measured figures.

Times are minutes from midnight; travel times are seconds (from `roads.matrix`).
"""

import random
from collections.abc import Callable
from typing import Any

from homecare.world.roads import Matrix

SEED = 2026
START = "2026-10-12"  # a Monday; four weeks run to Sunday 8 November
DAYS = 28
CLIENTS = 60
# Sized to the morning peak: 60 visits × (45 min + ~6 min driving) / 180 min ≈ 17 on
# duty, 21 with a 20% margin, and 21 × 7 / 5 ≈ 30 with two days off a week.
CARERS = 30

# Visit windows and lengths, in minutes from midnight.
SLOTS = {
    "morning": (420, 600, 45),
    "lunch": (720, 840, 30),
    "tea": (990, 1110, 30),
    "bed": (1170, 1320, 30),
}
PLANS = {1: ["morning"], 2: ["morning", "bed"], 3: ["morning", "lunch", "bed"], 4: list(SLOTS)}
PLAN_WEIGHTS = {1: 0.2, 2: 0.35, 3: 0.25, 4: 0.2}
NEEDS = {"personal care": 0.8, "medication": 0.5}
# Main groups: share of clients (assumed), continuity tier (3 = continuity matters most) and the
# skill they need.
GROUPS = {
    "dementia": (0.3, 3, "dementia"),
    "learning disability or autism": (0.1, 3, "learning disability"),
    "end of life": (0.05, 3, "end of life"),
    "general": (0.4, 1, None),
    "reablement": (0.15, 1, None),
}
TIME_CRITICAL = 0.1
INSULIN = 0.1
HOIST = 0.05
GENDER_PREFERENCE = 0.3
PREFERRED = {"female": 0.8, "male": 0.2}
# The prescribed medicine times on the medicine rounds, and the allowed gap either side.
PRESCRIBED = {"morning": 480, "bed": 1230}
ON_TIME = 30
INTIMATE = ("morning", "bed")
SKILLS = {
    "personal care": 1.0,
    "medication": 0.6,
    "dementia": 0.4,
    "learning disability": 0.3,
    "end of life": 0.3,
    "insulin": 0.3,
}
CARER_GENDERS = {"female": 0.8, "male": 0.2}
CONTRACTS = {"zero-hours": (0.4, 30), "part-time": (0.35, 20), "full-time": (0.25, 37.5)}
AGE_BANDS = ["18-24", "25-34", "35-44", "45-54", "55+"]
SHIFT = (420, 1320)  # a carer can work from 07:00 to 22:00
# SYNTHETIC names and descriptions for the note writer; ages and conditions are demo assumptions.
FIRST_NAMES = {
    "F": ["Doreen", "Grace", "Sunita", "Irene", "Hilda", "Bernadette", "Pauline", "Margaret",
          "Shirley", "Nasreen", "Edna", "Joyce", "Parveen", "Gloria", "Jean", "Mary"],
    "M": ["Tariq", "Leonard", "Desmond", "Kwame", "Stanley", "Arjun", "Winston", "Frank", "Hamid",
          "Derek", "Ronald", "Mohammed", "Errol", "Patrick", "Gurdeep", "Albert"],
}  # fmt: skip
SURNAMES = ["Hale", "Hussain", "Okafor", "Pike", "Patel", "Clarke", "Walsh", "Mensah", "Brown",
            "Moore", "Kelly", "Sharma", "Edwards", "Campbell", "Lewis", "Turner", "Adams", "Rahman",
            "Begum", "Ali", "Singh", "Murphy", "Johnson", "Ahmed", "Khan", "Wright"]  # fmt: skip
CONDITIONS = {
    "dementia": "dementia",
    "learning disability or autism": "a learning disability",
    "end of life": "receiving end of life care",
    "reablement": "recovering after a hospital stay",
}
GENERAL_CONDITIONS = ["heart failure", "COPD", "arthritis", "frailty", "type 2 diabetes",
                      "a stroke last year", "kidney disease", "poor sight"]  # fmt: skip
LIVING = ["lives alone", "lives alone", "lives with a partner", "lives with a daughter",
          "lives with a son"]  # fmt: skip


def weighted(rng: random.Random, weights: dict[Any, float]) -> Any:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def make_clients(rng: random.Random, nodes: list[str]) -> list[dict[str, Any]]:
    clients = []
    group_shares = {name: share for name, (share, _, _) in GROUPS.items()}
    for number, node in enumerate(nodes, start=1):
        needs = [need for need, share in NEEDS.items() if rng.random() < share]
        group = weighted(rng, group_shares)
        _, tier, skill = GROUPS[group]
        if skill:
            needs.append(skill)
        preference = weighted(rng, PREFERRED) if rng.random() < GENDER_PREFERENCE else None
        clients.append(
            {
                "id": f"C{number:02d}",
                "node": node,
                "group": group,
                "tier": tier,
                "needs": needs,
                "slots": PLANS[weighted(rng, PLAN_WEIGHTS)],
                "time_critical": rng.random() < TIME_CRITICAL,
                "insulin": rng.random() < INSULIN,
                "hoist": rng.random() < HOIST,
                "gender_preference": preference,
            }
        )
    return clients


def describe_clients(clients: list[dict[str, Any]], seed: int) -> None:
    """Give each client a SYNTHETIC name, sex, age and a line the note writer reads. A separate
    random stream, so adding these changes nothing else in the world."""
    rng = random.Random(seed + 1)
    names = []
    for sex, firsts in FIRST_NAMES.items():
        for first in firsts:
            for last in SURNAMES:
                names.append((sex, first, last))
    for client, (sex, first, last) in zip(clients, rng.sample(names, len(clients)), strict=True):
        young = client["group"] == "learning disability or autism"
        age = rng.randint(25, 70) if young else rng.randint(65, 96)
        condition = CONDITIONS.get(client["group"]) or rng.choice(GENERAL_CONDITIONS)
        details = [str(age), condition]
        if client["time_critical"]:
            details.append("Parkinson's, with timed medicine")
        if client["insulin"]:
            details.append("diabetes on insulin")
        if client["hoist"]:
            details.append("uses a hoist")
        details.append(rng.choice(LIVING))
        title = "Mrs" if sex == "F" else "Mr"
        client.update(name=f"{title} {first} {last}", sex=sex, age=age, details=", ".join(details))


def make_carers(rng: random.Random, nodes: list[str]) -> list[dict[str, Any]]:
    carers = []
    shares = {name: share for name, (share, _) in CONTRACTS.items()}
    for number, node in enumerate(nodes, start=1):
        contract = weighted(rng, shares)
        carers.append(
            {
                "id": f"K{number:02d}",
                "home": node,
                "skills": [skill for skill, share in SKILLS.items() if rng.random() < share],
                "gender": weighted(rng, CARER_GENDERS),
                "contract": contract,
                "weekly_hours": CONTRACTS[contract][1],
                # Two consecutive weekdays off (0 is Monday), staggered across carers.
                "days_off": [(number - 1) % 7, number % 7],
                "start": f"{rng.randint(2019, 2026)}-{rng.randint(1, 12):02d}-01",
                "age_band": rng.choice(AGE_BANDS),
            }
        )
    return carers


def day_minutes(client: dict[str, Any]) -> int:
    return sum(SLOTS[slot][2] for slot in client["slots"])


def visit_rules(client: dict[str, Any], slot: str) -> dict[str, Any]:
    """A visit's window, skills and carer rules, from the client's profile."""
    opens, closes, duration = SLOTS[slot]
    if client.get("time_critical") and slot in PRESCRIBED:
        opens, closes = PRESCRIBED[slot] - ON_TIME, PRESCRIBED[slot] + ON_TIME
    skills = list(client["needs"])
    if client.get("insulin") and slot == "morning":
        skills.append("insulin")
    intimate = slot in INTIMATE and "personal care" in client["needs"]
    return {
        "open": opens,
        "close": closes,
        "duration": duration,
        "skills": skills,
        "two_carers": bool(client.get("hoist")) and slot in INTIMATE,
        "gender": client.get("gender_preference") if intimate else None,
    }


def on_duty(carer: dict[str, Any], day: int) -> bool:
    """Whether the carer works on this day of the four weeks; day 0 is a Monday."""
    return day % 7 not in carer.get("days_off", [])


def can_do(carer: dict[str, Any], rules: dict[str, Any]) -> bool:
    """Whether a carer may do a visit alone: every skill, and the preferred gender if any."""
    if not set(rules["skills"]) <= set(carer["skills"]):
        return False
    return rules.get("gender") is None or carer.get("gender") == rules["gender"]


def usual_carers(
    clients: list[dict[str, Any]], carers: list[dict[str, Any]], travel: Matrix
) -> dict[str, str | None]:
    """Each client's usual carer: the nearest carer able to do every one of the client's visits,
    among those with room left in their week. None when no carer can."""
    room = {carer["id"]: carer["weekly_hours"] * 60 * 0.7 for carer in carers}
    usual: dict[str, str | None] = {}
    for client in clients:
        plan = [visit_rules(client, slot) for slot in client["slots"]]
        able = [c for c in carers if all(can_do(c, rules) for rules in plan)]
        week = day_minutes(client) * 7
        free = [c for c in able if room[c["id"]] >= week] or able
        if not free:
            usual[client["id"]] = None
            continue
        nearest = min(free, key=lambda c: (travel[c["home"]][client["node"]], c["id"]))
        usual[client["id"]] = nearest["id"]
        room[nearest["id"]] -= week
    return usual


def make_visits(clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
    visits: list[dict[str, Any]] = []
    for day in range(DAYS):
        for client in clients:
            for slot in client["slots"]:
                visits.append(
                    {
                        "id": f"V{len(visits) + 1:05d}",
                        "client": client["id"],
                        "day": day,
                        "slot": slot,
                        "tier": client["tier"],
                        **visit_rules(client, slot),
                    }
                )
    return visits


def build(
    nodes: list[str], travel_for: Callable[[list[str]], Matrix], seed: int = SEED
) -> dict[str, Any]:
    """The whole demo agency. `travel_for(places)` returns the driving-time matrix between the
    chosen places, so the caller decides where times come from (the road graph, or a test)."""
    rng = random.Random(seed)
    # Distinct junctions, so no carer lives where a client does.
    picked = rng.sample(sorted(nodes), CLIENTS + CARERS)
    clients = make_clients(rng, picked[:CLIENTS])
    describe_clients(clients, seed)
    carers = make_carers(rng, picked[CLIENTS:])
    places = sorted({c["node"] for c in clients} | {c["home"] for c in carers})
    travel = travel_for(places)
    return {
        "synthetic": True,
        "seed": seed,
        "start": START,
        "days": DAYS,
        "shift": list(SHIFT),
        "clients": clients,
        "carers": carers,
        "usual": usual_carers(clients, carers, travel),
        "visits": make_visits(clients),
        "travel": travel,
    }
