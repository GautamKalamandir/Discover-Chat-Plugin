"""Turns a captured (real) schema payload into a committable test fixture with the same structure.

Usage (from backend/):
    uv run python -m app.jobs.anonymize_capture spikes/captures/<bundle>/schema_payload.json \\
        tests/fixtures/schemas/captured-sales.json

Every string value becomes a consistent alias (the same original → the same alias everywhere, so
table/column references still line up); keys, nesting, numbers and booleans are kept, as are a few
structural vocabularies (data types). JSON embedded in strings is anonymized recursively. The output
is verified to contain none of the original strings.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Structural values that describe the format, not the company's model.
SAFE_VALUES = {
    "string",
    "int64",
    "double",
    "decimal",
    "datetime",
    "date",
    "boolean",
    "binary",
    "variant",
    "currency",
    "integer",
    "number",
    "text",
    "true",
    "false",
    "none",
    "null",
    "table",
    "column",
    "measure",
    "relationship",
    "hierarchy",
    "calculated",
    "data",
    "onetomany",
    "manytoone",
    "onetoone",
    "manytomany",
    "both",
    "single",
    "oneDirection".lower(),
}


class Anonymizer:
    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}

    def alias(self, value: str) -> str:
        if value.strip().lower() in SAFE_VALUES or not value.strip():
            return value
        if value not in self.aliases:
            self.aliases[value] = f"S{len(self.aliases) + 1}"
        return self.aliases[value]

    def walk(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {key: self.walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [self.walk(value) for value in node]
        if isinstance(node, str):
            embedded = _json_object(node)
            return json.dumps(self.walk(embedded)) if embedded is not None else self.alias(node)
        return node


def _json_object(text: str) -> Any:
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def anonymize(payload: Any) -> tuple[Any, list[str]]:
    """(anonymized payload, original strings that still appear in the output — must be empty)."""
    anonymizer = Anonymizer()
    result = anonymizer.walk(payload)
    output = json.dumps(result)
    leaks = [s for s in anonymizer.aliases if len(s) >= 3 and f'"{s}"' in output]
    return result, leaks


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.jobs.anonymize_capture", description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args(argv)
    payload = json.loads(args.source.read_text(encoding="utf-8"))
    result, leaks = anonymize(payload)
    if leaks:
        print(f"refusing to write: {len(leaks)} original values would remain")
        return 1
    args.target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.target} (structure kept, all names replaced)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
