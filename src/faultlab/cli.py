"""faultlab command line interface (argparse only, no third-party CLI deps)."""

from __future__ import annotations

import argparse
import os
import sys

from .report import dumps
from .runner import ResolutionError, run_scenario
from .schema import ScenarioError, load_scenario

EXIT_OK = 0
EXIT_SUBJECT_FAILED = 1
EXIT_INVALID = 2


def _ensure_cwd_importable() -> None:
    """Let scenarios reference modules in the working tree (e.g. examples.*)."""
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)


def _report_invalid(exc: ScenarioError, stderr) -> int:
    count = len(exc.errors)
    print(
        f"faultlab: scenario invalid ({count} error{'s' if count != 1 else ''}); "
        "no subject was imported or executed",
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
    return parser


def main(argv: list[str] | None = None, stdout=None, stderr=None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    return args.func(args, stdout, stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
