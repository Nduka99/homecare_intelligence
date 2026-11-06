"""What goes to the safeguarding lead's review queue, decided in code: every urgent concern the
model finds reaches a person.

- Every note with an "act today" or "act this visit" finding is queued.
- A note PIGuard tags as possible tampering is queued too; the tag can only add.
- A random share of the notes with no finding at all is queued too, so the lead's checks
  estimate how many concerns the model misses in use. The draw depends only on the note's ID and
  a seed, never on the note's text, so nothing written in a note can keep it out.
- Pattern rule (statutory guidance 14.18, "look beyond single incidents"): repeated missed tasks
  for one client within a window are queued as a possible neglect pattern. The count and window
  are starting values, not yet researched.
"""

import datetime
import hashlib
from typing import Any

from homecare.notes.categories import ACT_THIS_VISIT, ACT_TODAY

SAMPLE_SHARE = 0.07  # the plan's "5 to 10%"
FINDING = "finding"
RANDOM_CHECK = "random check of a note with no finding"
TAMPERING = "possible tampering"
PATTERN_COUNT = 3
PATTERN_DAYS = 7


def sampled(note_id: str, seed: str) -> bool:
    """Whether a no-finding note is drawn for review: a fixed draw from its ID and the seed."""
    draw = int(hashlib.sha256(f"{seed}:{note_id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return draw < SAMPLE_SHARE


def review_queue(results: list[dict[str, Any]], seed: str) -> list[dict[str, Any]]:
    """The notes to review, in the order to review them, each with why it is there.

    `results` holds each note's ID, its verified findings and, optionally, its tamper tag,
    in arrival order. The order is: notes with an "act today" finding, then "act this visit",
    then random checks, then notes queued only for possible tampering, each in arrival order.
    The tag only adds: a reason to a queued note, or a note nothing else would queue.
    """
    groups: list[list[dict[str, Any]]] = [[], [], [], []]
    for result in results:
        bands = {finding["band"] for finding in result["findings"]}
        group = None
        reasons = []
        if ACT_TODAY in bands:
            group, reasons = 0, [FINDING]
        elif ACT_THIS_VISIT in bands:
            group, reasons = 1, [FINDING]
        elif not result["findings"] and sampled(result["id"], seed):
            group, reasons = 2, [RANDOM_CHECK]
        if result.get("tamper"):
            reasons.append(TAMPERING)
            if group is None:
                group = 3
        if group is not None:
            groups[group].append({"note": result["id"], "reasons": reasons})
    queue = []
    for group_items in groups:
        queue.extend(group_items)
    return queue


def neglect_patterns(missed: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Clients with at least PATTERN_COUNT missed tasks within PATTERN_DAYS days; `missed`
    holds each missed-task finding's client and ISO date."""
    by_client: dict[str, list[datetime.date]] = {}
    for item in missed:
        by_client.setdefault(item["client"], []).append(datetime.date.fromisoformat(item["date"]))
    patterns = []
    window = datetime.timedelta(days=PATTERN_DAYS - 1)
    for client, dates in sorted(by_client.items()):
        dates.sort()
        for index in range(len(dates) - PATTERN_COUNT + 1):
            if dates[index + PATTERN_COUNT - 1] - dates[index] <= window:
                run = [d.isoformat() for d in dates[index : index + PATTERN_COUNT]]
                patterns.append({"client": client, "dates": run})
                break
    return patterns
