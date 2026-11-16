"""Synthetic referrals for a new home-care client, written by a local model from seeded profiles.

A spec is a client profile, which is the answer key, plus the facts that state it
(`referral_facts.json`). There is one fact for each true claim and one for the visit plan, a
reason for the referral, and a few distractors. A distractor names the topic of a false claim
without stating it ("controlled by diet", "her brother has dementia"), so the sets test whether
the reader claims only what is stated. The answer key never comes from the reader, the system it
tests.

The writer model turns the facts into a referral in a persona's voice. Code then checks the text:
- every fact's evidence survived;
- a false claim with no distractor has none of its topic words, so the writer cannot add a need,
  visit or preference the key lacks;
- the length is a referral's length.
A referral that fails is written again with a new seed, up to `ATTEMPTS` times, then left out.

The profiles use evaluation shares, not the world's own, so that rare fields have enough
positives to measure. They are this project's assumptions. Three rules keep a profile coherent:
time-critical medicine means carers give medicines; and a gender preference for intimate care,
or a hoist, means personal care.
"""

import json
import random
import re
from pathlib import Path
from typing import Any

from homecare import llm
from homecare.referrals import fields
from homecare.world import agency, writer

FACTS = Path(__file__).with_name("referral_facts.json")
ATTEMPTS = 4
MIN_WORDS = 40
MAX_WORDS = 320

# `perturb`: seed typing errors into the written text, as a hurried typist would leave them.
GLM = {"pool": "A", "writer": "glm-4.7-flash", "personas": "A", "perturb": False}
NEMOTRON = {"pool": "B", "writer": "nemotron-3.5-lightning", "personas": "B", "perturb": True}
QWEN = {"pool": "C", "writer": "qwen3.6-35b-a3b", "personas": "C", "perturb": True}
# No fourth writer family other than the reader's is on disk, so the second fresh set's writer
# repeats held-out's; its facts and personas are new.
NEMOTRON_D = NEMOTRON | {"pool": "D", "personas": "D"}
SETS: dict[str, dict[str, Any]] = {
    "referrals-dev": GLM | {"count": 150, "seed": 811},
    # The first held-out write ("referrals-heldout", registered, never read or scored) lost 61 of
    # 200 specs to evidence patterns that named one word for a concept; keeping the rest would
    # have left the easier phrasings. The same specs, with wider patterns, under a new name.
    "referrals-heldout-2": NEMOTRON | {"count": 200, "seed": 822},
    # Written after the held-out run, to decide the reader's second version: a third writer
    # family, and facts and personas that neither earlier set used.
    "referrals-fresh": QWEN | {"count": 200, "seed": 833},
    # Written after the second version was tested, to decide the reader's third version.
    "referrals-fresh-2": NEMOTRON_D | {"count": 200, "seed": 844},
}
# The app's demo referrals (`homecare referrals demo`): the development set's facts and personas,
# never scored, and never registered as an evaluation set.
DEMO: dict[str, Any] = GLM | {"count": 6, "seed": 855}
NEED_SHARES = {"personal care": 0.5, "medication": 0.5}
GROUP_SHARES = {None: 0.4, "dementia": 0.25, "learning disability": 0.2, "end of life": 0.15}
GENDER_SHARES = {None: 0.5, "female": 0.3, "male": 0.2}
FLAG_SHARE = 1 / 3
DISTRACTOR_SHARE = 0.3
MAX_DISTRACTORS = 3

PERSONAS = {
    "A": [
        "a hospital discharge coordinator who writes a structured, formal referral",
        "a social worker who writes a clear summary after a needs assessment",
        "a GP surgery receptionist passing on a short referral from the doctor",
    ],
    "B": [
        "a worried son or daughter emailing the agency in their own words",
        "a district nurse writing in clinical shorthand, with abbreviations such as 'pt', 'meds' "
        "and 'PC'",
        "an office worker typing up a referral taken over the phone, in note form",
        "a social worker writing a long assessment, with the care needs in the middle",
    ],
    "C": [
        "a hospital discharge-to-assess coordinator filling in a referral form with headings",
        "a GP practice nurse writing a brief handover to the agency",
        "a friend of the person writing on their behalf, chatty and out of order",
        "a council brokerage officer passing on the care package the council has commissioned",
    ],
    "D": [
        "a reablement therapist handing the person over after six weeks, in bullet points",
        "a community matron writing a terse care-plan summary under short labels such as "
        "'Calls:' and 'Background:'",
        "a grandchild with lasting power of attorney writing a long, polite letter",
        "an out-of-hours duty social worker sending a rushed email with little punctuation",
    ],
}
WRITER_SYSTEM = "You write realistic, fictional home-care referrals for testing software."
WRITER_SCHEMA = {
    "type": "object",
    # No string length bounds: llama-server turns long bounds into a grammar it cannot parse.
    # `problems` checks the length instead.
    "properties": {"referral": {"type": "string"}},
    "required": ["referral"],
}


def load(pool: str) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """The pool's facts, and every claim's topic patterns."""
    data = json.loads(FACTS.read_text(encoding="utf-8"))
    return [fact for fact in data["facts"] if fact["pool"] == pool], data["topics"]


def draw_profile(rng: random.Random) -> dict[str, Any]:
    """One client profile, in the agency's own keys, at the evaluation shares."""
    needs = [need for need, share in NEED_SHARES.items() if rng.random() < share]
    group = agency.weighted(rng, GROUP_SHARES)
    if group:
        needs.append(group)
    gender = agency.weighted(rng, GENDER_SHARES)
    flags = {flag: rng.random() < FLAG_SHARE for flag in fields.FLAGS}
    if flags["time_critical"]:
        needs.append("medication")
    if gender or flags["hoist"]:
        needs.append("personal care")
    return {
        "needs": [need for need in fields.NEEDS if need in needs],
        "slots": list(agency.PLANS[rng.choice(list(agency.PLANS))]),
        "gender_preference": gender,
        **flags,
    }


def pick_facts(
    rng: random.Random, facts: list[dict[str, Any]], claims: list[str]
) -> list[dict[str, Any]]:
    """A reason, the visit plan, one fact for each other true claim, then distractors."""
    states = [fact for fact in facts if fact["kind"] == "states"]
    visits = [claim for claim in claims if claim in fields.VISIT_CLAIMS]
    chosen = [rng.choice([fact for fact in facts if fact["kind"] == "filler"])]
    chosen.append(rng.choice([fact for fact in states if fact["claims"] == visits]))
    for claim in claims:
        if claim not in fields.VISIT_CLAIMS:
            chosen.append(rng.choice([fact for fact in states if fact["claims"] == [claim]]))
    false = set(fields.CLAIMS) - set(claims)
    candidates = []
    for fact in facts:
        fits = set(fact["claims"]) <= false and set(fact.get("when", [])) <= set(claims)
        if fact["kind"] == "distractor" and fits:
            candidates.append(fact)
    rng.shuffle(candidates)
    covered: set[str] = set()
    added = 0
    for fact in candidates:
        if added == MAX_DISTRACTORS:
            break
        if covered & set(fact["claims"]) or rng.random() >= DISTRACTOR_SHARE:
            continue
        chosen.append(fact)
        covered |= set(fact["claims"])
        added += 1
    rng.shuffle(chosen)
    return chosen


def specs(name: str, plan: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The set's specs: seeded, so every run of the writer gets the same ones. `plan` is the
    registered set's (`SETS[name]`) unless another is given, as for the demo."""
    plan = SETS[name] if plan is None else plan
    rng = random.Random(plan["seed"])
    facts, _ = load(plan["pool"])
    result = []
    for number in range(1, plan["count"] + 1):
        client = draw_profile(rng)
        claims = fields.claims_of(client)
        chosen = pick_facts(rng, facts, claims)
        sex = rng.choice(["F", "M"])
        young = "learning disability" in client["needs"]
        result.append(
            {
                "id": f"{name}-{number:03d}",
                "client": client,
                "claims": claims,
                "facts": [fact["id"] for fact in chosen],
                "sex": sex,
                "title": "Mrs" if sex == "F" else "Mr",
                "first_name": rng.choice(agency.FIRST_NAMES[sex]),
                "surname": rng.choice(agency.SURNAMES),
                "age": rng.randint(25, 70) if young else rng.randint(65, 96),
                "living": rng.choice(agency.LIVING),
                "persona": rng.choice(PERSONAS[plan["personas"]]),
                "seed": rng.randrange(1, 2**31),
            }
        )
    return result


def filled(text: str, spec: dict[str, Any]) -> str:
    """A fact's text for this client, starting with a capital letter."""
    female = spec["sex"] == "F"
    words = {
        "{name}": f"{spec['title']} {spec['surname']}",
        "{She}": "She" if female else "He",
        "{she}": "she" if female else "he",
        "{her}": "her" if female else "his",
    }
    for placeholder, word in words.items():
        text = text.replace(placeholder, word)
    return text[0].upper() + text[1:]


def writer_prompt(spec: dict[str, Any], facts: list[str]) -> str:
    lines = "\n".join(f"- {fact}" for fact in facts)
    person = f"{spec['title']} {spec['first_name']} {spec['surname']}"
    return f"""Write a referral for home care that an agency in England has just received.

Writer: {spec["persona"]}.
The person referred: {person}, aged {spec["age"]}, who {spec["living"]}.
Facts the referral must give, each in your own words:
{lines}

Rules:
- Write only the referral, as this writer would send it: plain text, 60 to 250 words.
- Give every fact above, keeping its exact meaning. Add no other care needs, conditions,
  medicines, equipment, visit times or carer preferences.

Reply as JSON: {{"referral": "<the referral>"}}"""


def problems(
    text: str,
    spec: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    topics: dict[str, list[str]],
) -> list[str]:
    """Why a written referral cannot be used, if it cannot."""
    found = []
    for fact_id in spec["facts"]:
        for group in by_id[fact_id]["must"]:
            if not any(re.search(pattern, text, re.IGNORECASE) for pattern in group):
                found.append(f"evidence lost: {fact_id} {group}")
    mentioned = set()
    for fact_id in spec["facts"]:
        mentioned |= set(by_id[fact_id]["claims"])
    for claim in fields.CLAIMS:
        if claim in mentioned:
            continue
        for pattern in topics[claim]:
            if re.search(pattern, text, re.IGNORECASE):
                found.append(f"mentions {claim}: {pattern}")
    words = len(text.split())
    if not MIN_WORDS <= words <= MAX_WORDS:
        found.append(f"length {words} words")
    return found


def write_referral(
    url: str,
    spec: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    topics: dict[str, list[str]],
    perturb: bool,
) -> dict[str, Any]:
    """The spec's referral: the first attempt that passes the checks, or why none did."""
    facts = [filled(by_id[fact_id]["text"], spec) for fact_id in spec["facts"]]
    reasons: list[str] = []
    for attempt in range(ATTEMPTS):
        seed = spec["seed"] + attempt
        # A reply cut off at max_tokens, or unusable, is one failed attempt, not the end of a run.
        try:
            reply = llm.chat(url, WRITER_SYSTEM, writer_prompt(spec, facts), WRITER_SCHEMA,
                             temperature=0.8, seed=seed, max_tokens=900)  # fmt: skip
        except llm.ModelServerError as error:
            reasons = [f"model error: {error}"]
            continue
        text = str(reply.get("referral", "")).strip() if isinstance(reply, dict) else ""
        reasons = problems(text, spec, by_id, topics)
        if reasons:
            continue
        changed = False
        if perturb:
            rng = random.Random(seed)
            for _ in range(5):
                candidate = writer.perturbed(text, spec["persona"], rng)
                if not problems(candidate, spec, by_id, topics):
                    text, changed = candidate, True
                    break
        return {**spec, "text": text, "attempts": attempt + 1, "perturbed": changed}
    return {**spec, "text": None, "attempts": ATTEMPTS, "failed": reasons}
