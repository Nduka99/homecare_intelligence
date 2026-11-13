"""Grouping near-duplicate findings into one decision item (the gate, written before the code).

The whole-note read and the sentence reads often return the same concern with overlapping
quotes, so the lead decided each copy: 292 urgent items on 94 urgent notes in the demo week,
with 57 same-category pairs where one quote contains the other (measured 11 Nov, before any
code). The gate on the committed demo week, fixed first:
- no (note, category) pair is lost;
- urgent items fall by at least those 57 pairs;
- every member quote is still found word for word in its note;
- a decision on a group labels every member.
With grouping switched off, the same item-count rule must fail.
"""

import re
from pathlib import Path

from homecare.app import db, screens, seed
from homecare.notes.categories import ACT_THIS_VISIT, ACT_TODAY
from homecare.notes.quotes import find_quote
from test_screens import csrf_of, first_queued, make, sign_in

URGENT = {ACT_TODAY, ACT_THIS_VISIT}
URGENT_ITEMS_BEFORE = 292
CONTAINED_PAIRS = 57


def test_grouping_keeps_every_category_and_quote_and_cuts_urgent_items() -> None:
    demo = seed.load()
    before = 0
    after = 0
    for note in demo["notes"]:
        findings = [dict(f, id=i, found_by=f["read"]) for i, f in enumerate(note["findings"])]
        groups = screens.group_findings(findings)
        assert {(f["category"]) for f in findings} == {g["category"] for g in groups}
        for group in groups:
            assert len({m["category"] for m in group["members"]}) == 1
            for member in group["members"]:
                assert find_quote(member["quote"], note["text"]) is not None
            assert group["quote"] == max((m["quote"] for m in group["members"]), key=len)
        before += sum(1 for f in findings if f["band"] in URGENT)
        after += sum(1 for g in groups if g["band"] in URGENT)
    assert before == URGENT_ITEMS_BEFORE
    assert after <= URGENT_ITEMS_BEFORE - CONTAINED_PAIRS


def test_unrelated_findings_are_never_grouped() -> None:
    findings = [
        {"id": 1, "category": "neglect", "band": "act today", "quote": "no food in the fridge",
         "reason": "", "found_by": "note"},
        {"id": 2, "category": "neglect", "band": "act today", "quote": "soiled bedding",
         "reason": "", "found_by": "sentence"},
        {"id": 3, "category": "physical abuse", "band": "act today",
         "quote": "no food in the fridge", "reason": "", "found_by": "sentence"},
    ]  # fmt: skip
    assert [len(g["members"]) for g in screens.group_findings(findings)] == [1, 1, 1]


def test_a_decision_on_a_group_labels_every_member(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    connection = db.connect(tmp_path / "demo.db")
    grouped = None
    for item in screens.day_queue(connection, 27, None):
        detail = screens.note_detail(connection, item["note"], None)
        if any(len(g["members"]) > 1 for g in detail["findings"]):
            grouped = detail
            break
    assert grouped is not None, "the demo week has a note with a grouped finding"
    group = next(g for g in grouped["findings"] if len(g["members"]) > 1)
    sign_in(client, "lead")
    page = client.get(f"/notes/{grouped['id']}").text
    assert len(re.findall(r'name="finding-\d+"', page)) // 3 == len(grouped["findings"])
    fields = {"csrf": csrf_of(page), f"finding-{group['id']}": "confirmed"}
    client.post(f"/notes/{grouped['id']}/decisions", data=fields)
    labelled = {row["finding"] for row in connection.execute("SELECT finding FROM decisions")}
    assert labelled == {m["id"] for m in group["members"]}


def test_first_queued_still_opens(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    sign_in(client, "lead")
    assert client.get(f"/notes/{first_queued(tmp_path)['id']}").status_code == 200
