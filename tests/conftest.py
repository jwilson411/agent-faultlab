from __future__ import annotations

import sys
from pathlib import Path

import pytest

import yaml

from faultlab.clock import FakeClock
from faultlab.http_server import HttpFaultServer
from faultlab.schema import Scenario, validate_document

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def scenario_from(text: str) -> Scenario:
    """Validate an inline YAML scenario, returning the parsed Scenario."""
    return validate_document(yaml.safe_load(text), source="<inline>")


@pytest.fixture
def http_fault_server():
    """Factory for started HttpFaultServers; every one is torn down after the test."""
    started: list[HttpFaultServer] = []

    def start(
        scenario: Scenario | str,
        clock: FakeClock | None = None,
        redact_extra=None,
    ) -> HttpFaultServer:
        if isinstance(scenario, str):
            scenario = scenario_from(scenario)
        server = HttpFaultServer(scenario, clock=clock, redact_extra=redact_extra)
        started.append(server)
        return server.start()

    try:
        yield start
    finally:
        for server in started:
            server.stop()


@pytest.fixture(autouse=True)
def reset_tool_calls():
    from tests import tool_module

    tool_module.REAL_CALLS.clear()
    tool_module.SEEN_CLOCK_MS.clear()
    yield
    tool_module.REAL_CALLS.clear()
    tool_module.SEEN_CLOCK_MS.clear()
