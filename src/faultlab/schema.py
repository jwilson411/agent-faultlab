"""Scenario loading and validation.

Validation is total: every problem in a document is reported with the path that
caused it, the rule id when one applies, what was wrong, and what is allowed.
Nothing in this module imports or executes the subject under test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import yaml

WHEN_VALUES = ("before", "after")
MATCH_VALUES = ("nth", "next_n", "always")
INJECT_KINDS = ("delay_ms", "exception", "return", "malformed", "http")
HTTP_ACTIONS = (
    "status",
    "timeout",
    "close_before_headers",
    "truncated_body",
    "malformed_json",
    "success",
)
HTTP_FIELDS = ("action", "status", "headers", "body", "retry_after")


@dataclass(frozen=True)
class ValidationError:
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


class ScenarioError(Exception):
    """Raised when a scenario document is not usable."""

    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e.path}: {e.message}" for e in errors))


@dataclass(frozen=True)
class Inject:
    kind: str  # delay | exception | return | malformed | http
    delay_ms: float | None = None
    exc_type: str | None = None
    exc_message: str = ""
    value: Any = None
    action: str | None = None
    status: int | None = None
    headers: tuple[tuple[str, str], ...] = ()
    retry_after: str | None = None

    @property
    def identity(self) -> str:
        """Canonical form used to decide whether two injects agree."""
        if self.kind == "delay":
            return f"delay:{self.delay_ms!r}"
        if self.kind == "exception":
            return f"exception:{self.exc_type}:{self.exc_message}"
        if self.kind == "http":
            return (
                f"http:{self.action}:{self.status}:{self.retry_after}:"
                f"{_stable(list(self.headers))}:{_stable(self.value)}"
            )
        return f"{self.kind}:{_stable(self.value)}"

    def describe(self) -> str:
        if self.kind == "delay":
            return f"delay_ms={self.delay_ms}"
        if self.kind == "exception":
            return f"exception {self.exc_type}"
        if self.kind == "http":
            suffix = f" {self.status}" if self.status is not None else ""
            return f"http {self.action}{suffix}"
        return f"{self.kind} {_stable(self.value)}"


@dataclass(frozen=True)
class Rule:
    id: str
    when: str
    match: str
    n: int | None
    inject: Inject

    def applies(self, call_index: int) -> bool:
        if self.match == "always":
            return True
        if self.match == "nth":
            return call_index == self.n
        return call_index <= (self.n or 0)


@dataclass(frozen=True)
class Scenario:
    version: int
    seed: int
    clock: str
    rules: list[Rule]
    target: str | None = None
    source: str | None = field(default=None, compare=False)


def _stable(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)
    except (TypeError, ValueError):  # pragma: no cover - default=repr covers it
        return repr(value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_yaml(path: str) -> Any:
    """Parse a YAML document, converting parse failures into ScenarioError."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except FileNotFoundError:
        raise ScenarioError([ValidationError(path, "scenario file not found")]) from None
    except yaml.YAMLError as exc:
        raise ScenarioError([ValidationError(path, f"invalid YAML: {exc}")]) from None


def validate_document(document: Any, source: str | None = None) -> Scenario:
    """Validate a parsed scenario document. Raises ScenarioError on any problem."""
    errors: list[ValidationError] = []

    if not isinstance(document, dict):
        raise ScenarioError(
            [ValidationError("<root>", "scenario must be a YAML mapping")]
        )

    allowed_root = {"version", "seed", "clock", "target", "rules"}
    for key in document:
        if key not in allowed_root:
            errors.append(
                ValidationError(
                    str(key),
                    "unknown field; allowed: " + ", ".join(sorted(allowed_root)),
                )
            )

    version = document.get("version")
    if version is None:
        errors.append(ValidationError("version", "required; must be the integer 1"))
    elif version != 1 or not _is_int(version):
        errors.append(ValidationError("version", f"must be 1, got {version!r}"))

    seed = document.get("seed")
    if seed is None:
        errors.append(ValidationError("seed", "required; must be an integer"))
    elif not _is_int(seed):
        errors.append(ValidationError("seed", f"must be an integer, got {seed!r}"))

    clock = document.get("clock")
    if clock is None:
        errors.append(ValidationError("clock", 'required; only "fake" is allowed'))
    elif clock != "fake":
        errors.append(
            ValidationError("clock", f'must be "fake", got {clock!r}; real clocks are not supported')
        )

    target = _validate_target(document.get("target"), "target", errors)
    rules = _validate_rules(document.get("rules"), errors)

    if not errors:
        errors.extend(_find_contradictions(rules))

    if errors:
        raise ScenarioError(errors)

    return Scenario(
        version=1,
        seed=int(seed),
        clock="fake",
        rules=rules,
        target=target,
        source=source,
    )


def _validate_target(raw: Any, path: str, errors: list[ValidationError]) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(ValidationError(path, "must be a mapping with a 'callable' field"))
        return None
    for key in raw:
        if key != "callable":
            errors.append(
                ValidationError(f"{path}.{key}", "unknown field; allowed: callable")
            )
    callable_ref = raw.get("callable")
    if callable_ref is None:
        errors.append(
            ValidationError(f"{path}.callable", "required; expected 'package.module:function'")
        )
        return None
    if not isinstance(callable_ref, str) or callable_ref.count(":") != 1:
        errors.append(
            ValidationError(
                f"{path}.callable",
                f"must be a string of the form 'package.module:function', got {callable_ref!r}",
            )
        )
        return None
    return callable_ref


def _validate_rules(raw: Any, errors: list[ValidationError]) -> list[Rule]:
    if raw is None:
        errors.append(ValidationError("rules", "required; must be a non-empty list"))
        return []
    if not isinstance(raw, list):
        errors.append(ValidationError("rules", f"must be a list, got {type(raw).__name__}"))
        return []
    if not raw:
        errors.append(ValidationError("rules", "must contain at least one rule"))
        return []

    rules: list[Rule] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        path = f"rules[{index}]"
        rule = _validate_rule(item, path, errors)
        if rule is None:
            continue
        if rule.id in seen_ids:
            errors.append(
                ValidationError(f"{path}.id", f"duplicate rule id {rule.id!r}; ids must be unique")
            )
            continue
        seen_ids.add(rule.id)
        rules.append(rule)
    return rules


def _validate_rule(raw: Any, path: str, errors: list[ValidationError]) -> Rule | None:
    if not isinstance(raw, dict):
        errors.append(ValidationError(path, "each rule must be a mapping"))
        return None

    allowed = {"id", "when", "match", "n", "inject"}
    for key in raw:
        if key not in allowed:
            errors.append(
                ValidationError(
                    f"{path}.{key}", "unknown field; allowed: " + ", ".join(sorted(allowed))
                )
            )

    rule_id = raw.get("id")
    if rule_id is None:
        errors.append(ValidationError(f"{path}.id", "required; must be a unique string"))
    elif not isinstance(rule_id, str) or not rule_id:
        errors.append(ValidationError(f"{path}.id", f"must be a non-empty string, got {rule_id!r}"))

    when = raw.get("when")
    if when is None:
        errors.append(
            ValidationError(f"{path}.when", "required; allowed: " + ", ".join(WHEN_VALUES))
        )
    elif when not in WHEN_VALUES:
        errors.append(
            ValidationError(
                f"{path}.when", f"got {when!r}; allowed: " + ", ".join(WHEN_VALUES)
            )
        )

    match = raw.get("match")
    if match is None:
        errors.append(
            ValidationError(f"{path}.match", "required; allowed: " + ", ".join(MATCH_VALUES))
        )
    elif match not in MATCH_VALUES:
        errors.append(
            ValidationError(
                f"{path}.match", f"got {match!r}; allowed: " + ", ".join(MATCH_VALUES)
            )
        )

    n = raw.get("n")
    has_n = "n" in raw
    if match == "always":
        if has_n:
            errors.append(
                ValidationError(f"{path}.n", "forbidden when match is 'always'; remove it")
            )
        n = None
    elif match in ("nth", "next_n"):
        if not has_n:
            errors.append(
                ValidationError(
                    f"{path}.n", f"required when match is '{match}'; must be a positive integer"
                )
            )
            n = None
        elif not _is_int(n) or n < 1:
            errors.append(
                ValidationError(f"{path}.n", f"must be a positive integer, got {n!r}")
            )
            n = None
    else:
        n = None

    inject = _validate_inject(raw.get("inject"), f"{path}.inject", errors)

    if inject is not None and inject.kind == "http" and when == "after":
        errors.append(
            ValidationError(
                f"{path}.when",
                "http injects are the response itself; only 'before' is allowed",
            )
        )
        return None

    if inject is None or rule_id is None or when not in WHEN_VALUES or match not in MATCH_VALUES:
        return None
    if match in ("nth", "next_n") and n is None:
        return None
    return Rule(id=str(rule_id), when=when, match=match, n=n, inject=inject)


def _validate_inject(raw: Any, path: str, errors: list[ValidationError]) -> Inject | None:
    if raw is None:
        errors.append(
            ValidationError(path, "required; exactly one of: " + ", ".join(INJECT_KINDS))
        )
        return None
    if not isinstance(raw, dict):
        errors.append(ValidationError(path, "must be a mapping"))
        return None

    present = [key for key in INJECT_KINDS if key in raw]
    for key in raw:
        if key not in INJECT_KINDS:
            errors.append(
                ValidationError(
                    f"{path}.{key}", "unknown field; allowed: " + ", ".join(INJECT_KINDS)
                )
            )
    if len(present) != 1:
        errors.append(
            ValidationError(
                path,
                f"must contain exactly one of {', '.join(INJECT_KINDS)}; found "
                + (", ".join(present) if present else "none"),
            )
        )
        return None

    kind = present[0]
    if kind == "delay_ms":
        value = raw["delay_ms"]
        if not _is_number(value) or value < 0:
            errors.append(
                ValidationError(
                    f"{path}.delay_ms", f"must be a non-negative number, got {value!r}"
                )
            )
            return None
        return Inject(kind="delay", delay_ms=value)

    if kind == "exception":
        spec = raw["exception"]
        if not isinstance(spec, dict):
            errors.append(
                ValidationError(
                    f"{path}.exception", "must be a mapping with 'type' and optional 'message'"
                )
            )
            return None
        for key in spec:
            if key not in ("type", "message"):
                errors.append(
                    ValidationError(
                        f"{path}.exception.{key}", "unknown field; allowed: message, type"
                    )
                )
        exc_type = spec.get("type")
        if not isinstance(exc_type, str) or not exc_type:
            errors.append(
                ValidationError(
                    f"{path}.exception.type",
                    f"required; a builtin name or 'module:Name', got {exc_type!r}",
                )
            )
            return None
        message = spec.get("message", "")
        if not isinstance(message, str):
            errors.append(
                ValidationError(
                    f"{path}.exception.message", f"must be a string, got {message!r}"
                )
            )
            return None
        return Inject(kind="exception", exc_type=exc_type, exc_message=message)

    if kind == "return":
        return Inject(kind="return", value=raw["return"])

    if kind == "http":
        return _validate_http(raw["http"], f"{path}.http", errors)

    spec = raw["malformed"]
    if not isinstance(spec, dict):
        errors.append(
            ValidationError(f"{path}.malformed", "must be a mapping with a 'value' field")
        )
        return None
    for key in spec:
        if key != "value":
            errors.append(
                ValidationError(f"{path}.malformed.{key}", "unknown field; allowed: value")
            )
    if "value" not in spec:
        errors.append(
            ValidationError(f"{path}.malformed.value", "required; the malformed payload to return")
        )
        return None
    return Inject(kind="malformed", value=spec["value"])


def _validate_http(raw: Any, path: str, errors: list[ValidationError]) -> Inject | None:
    if not isinstance(raw, dict):
        errors.append(ValidationError(path, "must be a mapping with an 'action' field"))
        return None
    for key in raw:
        if key not in HTTP_FIELDS:
            errors.append(
                ValidationError(
                    f"{path}.{key}", "unknown field; allowed: " + ", ".join(sorted(HTTP_FIELDS))
                )
            )

    action = raw.get("action")
    if action is None:
        errors.append(
            ValidationError(f"{path}.action", "required; allowed: " + ", ".join(HTTP_ACTIONS))
        )
        return None
    if action not in HTTP_ACTIONS:
        errors.append(
            ValidationError(
                f"{path}.action", f"got {action!r}; allowed: " + ", ".join(HTTP_ACTIONS)
            )
        )
        return None

    status = raw.get("status")
    if action == "status":
        if status is None:
            errors.append(
                ValidationError(
                    f"{path}.status", "required when action is 'status'; an HTTP status code"
                )
            )
            return None
        if not _is_int(status) or not 100 <= status <= 599:
            errors.append(
                ValidationError(
                    f"{path}.status", f"must be an integer in 100..599, got {status!r}"
                )
            )
            return None
    elif status is not None:
        errors.append(
            ValidationError(f"{path}.status", f"only allowed when action is 'status', not {action!r}")
        )
        return None

    headers: tuple[tuple[str, str], ...] = ()
    raw_headers = raw.get("headers")
    if raw_headers is not None:
        if not isinstance(raw_headers, dict):
            errors.append(ValidationError(f"{path}.headers", "must be a mapping of str to str"))
            return None
        bad = [
            key
            for key, value in raw_headers.items()
            if not isinstance(key, str) or not isinstance(value, str)
        ]
        if bad:
            errors.append(
                ValidationError(
                    f"{path}.headers", "header names and values must be strings"
                )
            )
            return None
        headers = tuple(sorted((str(k), str(v)) for k, v in raw_headers.items()))

    retry_after = raw.get("retry_after")
    if retry_after is not None:
        if not (isinstance(retry_after, str) or _is_int(retry_after)):
            errors.append(
                ValidationError(
                    f"{path}.retry_after",
                    f"must be a string or an integer number of seconds, got {retry_after!r}",
                )
            )
            return None
        retry_after = str(retry_after)

    return Inject(
        kind="http",
        action=action,
        status=int(status) if status is not None else None,
        headers=headers,
        value=raw.get("body"),
        retry_after=retry_after,
    )


def _overlaps(left: Rule, right: Rule) -> bool:
    """True when both rules can fire on the same (when, call-index) slot."""
    if left.when != right.when:
        return False
    if left.match == "always" or right.match == "always":
        return True
    if left.match == "nth" and right.match == "nth":
        return left.n == right.n
    if left.match == "next_n" and right.match == "next_n":
        return True  # both cover call 1
    nth, next_n = (left, right) if left.match == "nth" else (right, left)
    return (nth.n or 0) <= (next_n.n or 0)


def _find_contradictions(rules: list[Rule]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for i, left in enumerate(rules):
        for right in rules[i + 1 :]:
            if not _overlaps(left, right):
                continue
            if left.inject.identity == right.inject.identity:
                continue
            errors.append(
                ValidationError(
                    "rules",
                    f"contradictory rules {left.id!r} and {right.id!r}: both apply to "
                    f"when={left.when} on the same call, but inject "
                    f"{left.inject.describe()} and {right.inject.describe()}; "
                    "overlapping rules must agree on kind and value",
                )
            )
    return errors


def load_scenario(path: str) -> Scenario:
    """Load and validate a scenario file. Raises ScenarioError on any problem."""
    return validate_document(load_yaml(path), source=path)
