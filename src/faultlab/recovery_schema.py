"""Recovery scenario loading and validation.

A recovery scenario is a different document from an AF-01 fault scenario. It
says where to crash a worker subprocess and what the harness is allowed to
spend; it says nothing about injecting faults into a callable. The two schemas
therefore get separate loaders, so each stays strict about its own unknown
fields, and ``faultlab validate`` keeps rejecting this document.

Validation is total in the same way AF-01 validation is: every problem is
reported with the path that caused it. Nothing here starts a process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schema import ScenarioError, ValidationError, load_yaml

KILL_WHEN = ("before", "after")
KILL_EVENTS = ("checkpoint", "side_effect")
ASSERTION_NAMES = ("exactly-once", "at-most-once", "no-key-reuse")
DEFAULT_ASSERTIONS = ("exactly-once", "at-most-once")

DEFAULT_RUNTIME_MS = 5000
DEFAULT_MAX_RESTARTS = 1
DEFAULT_MAX_OUTPUT_BYTES = 65536

ROOT_FIELDS = ("version", "kind", "seed", "kill", "limits", "assertions")
CHECKPOINT_KILL_FIELDS = ("when", "checkpoint")
EVENT_KILL_FIELDS = ("event", "n", "name")
LIMIT_FIELDS = ("runtime_ms", "max_restarts", "max_output_bytes")


@dataclass(frozen=True)
class KillSpec:
    """Where the harness terminates the worker, on the first process start."""

    style: str  # checkpoint | event
    when: str | None = None
    checkpoint: str | None = None
    event: str | None = None
    n: int | None = None
    name: str | None = None

    def describe(self) -> str:
        """The ``termination_point`` string recorded in the report."""
        if self.style == "checkpoint":
            return f"{self.when}:{self.checkpoint}"
        return f"after_event:{self.event}:{self.n}"


@dataclass(frozen=True)
class Limits:
    """What one recovery run may spend. Every field has a default."""

    runtime_ms: int = DEFAULT_RUNTIME_MS
    max_restarts: int = DEFAULT_MAX_RESTARTS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES


@dataclass(frozen=True)
class RecoveryScenario:
    version: int
    kind: str
    seed: int
    kill: KillSpec
    limits: Limits
    assertions: tuple[str, ...]
    source: str | None = None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _allowed(fields: tuple[str, ...]) -> str:
    return "allowed: " + ", ".join(sorted(fields))


def load_recovery_scenario(path: str) -> RecoveryScenario:
    """Load and validate a recovery scenario file. Raises ScenarioError."""
    return validate_recovery_document(load_yaml(path), source=path)


def validate_recovery_document(document: Any, source: str | None = None) -> RecoveryScenario:
    """Validate a parsed recovery document. Raises ScenarioError on any problem."""
    errors: list[ValidationError] = []

    if not isinstance(document, dict):
        raise ScenarioError(
            [ValidationError("<root>", "recovery scenario must be a YAML mapping")]
        )

    for key in document:
        if key not in ROOT_FIELDS:
            errors.append(ValidationError(str(key), f"unknown field; {_allowed(ROOT_FIELDS)}"))

    version = document.get("version")
    if version is None:
        errors.append(ValidationError("version", "required; must be the integer 1"))
    elif version != 1 or not _is_int(version):
        errors.append(ValidationError("version", f"must be 1, got {version!r}"))

    kind = document.get("kind")
    if kind is None:
        errors.append(ValidationError("kind", 'required; must be "recovery"'))
    elif kind != "recovery":
        errors.append(
            ValidationError("kind", f'must be "recovery", got {kind!r}')
        )

    seed = document.get("seed")
    if seed is None:
        errors.append(ValidationError("seed", "required; must be an integer"))
    elif not _is_int(seed):
        errors.append(ValidationError("seed", f"must be an integer, got {seed!r}"))

    kill = _validate_kill(document.get("kill"), errors)
    limits = _validate_limits(document.get("limits"), errors)
    assertions = _validate_assertions(document.get("assertions"), errors)

    if errors:
        raise ScenarioError(errors)

    assert kill is not None  # no errors means the kill spec was built
    return RecoveryScenario(
        version=1,
        kind="recovery",
        seed=int(seed),
        kill=kill,
        limits=limits,
        assertions=assertions,
        source=source,
    )


def _validate_kill(raw: Any, errors: list[ValidationError]) -> KillSpec | None:
    if raw is None:
        errors.append(
            ValidationError(
                "kill",
                "required; a mapping of 'when' and 'checkpoint', or of 'event' and 'n'",
            )
        )
        return None
    if not isinstance(raw, dict):
        errors.append(ValidationError("kill", f"must be a mapping, got {type(raw).__name__}"))
        return None

    keys = set(raw)
    by_checkpoint = keys & set(CHECKPOINT_KILL_FIELDS)
    by_event = keys & set(EVENT_KILL_FIELDS)
    if by_checkpoint and by_event:
        errors.append(
            ValidationError(
                "kill",
                "use either 'when' and 'checkpoint' or 'event' and 'n', not both: "
                + ", ".join(sorted(by_checkpoint | by_event)),
            )
        )
        return None
    if not by_checkpoint and not by_event:
        errors.append(
            ValidationError(
                "kill",
                "must set either 'when' and 'checkpoint' or 'event' and 'n'",
            )
        )
        return None

    if by_checkpoint:
        return _validate_checkpoint_kill(raw, errors)
    return _validate_event_kill(raw, errors)


def _validate_checkpoint_kill(raw: dict, errors: list[ValidationError]) -> KillSpec | None:
    before = len(errors)
    for key in raw:
        if key not in CHECKPOINT_KILL_FIELDS:
            errors.append(
                ValidationError(f"kill.{key}", f"unknown field; {_allowed(CHECKPOINT_KILL_FIELDS)}")
            )

    when = raw.get("when")
    if when is None:
        errors.append(ValidationError("kill.when", "required; one of " + ", ".join(KILL_WHEN)))
    elif when not in KILL_WHEN:
        errors.append(
            ValidationError("kill.when", f"must be one of {', '.join(KILL_WHEN)}, got {when!r}")
        )

    checkpoint = raw.get("checkpoint")
    if checkpoint is None:
        errors.append(ValidationError("kill.checkpoint", "required; a non-empty checkpoint name"))
    elif not isinstance(checkpoint, str) or not checkpoint:
        errors.append(
            ValidationError(
                "kill.checkpoint", f"must be a non-empty string, got {checkpoint!r}"
            )
        )

    if len(errors) > before:
        return None
    return KillSpec(style="checkpoint", when=when, checkpoint=checkpoint)


def _validate_event_kill(raw: dict, errors: list[ValidationError]) -> KillSpec | None:
    before = len(errors)
    for key in raw:
        if key not in EVENT_KILL_FIELDS:
            errors.append(
                ValidationError(f"kill.{key}", f"unknown field; {_allowed(EVENT_KILL_FIELDS)}")
            )

    event = raw.get("event")
    if event is None:
        errors.append(ValidationError("kill.event", "required; one of " + ", ".join(KILL_EVENTS)))
    elif event not in KILL_EVENTS:
        errors.append(
            ValidationError(
                "kill.event", f"must be one of {', '.join(KILL_EVENTS)}, got {event!r}"
            )
        )

    n = raw.get("n")
    if n is None:
        errors.append(ValidationError("kill.n", "required; a positive integer"))
    elif not _is_int(n) or n < 1:
        errors.append(ValidationError("kill.n", f"must be a positive integer, got {n!r}"))

    name = raw.get("name")
    if name is not None and (not isinstance(name, str) or not name):
        errors.append(ValidationError("kill.name", f"must be a non-empty string, got {name!r}"))

    if len(errors) > before:
        return None
    return KillSpec(style="event", event=event, n=int(n), name=name)


def _validate_limits(raw: Any, errors: list[ValidationError]) -> Limits:
    if raw is None:
        return Limits()
    if not isinstance(raw, dict):
        errors.append(ValidationError("limits", f"must be a mapping, got {type(raw).__name__}"))
        return Limits()

    for key in raw:
        if key not in LIMIT_FIELDS:
            errors.append(
                ValidationError(f"limits.{key}", f"unknown field; {_allowed(LIMIT_FIELDS)}")
            )

    values = {
        "runtime_ms": DEFAULT_RUNTIME_MS,
        "max_restarts": DEFAULT_MAX_RESTARTS,
        "max_output_bytes": DEFAULT_MAX_OUTPUT_BYTES,
    }
    for field_name, minimum in (
        ("runtime_ms", 1),
        ("max_restarts", 0),
        ("max_output_bytes", 1),
    ):
        given = raw.get(field_name)
        if given is None:
            continue
        if not _is_int(given) or given < minimum:
            wanted = "a positive integer" if minimum else "a non-negative integer"
            errors.append(
                ValidationError(f"limits.{field_name}", f"must be {wanted}, got {given!r}")
            )
            continue
        values[field_name] = int(given)
    return Limits(**values)


def _validate_assertions(raw: Any, errors: list[ValidationError]) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_ASSERTIONS
    if not isinstance(raw, list):
        errors.append(ValidationError("assertions", f"must be a list, got {type(raw).__name__}"))
        return DEFAULT_ASSERTIONS
    if not raw:
        errors.append(ValidationError("assertions", "must contain at least one assertion name"))
        return DEFAULT_ASSERTIONS

    names: list[str] = []
    for index, item in enumerate(raw):
        path = f"assertions[{index}]"
        if item not in ASSERTION_NAMES:
            errors.append(
                ValidationError(
                    path, f"unknown assertion {item!r}; {_allowed(ASSERTION_NAMES)}"
                )
            )
            continue
        if item in names:
            errors.append(ValidationError(path, f"duplicate assertion {item!r}"))
            continue
        names.append(item)
    return tuple(names) if names else DEFAULT_ASSERTIONS
