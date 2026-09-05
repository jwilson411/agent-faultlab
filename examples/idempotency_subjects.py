"""Two retry loops around the same synthetic side effect.

Both send the same message with the same idempotency key, and both meet the
same fault: the first attempt commits and then loses its response. The only
difference is which oracle entry point they retry through.

``naive_retry`` uses ``oracle.effect``, which always runs the side effect. Its
retry sends the message a second time, so the ledger records a
``duplicate_commit``.

``idempotent_retry`` uses ``oracle.execute``, which looks the key up first. Its
retry replays the stored result, so the side effect runs exactly once and the
lost response is recovered.
"""

from __future__ import annotations

from typing import Any, Sequence

from faultlab.idempotency import IdempotencyOracle, ResponseLost, SideEffectSink

OPERATION = "send_message"
KEY = "msg-2f1c"
PAYLOAD = {"body": "the build finished", "channel": "#releases"}

#: One drop on the first attempt, then a clean retry.
DROP_THEN_RETRY: tuple[str | None, ...] = ("drop", None)


def _retry(
    oracle: IdempotencyOracle,
    sink: SideEffectSink,
    entry: str,
    *,
    operation: str,
    key: str,
    payload: Any,
    faults: Sequence[str | None],
) -> Any:
    run = getattr(oracle, entry)
    last_error: ResponseLost | None = None
    for call, fault in enumerate(faults, start=1):
        try:
            return run(operation, key, payload, sink.send, call=call, fault=fault)
        except ResponseLost as exc:
            last_error = exc
    raise RuntimeError(f"{operation} gave up after {len(faults)} attempts") from last_error


def naive_retry(
    oracle: IdempotencyOracle,
    sink: SideEffectSink,
    *,
    operation: str = OPERATION,
    key: str = KEY,
    payload: Any = None,
    faults: Sequence[str | None] = DROP_THEN_RETRY,
) -> Any:
    """Retry by performing the side effect again. Duplicates on a lost response."""
    return _retry(
        oracle,
        sink,
        "effect",
        operation=operation,
        key=key,
        payload=PAYLOAD if payload is None else payload,
        faults=faults,
    )


def idempotent_retry(
    oracle: IdempotencyOracle,
    sink: SideEffectSink,
    *,
    operation: str = OPERATION,
    key: str = KEY,
    payload: Any = None,
    faults: Sequence[str | None] = DROP_THEN_RETRY,
) -> Any:
    """Retry through the key lookup. Replays the stored result instead."""
    return _retry(
        oracle,
        sink,
        "execute",
        operation=operation,
        key=key,
        payload=PAYLOAD if payload is None else payload,
        faults=faults,
    )


NAIVE = naive_retry
IDEMPOTENT = idempotent_retry
