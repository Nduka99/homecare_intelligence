"""Referral scores against answers worked out by hand, and the gate's positive control: a perfect
run scored against shuffled keys must fail the gate."""

from typing import Any

from homecare.evaluation import referrals as scores
from homecare.evaluation.metrics import mcnemar_exact
from homecare.referrals import fields


def referral(number: int, claims: list[str]) -> dict[str, Any]:
    return {"id": f"r{number}", "claims": claims, "client": fields.profile(claims)}


def run(claims: list[str], read: str = "both", dropped: int = 0) -> dict[str, Any]:
    found = [{"claim": claim, "quote": "q", "read": read} for claim in claims]
    lost = [{"claim": "need: dementia", "quote": "not there"}] * dropped
    return {"claims": found, "dropped": lost}


BASE = ["need: personal care", "visit: morning"]
REFERRALS = [
    referral(1, [*BASE, "insulin by carers"]),
    referral(2, [*BASE, "insulin by carers"]),
    referral(3, BASE),
    referral(4, [*BASE, "carer gender: female"]),
]


def test_recall_precision_and_corrections_match_hand_counts() -> None:
    runs = {
        "r1": run([*BASE, "insulin by carers"]),
        "r2": run(BASE),  # missed insulin
        "r3": run([*BASE, "insulin by carers"], read="referral"),  # false insulin
        "r4": run([*BASE, "carer gender: female"], dropped=2),
    }
    report = scores.score(REFERRALS, runs)
    assert report["recall"]["insulin by carers"]["found"] == 1
    assert report["recall"]["insulin by carers"]["total"] == 2
    assert report["precision"]["insulin by carers"]["found"] == 1
    assert report["precision"]["insulin by carers"]["total"] == 2
    assert report["complete"]["found"] == 3 and report["clean"]["found"] == 3
    assert report["corrections_per_referral"] == 0.5
    assert report["missed"] == [{"id": "r2", "claim": "insulin by carers"}]
    assert report["false"] == [{"id": "r3", "claim": "insulin by carers"}]
    assert report["quote_drops"]["found"] == 2 and report["quote_drops"]["total"] == 13
    # 10 true claims were found (3 + 2 + 2 + 3); r3's 2 came from the whole-referral read only.
    assert report["true_claims_needing_whole_referral"]["found"] == 2
    assert report["true_claims_needing_whole_referral"]["total"] == 10


# Every claim appears, so the gate can measure each one.
EVERY_CLAIM = [
    referral(1, [claim for claim in fields.CLAIMS if claim != "carer gender: male"]),
    referral(2, [*BASE, "carer gender: male"]),
    referral(3, BASE),
]


def perfect() -> dict[str, dict[str, Any]]:
    return {r["id"]: run(r["claims"]) for r in EVERY_CLAIM}


def test_a_perfect_run_passes_and_one_miss_fails_the_gate() -> None:
    assert scores.gate(scores.score(EVERY_CLAIM, perfect())) == []
    without_insulin = [c for c in EVERY_CLAIM[0]["claims"] if c != "insulin by carers"]
    runs = perfect() | {"r1": run(without_insulin)}
    assert scores.gate(scores.score(EVERY_CLAIM, runs)) == ["recall insulin by carers: 0.00"]


def test_a_claim_no_referral_has_fails_the_gate_as_not_measured() -> None:
    failures = scores.gate(scores.score(REFERRALS, {r["id"]: run(r["claims"]) for r in REFERRALS}))
    assert "recall need: dementia: not measured, no referral has it" in failures


def test_the_control_a_perfect_run_against_shuffled_keys_fails_the_gate() -> None:
    shuffled = scores.shuffled(EVERY_CLAIM, seed=1)
    assert scores.gate(scores.score(shuffled, perfect()))


def test_shuffled_keys_are_a_permutation_with_no_referral_keeping_its_own() -> None:
    many = [referral(n, [*BASE, fields.CLAIMS[n % len(fields.CLAIMS)]]) for n in range(30)]
    shuffled = scores.shuffled(many, seed=7)
    assert [r["id"] for r in shuffled] == [r["id"] for r in many]
    assert sorted(map(str, (r["claims"] for r in shuffled))) == sorted(
        map(str, (r["claims"] for r in many))
    )
    for item, moved in zip(many, shuffled, strict=True):
        assert moved["claims"] is not item["claims"]


def test_without_the_quote_check_dropped_claims_count_again() -> None:
    runs = {"r3": run(BASE, dropped=1)}
    unchecked = scores.with_dropped(runs)
    assert scores.found(unchecked["r3"]) == {*BASE, "need: dementia"}
    assert unchecked["r3"]["dropped"] == []


def test_matching_agreement_counts_visits_with_the_same_allowed_carers() -> None:
    carers = [
        {"id": "K01", "skills": ["personal care", "insulin"], "gender": "female"},
        {"id": "K02", "skills": ["personal care"], "gender": "male"},
    ]
    two_visits = [*BASE, "visit: bed", "insulin by carers"]
    referrals = [referral(1, two_visits)]
    same = scores.matching_agreement(referrals, {"r1": run(two_visits)}, carers)
    assert (same["found"], same["total"]) == (2, 2)
    # Without the insulin claim, K02 is wrongly allowed on the morning visit; bedtime agrees.
    missed = scores.matching_agreement(referrals, {"r1": run([*BASE, "visit: bed"])}, carers)
    assert (missed["found"], missed["total"]) == (1, 2)


def test_harmful_visits_count_a_carer_the_key_would_not_allow() -> None:
    carers = [
        {"id": "K01", "skills": ["personal care", "insulin"], "gender": "female"},
        {"id": "K02", "skills": ["personal care"], "gender": "male"},
    ]
    two_visits = [*BASE, "visit: bed", "insulin by carers"]
    referrals = [referral(1, two_visits)]
    # Without the insulin claim K02, untrained, is allowed on the morning visit.
    harm = scores.harmful_visits(referrals, {"r1": run([*BASE, "visit: bed"])}, carers)
    assert (harm["found"], harm["total"], harm["visits"]) == (1, 2, ["r1 morning"])
    # A missed visit is missing, not harmful.
    harm = scores.harmful_visits(referrals, {"r1": run(BASE + ["insulin by carers"])}, carers)
    assert harm["found"] == 0


def test_the_exact_mcnemar_p_value_matches_the_binomial_test() -> None:
    # Two-sided binomial test at one half: 6 of 6 gives 2/64, 9 of 10 gives 2 * 11/1024.
    assert mcnemar_exact(6, 0) == 0.03125
    assert mcnemar_exact(0, 5) == 0.0625
    assert mcnemar_exact(9, 1) == 0.021484375
    assert mcnemar_exact(3, 3) == 1.0
    assert mcnemar_exact(0, 0) == 1.0


def test_paired_counts_fixes_breaks_and_errors_per_referral_and_claim() -> None:
    before = {
        "r1": run(BASE),  # misses insulin
        "r2": run(BASE),  # misses insulin
        "r3": run(BASE),
        "r4": run([*BASE, "carer gender: female"]),
    }
    after = {
        "r1": run([*BASE, "insulin by carers"]),
        "r2": run([*BASE, "insulin by carers"]),
        "r3": run([*BASE, "insulin by carers"]),  # a new false claim
        "r4": run([*BASE, "carer gender: female"]),
    }
    pair = scores.paired(REFERRALS, before, after, ("insulin by carers", "visit: morning"))
    assert pair["instances"] == 8
    assert (pair["errors_before"], pair["errors_after"]) == (2, 1)
    assert (pair["fixes"], pair["breaks"]) == (2, 1)
    assert pair["p"] == 1.0


def test_exact_only_keeps_claims_some_read_quoted_word_for_word() -> None:
    claims = [
        {"claim": "visit: morning", "quote": "q", "read": "both", "match": "exact"},
        {"claim": "insulin by carers", "quote": "q", "read": "sentence", "match": "spelling"},
        {"claim": "need: personal care", "quote": "q", "read": "both"},  # a first-version run
    ]
    runs = {"r1": {"claims": claims, "dropped": []}}
    assert scores.found(scores.exact_only(runs)["r1"]) == {"visit: morning", "need: personal care"}
    assert scores.spelling_matches(REFERRALS[:1], runs) == [
        {"id": "r1", "claim": "insulin by carers", "true": True}
    ]
