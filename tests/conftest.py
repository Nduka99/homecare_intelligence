"""Hypothesis profiles for the route fuzzer. "ci", the default, is repeatable: the same inputs on
every run. "deep" tries ten times as many inputs, at random, and prints the seed of a failure:

    uv run pytest tests/test_fuzz_routes.py --hypothesis-profile=deep --hypothesis-show-statistics
"""

from hypothesis import HealthCheck, settings

settings.register_profile(
    "ci",
    max_examples=100,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "deep", max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile("ci")
