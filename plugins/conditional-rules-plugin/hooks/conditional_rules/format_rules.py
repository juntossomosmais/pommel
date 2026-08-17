#!/usr/bin/env python3
"""Reformat rules.json in place with the plugin's inline-leaf style.

Layout rules:
  - 2-space indent.
  - A dict whose values are all primitives (and whose inline form fits within
    LINE_WIDTH at the current depth) is rendered on a single line.
  - Every other object and array is fully expanded.

`json.tool` cannot reproduce this layout — do not use it on rules.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

INDENT = "  "
LINE_WIDTH = 120


def _is_prim(value: object) -> bool:
    return isinstance(value, (str, int, float, bool, type(None)))


def _fmt(node: object, depth: int = 0) -> str:
    pad = INDENT * depth
    child_pad = INDENT * (depth + 1)

    if _is_prim(node):
        return json.dumps(node, ensure_ascii=False)

    if isinstance(node, list):
        if not node:
            return "[]"
        parts = [_fmt(v, depth + 1) for v in node]
        return "[\n" + ",\n".join(child_pad + p for p in parts) + "\n" + pad + "]"

    if isinstance(node, dict):
        if not node:
            return "{}"
        if all(_is_prim(v) for v in node.values()):
            inline = (
                "{ "
                + ", ".join(
                    f"{json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}"
                    for k, v in node.items()
                )
                + " }"
            )
            if len(pad) + len(inline) <= LINE_WIDTH:
                return inline
        parts = [
            f"{child_pad}{json.dumps(k, ensure_ascii=False)}: {_fmt(v, depth + 1)}"
            for k, v in node.items()
        ]
        return "{\n" + ",\n".join(parts) + "\n" + pad + "}"

    raise TypeError(f"unsupported JSON node type: {type(node).__name__}")


def main() -> None:
    rules_file = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("rules.json")
    data = json.loads(rules_file.read_text(encoding="utf-8"))
    rules_file.write_text(_fmt(data) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
