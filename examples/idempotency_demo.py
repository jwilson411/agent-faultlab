"""Run the naive and the idempotent client against the same drop fault.

    python -m examples.idempotency_demo

Two canonical JSON lines come out. The first is the naive client: two side
effects and a ``duplicate_commit``. The second is the idempotent client: one
side effect and no violations. Both are byte-identical on every run.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from typing import Any

from faultlab.idempotency import IdempotencyOracle, Ledger, SideEffectSink
from faultlab.report import dumps, jsonable

from examples.idempotency_subjects import (
    DROP_THEN_RETRY,
    OPERATION,
    idempotent_retry,
    naive_retry,
)

ASSERTIONS = ("exactly-once", "at-most-once", "no-key-reuse")


def _report(client: str, oracle: IdempotencyOracle, sink, result: Any) -> dict[str, Any]:
    violations = (
        oracle.assert_exactly_once()
        + oracle.assert_at_most_once()
        + oracle.assert_no_key_reuse()
    )
    unique: list[dict[str, Any]] = []
    for violation in violations:
        if violation not in unique:
            unique.append(violation)
    unique.sort(key=dumps)
    return {
        "assertions": list(ASSERTIONS),
        "client": client,
        "commits": len(oracle.ledger.commits()),
        "ok": not unique,
        "result": jsonable(result),
        "side_effects": len(sink),
        "violations": unique,
    }


def _fresh(ledger_path: str) -> Ledger:
    if os.path.exists(ledger_path):
        os.remove(ledger_path)
    for suffix in ("-wal", "-shm"):
        stale = ledger_path + suffix
        if os.path.exists(stale):
            os.remove(stale)
    return Ledger(ledger_path)


def _run(client: str, ledger_path: str) -> dict[str, Any]:
    retry = naive_retry if client == "naive" else idempotent_retry
    ledger = _fresh(ledger_path)
    try:
        oracle = IdempotencyOracle(ledger)
        sink = SideEffectSink(name=OPERATION)
        result = retry(oracle, sink, faults=DROP_THEN_RETRY)
        return _report(client, oracle, sink, result)
    finally:
        ledger.close()


def run_naive(ledger_path: str) -> dict[str, Any]:
    """Naive retry under a drop-after-commit fault. Fails the oracle."""
    return _run("naive", ledger_path)


def run_idempotent(ledger_path: str) -> dict[str, Any]:
    """Idempotent retry under the same fault. Passes the oracle."""
    return _run("idempotent", ledger_path)


def main(argv: list[str] | None = None, stdout=None) -> int:
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(
        prog="idempotency_demo",
        description="Naive versus idempotent retry under a drop-after-commit fault.",
    )
    parser.add_argument(
        "--ledger",
        metavar="DIR",
        help="directory for the two ledger files (default: a temporary directory)",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="faultlab-idempotency-") as scratch:
        directory = args.ledger or scratch
        os.makedirs(directory, exist_ok=True)
        stdout.write(dumps(run_naive(os.path.join(directory, "naive.sqlite"))))
        stdout.write(dumps(run_idempotent(os.path.join(directory, "idempotent.sqlite"))))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
