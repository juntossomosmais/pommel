#!/usr/bin/env python3
"""PreToolUse entry point for the conditional-rules hook system."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from conditional_rules import run_entry

if __name__ == "__main__":
    sys.exit(run_entry("PreToolUse", "conditional_rules_pre"))
