"""Proportions with honest intervals. `wilson` is copied from PetroKnowledge
`evaluation/metrics.py` (MIT, same author)."""

import math


def wilson(found: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for found/total (95% by default). It stays inside [0, 1] and
    behaves at small counts and at 0% or 100%, where the normal approximation does not."""
    if total == 0:
        return (0.0, 0.0)
    p = found / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def proportion(found: int, total: int) -> dict[str, float | int]:
    low, high = wilson(found, total)
    rate = found / total if total else 0.0
    return {"found": found, "total": total, "rate": rate, "low": low, "high": high}
