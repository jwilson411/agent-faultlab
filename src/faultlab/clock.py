"""Injectable fake clock.

The library never calls ``time.sleep`` and never reads the wall clock. Every
scenario starts at a fixed origin of 0 ms so reports are byte-identical across
runs and machines.
"""

from __future__ import annotations


class FakeClock:
    """A monotonic millisecond clock that only moves when told to."""

    __slots__ = ("_now_ms",)

    def __init__(self, start_ms: int = 0) -> None:
        if start_ms < 0:
            raise ValueError("start_ms must be non-negative")
        self._now_ms = int(start_ms)

    def now_ms(self) -> int:
        """Current fake time in milliseconds since the scenario origin."""
        return self._now_ms

    def advance_ms(self, delta_ms: float) -> int:
        """Advance the clock. Returns the new time."""
        if delta_ms < 0:
            raise ValueError("delta_ms must be non-negative")
        self._now_ms += int(delta_ms)
        return self._now_ms

    def sleep_ms(self, delta_ms: float) -> int:
        """Drop-in for a sleep in subject code: advances, never blocks."""
        return self.advance_ms(delta_ms)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FakeClock(now_ms={self._now_ms})"
