"""Review log for ``/question-review``: ``python -m text2sql review`` (review JSON on stdin).

The reviewer agent sends only its judgement. Everything that can be looked up or generated
is filled in here, so the agent cannot misreport the run it reviewed: the trace fields are
copied from the trace found by ``trace_id``, and ``id``, ``timestamp``, ``review_type`` and
``meta.git_commit`` are generated. Each review is appended as one line to a JSONL file
(default ``questions/logs/question-reviews/<YYYYMMDD>.jsonl``). This module never opens the
database. The format is STABLE: add fields only in a backwards-compatible way.

Input (stdin, one JSON object): the single source of truth is ``schemas/question_review.json``,
checked by ``json_contract``. In short, every key required and no others:
  trace_id, verdict, confidence, findings {logic, data, unverifiable, assumptions},
  corrected_sql, injection_attempt, summary, model
On top of the schema: the whole input is at most ``config.REVIEW_MAX_INPUT_CHARS``, and
``trace_id`` must name an existing trace. The schema's ``maxLength``/``maxItems`` mirror
``config.REVIEW_MAX_*`` (tests/test_question_review_schema.py keeps them equal). A rejection
lists at most ``json_contract.MAX_ERRORS`` errors.

Record (one line of the log):
{
  "id":                str,    uuid4 hex
  "timestamp":         str,    UTC, ISO 8601, seconds
  "trace_id", "trace_timestamp":                 from the trace ("id", "timestamp")
  "input", "input_type", "refused_by":           from the trace
  "trace_verdict":     "accepted" | "refused" | "error",   the trace's "verdict"
  "review_type":       "answer" | "refusal" | "sql_only",  derived from the trace:
                       answer = accepted question, refusal = refused by generation,
                       sql_only = raw SQL, refused by another layer, or error
  "verdict", "confidence", "findings", "corrected_sql", "injection_attempt", "summary":
                       from the input (for a refusal, "correct" = right to decline)
  "normalized_sql":    str | null,   the trace's normalized_sql, else its generated_sql
  "meta":              {"model": str | null, "git_commit": str}
}
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from text2sql import config, json_contract, trace

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "question_review.json"


class ReviewError(ValueError):
    """The review was rejected (bad input or unknown trace): exit code 2."""


def schema_errors(payload: Any) -> list[str]:
    """Return the errors of the reviewer's input against the schema; empty when valid."""
    return json_contract.check_schema(payload, SCHEMA_PATH)


def validate(payload: Any) -> dict[str, Any]:
    """Check the reviewer's input against the schema; raise ReviewError if it is off."""
    if errors := schema_errors(payload):
        raise ReviewError("; ".join(json_contract.capped(errors)))
    return payload


def find_trace(trace_id: str, runs_dir: Path = config.RUNS_DIR) -> dict[str, Any]:
    """Return the trace with this id, newest log file first; raise ReviewError if absent."""
    needle = f'"id": "{trace_id}"'
    for path in sorted(runs_dir.glob("*.jsonl"), reverse=True):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if needle not in line:
                    continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(t, dict) and t.get("id") == trace_id:
                    return t
    raise ReviewError(f"no trace with id {trace_id} in {runs_dir}")


def review_type(t: dict[str, Any]) -> str:
    if t.get("input_type") == "raw_sql":
        return "sql_only"
    if t.get("verdict") == "accepted":
        return "answer"
    if t.get("verdict") == "refused" and t.get("refused_by") == "generation":
        return "refusal"
    return "sql_only"


def build_record(payload: dict[str, Any], t: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "timestamp": now.isoformat(timespec="seconds"),
        "trace_id": t["id"],
        "trace_timestamp": t.get("timestamp"),
        "input": t.get("input"),
        "input_type": t.get("input_type"),
        "trace_verdict": t.get("verdict"),
        "refused_by": t.get("refused_by"),
        "review_type": review_type(t),
        "verdict": payload["verdict"],
        "confidence": payload["confidence"],
        "normalized_sql": t.get("normalized_sql") or t.get("generated_sql"),
        "findings": payload["findings"],
        "corrected_sql": payload["corrected_sql"],
        "injection_attempt": payload["injection_attempt"],
        "summary": payload["summary"],
        "meta": {"model": payload["model"], "git_commit": trace.git_commit()},
    }


def default_review_path(now: datetime) -> Path:
    return config.REVIEWS_DIR / f"{now:%Y%m%d}.jsonl"


def log_review(
    raw: str, runs_dir: Path = config.RUNS_DIR, review_file: Path | None = None
) -> tuple[dict[str, Any], Path]:
    """Validate the reviewer's JSON, complete it from its trace, append it; return both."""
    if len(raw) > config.REVIEW_MAX_INPUT_CHARS:
        raise ReviewError(f"input longer than {config.REVIEW_MAX_INPUT_CHARS} characters")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ReviewError(f"input is not valid JSON: {e}") from None
    except RecursionError:
        raise ReviewError("input is nested too deeply") from None
    validate(payload)
    now = datetime.now(UTC)
    record = build_record(payload, find_trace(payload["trace_id"], runs_dir), now)
    path = trace.append_jsonl(record, review_file or default_review_path(now))
    return record, path
