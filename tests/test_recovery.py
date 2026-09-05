"""End-to-end tests for `faultlab recovery-run`, plus recovery schema checks.

Every run here starts a real subprocess and kills it, so the tests assert on
what survived the crash rather than on how the crash was timed. Nothing sleeps:
the runtime-limit worker blocks on a read that never answers, and the
output-limit worker writes as fast as the pipe drains.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

import pytest
import yaml

from faultlab.recovery_schema import validate_recovery_document
from faultlab.schema import ScenarioError

from .test_cli import faultlab, last_json

WORKER = [sys.executable, "examples/recovery_worker.py"]
HELPER = [sys.executable, "tests/recovery_helpers.py"]

BEFORE_COMMIT = "examples/recovery_crash_before_commit.yaml"
AFTER_COMMIT = "examples/recovery_crash_after_commit.yaml"
CORRUPT = "examples/recovery_corrupt_checkpoint.yaml"

BASE = """
version: 1
kind: recovery
seed: 9
kill:
  when: after
  checkpoint: commit
"""


def write_scenario(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def errors_for(text: str) -> list[tuple[str, str]]:
    with pytest.raises(ScenarioError) as excinfo:
        validate_recovery_document(yaml.safe_load(text), source="<inline>")
    return [(e.path, e.message) for e in excinfo.value.errors]


# -- crash before the commit --------------------------------------------------


def test_crash_before_commit_restarts_once_and_commits_exactly_once():
    result = faultlab("recovery-run", BEFORE_COMMIT, "--", *WORKER)
    assert result.returncode == 0, result.stderr.decode()
    payload = json.loads(result.stdout.decode())

    assert payload["ok"] is True
    assert payload["reasons"] == []
    assert payload["restarts"] == 1
    assert payload["kill_point_reached"] is True
    assert payload["termination_point"] == "after:pre_commit"
    assert payload["invariants"] == {"exactly-once": True, "at-most-once": True}
    assert payload["duplicate_side_effects"] == []
    assert payload["ledger_missing"] is False
    assert payload["limit"] is None
    assert payload["scenario"] == BEFORE_COMMIT
    assert payload["seed"] == 1
    assert payload["command"] == WORKER

    # SIGKILL is asynchronous, so the first attempt may die before the ledger
    # write, inside it, or just after it. When it died after, the restart
    # replays the stored result and reports no send of its own. Either way the
    # ledger holds one commit, which is what the invariants above assert.
    assert len(payload["side_effects"]) <= 1
    for effect in payload["side_effects"]:
        assert effect["operation"] == "send_message"
        assert effect["idempotency_key"] == "msg-recovery"

    first, second = payload["attempts"]
    assert first["killed"] is True and first["exit_code"] is None
    assert first["checkpoints"] == ["begin", "pre_commit"]
    assert second["killed"] is False and second["done"] is True
    assert second["checkpoints"][-1] == "commit"


def test_crash_before_commit_stdout_is_one_line_of_canonical_json():
    result = faultlab("recovery-run", BEFORE_COMMIT, "--", *WORKER)
    text = result.stdout.decode()
    assert text.endswith("\n") and text.count("\n") == 1
    assert text == canonical(json.loads(text))
    assert '"ok":true' in text  # canonical separators, no padding


def test_a_successful_run_leaves_no_temporary_state_directory():
    pattern = os.path.join(tempfile.gettempdir(), "faultlab-recovery-*")
    before = set(glob.glob(pattern))
    result = faultlab("recovery-run", BEFORE_COMMIT, "--", *WORKER)
    assert result.returncode == 0
    assert set(glob.glob(pattern)) - before == set()


# -- crash after the commit ---------------------------------------------------


def test_crash_after_commit_does_not_send_the_message_twice():
    result = faultlab("recovery-run", AFTER_COMMIT, "--", *WORKER)
    assert result.returncode == 0, result.stderr.decode()
    payload = json.loads(result.stdout.decode())

    assert payload["ok"] is True
    assert payload["restarts"] == 1
    assert payload["kill_point_reached"] is True
    assert payload["termination_point"] == "after:commit"
    assert payload["invariants"] == {"exactly-once": True, "at-most-once": True}
    assert payload["duplicate_side_effects"] == []
    assert payload["violations"] == []
    assert len(payload["side_effects"]) == 1

    first, second = payload["attempts"]
    assert first["checkpoints"] == ["begin", "pre_commit", "commit"]
    assert first["killed"] is True
    # The restart reads the checkpoint file and skips straight to commit.
    assert second["checkpoints"] == ["begin", "commit"]
    assert second["side_effects"] == []
    assert second["done"] is True


def test_crash_after_commit_report_is_byte_identical_across_runs():
    first = faultlab("recovery-run", AFTER_COMMIT, "--", *WORKER)
    second = faultlab("recovery-run", AFTER_COMMIT, "--", *WORKER)
    assert first.returncode == 0 and second.returncode == 0
    assert first.stdout == second.stdout


def test_recovery_junit_report_names_the_recovery_suite(tmp_path):
    report = tmp_path / "recovery.xml"
    result = faultlab("recovery-run", AFTER_COMMIT, "--junit", str(report), "--", *WORKER)
    assert result.returncode == 0

    text = report.read_text(encoding="utf-8")
    root = ET.fromstring(text)
    suite = root.find(".//testsuite")
    assert root.get("name") == "faultlab-recovery"
    assert suite.get("name") == "faultlab-recovery"
    assert suite.get("failures") == "0"
    assert suite.get("timestamp") is None

    cases = root.findall(".//testcase")
    assert [case.get("name") for case in cases] == [
        "exactly-once",
        "at-most-once",
        "kill_point_reached",
        "no_duplicate_side_effects",
    ]
    assert all(case.get("classname") == "faultlab.recovery" for case in cases)
    assert root.findall(".//failure") == []


# -- a restart that cannot recover --------------------------------------------


def test_corrupt_checkpoint_reports_a_worker_error_and_keeps_the_state_dir(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    checkpoint = state_dir / "checkpoint.json"
    checkpoint.write_text("not json at all", encoding="utf-8")

    result = faultlab(
        "recovery-run",
        CORRUPT,
        "--",
        *WORKER,
        env_extra={"FAULTLAB_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout.decode())

    assert payload["ok"] is False
    assert "worker_error" in payload["reasons"]
    assert payload["kill_point_reached"] is True
    assert payload["restarts"] == 1
    assert payload["attempts"][-1]["error"] == "corrupt checkpoint"
    assert payload["attempts"][-1]["exit_code"] == 2
    # Nothing was sent, so there is nothing to have sent twice.
    assert payload["side_effects"] == []
    assert payload["duplicate_side_effects"] == []
    assert payload["invariants"] == {"exactly-once": None, "at-most-once": None}

    # The harness uses the directory it was given and does not delete it.
    assert state_dir.is_dir()
    assert checkpoint.read_text(encoding="utf-8") == "not json at all"


def test_invalid_recovery_scenario_exits_two_without_starting_anything(tmp_path):
    path = write_scenario(tmp_path, "unknown.yaml", BASE + "\nrules: []\n")
    result = faultlab("recovery-run", path, "--", *WORKER)
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"faultlab: scenario invalid" in result.stderr
    assert b"no command was started" in result.stderr
    payload = last_json(result.stderr)
    assert payload["ok"] is False
    assert [error["path"] for error in payload["errors"]] == ["rules"]


# -- limits -------------------------------------------------------------------


def test_runtime_limit_stops_a_blocked_worker_and_leaves_nothing_running(tmp_path):
    state_dir = tmp_path / "state"
    path = write_scenario(
        tmp_path,
        "runtime.yaml",
        BASE + "limits:\n  runtime_ms: 500\n  max_restarts: 1\n",
    )
    result = faultlab(
        "recovery-run",
        path,
        "--",
        *HELPER,
        "block",
        env_extra={"FAULTLAB_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout.decode())

    assert payload["ok"] is False
    assert payload["limit"] == "runtime"
    assert "limit:runtime" in payload["reasons"]
    assert payload["kill_point_reached"] is False
    assert payload["termination_point"] is None
    # The worker never reached the kill point, so it is not restarted.
    assert payload["restarts"] == 0
    assert payload["attempts"][0]["limit"] == "runtime"
    assert payload["attempts"][0]["exit_code"] is None

    pid_file = state_dir / "pid"
    assert pid_file.is_file(), "the helper never started"
    pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(OSError):
        os.kill(pid, 0)


def test_output_limit_stops_a_flooding_worker(tmp_path):
    path = write_scenario(
        tmp_path,
        "output.yaml",
        BASE + "limits:\n  runtime_ms: 10000\n  max_output_bytes: 2048\n",
    )
    result = faultlab("recovery-run", path, "--", *HELPER, "flood")
    assert result.returncode == 1
    payload = json.loads(result.stdout.decode())

    assert payload["ok"] is False
    assert payload["limit"] == "output"
    assert "limit:output" in payload["reasons"]
    assert payload["kill_point_reached"] is False
    assert payload["restarts"] == 0
    assert payload["attempts"][0]["limit"] == "output"
    assert payload["attempts"][0]["done"] is False


# -- recovery schema ----------------------------------------------------------


def test_minimal_recovery_scenario_validates():
    scenario = validate_recovery_document(yaml.safe_load(BASE), source="<inline>")
    assert scenario.seed == 9
    assert scenario.kill.describe() == "after:commit"
    assert scenario.assertions == ("exactly-once", "at-most-once")
    assert scenario.limits.runtime_ms == 5000
    assert scenario.limits.max_restarts == 1
    assert scenario.limits.max_output_bytes == 65536


def test_unknown_root_field_names_its_path():
    paths = [path for path, _ in errors_for(BASE + "\nrules: []\n")]
    assert paths == ["rules"]


def test_unknown_kill_field_names_its_path():
    paths = [path for path, _ in errors_for(BASE.replace("  when: after", "  whn: after"))]
    assert "kill.whn" in paths
    assert "kill.when" in paths


def test_mixing_the_two_kill_styles_is_rejected():
    text = BASE + "  event: side_effect\n  n: 1\n"
    (path, message), = errors_for(text)
    assert path == "kill"
    assert "not both" in message
    assert "checkpoint" in message and "event" in message


def test_a_missing_kill_is_rejected():
    text = "version: 1\nkind: recovery\nseed: 1\n"
    messages = {path: message for path, message in errors_for(text)}
    assert "kill" in messages
    assert "required" in messages["kill"]


def test_an_event_kill_needs_a_positive_n():
    text = "version: 1\nkind: recovery\nseed: 1\nkill:\n  event: side_effect\n  n: 0\n"
    assert [path for path, _ in errors_for(text)] == ["kill.n"]


def test_bad_limits_are_rejected_field_by_field():
    text = BASE + (
        "limits:\n"
        "  runtime_ms: 0\n"
        "  max_restarts: -1\n"
        "  max_output_bytes: nope\n"
        "  budget_ms: 5\n"
    )
    paths = [path for path, _ in errors_for(text)]
    assert sorted(paths) == [
        "limits.budget_ms",
        "limits.max_output_bytes",
        "limits.max_restarts",
        "limits.runtime_ms",
    ]


def test_unknown_assertion_names_are_rejected():
    text = BASE + "assertions:\n  - at-least-once\n"
    (path, message), = errors_for(text)
    assert path == "assertions[0]"
    assert "at-least-once" in message


def test_the_wrong_kind_is_rejected_so_af01_scenarios_cannot_be_run_here():
    text = BASE.replace("kind: recovery", "kind: scenario")
    messages = {path: message for path, message in errors_for(text)}
    assert "kind" in messages
