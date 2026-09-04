"""A tool and a retry helper, with no sleeps, no network and no model calls.

``flaky_backend`` is the tool a scenario wraps. ``retry_until_ok`` is the
subject under test: the retry loop whose behaviour we want to exercise.
"""

from __future__ import annotations

MAX_ATTEMPTS = 5
BACKOFF_MS = 100


def flaky_backend(clock=None):
    """The tool. On its own it always succeeds; faults come from the scenario."""
    return "ok"


def retry_until_ok(clock=None):
    """Call the backend up to MAX_ATTEMPTS times, retrying transient failures.

    Backoff is accounted for on the injectable clock. Nothing here sleeps.
    """
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return flaky_backend(clock=clock)
        except (TimeoutError, ConnectionError) as exc:
            last_error = exc
            if clock is not None and attempt < MAX_ATTEMPTS:
                clock.advance_ms(BACKOFF_MS * attempt)
    raise RuntimeError(f"backend failed after {MAX_ATTEMPTS} attempts: {last_error}")
