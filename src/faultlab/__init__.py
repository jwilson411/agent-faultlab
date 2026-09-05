"""agent-faultlab: deterministic callable, HTTP and idempotency fault injection with a scenario runner."""

from __future__ import annotations

from .clock import FakeClock
from .http_server import HttpFaultServer, HttpRequestRecord, redact_headers
from .idempotency import (
    AttemptRecord,
    CommitRecord,
    IdempotencyOracle,
    KeyReuseError,
    Ledger,
    LedgerError,
    ResponseLost,
    SideEffectSink,
    fingerprint,
)
from .report import dumps, jsonable
from .runner import FaultProxy, ResolutionError, run_scenario
from .schema import (
    Inject,
    Rule,
    Scenario,
    ScenarioError,
    ValidationError,
    load_scenario,
    validate_document,
)

__version__ = "0.1.0"

__all__ = [
    "AttemptRecord",
    "CommitRecord",
    "FakeClock",
    "FaultProxy",
    "HttpFaultServer",
    "HttpRequestRecord",
    "IdempotencyOracle",
    "Inject",
    "KeyReuseError",
    "Ledger",
    "LedgerError",
    "ResolutionError",
    "ResponseLost",
    "Rule",
    "Scenario",
    "ScenarioError",
    "SideEffectSink",
    "ValidationError",
    "__version__",
    "dumps",
    "fingerprint",
    "jsonable",
    "load_scenario",
    "redact_headers",
    "run_scenario",
    "validate_document",
]
