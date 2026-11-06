"""The continuity sweep: how much extra driving it is worth to keep high-need clients with their
usual carer. The rule counts avoidable breaks only, those on days the usual carer works.

Plain data and arithmetic only: the CLI runs the solver and passes the week totals in here.
"""

from typing import Any

BASE = 15  # minutes of driving one visit by a stranger is worth (an assumption, not a measurement)
MULTIPLIERS = [1, 2, 4, 8]
# Chosen from the sweep on two synthetic agencies: their best values, x2 and x4, differ by no more
# than run-to-run noise, and x2 drives less.
HIGH_NEED = 2
WEEK = 7
# (continuity, high_need) for each named row; "no continuity" is the reference.
SETTINGS = {"no continuity": (0, 1)} | {f"high need x{m}": (BASE, m) for m in MULTIPLIERS}
MEASURES = [
    "covered",
    "uncovered",
    "driving_minutes",
    "continuity_breaks",
    "high_need_breaks",
    "avoidable_breaks",
    "avoidable_high_need_breaks",
]


def totals(results: list[dict[str, Any]]) -> dict[str, int]:
    """A week's totals from each day's checker result; problems are counted, never hidden."""
    found = {measure: 0 for measure in MEASURES + ["problems"]}
    for result in results:
        for measure in MEASURES:
            found[measure] += result[measure]
        found["problems"] += len(result["problems"])
    return found


def knee(high_need_breaks: dict[int, int]) -> int:
    """The smallest multiplier m for which doubling it cuts high-need breaks by less than a
    quarter; the largest multiplier if every doubling cuts more."""
    for m in sorted(high_need_breaks):
        if 2 * m not in high_need_breaks:
            continue
        before = high_need_breaks[m]
        after = high_need_breaks[2 * m]
        if before == 0 or (before - after) / before < 0.25:
            return m
    return max(high_need_breaks)
