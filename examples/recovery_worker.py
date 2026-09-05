"""A worker that can be crashed anywhere and still send its message once.

Run it under the recovery harness:

    faultlab recovery-run examples/recovery_crash_before_commit.yaml -- \\
        python examples/recovery_worker.py

The worker keeps two pieces of state in ``FAULTLAB_STATE_DIR``: an
idempotency ledger, and a checkpoint file written atomically once the side
effect has committed. It reports what it is doing as JSONL events on stdout,
one JSON object per line, flushed immediately so the harness can terminate the
process at an exact point.

Nothing here sleeps and nothing here talks to the network.
"""

from __future__ import annotations

import json
import os
import sys

from faultlab.idempotency import IdempotencyOracle, Ledger, SideEffectSink, fingerprint

OPERATION = "send_message"
KEY = "msg-recovery"
PAYLOAD = {"body": "the deploy finished", "channel": "#releases"}

CHECKPOINT_NAME = "checkpoint.json"
EXIT_OK = 0
EXIT_CORRUPT = 2


def emit(event: str, **fields: object) -> None:
    """Write one event line and flush, so the harness sees it immediately."""
    payload = dict(fields)
    payload["event"] = event
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)


def read_checkpoint(path: str) -> bool | None:
    """True committed, None absent. Anything else is corrupt and raises."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        raise ValueError("corrupt checkpoint") from None
    if not isinstance(document, dict) or document.get("committed") is not True:
        raise ValueError("corrupt checkpoint")
    return True


def write_checkpoint(path: str) -> None:
    """Write the checkpoint atomically: a temp file in the same directory, then replace."""
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"committed": True}, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def main() -> int:
    state_dir = os.environ.get("FAULTLAB_STATE_DIR")
    if not state_dir:
        emit("error", message="FAULTLAB_STATE_DIR is not set")
        return EXIT_CORRUPT
    os.makedirs(state_dir, exist_ok=True)
    ledger_path = os.environ.get("FAULTLAB_LEDGER") or os.path.join(state_dir, "ledger.sqlite")
    checkpoint = os.path.join(state_dir, CHECKPOINT_NAME)

    emit("checkpoint", name="begin")

    try:
        committed = read_checkpoint(checkpoint)
    except ValueError as exc:
        emit("error", message=str(exc))
        return EXIT_CORRUPT

    if committed:
        # The side effect already landed before the crash; do not send again.
        emit("checkpoint", name="commit")
        emit("done")
        return EXIT_OK

    emit("checkpoint", name="pre_commit")
    sink = SideEffectSink(name=OPERATION)
    with Ledger(ledger_path) as ledger:
        IdempotencyOracle(ledger).execute(OPERATION, KEY, PAYLOAD, sink.send, call=1)
    if len(sink):
        emit(
            "side_effect",
            operation=OPERATION,
            idempotency_key=KEY,
            payload_fingerprint=fingerprint(PAYLOAD),
        )

    write_checkpoint(checkpoint)
    emit("checkpoint", name="commit")
    emit("done")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
