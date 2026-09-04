"""Canonical JSON report construction.

Reports contain no wall-clock times, no hostnames and no durations, so the same
scenario and seed always produce byte-identical output.
"""

from __future__ import annotations

import json
from typing import Any

JSON_SCALARS = (str, int, float, bool)


def jsonable(value: Any) -> Any:
    """Best-effort conversion to a JSON-safe, deterministic value."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, JSON_SCALARS):
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    return repr(value)


def dumps(payload: Any) -> str:
    """Canonical JSON encoding with a trailing newline."""
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    )
