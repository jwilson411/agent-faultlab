"""agent-faultlab: deterministic callable fault injection and scenario runner."""

from __future__ import annotations

from .clock import FakeClock
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
    "run_scenario",
    "validate_document",
]
