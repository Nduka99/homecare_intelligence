"""Scores for one reader run on one set of synthetic referrals.

A referral's answer key is the profile it was written from (`claims`), never the reader's output.
Each measure is per referral, with a Wilson 95% interval:
- each claim's recall: of the referrals whose key has it, the share the run claimed it for;
- each claim's precision: of the referrals the run claimed it for, the share whose key has it;
- complete: referrals with every key claim found; clean: referrals with no false claim;
- corrections per referral: false plus missed claims, the person's work when confirming;
- quote drops: claims dropped because their quote is not in the referral;
- claims only the whole-referral read found, which an injection anywhere in it could remove;
- harmful visits: visits where the read profile allows a carer the key's profile does not;
- for two runs over one set, the claims the second fixed and broke, with an exact McNemar test.
Model errors count as "no claims", which can only lower recall.

The gate was fixed before any run: every claim's recall at least RECALL_GATE and precision at
least PRECISION_GATE. "visit: morning" is in every key, so its precision cannot fail. The
positive control scores the same run against shuffled keys, and must fail the gate.
"""

import random
from typing import Any

from homecare.evaluation.metrics import mcnemar_exact, proportion
from homecare.referrals import fields, match

RECALL_GATE = 0.95
PRECISION_GATE = 0.85


def found(run: dict[str, Any]) -> set[str]:
    """The claims of a run's verified reads."""
    return {claim["claim"] for claim in run.get("claims", [])}


def score(referrals: list[dict[str, Any]], runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    recall = {}
    precision = {}
    for claim in fields.CLAIMS:
        positives = [r for r in referrals if claim in r["claims"]]
        caught = [r for r in positives if claim in found(runs[r["id"]])]
        recall[claim] = proportion(len(caught), len(positives))
        flagged = [r for r in referrals if claim in found(runs[r["id"]])]
        right = [r for r in flagged if claim in r["claims"]]
        precision[claim] = proportion(len(right), len(flagged))

    missed = []
    false = []
    complete = 0
    clean = 0
    corrections = 0
    for referral in referrals:
        key = set(referral["claims"])
        got = found(runs[referral["id"]])
        for claim in sorted(key - got):
            missed.append({"id": referral["id"], "claim": claim})
        for claim in sorted(got - key):
            false.append({"id": referral["id"], "claim": claim})
        complete += key <= got
        clean += got <= key
        corrections += len(key ^ got)

    true_found = 0
    whole_only = 0
    kept = 0
    dropped = 0
    for referral in referrals:
        run = runs[referral["id"]]
        kept += len(run.get("claims", []))
        dropped += len(run.get("dropped", []))
        for claim in run.get("claims", []):
            if claim["claim"] in referral["claims"]:
                true_found += 1
                whole_only += claim["read"] == "referral"
    count = len(referrals)
    return {
        "referrals": count,
        "recall": recall,
        "precision": precision,
        "complete": proportion(complete, count),
        "clean": proportion(clean, count),
        "corrections_per_referral": round(corrections / count, 2) if count else 0.0,
        "quote_drops": proportion(dropped, kept + dropped),
        "true_claims_needing_whole_referral": proportion(whole_only, true_found),
        "errors": [r["id"] for r in referrals if "error" in runs[r["id"]]],
        "missed": missed,
        "false": false,
    }


def gate(report: dict[str, Any]) -> list[str]:
    """Every claim that fails the gate, with its measure; empty when the gate passes. A claim no
    referral has cannot be measured, which fails; a claim the run never made has no precision."""
    failures = []
    for claim in fields.CLAIMS:
        recall = report["recall"][claim]
        if recall["total"] == 0:
            failures.append(f"recall {claim}: not measured, no referral has it")
        elif recall["rate"] < RECALL_GATE:
            failures.append(f"recall {claim}: {recall['rate']:.2f}")
        precision = report["precision"][claim]
        if precision["total"] and precision["rate"] < PRECISION_GATE:
            failures.append(f"precision {claim}: {precision['rate']:.2f}")
    return failures


def shuffled(referrals: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    """The referrals with their answer keys moved one place round a seeded random order, so no
    referral keeps its own key."""
    order = list(range(len(referrals)))
    random.Random(seed).shuffle(order)
    result = list(referrals)
    for place, index in enumerate(order):
        donor = referrals[order[(place + 1) % len(order)]]
        result[index] = {**referrals[index], "claims": donor["claims"], "client": donor["client"]}
    return result


def with_dropped(runs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The runs as they would be without the quote check: every dropped claim kept."""
    result = {}
    for name, run in runs.items():
        extra = [{**claim, "read": "dropped"} for claim in run.get("dropped", [])]
        result[name] = {**run, "claims": run.get("claims", []) + extra, "dropped": []}
    return result


def exact_only(runs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The runs as the exact quote check alone would leave them: claims only the spelling-tolerant
    match found are taken out. A claim that any read quoted exactly keeps its exact quote."""
    result = {}
    for name, run in runs.items():
        claims = [c for c in run.get("claims", []) if c.get("match", "exact") == "exact"]
        result[name] = {**run, "claims": claims}
    return result


def spelling_matches(
    referrals: list[dict[str, Any]], runs: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every claim only the spelling-tolerant match found, and whether the key has it. The quotes
    stay in the run file, to be read beside their referrals."""
    found_here = []
    for referral in referrals:
        for claim in runs[referral["id"]].get("claims", []):
            if claim.get("match") == "spelling":
                true = claim["claim"] in referral["claims"]
                found_here.append({"id": referral["id"], "claim": claim["claim"], "true": true})
    return found_here


def visit_carers(
    referral: dict[str, Any], run: dict[str, Any], carers: list[dict[str, Any]]
) -> list[tuple[str, set[str], set[str] | None]]:
    """Each of the key's visits: its slot, the carers the key allows, and the carers the read
    profile allows (None when the read profile lacks that visit)."""
    key = {
        visit["slot"]: set(visit["carers"]) for visit in match.visits(referral["client"], carers)
    }
    read = fields.profile(sorted(found(run)))
    got = {visit["slot"]: set(visit["carers"]) for visit in match.visits(read, carers)}
    return [(slot, able, got.get(slot)) for slot, able in key.items()]


def matching_agreement(
    referrals: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    carers: list[dict[str, Any]],
) -> dict[str, float | int]:
    """Of the key's visits, the share where the read profile allows exactly the carers the key
    allows (a visit the read profile lacks counts as a disagreement)."""
    agree = 0
    total = 0
    for referral in referrals:
        for _, able, got in visit_carers(referral, runs[referral["id"]], carers):
            total += 1
            agree += got == able
    return proportion(agree, total)


def harmful_visits(
    referrals: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    carers: list[dict[str, Any]],
) -> dict[str, Any]:
    """The key's visits where the read profile allows a carer the key does not: one without a
    needed skill, or against the person's preference. This is the harm if a draft were confirmed
    unchanged; a visit the read profile lacks is missing, not harmful, and counted elsewhere."""
    harmful = []
    total = 0
    for referral in referrals:
        for slot, able, got in visit_carers(referral, runs[referral["id"]], carers):
            total += 1
            if got is not None and got - able:
                harmful.append(f"{referral['id']} {slot}")
    return {**proportion(len(harmful), total), "visits": harmful}


TARGETED = (*fields.VISIT_CLAIMS, "insulin by carers")


def paired(
    referrals: list[dict[str, Any]],
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    claims: tuple[str, ...],
) -> dict[str, Any]:
    """Two runs over the same referrals, compared on each referral and claim of `claims`. A run
    is right on one when it claims it exactly when the key has it. Counts each run's errors, the
    instances `after` fixed and broke, and the exact McNemar p-value on those two counts."""
    fixes = 0
    breaks = 0
    errors_before = 0
    errors_after = 0
    for referral in referrals:
        key = set(referral["claims"])
        got_before = found(before[referral["id"]])
        got_after = found(after[referral["id"]])
        for claim in claims:
            right_before = (claim in got_before) == (claim in key)
            right_after = (claim in got_after) == (claim in key)
            errors_before += not right_before
            errors_after += not right_after
            fixes += right_after and not right_before
            breaks += right_before and not right_after
    return {
        "instances": len(referrals) * len(claims),
        "errors_before": errors_before,
        "errors_after": errors_after,
        "fixes": fixes,
        "breaks": breaks,
        "p": mcnemar_exact(fixes, breaks),
    }
