"""faultlab command line interface (argparse only, no third-party CLI deps)."""

from __future__ import annotations

import argparse
import os
import sys

from . import junit
from .idempotency import IdempotencyOracle, Ledger, LedgerError, describe_violation
from .recovery import RecoveryError, junit_cases, run_recovery
from .recovery_schema import load_recovery_scenario
from .report import dumps
from .runner import ResolutionError, run_scenario
from .schema import ScenarioError, ValidationError, load_scenario

EXIT_OK = 0
EXIT_SUBJECT_FAILED = 1
EXIT_INVALID = 2

ASSERTIONS = ("exactly-once", "at-most-once", "no-key-reuse")


def _ensure_cwd_importable() -> None:
    """Let scenarios reference modules in the working tree (e.g. examples.*)."""
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)


def _report_invalid(
    exc: ScenarioError,
    stderr,
    consequence: str = "no subject was imported or executed",
) -> int:
    count = len(exc.errors)
    print(
        f"faultlab: scenario invalid ({count} error{'s' if count != 1 else ''}); "
        f"{consequence}",
        file=stderr,
    )
    stderr.write(dumps({"ok": False, "errors": [e.as_dict() for e in exc.errors]}))
    return EXIT_INVALID


def _cmd_validate(args: argparse.Namespace, stdout, stderr) -> int:
    try:
        scenario = load_scenario(args.path)
    except ScenarioError as exc:
        return _report_invalid(exc, stderr)
    stdout.write(dumps({"ok": True, "scenario": args.path, "rules": len(scenario.rules)}))
    return EXIT_OK


def _cmd_run(args: argparse.Namespace, stdout, stderr) -> int:
    try:
        scenario = load_scenario(args.path)
    except ScenarioError as exc:
        return _report_invalid(exc, stderr)

    _ensure_cwd_importable()
    try:
        payload, code = run_scenario(scenario, args.subject, args.args)
    except ResolutionError as exc:
        print(f"faultlab: {exc}", file=stderr)
        stderr.write(dumps({"ok": False, "errors": [{"path": "target", "message": str(exc)}]}))
        return EXIT_INVALID
    stdout.write(dumps(payload))
    return code


def _run_assertion(oracle: IdempotencyOracle, name: str) -> list[dict]:
    if name == "exactly-once":
        return oracle.assert_exactly_once()
    if name == "at-most-once":
        return oracle.assert_at_most_once()
    return oracle.assert_no_key_reuse()


def _cmd_oracle(args: argparse.Namespace, stdout, stderr) -> int:
    names: list[str] = []
    for name in args.assertions or ASSERTIONS:
        if name not in names:
            names.append(name)

    try:
        ledger = Ledger.open_existing(args.ledger)
    except LedgerError as exc:
        print(f"faultlab: {exc}", file=stderr)
        stderr.write(dumps({"ok": False, "errors": [{"path": args.ledger, "message": str(exc)}]}))
        return EXIT_INVALID

    try:
        oracle = IdempotencyOracle(ledger)
        per_assertion = {name: _run_assertion(oracle, name) for name in names}
    finally:
        ledger.close()

    seen: set[str] = set()
    violations: list[dict] = []
    for name in names:
        for violation in per_assertion[name]:
            marker = dumps(violation)
            if marker not in seen:
                seen.add(marker)
                violations.append(violation)
    violations.sort(key=dumps)

    payload = {
        "assertions": names,
        "ledger": args.ledger,
        "ok": not violations,
        "violations": violations,
    }
    text = dumps(payload)
    stdout.write(text)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            handle.write(text)
    if args.junit:
        junit.write(
            args.junit,
            [
                junit.TestCase(
                    name=name,
                    failures=tuple(
                        junit.Failure(message=describe_violation(v), details=dumps(v).strip())
                        for v in per_assertion[name]
                    ),
                )
                for name in names
            ],
        )
    return EXIT_OK if not violations else EXIT_SUBJECT_FAILED


def _cmd_recovery_run(args: argparse.Namespace, stdout, stderr) -> int:
    consequence = "no command was started"
    try:
        scenario = load_recovery_scenario(args.path)
    except ScenarioError as exc:
        return _report_invalid(exc, stderr, consequence)

    if not args.command:
        exc = ScenarioError(
            [ValidationError("command", "required; put the worker command after '--'")]
        )
        return _report_invalid(exc, stderr, consequence)

    try:
        payload, code = run_recovery(scenario, args.command)
    except RecoveryError as exc:
        print(f"faultlab: {exc}", file=stderr)
        stderr.write(dumps({"ok": False, "errors": [{"path": "command", "message": str(exc)}]}))
        return EXIT_INVALID

    text = dumps(payload)
    stdout.write(text)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            handle.write(text)
    if args.junit:
        junit.write(args.junit, junit_cases(payload), suite=junit.RECOVERY_SUITE)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="faultlab",
        description="Deterministic callable fault injection and scenario runner.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate", help="validate a scenario file without executing anything"
    )
    validate.add_argument("path", help="path to a scenario YAML file")
    validate.set_defaults(func=_cmd_validate)

    run = subparsers.add_parser("run", help="run a subject with the scenario's tool wrapped")
    run.add_argument("path", help="path to a scenario YAML file")
    run.add_argument("subject", metavar="module:function", help="the subject under test")
    run.add_argument("args", nargs="*", help="positional string arguments for the subject")
    run.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="emit a canonical JSON report to stdout (always on; accepted for clarity)",
    )
    run.set_defaults(func=_cmd_run)

    oracle = subparsers.add_parser(
        "oracle", help="assert idempotency invariants against a recorded ledger"
    )
    oracle.add_argument("ledger", help="path to an existing SQLite ledger file")
    oracle.add_argument(
        "--assert",
        dest="assertions",
        action="append",
        choices=list(ASSERTIONS),
        metavar="NAME",
        help="repeatable; one of " + ", ".join(ASSERTIONS) + " (default: all three)",
    )
    oracle.add_argument("--json-out", dest="json_out", help="also write the JSON report here")
    oracle.add_argument("--junit", help="write a JUnit XML report here")
    oracle.set_defaults(func=_cmd_oracle)

    recovery = subparsers.add_parser(
        "recovery-run",
        help="crash and restart a worker subprocess, then assert recovery invariants",
    )
    recovery.add_argument("path", help="path to a recovery scenario YAML file")
    recovery.add_argument("--json-out", dest="json_out", help="also write the JSON report here")
    recovery.add_argument("--junit", help="write a JUnit XML report here")
    recovery.add_argument(
        "command",
        nargs="*",
        metavar="COMMAND",
        help="the worker command, after a literal '--'",
    )
    recovery.set_defaults(func=_cmd_recovery_run)
    return parser


def _split_command(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """Split ``recovery-run`` argv at the first '--'.

    Only recovery-run takes a worker command, so no other subcommand's
    handling of '--' changes.
    """
    if not argv or argv[0] != "recovery-run" or "--" not in argv:
        return argv, None
    index = argv.index("--")
    return argv[:index], argv[index + 1 :]


def main(argv: list[str] | None = None, stdout=None, stderr=None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    head, command = _split_command(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(head)
    if command is not None:
        args.command = command
    return args.func(args, stdout, stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
