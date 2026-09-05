"""A minimal JUnit XML writer (stdlib ElementTree only).

The XML carries no wall-clock timestamp and no duration: a run of the same
ledger produces byte-identical bytes. When a consumer insists on a
``timestamp`` attribute, pass ``timestamp=FIXED_TIMESTAMP``.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

FIXED_TIMESTAMP = "1970-01-01T00:00:00"
FIXED_TIME = "0.000"
DEFAULT_SUITE = "faultlab-oracle"
DEFAULT_CLASSNAME = "faultlab.oracle"
RECOVERY_SUITE = "faultlab-recovery"
RECOVERY_CLASSNAME = "faultlab.recovery"


@dataclass(frozen=True)
class Failure:
    """One failure inside a testcase."""

    message: str
    details: str = ""
    type: str = "IdempotencyViolation"


@dataclass(frozen=True)
class TestCase:
    """One named testcase; zero failures means it passed."""

    name: str
    classname: str = DEFAULT_CLASSNAME
    failures: tuple[Failure, ...] = field(default_factory=tuple)


def render(
    cases: list[TestCase],
    *,
    suite: str = DEFAULT_SUITE,
    timestamp: str | None = None,
) -> str:
    """Render testcases as a JUnit XML document ending in a newline."""
    failures = sum(1 for case in cases if case.failures)
    suites = ET.Element(
        "testsuites",
        {"name": suite, "tests": str(len(cases)), "failures": str(failures)},
    )
    attrs = {
        "name": suite,
        "tests": str(len(cases)),
        "failures": str(failures),
        "errors": "0",
        "skipped": "0",
        "time": FIXED_TIME,
    }
    if timestamp is not None:
        attrs["timestamp"] = timestamp
    suite_element = ET.SubElement(suites, "testsuite", attrs)
    for case in cases:
        case_element = ET.SubElement(
            suite_element,
            "testcase",
            {"name": case.name, "classname": case.classname, "time": FIXED_TIME},
        )
        for failure in case.failures:
            element = ET.SubElement(
                case_element,
                "failure",
                {"message": failure.message, "type": failure.type},
            )
            element.text = failure.details or failure.message
    ET.indent(suites, space="  ")
    return ET.tostring(suites, encoding="unicode") + "\n"


def write(
    path: str | os.PathLike[str],
    cases: list[TestCase],
    *,
    suite: str = DEFAULT_SUITE,
    timestamp: str | None = None,
) -> str:
    """Render and write a JUnit XML report. Returns what was written."""
    text = render(cases, suite=suite, timestamp=timestamp)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return text
