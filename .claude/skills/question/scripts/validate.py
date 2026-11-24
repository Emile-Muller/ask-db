"""Validate a /question report against ../schema.json.

Usage:
    python validate.py [report.json]     (reads stdin when no file is given)

Prints "valid" and exits 0, or prints one error per line and exits 2 (1 on unreadable
input). The /question contract lives in this skill: ../schema.json plus the cross-field
rules below. Only the generic checker and command line are shared
(src/text2sql/json_contract.py, also used by the /question-review writer).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from text2sql import json_contract
from text2sql.config import PREVIEW_ROWS

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.json"
LAYERS = ("input_screen", "generation", "validator", "executor", "output_check")


def cross_checks(report: dict[str, Any]) -> list[str]:
    """Rules between fields that schema.json cannot express (run after it passes)."""
    errors: list[str] = []
    stages = report["stages"]
    statuses = [stages[name] for name in LAYERS]
    if "fail" in statuses:
        first = statuses.index("fail")
        if any(s != "skipped" for s in statuses[first + 1 :]):
            errors.append("$.stages: every stage after the first fail must be skipped")
    failed = next((n for n in LAYERS if stages[n] == "fail"), None)
    if report["verdict"] == "accepted" and failed:
        errors.append(f"$.stages: accepted, yet {failed} failed")
    if report["verdict"] == "refused" and failed != report["decided_by"]:
        errors.append(f"$.decided_by: {report['decided_by']} refused, yet failed stage is {failed}")
    result = report["result"]
    if result:
        width = len(result["columns"])
        for i, row in enumerate(result["rows"]):
            if len(row) != width:
                errors.append(f"$.result.rows[{i}]: {len(row)} values for {width} columns")
        expected = min(result["row_count"], PREVIEW_ROWS)
        if len(result["rows"]) != expected:
            errors.append(f"$.result.rows: {len(result['rows'])} rows, expected {expected}")
    return errors


def validate(report: Any) -> list[str]:
    return json_contract.check_schema(report, SCHEMA_PATH) or cross_checks(report)


def main(argv: list[str]) -> int:
    return json_contract.main(argv, validate, "report")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
