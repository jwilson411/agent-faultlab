"""Crash/restart recovery harness.

The harness runs a worker as a local subprocess, reads a JSONL event stream
from its stdout, terminates its process group at a named point, restarts it
against the same state directory, and then asks the AF-03 idempotency oracle
whether the recovery kept its invariants.

The intentional crash is applied on the first process start only. A restart
runs to ``done``, to ``error``, or into a limit; that is what proves recovery
rather than luck. Once the kill point is observed the harness stops reading
that attempt's output, so the report describes the same events every time.

Nothing here sleeps. The bounded waits are for reaping a signalled child and
for capping a runaway one; the report carries no durations, no PIDs, no ports
and no wall-clock times.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import junit
from .idempotency import IdempotencyOracle, Ledger, LedgerError, describe_violation
from .recovery_schema import KillSpec, Limits, RecoveryScenario
from .report import dumps

#: Bytes read per ready file descriptor.
CHUNK = 65536

#: Seconds allowed for a signalled child to be reaped before escalating.
REAP_TIMEOUT = 5.0

#: Seconds a child gets to exit after closing both pipes, on top of whatever is
#: left of its runtime budget. A worker that has stopped writing is already on
#: its way out, and calling that last moment a runtime overrun would make the
#: report depend on how loaded the machine is.
EOF_GRACE = 0.5

STATE_DIR_ENV = "FAULTLAB_STATE_DIR"
LEDGER_ENV = "FAULTLAB_LEDGER"
LEDGER_NAME = "ledger.sqlite"

#: Which violation kinds make which assertion fail; used for the JUnit detail.
ASSERTION_KINDS = {
    "at-most-once": ("duplicate_commit",),
    "no-key-reuse": ("key_reuse_different_payload",),
    "exactly-once": (
        "duplicate_commit",
        "started_never_committed",
        "response_lost_after_commit",
        "no_commit",
    ),
}

EXIT_OK = 0
EXIT_FAILED = 1


class RecoveryError(Exception):
    """Raised when the harness cannot start the worker at all."""


@dataclass
class Attempt:
    """What one process start did, with nothing machine-specific in it."""

    checkpoints: list[str] = field(default_factory=list)
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    error: str | None = None
    killed: bool = False  # the scenario's intentional crash fired
    limit: str | None = None  # runtime | output
    exit_code: int | None = None
    output_bytes: int = 0

    def as_dict(self, index: int) -> dict[str, Any]:
        return {
            "attempt": index,
            "checkpoints": list(self.checkpoints),
            "done": self.done,
            "error": self.error,
            "exit_code": self.exit_code,
            "killed": self.killed,
            "limit": self.limit,
            "side_effects": list(self.side_effects),
        }


def run_recovery(
    scenario: RecoveryScenario,
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Crash and restart ``command``, then report the recovery invariants.

    Returns the canonical report payload and the process exit code. The state
    directory comes from ``FAULTLAB_STATE_DIR`` when the caller set one, in
    which case it is left alone; otherwise a temporary one is created and
    removed on every exit path.
    """
    command = list(command)
    if not command:
        raise RecoveryError("no command to run")

    child_env = dict(os.environ if env is None else env)
    supplied = child_env.get(STATE_DIR_ENV)
    created: str | None = None
    if supplied:
        state_dir = os.path.abspath(supplied)
        os.makedirs(state_dir, exist_ok=True)
    else:
        created = tempfile.mkdtemp(prefix="faultlab-recovery-")
        state_dir = created

    ledger_path = os.path.join(state_dir, LEDGER_NAME)
    child_env[STATE_DIR_ENV] = state_dir
    child_env[LEDGER_ENV] = ledger_path

    try:
        attempts = _run_attempts(scenario, command, child_env, cwd)
        return _report(scenario, command, attempts, ledger_path)
    finally:
        if created is not None:
            shutil.rmtree(created, ignore_errors=True)


def _run_attempts(
    scenario: RecoveryScenario,
    command: list[str],
    child_env: dict[str, str],
    cwd: str | None,
) -> list[Attempt]:
    limits = scenario.limits
    attempts: list[Attempt] = []
    restarts = 0
    consumed = 0
    while True:
        kill = scenario.kill if not attempts else None
        attempt = _run_attempt(command, child_env, cwd, limits, kill, consumed)
        attempts.append(attempt)
        consumed += attempt.output_bytes
        if attempt.killed and restarts < limits.max_restarts:
            restarts += 1
            continue
        return attempts


def _run_attempt(
    command: list[str],
    child_env: dict[str, str],
    cwd: str | None,
    limits: Limits,
    kill: KillSpec | None,
    consumed: int,
) -> Attempt:
    """Start the worker once, read its events, and stop at the first stop reason.

    stdin is a pipe the harness never writes to, so a worker that reads a line
    blocks there instead of seeing an inherited terminal or an instant EOF.
    """
    attempt = Attempt()
    try:
        proc = subprocess.Popen(  # noqa: S603 - the command is the caller's own
            command,
            cwd=cwd,
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise RecoveryError(f"cannot start {command[0]!r}: {exc}") from None

    deadline = time.monotonic() + limits.runtime_ms / 1000.0
    try:
        crashed = _pump(proc, attempt, kill, limits, consumed, deadline)
        if crashed:
            _signal_group(proc, signal.SIGKILL)
        else:
            try:
                proc.wait(timeout=max(deadline - time.monotonic(), EOF_GRACE))
            except subprocess.TimeoutExpired:
                attempt.limit = "runtime"
                crashed = True
                _signal_group(proc, signal.SIGKILL)
        _reap(proc)
        attempt.exit_code = None if crashed else proc.returncode
    finally:
        _cleanup(proc)
    return attempt


def _pump(
    proc: subprocess.Popen,
    attempt: Attempt,
    kill: KillSpec | None,
    limits: Limits,
    consumed: int,
    deadline: float,
) -> bool:
    """Read both pipes until a stop reason. True means the child must be killed.

    Only stdout is parsed as events; stderr is captured and counted, never
    interpreted. Reading stops the instant the kill point is parsed, so events
    the worker managed to write before the signal landed cannot leak into the
    report.
    """
    buffer = b""
    counts: dict[str, int] = {}
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                attempt.limit = "runtime"
                return True
            ready = selector.select(remaining)
            if not ready:
                attempt.limit = "runtime"
                return True
            for key, _mask in ready:
                try:
                    chunk = os.read(key.fd, CHUNK)
                except OSError:  # pragma: no cover - the pipe went away
                    chunk = b""
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                attempt.output_bytes += len(chunk)
                if consumed + attempt.output_bytes > limits.max_output_bytes:
                    attempt.limit = "output"
                    return True
                if key.data != "stdout":
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    if _consume(line, attempt, kill, counts):
                        attempt.killed = True
                        return True
    return False


def _consume(
    line: bytes,
    attempt: Attempt,
    kill: KillSpec | None,
    counts: dict[str, int],
) -> bool:
    """Record one stdout line. True means this line is the kill point.

    A line that is not a JSON object carrying a string ``event`` is ignored as
    an event; it still counted toward the output limit when it was read.
    """
    try:
        event = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return False
    if not isinstance(event, dict):
        return False
    kind = event.get("event")
    if not isinstance(kind, str):
        return False

    fire = kill is not None and _matches(kill, kind, event, counts)
    if kind == "checkpoint":
        name = event.get("name")
        if isinstance(name, str) and name:
            if fire and kill.style == "checkpoint" and kill.when == "before":
                return True  # terminated before this checkpoint completed
            attempt.checkpoints.append(name)
    elif kind == "side_effect":
        attempt.side_effects.append(_side_effect(event))
    elif kind == "done":
        attempt.done = True
    elif kind == "error":
        message = event.get("message")
        attempt.error = message if isinstance(message, str) else ""
    return fire


def _side_effect(event: dict[str, Any]) -> dict[str, Any]:
    record = {
        "idempotency_key": str(event.get("idempotency_key", "")),
        "operation": str(event.get("operation", "")),
    }
    fingerprint = event.get("payload_fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        record["payload_fingerprint"] = fingerprint
    return record


def _matches(
    kill: KillSpec,
    kind: str,
    event: dict[str, Any],
    counts: dict[str, int],
) -> bool:
    if kill.style == "checkpoint":
        return kind == "checkpoint" and event.get("name") == kill.checkpoint
    if kind != kill.event:
        return False
    if kill.name is not None:
        label = event.get("name") if kind == "checkpoint" else event.get("operation")
        if label != kill.name:
            return False
    counts[kind] = counts.get(kind, 0) + 1
    return counts[kind] == kill.n


# -- process control ---------------------------------------------------


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the child's process group; it is its own session leader.

    Skipped once the child has been reaped, because the pid is free to be
    reused by then and the harness must never signal a stranger.
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _reap(proc: subprocess.Popen) -> None:
    """Wait for a signalled child, escalating to SIGKILL once if it lingers."""
    try:
        proc.wait(timeout=REAP_TIMEOUT)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is not refusable
        _signal_group(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass


def _cleanup(proc: subprocess.Popen) -> None:
    """Leave nothing running and no descriptor open, on every exit path."""
    if proc.poll() is None:
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=REAP_TIMEOUT)
        except subprocess.TimeoutExpired:  # pragma: no cover - workers are small
            _signal_group(proc, signal.SIGKILL)
            _reap(proc)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:  # pragma: no cover - already closed
                pass


# -- reporting ---------------------------------------------------------


def _run_assertion(oracle: IdempotencyOracle, name: str) -> list[dict[str, Any]]:
    if name == "exactly-once":
        return oracle.assert_exactly_once()
    if name == "at-most-once":
        return oracle.assert_at_most_once()
    return oracle.assert_no_key_reuse()


def _report(
    scenario: RecoveryScenario,
    command: list[str],
    attempts: list[Attempt],
    ledger_path: str,
) -> tuple[dict[str, Any], int]:
    reasons: list[str] = []

    kill_reached = any(attempt.killed for attempt in attempts)
    if not kill_reached:
        reasons.append("kill_point_not_reached")

    limit = next((a.limit for a in attempts if a.limit is not None), None)
    if limit is not None:
        reasons.append(f"limit:{limit}")

    last = attempts[-1]
    if last.error is not None:
        reasons.append("worker_error")
    elif last.killed:
        reasons.append("restarts_exhausted")
    elif last.limit is None and not last.done:
        reasons.append("worker_did_not_finish")

    invariants, violations, duplicates, missing = _oracle_result(
        scenario.assertions, ledger_path, reasons
    )

    payload = {
        "assertions": list(scenario.assertions),
        "attempts": [a.as_dict(index) for index, a in enumerate(attempts, start=1)],
        "checkpoint_sequence": [name for a in attempts for name in a.checkpoints],
        "command": list(command),
        "duplicate_side_effects": duplicates,
        "invariants": invariants,
        "kill_point_reached": kill_reached,
        "ledger_missing": missing,
        "limit": limit,
        "ok": not reasons,
        "reasons": sorted(reasons),
        "restarts": len(attempts) - 1,
        "scenario": scenario.source,
        "seed": scenario.seed,
        "side_effects": [effect for a in attempts for effect in a.side_effects],
        "termination_point": scenario.kill.describe() if kill_reached else None,
        "violations": violations,
    }
    return payload, EXIT_OK if payload["ok"] else EXIT_FAILED


def _oracle_result(
    assertions: Sequence[str],
    ledger_path: str,
    reasons: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], bool]:
    """Ask the AF-03 oracle about the ledger the worker left behind."""
    if not os.path.isfile(ledger_path):
        reasons.append("ledger_missing")
        return {name: None for name in assertions}, [], [], True

    try:
        ledger = Ledger.open_existing(ledger_path)
    except LedgerError:
        reasons.append("ledger_invalid")
        return {name: None for name in assertions}, [], [], True

    try:
        oracle = IdempotencyOracle(ledger)
        per_assertion = {name: _run_assertion(oracle, name) for name in assertions}
        duplicates = sorted(
            {
                str(item["idempotency_key"])
                for item in oracle.violations()
                if item["kind"] == "duplicate_commit"
            }
        )
    finally:
        ledger.close()

    seen: set[str] = set()
    violations: list[dict[str, Any]] = []
    for name in assertions:
        if per_assertion[name]:
            reasons.append(f"assertion:{name}")
        for item in per_assertion[name]:
            marker = dumps(item)
            if marker not in seen:
                seen.add(marker)
                violations.append(item)
    violations.sort(key=dumps)

    if duplicates:
        reasons.append("duplicate_side_effects")
    invariants = {name: not per_assertion[name] for name in assertions}
    return invariants, violations, duplicates, False


def junit_cases(payload: dict[str, Any]) -> list[junit.TestCase]:
    """One testcase per assertion, plus the two harness-level checks."""
    cases: list[junit.TestCase] = []
    violations = payload.get("violations", [])
    for name in payload.get("assertions", []):
        state = payload.get("invariants", {}).get(name)
        if state is True:
            failures: tuple[junit.Failure, ...] = ()
        elif state is None:
            failures = (
                junit.Failure(
                    message=f"{name} was not evaluated: no ledger was written",
                    type="RecoveryFailure",
                ),
            )
        else:
            kinds = ASSERTION_KINDS.get(name, ())
            failures = tuple(
                junit.Failure(message=describe_violation(item), details=dumps(item).strip())
                for item in violations
                if item.get("kind") in kinds
            ) or (junit.Failure(message=f"{name} failed", type="RecoveryFailure"),)
        cases.append(
            junit.TestCase(name=name, classname=junit.RECOVERY_CLASSNAME, failures=failures)
        )

    reasons = payload.get("reasons", [])
    cases.append(
        junit.TestCase(
            name="kill_point_reached",
            classname=junit.RECOVERY_CLASSNAME,
            failures=()
            if payload.get("kill_point_reached")
            else (
                junit.Failure(
                    message="the worker never reached the scenario's kill point"
                    f" ({payload.get('termination_point') or 'none'})",
                    details=dumps({"reasons": reasons}).strip(),
                    type="RecoveryFailure",
                ),
            ),
        )
    )
    duplicates = payload.get("duplicate_side_effects", [])
    cases.append(
        junit.TestCase(
            name="no_duplicate_side_effects",
            classname=junit.RECOVERY_CLASSNAME,
            failures=()
            if not duplicates
            else (
                junit.Failure(
                    message="the side effect committed twice for "
                    + ", ".join(duplicates),
                    details=dumps({"duplicate_side_effects": duplicates}).strip(),
                ),
            ),
        )
    )
    return cases
