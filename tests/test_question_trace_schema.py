"""The /question trace contract: src/text2sql/schemas/question_trace.json plus
trace.cross_checks, enforced by trace.append before anything reaches the log. Offline."""

from __future__ import annotations

import copy
import json

import pytest

from text2sql import cli, config, trace
from text2sql.trace import FAIL, PASS, SKIPPED, TraceBuilder


def accepted(row_count: int = 2) -> dict:
    b = TraceBuilder("Top sellers", "natural_language", model="claude-opus-5-5")
    for name in trace.LAYERS:
        b.record(name, PASS, {})
    b.generated_sql = "SELECT seller_id FROM s"
    b.normalized_sql = "SELECT seller_id FROM s LIMIT 100"
    shown = min(row_count, config.PREVIEW_ROWS)
    result = {
        "columns": ["seller_id", "revenue"],
        "rows": [["a", 1.5]] * shown,
        "row_count": row_count,
        "truncated": row_count > shown,
    }
    return b.accepted(result)


def refused_by_validator() -> dict:
    b = TraceBuilder("SELECT 1; DROP TABLE x", "raw_sql")
    b.record("input_screen", PASS, {})
    b.record("generation", SKIPPED, {"reason": "raw SQL input: generation not used"})
    b.record("validator", FAIL, {"checks": [], "reason": "more than one statement"})
    return b.refused("validator", "more than one statement")


def errored() -> dict:
    b = TraceBuilder("q", "natural_language")
    b.record("input_screen", FAIL, {"error": "RuntimeError: boom"})
    return b.error("input_screen", "internal error: RuntimeError: boom")


def as_json(t: dict) -> dict:
    return json.loads(json.dumps(t, default=str))


def edited(base: dict, path: list, value) -> dict:
    t = copy.deepcopy(as_json(base))
    *parents, key = path
    node = t
    for p in parents:
        node = node[p]
    if value is KeyError:
        del node[key]
    else:
        node[key] = value
    return t


VALID = [
    accepted(),
    accepted(row_count=0),
    accepted(row_count=config.PREVIEW_ROWS + 5),  # truncated preview
    refused_by_validator(),
    errored(),
    TraceBuilder("q", "raw_sql").error(None, "internal error outside any stage"),
]

INVALID = [
    edited(accepted(), ["id"], "X" * 32),
    edited(accepted(), ["timestamp"], "2026-11-24 10:00:00"),
    edited(accepted(), ["verdict"], "ok"),
    edited(accepted(), ["meta", "git_commit"], ""),
    edited(accepted(), ["extra"], 1),
    edited(accepted(), ["result"], None),  # accepted needs a result
    edited(accepted(), ["normalized_sql"], None),
    edited(accepted(), ["reason"], "why"),
    edited(accepted(), ["stages", 0, "status"], "ok"),
    edited(accepted(), ["stages", 0, "duration_ms"], -1),
    edited(accepted(), ["stages", 0, "duration_ms"], 1.5),
    edited(accepted(), ["stages", 4], KeyError),  # only 4 stages
    edited(accepted(), ["stages", 0, "name"], "generation"),  # out of order
    edited(accepted(), ["stages", 2, "status"], FAIL),  # failed yet accepted
    edited(accepted(), ["result", "rows", 0], ["a"]),  # row narrower than columns
    edited(accepted(), ["result", "row_count"], 3),  # rows do not match row_count
    edited(accepted(), ["result", "truncated"], True),
    edited(accepted(config.PREVIEW_ROWS + 5), ["result", "truncated"], False),
    edited(refused_by_validator(), ["refused_by"], "executor"),  # not the failed stage
    edited(refused_by_validator(), ["reason"], None),
    edited(refused_by_validator(), ["stages", 3, "status"], PASS),  # ran after a fail
    edited(
        refused_by_validator(),
        ["result"],
        {"columns": [], "rows": [], "row_count": 0, "truncated": False},
    ),  # fmt: skip
    [],
    None,
]


@pytest.mark.parametrize("t", VALID)
def test_valid_traces_pass(t):
    assert trace.schema_errors(as_json(t)) == []


@pytest.mark.parametrize("t", INVALID)
def test_invalid_traces_are_rejected(t):
    assert trace.schema_errors(t)


def test_append_rejects_an_invalid_trace_and_writes_nothing(tmp_path):
    target = tmp_path / "t.jsonl"
    with pytest.raises(trace.TraceError, match=r"\$\.verdict"):
        trace.append(edited(accepted(), ["verdict"], "ok"), target)
    assert not target.exists()
    trace.append(accepted(), target)
    assert len(target.read_text(encoding="utf-8").splitlines()) == 1


def test_append_checks_the_serialized_line(tmp_path):
    """Non-JSON values are checked as written (default=str), not as Python objects."""
    t = accepted()
    t["stages"][0]["duration_ms"] = object()  # becomes a string in the log line
    with pytest.raises(trace.TraceError, match="duration_ms"):
        trace.append(t, tmp_path / "t.jsonl")


def test_cli_does_not_log_or_print_an_invalid_trace(tmp_path, monkeypatch, capsys):
    bad = edited(accepted(), ["refused_by"], "validator")
    monkeypatch.setattr(cli, "run_pipeline", lambda *a, **k: bad)
    target = tmp_path / "t.jsonl"
    code = cli.main(["run", "--sql", "SELECT 1", "--trace-file", str(target)])
    out = capsys.readouterr()
    assert code == cli.EXIT_ERROR
    assert out.out == "" and "trace rejected, not logged" in out.err
    assert not target.exists()


def test_limits_match_config():
    schema = json.loads(trace.SCHEMA_PATH.read_text(encoding="utf-8"))
    props = schema["properties"]
    assert props["result"]["properties"]["rows"]["maxItems"] == config.PREVIEW_ROWS
    stages = props["stages"]
    assert stages["minItems"] == stages["maxItems"] == len(trace.LAYERS)
    assert stages["items"]["properties"]["name"]["enum"] == list(trace.LAYERS)
    assert stages["items"]["properties"]["layer"]["enum"] == list(range(1, len(trace.LAYERS) + 1))
    assert props["refused_by"]["enum"] == [*trace.LAYERS, None]
