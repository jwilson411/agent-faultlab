"""Worker stand-ins for the recovery harness tests.

Run as ``python tests/recovery_helpers.py MODE``. Each mode speaks the same
JSONL event protocol as ``examples/recovery_worker.py`` and then misbehaves in
one specific way, so the harness limits and failure paths have something to
catch.

No mode sleeps. ``block`` waits on a read from a stdin pipe the harness never
writes to, which is a blocking syscall rather than a timed wait, and ``flood``
writes as fast as the pipe drains.

Every mode writes its pid to ``$FAULTLAB_STATE_DIR/pid`` before doing anything
else, so a test can prove afterwards that the process group is gone.
"""

from __future__ import annotations

import json
import os
import sys

MODES = ("block", "flood", "clean", "noisy")

#: One line of padding for ``flood``; a few of these pass any sane byte limit.
PADDING = "x" * 4096


def emit(event: str, **fields: object) -> None:
    payload = dict(fields)
    payload["event"] = event
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)


def record_pid() -> None:
    state_dir = os.environ.get("FAULTLAB_STATE_DIR")
    if not state_dir:
        return
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "pid"), "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "clean"
    if mode not in MODES:
        emit("error", message=f"unknown mode {mode!r}")
        return 2

    record_pid()
    emit("checkpoint", name="begin")

    if mode == "block":
        # Nothing ever answers, so only the harness runtime limit ends this.
        sys.stdin.readline()
        emit("done")
        return 0

    if mode == "flood":
        try:
            while True:
                print(PADDING, flush=True)
        except BrokenPipeError:  # pragma: no cover - the harness kills first
            return 0

    if mode == "noisy":
        # Not events, but still bytes the harness must count.
        print("plain text, not JSON", flush=True)
        print("[1, 2, 3]", flush=True)
        print('{"no_event_key": true}', flush=True)

    emit("checkpoint", name="commit")
    emit("done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
