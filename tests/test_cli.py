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


# -- faultlab oracle ---------------------------------------------------------

ALL_ASSERTIONS = ["exactly-once", "at-most-once", "no-key-reuse"]


def _ledger_with(tmp_path, name: str, build) -> str:
    """Build a ledger with ``build(oracle, sink)`` and return its closed path."""
    from examples.idempotency_subjects import OPERATION
    from faultlab.idempotency import IdempotencyOracle, Ledger, SideEffectSink

    path = tmp_path / name
    ledger = Ledger(path)
    try:
        build(IdempotencyOracle(ledger), SideEffectSink(name=OPERATION))
    finally:
        ledger.close()
    return str(path)


def duplicate_commit_ledger(tmp_path) -> str:
    """A naive retry under a drop-after-commit fault: one duplicate_commit."""
    from examples.idempotency_subjects import DROP_THEN_RETRY, naive_retry

    return _ledger_with(
        tmp_path,
        "duplicate.sqlite",
        lambda oracle, sink: naive_retry(oracle, sink, faults=DROP_THEN_RETRY),
    )


def key_reuse_ledger(tmp_path) -> str:
    """One key committed, then reused with a different payload."""
    from examples.idempotency_subjects import KEY, OPERATION, PAYLOAD
    from faultlab.idempotency import KeyReuseError

    def build(oracle, sink):
        oracle.execute(OPERATION, KEY, PAYLOAD, sink.send, call=1)
        try:
            oracle.execute(OPERATION, KEY, {"body": "different"}, sink.send, call=2)
        except KeyReuseError:
            pass

    return _ledger_with(tmp_path, "key_reuse.sqlite", build)


def test_oracle_missing_ledger_exits_two(tmp_path):
    missing = str(tmp_path / "nope.sqlite")
    result = faultlab("oracle", missing)
    assert result.returncode == 2
    assert result.stdout == b""
    payload = last_json(result.stderr)
    assert payload["ok"] is False
    assert payload["errors"][0]["path"] == missing
    assert "ledger not found" in payload["errors"][0]["message"]
    assert b"faultlab: ledger not found" in result.stderr


def test_oracle_rejects_a_file_that_is_not_a_ledger(tmp_path):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("this is not a database\n", encoding="utf-8")
    result = faultlab("oracle", str(bogus))
    assert result.returncode == 2
    assert result.stdout == b""
    assert "not a faultlab ledger" in last_json(result.stderr)["errors"][0]["message"]


def test_oracle_at_most_once_reports_the_duplicate_commit(tmp_path):
    path = duplicate_commit_ledger(tmp_path)
    result = faultlab("oracle", path, "--assert", "at-most-once")
    assert result.returncode == 1
    assert result.stderr == b""
    text = result.stdout.decode()
    assert text.endswith("\n") and text.count("\n") == 1
    payload = json.loads(text)
    assert payload["ok"] is False
    assert payload["assertions"] == ["at-most-once"]
    assert payload["ledger"] == path
    assert len(payload["violations"]) == 1
    violation = payload["violations"][0]
    assert violation["kind"] == "duplicate_commit"
    assert violation["operation"] == "send_message"
    assert violation["call"] == 2
    assert violation["idempotency_key"] == "msg-2f1c"


def test_oracle_exactly_once_fails_on_the_same_ledger(tmp_path):
    path = duplicate_commit_ledger(tmp_path)
    result = faultlab("oracle", path, "--assert", "exactly-once")
    assert result.returncode == 1
    payload = json.loads(result.stdout.decode())
    assert payload["assertions"] == ["exactly-once"]
    assert payload["ok"] is False
    assert [v["kind"] for v in payload["violations"]] == ["duplicate_commit"]


def test_oracle_no_key_reuse_passes_on_a_duplicate_commit_ledger(tmp_path):
    path = duplicate_commit_ledger(tmp_path)
    result = faultlab("oracle", path, "--assert", "no-key-reuse")
    assert result.returncode == 0
    payload = json.loads(result.stdout.decode())
    assert payload == {
        "assertions": ["no-key-reuse"],
        "ledger": path,
        "ok": True,
        "violations": [],
    }


def test_oracle_no_key_reuse_reports_a_reused_key(tmp_path):
    path = key_reuse_ledger(tmp_path)
    result = faultlab("oracle", path, "--assert", "no-key-reuse")
    assert result.returncode == 1
    payload = json.loads(result.stdout.decode())
    assert [v["kind"] for v in payload["violations"]] == ["key_reuse_different_payload"]
    assert payload["violations"][0]["operation"] == "send_message"
    assert payload["violations"][0]["call"] == 2


def test_oracle_at_most_once_passes_on_a_key_reuse_ledger(tmp_path):
    path = key_reuse_ledger(tmp_path)
    result = faultlab("oracle", path, "--assert", "at-most-once")
    assert result.returncode == 0
    assert json.loads(result.stdout.decode())["ok"] is True


def test_oracle_without_assert_runs_all_three(tmp_path):
    path = duplicate_commit_ledger(tmp_path)
    result = faultlab("oracle", path)
    assert result.returncode == 1
    payload = json.loads(result.stdout.decode())
    assert payload["assertions"] == ALL_ASSERTIONS
    # exactly-once and at-most-once both name the duplicate; it is reported once.
    assert [v["kind"] for v in payload["violations"]] == ["duplicate_commit"]


def test_oracle_passes_on_an_idempotent_run(tmp_path):
    from examples.idempotency_subjects import DROP_THEN_RETRY, idempotent_retry

    path = _ledger_with(
        tmp_path,
        "idempotent.sqlite",
        lambda oracle, sink: idempotent_retry(oracle, sink, faults=DROP_THEN_RETRY),
    )
    result = faultlab("oracle", path)
    assert result.returncode == 0
    payload = json.loads(result.stdout.decode())
    assert payload["ok"] is True
    assert payload["violations"] == []


def test_oracle_json_out_matches_stdout(tmp_path):
    path = duplicate_commit_ledger(tmp_path)
    out = tmp_path / "report.json"
    result = faultlab("oracle", path, "--json-out", str(out))
    assert result.returncode == 1
    assert out.read_bytes() == result.stdout


def test_oracle_junit_names_every_assertion_and_carries_no_wall_clock(tmp_path):
    import re
    import xml.etree.ElementTree as ET

    path = duplicate_commit_ledger(tmp_path)
    report = tmp_path / "out.xml"
    result = faultlab(
        "oracle",
        path,
        "--assert",
        "exactly-once",
        "--assert",
        "at-most-once",
        "--assert",
        "no-key-reuse",
        "--junit",
        str(report),
    )
    assert result.returncode == 1

    text = report.read_text(encoding="utf-8")
    root = ET.fromstring(text)
    cases = root.findall(".//testcase")
    assert [case.get("name") for case in cases] == ALL_ASSERTIONS

    failed = {
        case.get("name"): case.find("failure")
        for case in cases
        if case.find("failure") is not None
    }
    assert sorted(failed) == ["at-most-once", "exactly-once"]
    for name, failure in failed.items():
        blob = (failure.get("message") or "") + (failure.text or "")
        assert "send_message" in blob, name
        assert "call" in blob and "2" in blob, name

    # No live wall clock anywhere: no timestamp attribute, no measured duration.
    assert root.find(".//testsuite").get("timestamp") is None
    assert not re.search(r"\d{4}-\d{2}-\d{2}T", text)
    assert all(case.get("time") == "0.000" for case in cases)

    second = tmp_path / "out2.xml"
    faultlab("oracle", path, "--junit", str(second))
    assert second.read_text(encoding="utf-8") == text


def test_oracle_junit_marks_a_clean_run_with_no_failures(tmp_path):
    import xml.etree.ElementTree as ET

    from examples.idempotency_subjects import DROP_THEN_RETRY, idempotent_retry

    path = _ledger_with(
        tmp_path,
        "clean.sqlite",
        lambda oracle, sink: idempotent_retry(oracle, sink, faults=DROP_THEN_RETRY),
    )
    report = tmp_path / "clean.xml"
    result = faultlab("oracle", path, "--junit", str(report))
    assert result.returncode == 0
    root = ET.fromstring(report.read_text(encoding="utf-8"))
    assert [case.get("name") for case in root.findall(".//testcase")] == ALL_ASSERTIONS
    assert root.findall(".//failure") == []
    assert root.find(".//testsuite").get("failures") == "0"


def test_oracle_rejects_an_unknown_assertion(tmp_path):
    path = duplicate_commit_ledger(tmp_path)
    result = faultlab("oracle", path, "--assert", "at-least-once")
    assert result.returncode == 2
    assert result.stdout == b""
