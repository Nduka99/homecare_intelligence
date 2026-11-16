"""Matching a confirmed referral to carers, with the agency's own rules unchanged.

Only a profile a person confirmed is matched: the reader's draft is a suggestion, and a model's
claim never decides who may visit on its own.
"""

from typing import Any

from homecare.referrals import fields
from homecare.world import agency


def refusals(draft: dict[str, Any]) -> list[str]:
    """Why the draft cannot be matched yet; empty when it can."""
    found = []
    if not draft.get("confirmed_by"):
        found.append("no person has confirmed the profile (set confirmed_by)")
    if draft.get("conflicts"):
        found.append(f"settle the conflicting fields first: {', '.join(draft['conflicts'])}")
    client = draft.get("profile", {})
    if not client.get("slots"):
        found.append("the profile has no visits")
    for slot in client.get("slots", []):
        if slot not in fields.VISITS:
            found.append(f"unknown visit: {slot}")
    for need in client.get("needs", []):
        if need not in fields.NEEDS:
            found.append(f"unknown need: {need}")
    return found


def visits(client: dict[str, Any], carers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each visit's rules, from `agency.visit_rules`, and the carers `agency.can_do` allows."""
    result = []
    for slot in client["slots"]:
        rules = agency.visit_rules(client, slot)
        able = [carer["id"] for carer in carers if agency.can_do(carer, rules)]
        result.append({"slot": slot, **rules, "carers": able})
    return result
