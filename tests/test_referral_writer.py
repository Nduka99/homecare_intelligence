"""The synthetic referrals and their answer keys. The key is the profile a spec was drawn from;
these tests check that the facts given to the writer state exactly that profile."""

import random
from typing import Any

import pytest

from homecare import llm
from homecare.referrals import fields
from homecare.world import referral_writer as rw


def facts_by_id(pool: str) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    facts, topics = rw.load(pool)
    return {fact["id"]: fact for fact in facts}, topics


def joined_facts(spec: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    return " ".join(rw.filled(by_id[fact_id]["text"], spec) for fact_id in spec["facts"])


def test_specs_are_the_same_on_every_run() -> None:
    assert rw.specs("referrals-dev") == rw.specs("referrals-dev")
    # A plan given outright gives the same specs as the registered one.
    assert rw.specs("referrals-dev", rw.SETS["referrals-dev"]) == rw.specs("referrals-dev")
    demo = rw.specs("referrals-demo", rw.DEMO)
    assert [spec["id"] for spec in demo] == [f"referrals-demo-00{n}" for n in range(1, 7)]


def test_the_held_out_set_shares_no_fact_persona_or_writer_with_dev() -> None:
    dev = rw.specs("referrals-dev")
    held_out = rw.specs("referrals-heldout-2")
    assert not {f for s in held_out for f in s["facts"]} & {f for s in dev for f in s["facts"]}
    assert not {s["persona"] for s in held_out} & {s["persona"] for s in dev}
    assert rw.SETS["referrals-heldout-2"]["writer"] != rw.SETS["referrals-dev"]["writer"]


def test_the_fresh_set_shares_no_fact_persona_or_writer_with_either_earlier_set() -> None:
    fresh = rw.specs("referrals-fresh")
    for name in ("referrals-dev", "referrals-heldout-2"):
        earlier = rw.specs(name)
        assert not {f for s in fresh for f in s["facts"]} & {f for s in earlier for f in s["facts"]}
        assert not {s["persona"] for s in fresh} & {s["persona"] for s in earlier}
        assert rw.SETS["referrals-fresh"]["writer"] != rw.SETS[name]["writer"]


def test_the_second_fresh_set_shares_no_fact_or_persona_with_any_earlier_set() -> None:
    """Its writer repeats held-out's (no other family is on disk), so its words must be new."""
    fresh_2 = rw.specs("referrals-fresh-2")
    for name in ("referrals-dev", "referrals-heldout-2", "referrals-fresh"):
        earlier = rw.specs(name)
        assert not {f for s in fresh_2 for f in s["facts"]} & {
            f for s in earlier for f in s["facts"]
        }
        assert not {s["persona"] for s in fresh_2} & {s["persona"] for s in earlier}
    assert rw.SETS["referrals-fresh-2"]["writer"] != rw.SETS["referrals-fresh"]["writer"]


def test_every_spec_s_own_facts_pass_the_checks() -> None:
    """A spec whose facts break their own checks could never be written, or would carry a key the
    text contradicts. Length is the model's to meet, so it is padded here."""
    for name, plan in rw.SETS.items():
        by_id, topics = facts_by_id(plan["pool"])
        for spec in rw.specs(name):
            text = joined_facts(spec, by_id) + " and so on" * 20
            assert rw.problems(text, spec, by_id, topics) == [], spec["id"]


def test_the_facts_state_exactly_the_key() -> None:
    for name, plan in rw.SETS.items():
        by_id, _ = facts_by_id(plan["pool"])
        for spec in rw.specs(name):
            stated = set()
            for fact_id in spec["facts"]:
                if by_id[fact_id]["kind"] == "states":
                    stated |= set(by_id[fact_id]["claims"])
            assert sorted(stated) == sorted(spec["claims"]), spec["id"]
            assert spec["claims"] == fields.claims_of(spec["client"])


def test_every_fact_names_known_claims_and_every_claim_can_be_stated() -> None:
    for pool in ("A", "B", "C", "D"):
        facts, topics = rw.load(pool)
        assert sorted(topics) == sorted(fields.CLAIMS)
        stated = set()
        for fact in facts:
            assert set(fact["claims"]) <= set(fields.CLAIMS)
            assert fact["must"], fact["id"]
            if fact["kind"] == "states":
                stated |= set(fact["claims"])
        assert stated == set(fields.CLAIMS)


def test_profiles_follow_the_coherence_rules() -> None:
    rng = random.Random(1)
    for _ in range(2000):
        client = rw.draw_profile(rng)
        if client["time_critical"]:
            assert "medication" in client["needs"]
        if client["gender_preference"] or client["hoist"]:
            assert "personal care" in client["needs"]
        assert client["slots"][0] == "morning"


def test_a_referral_that_lost_evidence_or_adds_a_need_is_rejected() -> None:
    by_id, topics = facts_by_id("A")
    spec = next(s for s in rw.specs("referrals-dev") if "insulin by carers" not in s["claims"])
    # With no distractor about insulin, the key's "no insulin" must hold in the text.
    kept = [f for f in spec["facts"] if "insulin by carers" not in by_id[f]["claims"]]
    spec = {**spec, "facts": kept}
    good = joined_facts(spec, by_id) + " and so on" * 20
    assert rw.problems(good, spec, by_id, topics) == []
    added = good + " Carers will also give her insulin."
    assert "mentions insulin by carers: \\binsulin\\b" in rw.problems(added, spec, by_id, topics)
    first = by_id[spec["facts"][0]]
    lost = rw.problems("nothing here " * 30, spec, by_id, topics)
    assert f"evidence lost: {first['id']} {first['must'][0]}" in lost


def test_a_cut_off_reply_is_one_failed_attempt_not_the_end_of_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_id, topics = facts_by_id("A")
    spec = rw.specs("referrals-dev")[0]
    good = joined_facts(spec, by_id) + " and so on" * 20
    replies: list[Any] = [llm.ModelServerError("the model's reply did not finish (length)")]
    replies.append({"referral": good})

    def chat(*args: Any, **kwargs: Any) -> Any:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(llm, "chat", chat)
    written = rw.write_referral("http://model", spec, by_id, topics, perturb=False)
    assert written["text"] == good and written["attempts"] == 2

    def always_cut(*args: Any, **kwargs: Any) -> Any:
        raise llm.ModelServerError("the model's reply did not finish (length)")

    monkeypatch.setattr(llm, "chat", always_cut)
    failed = rw.write_referral("http://model", spec, by_id, topics, perturb=False)
    assert failed["text"] is None and failed["failed"][0].startswith("model error")


def test_a_short_or_long_referral_is_rejected() -> None:
    by_id, topics = facts_by_id("A")
    spec = rw.specs("referrals-dev")[0]
    text = joined_facts(spec, by_id)
    assert any(p.startswith("length") for p in rw.problems(text[:60], spec, by_id, topics))
    long_text = text + " word" * rw.MAX_WORDS
    assert any(p.startswith("length") for p in rw.problems(long_text, spec, by_id, topics))
