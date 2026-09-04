from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from .conftest import REPO_ROOT

_SCRIPT = shutil.which("faultlab")
BASE_CMD = [_SCRIPT] if _SCRIPT else [sys.executable, "-m", "faultlab.cli"]

SUBJECT = "examples.retry_subject:retry_until_ok"


def faultlab(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("FAULTLAB_SPY_MARKER", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        BASE_CMD + list(args),
        cwd=str(REPO_ROOT),
        capture_output=True,
        env=env,
    )


def last_json(stream: bytes) -> dict:
    lines = [line for line in stream.decode().splitlines() if line.startswith("{")]
    return json.loads(lines[-1])


def test_validate_ok():
    result = faultlab("validate", "examples/fail_fail_succeed.yaml")
    assert result.returncode == 0
    assert result.stdout.decode() == (
        '{"ok":true,"rules":2,"scenario":"examples/fail_fail_succeed.yaml"}\n'
    )


def test_validate_unknown_field_exits_two():
    result = faultlab("validate", "examples/invalid_unknown_field.yaml")
    assert result.returncode == 2
    assert result.stdout == b""
    payload = last_json(result.stderr)
    assert payload["ok"] is False
    assert any(error["path"] == "rules[0].whn" for error in payload["errors"])
    assert b"faultlab: scenario invalid" in result.stderr


def test_validate_contradiction_lists_both_rule_ids():
    result = faultlab("validate", "examples/invalid_contradiction.yaml")
    assert result.returncode == 2
    payload = last_json(result.stderr)
    message = payload["errors"][0]["message"]
    assert "'always-timeout'" in message and "'second-call-returns'" in message


def test_run_fail_fail_succeed_example():
    result = faultlab("run", "examples/fail_fail_succeed.yaml", SUBJECT)
    assert result.returncode == 0, result.stderr.decode()
    payload = json.loads(result.stdout.decode())
    assert payload["ok"] is True
    assert payload["result"] == "ok"
    assert payload["seed"] == 42
    assert payload["subject"] == SUBJECT
    assert payload["target"] == "examples.retry_subject:flaky_backend"
    assert len(payload["sequence"]) == 3
    assert payload["clock_ms"] == 300


def test_run_output_is_byte_identical_across_processes():
    first = faultlab("run", "examples/fail_fail_succeed.yaml", SUBJECT)
    second = faultlab("run", "examples/fail_fail_succeed.yaml", SUBJECT)
    assert first.stdout == second.stdout
    assert first.stdout.endswith(b"\n")
    assert b'{"call":1,' in first.stdout  # canonical separators, no padding


def test_run_delay_example_does_not_sleep():
    result = faultlab("run", "examples/delay_fake_clock.yaml", SUBJECT)
    assert result.returncode == 0
    payload = json.loads(result.stdout.decode())
    assert payload["clock_ms"] == 10000
    assert payload["sequence"][0]["injects"][0]["kind"] == "delay"


def test_run_after_malformed_example_tags_the_payload():
    result = faultlab("run", "examples/after_malformed.yaml", SUBJECT)
    assert result.returncode == 0
    payload = json.loads(result.stdout.decode())
    assert payload["result"] == {"status": "ok", "payload": None}
    assert payload["sequence"][0]["injects"][0]["kind"] == "malformed"


def test_run_next_n_example():
    result = faultlab("run", "examples/next_n.yaml", SUBJECT)
    assert result.returncode == 0
    payload = json.loads(result.stdout.decode())
    assert [entry["call"] for entry in payload["sequence"]] == [1, 2, 3, 4]
    assert [bool(entry["injects"]) for entry in payload["sequence"]] == [
        True,
        True,
        True,
        False,
    ]


def test_run_reports_exit_one_when_the_subject_raises(tmp_path):
    scenario = tmp_path / "always_fail.yaml"
    scenario.write_text(
        "version: 1\nseed: 1\nclock: fake\n"
        "target:\n  callable: examples.retry_subject:flaky_backend\n"
        "rules:\n  - id: down\n    when: before\n    match: always\n"
        '    inject:\n      exception:\n        type: ConnectionError\n        message: "down"\n',
        encoding="utf-8",
    )
    result = faultlab("run", str(scenario), SUBJECT)
    assert result.returncode == 1
    payload = json.loads(result.stdout.decode())
    assert payload["ok"] is False
    assert payload["error"]["type"] == "RuntimeError"
    assert len(payload["sequence"]) == 5


def test_run_does_not_import_the_subject_when_the_scenario_is_invalid(tmp_path):
    marker = tmp_path / "imported.txt"
    result = faultlab(
        "run",
        "examples/invalid_contradiction.yaml",
        "tests.spy_subject:subject",
        env_extra={"FAULTLAB_SPY_MARKER": str(marker)},
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert not marker.exists()


def test_run_imports_the_subject_when_the_scenario_is_valid(tmp_path):
    marker = tmp_path / "imported.txt"
    scenario = tmp_path / "spy.yaml"
    scenario.write_text(
        "version: 1\nseed: 1\nclock: fake\n"
        "target:\n  callable: tests.spy_subject:subject\n"
        'rules:\n  - id: r1\n    when: before\n    match: always\n    inject:\n      return: "x"\n',
        encoding="utf-8",
    )
    result = faultlab(
        "run",
        str(scenario),
        "tests.spy_subject:subject",
        env_extra={"FAULTLAB_SPY_MARKER": str(marker)},
    )
    assert result.returncode == 0
    assert marker.exists()


def test_run_unresolvable_target_exits_two():
    result = faultlab("run", "examples/fail_fail_succeed.yaml", "no.such.module:fn")
    assert result.returncode == 2
    assert result.stdout == b""
    assert last_json(result.stderr)["ok"] is False
