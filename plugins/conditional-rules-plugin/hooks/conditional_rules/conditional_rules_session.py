#!/usr/bin/env python3
"""SessionStart entry point for the conditional-rules hook system.

Wipes the per-session dedup cache, then evaluates and injects any rules whose
`fires_on_matcher` reaches SessionStart.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from conditional_rules import run_entry

if __name__ == "__main__":
    sys.exit(run_entry("SessionStart", "conditional_rules_session"))
