"""How many forms one visitor's address may send: a few at a time, and a set number in each
window of time. A form that arrives slowly holds a place while it arrives, so one address cannot
take every place, and one address cannot fill the demo's storage for everyone else. The OWASP API
Security Top 10 (2023, API4) asks for limits on how often one client can run one operation.

The counts live in memory only, are never logged or stored, and start again in each window.
Plain functions on a plain dict, called from the server's one event loop, so no lock is needed.
"""

from typing import Any

WINDOW = "window"  # refused: this address has sent its forms for this window
AT_ONCE = "at once"  # refused: this address already has its forms arriving


def new() -> dict[str, Any]:
    return {"started": 0.0, "sent": {}, "arriving": {}}


def admit(
    state: dict[str, Any], address: str, now: float, per_window: int, window: float, at_once: int
) -> str:
    """ "ok" and the form is counted, or why it is refused: WINDOW or AT_ONCE."""
    if now - state["started"] >= window:
        state["started"] = now
        state["sent"] = {}
    if state["arriving"].get(address, 0) >= at_once:
        return AT_ONCE
    if state["sent"].get(address, 0) >= per_window:
        return WINDOW
    state["sent"][address] = state["sent"].get(address, 0) + 1
    state["arriving"][address] = state["arriving"].get(address, 0) + 1
    return "ok"


def arrived(state: dict[str, Any], address: str) -> None:
    """The address's form has arrived, or been refused: its place is free again."""
    left = state["arriving"][address] - 1
    if left:
        state["arriving"][address] = left
    else:
        del state["arriving"][address]


def seconds_left(state: dict[str, Any], now: float, window: float) -> int:
    """Whole seconds until this window ends, at least one."""
    return max(1, int(state["started"] + window - now) + 1)
