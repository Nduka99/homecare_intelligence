"""What a referral can state about a new client, and how those claims become a profile.

A claim is one fact the matching needs, named after the agency's profile fields, so a profile built
from confirmed claims feeds `world.agency.visit_rules` and `can_do` unchanged:
- `needs`: the care skills every visit needs;
- `slots`: which of the day's visits the person gets;
- `gender_preference`: the carers' gender for intimate care, or None;
- `time_critical`, `insulin`, `hoist`: the hard rules for timed medicine, a delegated insulin
  task and two-carer visits.
The meanings are written once, here: the reader's prompt and the synthetic referrals' facts both
use them.
"""

from typing import Any

from homecare.world import agency

GROUP_NEEDS = tuple(skill for _, _, skill in agency.GROUPS.values() if skill)
NEEDS = tuple(agency.NEEDS) + GROUP_NEEDS
VISITS = tuple(agency.SLOTS)
FLAGS = ("time_critical", "insulin", "hoist")

NEED_CLAIMS = {f"need: {need}": need for need in NEEDS}
VISIT_CLAIMS = {f"visit: {slot}": slot for slot in VISITS}
GENDER_CLAIMS = {"carer gender: female": "female", "carer gender: male": "male"}
FLAG_CLAIMS = {
    "time-critical medicine": "time_critical",
    "insulin by carers": "insulin",
    "two carers with a hoist": "hoist",
}
# Each visit is defined alike: its place in the agency's day, its window from the planner's own
# slots, and the names councils give the call (call bands: breakfast, lunch, tea, bed). So a
# referral may name a visit, give its time, or give only how many calls a day.
CALLS = {
    "morning": ("the first call of the day", "the breakfast or getting-up call"),
    "lunch": ("the midday call", "the lunch call"),
    "tea": ("the late-afternoon call", "the tea call"),
    "bed": ("the last call of the day", "the bed or night call"),
}


def clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def visit_meaning(slot: str) -> str:
    opens, closes, _ = agency.SLOTS[slot]
    place, names = CALLS[slot]
    return f"{place}, {clock(opens)}–{clock(closes)} ({names})"


MEANINGS = {
    "need: personal care": "carers help the person wash, dress, bathe or use the toilet",
    "need: medication": "carers give or prompt the person's medicines",
    "need: dementia": "the person has dementia",
    "need: learning disability": "the person has a learning disability or autism",
    "need: end of life": "the person is receiving end of life (palliative) care",
    "visit: morning": visit_meaning("morning"),
    "visit: lunch": visit_meaning("lunch"),
    "visit: tea": visit_meaning("tea"),
    "visit: bed": visit_meaning("bed"),
    "carer gender: female": "the person wants only female carers for personal care",
    "carer gender: male": "the person wants only male carers for personal care",
    "time-critical medicine": "carers must give a medicine at a set time, within 30 minutes",
    "insulin by carers": "carers give the person's insulin, a task a nurse delegated to them",
    "two carers with a hoist": "the person is moved with a hoist, so visits need two carers",
}
CLAIMS = tuple(MEANINGS)


def profile(claims: list[str]) -> dict[str, Any]:
    """The client profile the claims state. Both genders claimed is a conflict, left as None for
    a person to settle (`conflicts`)."""
    genders = [GENDER_CLAIMS[claim] for claim in claims if claim in GENDER_CLAIMS]
    result: dict[str, Any] = {
        "needs": [need for claim, need in NEED_CLAIMS.items() if claim in claims],
        "slots": [slot for claim, slot in VISIT_CLAIMS.items() if claim in claims],
        "gender_preference": genders[0] if len(set(genders)) == 1 else None,
    }
    for claim, flag in FLAG_CLAIMS.items():
        result[flag] = claim in claims
    return result


def conflicts(claims: list[str]) -> list[str]:
    """Fields the claims contradict themselves on."""
    if len({claim for claim in claims if claim in GENDER_CLAIMS}) > 1:
        return ["gender_preference"]
    return []


def claims_of(client: dict[str, Any]) -> list[str]:
    """The claims a profile states, in CLAIMS order: the reverse of `profile`."""
    found = []
    for claim, need in NEED_CLAIMS.items():
        if need in client["needs"]:
            found.append(claim)
    for claim, slot in VISIT_CLAIMS.items():
        if slot in client["slots"]:
            found.append(claim)
    for claim, gender in GENDER_CLAIMS.items():
        if client.get("gender_preference") == gender:
            found.append(claim)
    for claim, flag in FLAG_CLAIMS.items():
        if client.get(flag):
            found.append(claim)
    return found
