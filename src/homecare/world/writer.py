"""Synthetic visit notes, written by a local model from seeded specs.

A spec names the client, the visit and what happened (one or two scenario templates, or a
routine visit). Its labels are the templates' categories, so the labels never come from the
extractor, the system they test. The writer model rewrites the spec in a persona's voice, and
code then checks the note:
- every evidence word group of every template survived (`must` in scenarios.json);
- no category word gives the label away;
- the length is a note's length.
A note that fails is written again with a new seed, up to `ATTEMPTS` times, then left out.

The held-out set, kept apart for final results, differs on every axis the project controls: a
different writer family, other templates (pool B), other personas, and seeded perturbations in
code (typos, shorthand, lost punctuation).
"""

import json
import random
import re
from pathlib import Path
from typing import Any

from homecare import llm
from homecare.notes.categories import SAFEGUARDING

SCENARIOS = Path(__file__).with_name("scenarios.json")
ATTEMPTS = 4
MIN_WORDS = 12
MAX_WORDS = 260
# A note naming its own category would test reading the label, not the situation.
LABEL_WORDS = ("abuse", "neglect", "safeguard", "slavery", "soft sign", "discriminat", "exploit")

GLM = {"pool": "A", "writer": "glm-4.7-flash", "personas": "A"}
NEMOTRON = {"pool": "B", "writer": "nemotron-3.5-lightning", "personas": "B"}
SETS: dict[str, dict[str, Any]] = {
    "notes-dev": GLM | {"count": 120, "seed": 101},
    "notes-test": GLM | {"count": 200, "seed": 202},
    "notes-attack": GLM | {"count": 24, "seed": 404},
    # The v2 attack sets: one for the adaptive attacker to practise on, one run once.
    "notes-attack-dev": GLM | {"count": 12, "seed": 505},
    "notes-attack-final": GLM | {"count": 24, "seed": 606},
    "notes-attack-fresh": GLM | {"count": 24, "seed": 707},
    "notes-heldout": NEMOTRON | {"count": 180, "seed": 303},
}
# Share of each set's notes by kind; the attack set has concerns only.
MIX = {
    "routine": 0.33,
    "safeguarding": 0.33,
    "deterioration": 0.14,
    "missed": 0.12,
    "quality": 0.08,
}
ATTACK_MIX = {"safeguarding": 0.5, "deterioration": 0.25, "missed": 0.25}
SECOND_CONCERN = 0.15  # share of concern notes that also carry a concern of another kind
# The demo week (DEMO only): the share of visit notes with a concern is an assumption, as no
# published rate was found; concerns split by MIX. The rota's slots map to the writer's visits.
DEMO_CONCERN = 0.15
WRITER_VISIT = {"morning": "morning", "lunch": "lunch", "tea": "tea-time", "bed": "evening"}

PERSONAS = {
    "A": [
        "an experienced carer who writes clear, complete sentences",
        "a newer carer who writes short, simple sentences",
        "a carer at the end of a long shift who writes briefly and to the point",
        "a senior carer who writes in a formal, factual way",
    ],
    "B": [
        "a rushed carer typing on a phone in shorthand, with abbreviations such as 'pt', 'meds' "
        "and 'PC'",
        "a carer whose first language is not English, with some grammar mistakes",
        "a carer dictating into the phone's voice-to-text, so the text runs on with little "
        "punctuation and a few misheard words",
        "a carer who writes only one or two lines",
        "a chatty carer who writes a long note about the whole visit, with the important part "
        "in the middle",
    ],
}
CLIENTS = [
    ("Mrs Doreen Hale", 84, "dementia, lives alone"),
    ("Mr Tariq Hussain", 79, "Parkinson's, lives with his wife"),
    ("Mrs Grace Okafor", 88, "heart failure, uses a frame"),
    ("Mr Leonard Pike", 91, "frail, partially sighted"),
    ("Mrs Sunita Patel", 76, "type 2 diabetes, lives with her son"),
    ("Mr Desmond Clarke", 82, "COPD, lives alone"),
    ("Mrs Irene Walsh", 87, "arthritis, uses a wheelchair outdoors"),
    ("Mr Kwame Mensah", 73, "stroke last year, weak left side"),
    ("Mrs Hilda Brown", 93, "dementia, lives with her daughter"),
    ("Mr Stanley Moore", 80, "diabetes on insulin, lives alone"),
    ("Mrs Bernadette Kelly", 85, "osteoporosis, recent hip fracture"),
    ("Mr Arjun Sharma", 77, "early dementia, lives with his nephew"),
    ("Mrs Pauline Edwards", 81, "depression, lives alone"),
    ("Mr Winston Campbell", 86, "heart condition, uses a hoist"),
    ("Mrs Margaret Lewis", 90, "deaf, lives alone"),
    ("Mr Frank Turner", 78, "learning disability, lives with his brother"),
    ("Mrs Shirley Adams", 83, "kidney disease, lives with her husband"),
    ("Mr Hamid Rahman", 75, "MS, uses a wheelchair"),
]
VISITS = {
    "morning": "help to wash and dress, breakfast, morning tablets",
    "lunch": "lunch, a drink, check the person is comfortable",
    "tea-time": "tea, a drink, tidy the kitchen",
    "evening": "help into nightclothes and bed, evening tablets, leave a drink",
}
WRITER_SYSTEM = "You write realistic, fictional home-care visit notes for testing software."


def templates(pool: str) -> list[dict[str, Any]]:
    data = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    return [t for t in data["templates"] if t["pool"] == pool]


def kind(template: dict[str, Any]) -> str:
    category = template["category"]
    if not category:
        return "routine"
    if category in SAFEGUARDING:
        return "safeguarding"
    return {
        "possible deterioration (soft sign)": "deterioration",
        "missed task": "missed",
        "quality risk": "quality",
    }[category]


def client_sex(name: str) -> str:
    return "F" if name.startswith("Mrs ") else "M"


def sex_of(picked: list[dict[str, Any]]) -> str:
    """The client's sex the templates' wording needs ("" for either), or "conflict"."""
    needed = {t["sex"] for t in picked if t.get("sex")}
    if len(needed) > 1:
        return "conflict"
    return needed.pop() if needed else ""


def specs(name: str) -> list[dict[str, Any]]:
    """The set's specs: seeded, and spread evenly over each kind's templates."""
    plan = SETS[name]
    rng = random.Random(plan["seed"])
    pool = templates(plan["pool"])
    mix = ATTACK_MIX if name.startswith("notes-attack") else MIX
    chosen: list[list[dict[str, Any]]] = []
    for note_kind, share in mix.items():
        group = [t for t in pool if kind(t) == note_kind]
        rng.shuffle(group)
        for index in range(round(plan["count"] * share)):
            chosen.append([group[index % len(group)]])
    for picked in chosen:
        if kind(picked[0]) != "routine" and rng.random() < SECOND_CONCERN:
            others = [t for t in pool if kind(t) not in ("routine", kind(picked[0]))]
            others = [t for t in others if sex_of([picked[0], t]) != "conflict"]
            picked.append(rng.choice(others))
    rng.shuffle(chosen)
    result: list[dict[str, Any]] = []
    for number, picked in enumerate(chosen, 1):
        needed = sex_of(picked)
        client = rng.choice([c for c in CLIENTS if needed in ("", client_sex(c[0]))])
        visit = rng.choice(list(VISITS))
        result.append(
            {
                "id": f"{name}-{number:03d}",
                "templates": [t["id"] for t in picked],
                "labels": sorted({t["category"] for t in picked if t["category"]}),
                "client": client[0],
                "client_details": f"{client[1]}, {client[2]}",
                "visit": visit,
                "persona": rng.choice(PERSONAS[plan["personas"]]),
                "seed": rng.randrange(1, 2**31),
            }
        )
    return result


def fits(template: dict[str, Any], note_kind: str, sex: str, slot: str) -> bool:
    """Whether a template can write this note: its kind, the client's sex where its wording needs
    one, and the visit, where the situation names one (`slots` in scenarios.json)."""
    if kind(template) != note_kind or template.get("sex") not in (None, sex):
        return False
    return slot in template.get("slots", [slot])


def demo_specs(
    world: dict[str, Any], rotas: list[dict[str, Any]], name: str, seed: int
) -> list[dict[str, Any]]:
    """One spec per visit a carer made in the rotas: DEMO only, never an evaluation set.

    The client's details come from their profile; each carer keeps one writing style; a note is
    routine or carries one concern at the fixed rates below, and its labels come from the spec."""
    rng = random.Random(seed)
    clients = {client["id"]: client for client in world["clients"]}
    visits = {visit["id"]: visit for visit in world["visits"]}
    style = {carer["id"]: PERSONAS["A"][k % 4] for k, carer in enumerate(world["carers"])}
    concerns = {note_kind: share for note_kind, share in MIX.items() if note_kind != "routine"}
    pool = templates("A")
    result: list[dict[str, Any]] = []
    for rota in rotas:
        for carer_id in sorted(rota["routes"]):
            for visit_id in rota["routes"][carer_id]:
                visit = visits[visit_id]
                client = clients[visit["client"]]
                note_kind = "routine"
                if rng.random() < DEMO_CONCERN:
                    note_kind = rng.choices(list(concerns), weights=list(concerns.values()))[0]
                fitting = [t for t in pool if fits(t, note_kind, client["sex"], visit["slot"])]
                template = rng.choice(fitting)
                result.append(
                    {
                        "id": f"{name}-{len(result) + 1:04d}",
                        "templates": [template["id"]],
                        "labels": [template["category"]] if template["category"] else [],
                        "client": client["name"],
                        "client_details": client["details"],
                        "visit": WRITER_VISIT[visit["slot"]],
                        "persona": style[carer_id],
                        "seed": rng.randrange(1, 2**31),
                        "day": visit["day"],
                        "visit_id": visit_id,
                        "client_id": client["id"],
                        "carer_id": carer_id,
                    }
                )
    return result


def situation_text(spec: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    first_name = spec["client"].split()[1]
    return " ".join(
        by_id[t]["situation"].replace("{client}", first_name) for t in spec["templates"]
    )


def writer_prompt(spec: dict[str, Any], situation: str) -> str:
    return f"""Write the visit note a home-care worker in England would type into the agency's app
after this visit.

Writer: {spec["persona"]}.
Client: {spec["client"]}, {spec["client_details"]}.
Visit: {spec["visit"]} visit; planned tasks: {VISITS[spec["visit"]]}.
What happened: {situation}

Rules:
- Write only the note, as the carer would: plain text, no headings, 30 to 160 words.
- Include every fact under "What happened", in your own words. Add no other problems or worries.
- Do not name any category of concern; never use words such as abuse, neglect or safeguarding.

Reply as JSON: {{"note": "<the note>"}}"""


WRITER_SCHEMA = {
    "type": "object",
    # No string length bounds: llama-server turns long bounds into a grammar it cannot parse.
    # `problems` checks the length instead.
    "properties": {"note": {"type": "string"}},
    "required": ["note"],
}


def problems(note: str, must: list[list[str]]) -> list[str]:
    """Why a written note cannot be used, if it cannot."""
    found = []
    lowered = note.lower()
    for group in must:
        if not any(stem in lowered for stem in group):
            found.append(f"evidence lost: {group}")
    for word in LABEL_WORDS:
        if word in lowered:
            found.append(f"names a category: {word}")
    words = len(note.split())
    if not MIN_WORDS <= words <= MAX_WORDS:
        found.append(f"length {words} words")
    return found


SHORTHAND = {
    "medication": "meds", "tablets": "tabs", "personal care": "PC", "breakfast": "bkfst",
    "with": "w/", "and": "&", "because": "bc", "please": "pls", "client": "pt",
}  # fmt: skip


def perturbed(note: str, persona: str, rng: random.Random) -> str:
    """The note as a hurried phone typist would leave it: some typos (two inner letters
    swapped), and for the shorthand and voice-to-text personas, their habits."""
    words = note.split(" ")
    for index, word in enumerate(words):
        if len(word) >= 5 and word.isalpha() and rng.random() < 0.04:
            cut = rng.randrange(1, len(word) - 2)
            words[index] = word[:cut] + word[cut + 1] + word[cut] + word[cut + 2 :]
    text = " ".join(words)
    if "shorthand" in persona:
        for long, short in SHORTHAND.items():
            text = re.sub(rf"\b{re.escape(long)}\b", short, text, flags=re.IGNORECASE)
    if "voice-to-text" in persona:
        text = re.sub(r"[,.;:]", "", text.lower())
    return text


def write_note(
    url: str, spec: dict[str, Any], by_id: dict[str, dict[str, Any]], perturb: bool
) -> dict[str, Any]:
    """The spec's note: the first attempt that passes the checks, or a record of why none did."""
    must = [group for t in spec["templates"] for group in by_id[t]["must"]]
    situation = situation_text(spec, by_id)
    reasons: list[str] = []
    for attempt in range(ATTEMPTS):
        seed = spec["seed"] + attempt
        reply = llm.chat(url, WRITER_SYSTEM, writer_prompt(spec, situation), WRITER_SCHEMA,
                         temperature=0.8, seed=seed, max_tokens=700)  # fmt: skip
        note = str(reply.get("note", "")).strip() if isinstance(reply, dict) else ""
        reasons = problems(note, must)
        if reasons:
            continue
        changed = False
        if perturb:
            rng = random.Random(seed)
            for _ in range(5):
                candidate = perturbed(note, spec["persona"], rng)
                if not problems(candidate, must):
                    note, changed = candidate, True
                    break
        return {**spec, "text": note, "attempts": attempt + 1, "perturbed": changed}
    return {**spec, "text": None, "attempts": ATTEMPTS, "failed": reasons}
