from __future__ import annotations

import pytest

from faultlab.schema import ScenarioError, load_scenario

from .conftest import REPO_ROOT, scenario_from

BASE = """
version: 1
seed: 42
clock: fake
target:
  callable: tests.tool_module:tool
rules:
  - id: r1
    when: before
    match: always
    inject:
      delay_ms: 5
"""


def errors_for(text: str) -> list[tuple[str, str]]:
    with pytest.raises(ScenarioError) as excinfo:
        scenario_from(text)
    return [(e.path, e.message) for e in excinfo.value.errors]


def test_minimal_scenario_validates():
    scenario = scenario_from(BASE)
    assert scenario.seed == 42
    assert scenario.clock == "fake"
    assert scenario.target == "tests.tool_module:tool"
    assert [rule.id for rule in scenario.rules] == ["r1"]


def test_unknown_root_field_names_path():
    paths = [path for path, _ in errors_for(BASE + "\nextra: 1\n")]
    assert "extra" in paths


def test_unknown_rule_field_names_path():
    text = BASE.replace("    when: before", "    whn: before\n    when: before")
    paths = [path for path, _ in errors_for(text)]
    assert "rules[0].whn" in paths


def test_unknown_inject_field_names_path():
    text = BASE.replace("      delay_ms: 5", "      delay_ms: 5\n      bogus: 1")
    paths = [path for path, _ in errors_for(text)]
    assert "rules[0].inject.bogus" in paths


@pytest.mark.parametrize(
    "original,replacement,expected_path",
    [
        ("version: 1", "version: 2", "version"),
        ("clock: fake", "clock: real", "clock"),
    ],
)
def test_version_and_clock_are_pinned(original, replacement, expected_path):
    paths = [path for path, _ in errors_for(BASE.replace(original, replacement))]
    assert expected_path in paths


def test_seed_required_and_integer():
    assert "seed" in [path for path, _ in errors_for(BASE.replace("seed: 42", "seed: abc"))]
    assert "seed" in [path for path, _ in errors_for(BASE.replace("seed: 42\n", ""))]


def test_rules_must_be_non_empty():
    text = "version: 1\nseed: 1\nclock: fake\nrules: []\n"
    assert "rules" in [path for path, _ in errors_for(text)]


def test_duplicate_rule_ids_rejected():
    text = BASE + """  - id: r1
    when: after
    match: always
    inject:
      delay_ms: 5
"""
    paths = [path for path, _ in errors_for(text)]
    assert "rules[1].id" in paths


def test_when_and_match_values_constrained():
    assert "rules[0].when" in [
        path for path, _ in errors_for(BASE.replace("when: before", "when: during"))
    ]
    assert "rules[0].match" in [
        path for path, _ in errors_for(BASE.replace("match: always", "match: sometimes"))
    ]


def test_n_required_for_nth_and_next_n():
    text = BASE.replace("match: always", "match: nth")
    assert any(path == "rules[0].n" and "required" in msg for path, msg in errors_for(text))


def test_n_must_be_positive_integer():
    text = BASE.replace("match: always", "match: next_n\n    n: 0")
    assert any(path == "rules[0].n" and "positive" in msg for path, msg in errors_for(text))


def test_n_forbidden_for_always():
    text = BASE.replace("match: always", "match: always\n    n: 2")
    assert any(path == "rules[0].n" and "forbidden" in msg for path, msg in errors_for(text))


def test_inject_must_have_exactly_one_kind():
    text = BASE.replace("      delay_ms: 5", '      delay_ms: 5\n      return: "x"')
    assert any(
        path == "rules[0].inject" and "exactly one" in msg for path, msg in errors_for(text)
    )
    empty = BASE.replace("    inject:\n      delay_ms: 5", "    inject: {}")
    assert any(
        path == "rules[0].inject" and "exactly one" in msg for path, msg in errors_for(empty)
    )


def test_delay_ms_must_be_non_negative():
    text = BASE.replace("delay_ms: 5", "delay_ms: -1")
    assert any(
        path == "rules[0].inject.delay_ms" and "non-negative" in msg
        for path, msg in errors_for(text)
    )


CONTRADICTION = """
version: 1
seed: 42
clock: fake
rules:
  - id: {a}
    when: {when_a}
    match: {match_a}
{n_a}    inject:
{inject_a}
  - id: {b}
    when: {when_b}
    match: {match_b}
{n_b}    inject:
{inject_b}
"""


def _pair(when_a, match_a, n_a, inject_a, when_b, match_b, n_b, inject_b):
    return CONTRADICTION.format(
        a="ra",
        b="rb",
        when_a=when_a,
        when_b=when_b,
        match_a=match_a,
        match_b=match_b,
        n_a=f"    n: {n_a}\n" if n_a else "",
        n_b=f"    n: {n_b}\n" if n_b else "",
        inject_a=inject_a,
        inject_b=inject_b,
    )


DELAY_5 = "      delay_ms: 5"
DELAY_9 = "      delay_ms: 9"
RETURN_OK = '      return: "ok"'


def test_always_conflicts_with_overlapping_nth():
    text = _pair("before", "always", None, DELAY_5, "before", "nth", 3, RETURN_OK)
    messages = [msg for _, msg in errors_for(text)]
    assert any("'ra'" in msg and "'rb'" in msg for msg in messages)


def test_conflicting_delays_on_same_slot_rejected():
    text = _pair("before", "nth", 2, DELAY_5, "before", "next_n", 4, DELAY_9)
    assert any("'ra'" in msg and "'rb'" in msg for _, msg in errors_for(text))


def test_equal_delays_on_same_slot_allowed():
    text = _pair("before", "nth", 2, DELAY_5, "before", "next_n", 4, DELAY_5)
    assert len(scenario_from(text).rules) == 2


def test_non_overlapping_rules_allowed():
    text = _pair("before", "nth", 1, DELAY_5, "before", "nth", 2, RETURN_OK)
    assert len(scenario_from(text).rules) == 2


def test_different_when_does_not_conflict():
    text = _pair("before", "always", None, DELAY_5, "after", "always", None, RETURN_OK)
    assert len(scenario_from(text).rules) == 2


def test_nth_beyond_next_n_window_does_not_conflict():
    text = _pair("before", "next_n", 2, DELAY_5, "before", "nth", 3, RETURN_OK)
    assert len(scenario_from(text).rules) == 2


def test_example_scenarios_validate():
    for name in (
        "fail_fail_succeed",
        "nth",
        "next_n",
        "after_malformed",
        "delay_fake_clock",
    ):
        scenario = load_scenario(str(REPO_ROOT / "examples" / f"{name}.yaml"))
        assert scenario.rules


def test_invalid_examples_are_rejected():
    with pytest.raises(ScenarioError) as unknown:
        load_scenario(str(REPO_ROOT / "examples" / "invalid_unknown_field.yaml"))
    assert any(e.path == "rules[0].whn" for e in unknown.value.errors)

    with pytest.raises(ScenarioError) as contradiction:
        load_scenario(str(REPO_ROOT / "examples" / "invalid_contradiction.yaml"))
    assert any(
        "'always-timeout'" in e.message and "'second-call-returns'" in e.message
        for e in contradiction.value.errors
    )


def test_missing_file_is_a_validation_error():
    with pytest.raises(ScenarioError):
        load_scenario(str(REPO_ROOT / "examples" / "does_not_exist.yaml"))
