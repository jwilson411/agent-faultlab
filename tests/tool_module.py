"""A tool plus a subject that always calls it five times, for index assertions."""

from __future__ import annotations

REAL_CALLS: list[int] = []
SEEN_CLOCK_MS: list[int | None] = []


class CustomError(Exception):
    """Used to exercise 'module:Name' exception resolution."""


def tool(clock=None):
    REAL_CALLS.append(len(REAL_CALLS) + 1)
    SEEN_CLOCK_MS.append(clock.now_ms() if clock is not None else None)
    return "real"


def call_five(clock=None):
    """Call the tool five times, recording outcomes instead of propagating."""
    outcomes = []
    for _ in range(5):
        try:
            outcomes.append(tool(clock=clock))
        except Exception as exc:  # noqa: BLE001 - outcomes are the assertion surface
            outcomes.append(type(exc).__name__)
    return outcomes


def call_once(clock=None):
    return tool(clock=clock)
