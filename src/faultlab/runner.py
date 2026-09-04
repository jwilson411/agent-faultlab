"""Scenario execution: wrap a tool callable, run a subject, record what happened."""

from __future__ import annotations

import builtins
import importlib
import inspect
import random
from dataclasses import dataclass, field
from typing import Any, Callable

from .clock import FakeClock
from .report import jsonable
from .schema import Rule, Scenario


class ResolutionError(Exception):
    """Raised when a 'module:name' reference cannot be imported."""


@dataclass
class _CallRecord:
    call: int
    clock_ms_before: int
    clock_ms_after: int = 0
    injects: list[dict[str, Any]] = field(default_factory=list)
    raised: dict[str, str] | None = None
    returned: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "call": self.call,
            "clock_ms_before": self.clock_ms_before,
            "clock_ms_after": self.clock_ms_after,
            "injects": self.injects,
            "raised": self.raised,
            "returned": self.returned,
        }


class _ShortCircuit(Exception):
    """Internal signal: an inject supplied the return value."""

    def __init__(self, value: Any) -> None:
        self.value = value
        super().__init__("short circuit")


def resolve_callable(ref: str) -> tuple[Any, str, Callable[..., Any]]:
    """Resolve 'package.module:name' to (module, attribute_name, callable)."""
    if ref.count(":") != 1:
        raise ResolutionError(
            f"{ref!r} is not a valid reference; expected 'package.module:function'"
        )
    module_name, attr = ref.split(":")
    if not module_name or not attr:
        raise ResolutionError(
            f"{ref!r} is not a valid reference; expected 'package.module:function'"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ResolutionError(f"cannot import module {module_name!r}: {exc}") from None
    try:
        target = getattr(module, attr)
    except AttributeError:
        raise ResolutionError(f"module {module_name!r} has no attribute {attr!r}") from None
    if not callable(target):
        raise ResolutionError(f"{ref!r} is not callable")
    return module, attr, target


def resolve_exception(name: str) -> type[BaseException]:
    """Resolve an exception name: builtins first, then 'module:Name'."""
    if ":" not in name:
        candidate = getattr(builtins, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            return candidate
        raise ResolutionError(
            f"{name!r} is not a builtin exception; use 'module:Name' for custom types"
        )
    module_name, attr = name.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ResolutionError(f"cannot import module {module_name!r}: {exc}") from None
    candidate = getattr(module, attr, None)
    if not (isinstance(candidate, type) and issubclass(candidate, BaseException)):
        raise ResolutionError(f"{name!r} does not name an exception class")
    return candidate


def _accepts_clock(func: Callable[..., Any]) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return False
    return "clock" in signature.parameters


class FaultProxy:
    """Wraps a tool callable and applies a scenario's rules around each call."""

    def __init__(self, func: Callable[..., Any], scenario: Scenario, clock: FakeClock) -> None:
        self._func = func
        self._scenario = scenario
        self._clock = clock
        self._pass_clock = _accepts_clock(func)
        # Seeded for any future stochastic matcher; rule matching itself is
        # fully determined, which is why the same seed always replays.
        self.rng = random.Random(scenario.seed)
        self.call_count = 0
        self.records: list[_CallRecord] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        record = _CallRecord(call=self.call_count, clock_ms_before=self._clock.now_ms())
        self.records.append(record)
        try:
            try:
                self._apply(record, "before")
            except _ShortCircuit as short:
                record.returned = jsonable(short.value)
                return short.value

            if self._pass_clock and "clock" not in kwargs:
                kwargs = {**kwargs, "clock": self._clock}
            result = self._func(*args, **kwargs)

            try:
                self._apply(record, "after")
            except _ShortCircuit as short:
                record.returned = jsonable(short.value)
                return short.value

            record.returned = jsonable(result)
            return result
        except BaseException as exc:
            record.raised = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            record.clock_ms_after = self._clock.now_ms()

    def _matching(self, when: str) -> list[Rule]:
        return [
            rule
            for rule in self._scenario.rules
            if rule.when == when and rule.applies(self.call_count)
        ]

    def _apply(self, record: _CallRecord, when: str) -> None:
        for rule in self._matching(when):
            inject = rule.inject
            entry: dict[str, Any] = {"id": rule.id, "when": when, "kind": inject.kind}
            if inject.kind == "delay":
                entry["delay_ms"] = inject.delay_ms
                record.injects.append(entry)
                self._clock.advance_ms(inject.delay_ms or 0)
                continue
            if inject.kind == "exception":
                entry["type"] = inject.exc_type
                record.injects.append(entry)
                raise resolve_exception(inject.exc_type or "")(inject.exc_message)
            entry["value"] = jsonable(inject.value)
            record.injects.append(entry)
            raise _ShortCircuit(inject.value)


def run_scenario(
    scenario: Scenario, subject_ref: str, args: list[str] | None = None
) -> tuple[dict[str, Any], int]:
    """Run ``subject_ref`` with the scenario's target callable wrapped.

    Returns the report payload and the process exit code (0 subject returned,
    1 subject raised).
    """
    args = list(args or [])
    clock = FakeClock()
    target_ref = scenario.target or subject_ref

    target_module, target_attr, target_func = resolve_callable(target_ref)
    proxy = FaultProxy(target_func, scenario, clock)

    patched: list[tuple[Any, str, Any]] = [(target_module, target_attr, target_func)]
    subject_module = None
    if subject_ref == target_ref:
        subject: Callable[..., Any] = proxy
    else:
        subject_module, _subject_attr, subject = resolve_callable(subject_ref)
        if hasattr(subject_module, target_attr):
            patched.append((subject_module, target_attr, getattr(subject_module, target_attr)))

    for module, attr, _original in patched:
        setattr(module, attr, proxy)

    kwargs: dict[str, Any] = {}
    if subject is not proxy and _accepts_clock(subject):
        kwargs["clock"] = clock

    ok = True
    result: Any = None
    error: dict[str, str] | None = None
    try:
        result = subject(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - the subject's failure is the result
        ok = False
        error = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        for module, attr, original in patched:
            setattr(module, attr, original)

    payload: dict[str, Any] = {
        "clock_ms": clock.now_ms(),
        "ok": ok,
        "result": jsonable(result) if ok else None,
        "seed": scenario.seed,
        "sequence": [record.as_dict() for record in proxy.records],
        "subject": subject_ref,
        "target": target_ref,
    }
    if error is not None:
        payload["error"] = error
    return payload, (0 if ok else 1)
