"""Import spy: touches FAULTLAB_SPY_MARKER at import time.

Used to prove that a validation failure never imports the subject.
"""

from __future__ import annotations

import os
from pathlib import Path

_marker = os.environ.get("FAULTLAB_SPY_MARKER")
if _marker:
    Path(_marker).write_text("imported", encoding="utf-8")


def subject(clock=None):
    return "should not run"
