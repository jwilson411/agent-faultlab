"""agent-faultlab: deterministic callable and HTTP fault injection with a scenario runner."""

from __future__ import annotations

from .clock import FakeClock
from .http_server import HttpFaultServer, HttpRequestRecord, redact_headers
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
    "FakeClock",
    "FaultProxy",
    "HttpFaultServer",
    "HttpRequestRecord",
    "Inject",
    "ResolutionError",
    "Rule",
    "Scenario",
    "ScenarioError",
    "ValidationError",
    "__version__",
    "dumps",
    "jsonable",
    "load_scenario",
    "redact_headers",
    "run_scenario",
    "validate_document",
]
