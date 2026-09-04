from __future__ import annotations

import time

import pytest

from faultlab.clock import FakeClock
from faultlab.report import dumps
from faultlab.runner import ResolutionError, resolve_exception, run_scenario

from . import tool_module
from .conftest import scenario_from

FIVE = "tests.tool_module:call_five"
ONCE = "tests.tool_module:call_once"
TOOL = "tests.tool_module:tool"


def build(rules: str, seed: int = 42, target: str = TOOL) -> object:
    return scenario_from(
        f"version: 1\nseed: {seed}\nclock: fake\ntarget:\n  callable: {target}\nrules:\n{rules}"
    )


def injected_calls(payload: dict) -> list[int]:
    return [entry["call"] for entry in payload["sequence"] if entry["injects"]]


EXC_TIMEOUT = """    inject:
      exception:
        type: TimeoutError
        message: "boom"
"""


def test_nth_fires_on_that_call_only():
    scenario = build("  - id: r1\n    when: before\n    match: nth\n    n: 3\n" + EXC_TIMEOUT)
    payload, code = run_scenario(scenario, FIVE)
    assert code == 0
    assert injected_calls(payload) == [3]
    assert payload["result"] == ["real", "real", "TimeoutError", "real", "real"]
    assert len(tool_module.REAL_CALLS) == 4


def test_next_n_fires_on_the_first_n_calls():
    scenario = build("  - id: r1\n    when: before\n    match: next_n\n    n: 2\n" + EXC_TIMEOUT)
    payload, _ = run_scenario(scenario, FIVE)
    assert injected_calls(payload) == [1, 2]
    assert payload["result"] == ["TimeoutError", "TimeoutError", "real", "real", "real"]


def test_always_fires_on_every_call():
    scenario = build("  - id: r1\n    when: before\n    match: always\n" + EXC_TIMEOUT)
    payload, _ = run_scenario(scenario, FIVE)
    assert injected_calls(payload) == [1, 2, 3, 4, 5]
    assert tool_module.REAL_CALLS == []


def test_before_return_skips_the_real_call():
    scenario = build('  - id: r1\n    when: before\n    match: always\n    inject:\n      return: "stub"\n')
    payload, code = run_scenario(scenario, ONCE)
    assert code == 0
    assert payload["result"] == "stub"
    assert tool_module.REAL_CALLS == []
    assert payload["sequence"][0]["injects"][0]["kind"] == "return"


def test_after_return_replaces_the_real_result():
    scenario = build('  - id: r1\n    when: after\n    match: always\n    inject:\n      return: "stub"\n')
    payload, code = run_scenario(scenario, ONCE)
    assert code == 0
    assert payload["result"] == "stub"
    assert tool_module.REAL_CALLS == [1]
    assert payload["sequence"][0]["injects"][0]["when"] == "after"


def test_after_exception_raises_once_the_real_call_returned():
    scenario = build("  - id: r1\n    when: after\n    match: always\n" + EXC_TIMEOUT)
    payload, code = run_scenario(scenario, ONCE)
    assert code == 1
    assert payload["ok"] is False
    assert payload["error"] == {"type": "TimeoutError", "message": "boom"}
    assert tool_module.REAL_CALLS == [1]
    assert payload["sequence"][0]["raised"] == {"type": "TimeoutError", "message": "boom"}


def test_malformed_is_tagged_distinctly_from_return():
    scenario = build(
        "  - id: r1\n    when: after\n    match: always\n"
        '    inject:\n      malformed:\n        value:\n          status: "ok"\n          payload: null\n'
    )
    payload, code = run_scenario(scenario, ONCE)
    assert code == 0
    assert payload["result"] == {"status": "ok", "payload": None}
    inject = payload["sequence"][0]["injects"][0]
    assert inject["kind"] == "malformed"
    assert inject["value"] == {"status": "ok", "payload": None}


def test_delay_advances_the_fake_clock_without_sleeping():
    scenario = build(
        "  - id: r1\n    when: before\n    match: always\n    inject:\n      delay_ms: 10000\n"
    )
    started = time.perf_counter()
    payload, code = run_scenario(scenario, FIVE)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05
    assert code == 0
    assert payload["clock_ms"] == 50000
    assert payload["sequence"][0]["clock_ms_before"] == 0
    assert payload["sequence"][0]["clock_ms_after"] == 10000
    assert payload["sequence"][4]["clock_ms_after"] == 50000
    assert tool_module.REAL_CALLS == [1, 2, 3, 4, 5]


def test_delay_only_still_calls_the_real_function():
    scenario = build(
        "  - id: r1\n    when: before\n    match: nth\n    n: 1\n    inject:\n      delay_ms: 250\n"
    )
    payload, _ = run_scenario(scenario, ONCE)
    assert payload["result"] == "real"
    assert payload["sequence"][0]["injects"][0]["delay_ms"] == 250


def test_custom_exception_resolved_via_module_ref():
    scenario = build(
        "  - id: r1\n    when: before\n    match: always\n"
        "    inject:\n      exception:\n        type: tests.tool_module:CustomError\n"
        '        message: "custom"\n'
    )
    payload, code = run_scenario(scenario, ONCE)
    assert code == 1
    assert payload["error"] == {"type": "CustomError", "message": "custom"}


def test_resolve_exception_rejects_non_exceptions():
    with pytest.raises(ResolutionError):
        resolve_exception("NotAnException")
    with pytest.raises(ResolutionError):
        resolve_exception("tests.tool_module:tool")


def test_subject_equal_to_target_is_wrapped_directly():
    scenario = build(
        '  - id: r1\n    when: before\n    match: always\n    inject:\n      return: "direct"\n',
        target=TOOL,
    )
    payload, code = run_scenario(scenario, TOOL)
    assert code == 0
    assert payload["subject"] == TOOL
    assert payload["target"] == TOOL
    assert payload["result"] == "direct"
    assert len(payload["sequence"]) == 1


def test_target_defaults_to_subject_when_omitted():
    scenario = scenario_from(
        "version: 1\nseed: 42\nclock: fake\nrules:\n"
        '  - id: r1\n    when: before\n    match: always\n    inject:\n      return: "d"\n'
    )
    payload, _ = run_scenario(scenario, TOOL)
    assert payload["target"] == TOOL


def test_patched_tool_is_restored_after_the_run():
    original = tool_module.tool
    scenario = build("  - id: r1\n    when: before\n    match: always\n" + EXC_TIMEOUT)
    run_scenario(scenario, FIVE)
    assert tool_module.tool is original


def test_report_is_byte_identical_for_the_same_scenario_and_seed():
    text = "  - id: r1\n    when: before\n    match: next_n\n    n: 2\n" + EXC_TIMEOUT
    first, _ = run_scenario(build(text), FIVE)
    tool_module.REAL_CALLS.clear()
    second, _ = run_scenario(build(text), FIVE)
    assert dumps(first).encode() == dumps(second).encode()


def test_seed_is_reported_and_does_not_change_a_rule_determined_sequence():
    text = "  - id: r1\n    when: before\n    match: next_n\n    n: 2\n" + EXC_TIMEOUT
    seeded_42, _ = run_scenario(build(text, seed=42), FIVE)
    tool_module.REAL_CALLS.clear()
    seeded_7, _ = run_scenario(build(text, seed=7), FIVE)
    assert seeded_42["seed"] == 42
    assert seeded_7["seed"] == 7
    assert seeded_42["sequence"] == seeded_7["sequence"]


def test_fake_clock_never_moves_on_its_own():
    clock = FakeClock()
    assert clock.now_ms() == 0
    clock.advance_ms(1500)
    assert clock.now_ms() == 1500
    with pytest.raises(ValueError):
        clock.advance_ms(-1)


def test_wrapped_callable_receives_the_clock_when_it_accepts_one():
    scenario = build(
        "  - id: r1\n    when: before\n    match: nth\n    n: 1\n    inject:\n      delay_ms: 400\n"
    )
    payload, _ = run_scenario(scenario, ONCE)
    assert payload["clock_ms"] == 400
    assert tool_module.SEEN_CLOCK_MS == [400]


def test_fail_fail_succeed_example():
    from faultlab.schema import load_scenario

    from .conftest import REPO_ROOT

    scenario = load_scenario(str(REPO_ROOT / "examples" / "fail_fail_succeed.yaml"))
    payload, code = run_scenario(scenario, "examples.retry_subject:retry_until_ok")
    assert code == 0
    assert payload["ok"] is True
    assert payload["result"] == "ok"
    assert [entry["call"] for entry in payload["sequence"]] == [1, 2, 3]
    assert payload["sequence"][0]["raised"]["type"] == "TimeoutError"
    assert payload["sequence"][1]["raised"]["type"] == "ConnectionError"
    assert payload["sequence"][2]["returned"] == "ok"
    assert payload["clock_ms"] == 300
