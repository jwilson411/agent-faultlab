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
from .recovery import RecoveryError, run_recovery
from .recovery_schema import (
    KillSpec,
    Limits,
    RecoveryScenario,
    load_recovery_scenario,
    validate_recovery_document,
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
    "KillSpec",
    "Ledger",
    "LedgerError",
    "Limits",
    "RecoveryError",
    "RecoveryScenario",
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
    "load_recovery_scenario",
    "load_scenario",
    "redact_headers",
    "run_recovery",
    "run_scenario",
    "validate_document",
    "validate_recovery_document",
]
