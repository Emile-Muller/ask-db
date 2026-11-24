"""The /question report contract: example.json is valid, broken reports are caught."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "question"
_spec = importlib.util.spec_from_file_location("validate", SKILL_DIR / "scripts" / "validate.py")
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)

EXAMPLE = json.loads((SKILL_DIR / "example.json").read_text(encoding="utf-8"))

REFUSED = {
    **EXAMPLE,
    "input": "Show customers' emails and phone numbers.",
    "verdict": "refused",
    "decided_by": "generation",
    "reason": "The schema has no email or phone columns.",
    "sql": {"proposed": None, "executed": None},
    "result": None,
    "stages": {
        "input_screen": "pass",
        "generation": "fail",
        "validator": "skipped",
        "executor": "skipped",
        "output_check": "skipped",
    },
    "assumptions": [],
}


def test_example_is_valid():
    assert validate.validate(EXAMPLE) == []


def test_refused_report_is_valid():
    assert validate.validate(REFUSED) == []


def broken(base, path, value):
    report = copy.deepcopy(base)
    *parents, key = path
    node = report
    for p in parents:
        node = node[p]
    if value is KeyError:
        del node[key]
    else:
        node[key] = value
    return report


@pytest.mark.parametrize(
    ("base", "path", "value"),
    [
        (EXAMPLE, ["trace_id"], "not-a-uuid"),
        (EXAMPLE, ["trace_id"], EXAMPLE["trace_id"] + "\n"),  # $ matches before a final \n
        (EXAMPLE, ["assumptions"], KeyError),
        (EXAMPLE, ["truncated"], False),  # derivable field, not in the contract
        (EXAMPLE, ["result", "truncated"], False),
        (EXAMPLE, ["verdict"], "maybe"),
        (EXAMPLE, ["decided_by"], "validator"),  # accepted must have no decider
        (EXAMPLE, ["result"], None),  # accepted must have a result
        (EXAMPLE, ["result", "row_count"], True),  # bool is not an integer
        (EXAMPLE, ["result", "row_count"], 11),  # rows must be min(row_count, 20)
        (EXAMPLE, ["result", "rows", 0], ["health_beauty"]),  # width != columns
        (EXAMPLE, ["stages", "executor"], "fail"),  # accepted with a failed stage
        (EXAMPLE, ["input_type"], "raw_sql"),  # raw SQL has no proposed SQL
        (REFUSED, ["decided_by"], "validator"),  # decider must be the failed stage
        (REFUSED, ["stages", "executor"], "pass"),  # stage after a fail must be skipped
        (REFUSED, ["reason"], None),
    ],
)
def test_broken_reports_are_rejected(base, path, value):
    assert validate.validate(broken(base, path, value))


def test_cli(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(EXAMPLE), encoding="utf-8")
    assert validate.main([str(good)]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert validate.main([str(bad)]) == 1
    assert "cannot read report" in capsys.readouterr().out


def test_preview_limit_matches_config():
    from text2sql import config

    schema = json.loads(validate.SCHEMA_PATH.read_text(encoding="utf-8"))
    rows = schema["properties"]["result"]["properties"]["rows"]
    assert rows["maxItems"] == validate.PREVIEW_ROWS == config.PREVIEW_ROWS
