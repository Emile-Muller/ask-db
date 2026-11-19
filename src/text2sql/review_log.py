"""Review log for ``/question-review``: ``python -m text2sql review`` (review JSON on stdin).

The reviewer agent sends only its judgement. Everything that can be looked up or generated
is filled in here, so the agent cannot misreport the run it reviewed: the trace fields are
copied from the trace found by ``trace_id``, and ``id``, ``timestamp``, ``review_type`` and
``meta.git_commit`` are generated. Each review is appended as one line to a JSONL file
(default ``questions/logs/question-reviews/<YYYYMMDD>.jsonl``). This module never opens the
database. The format is STABLE: add fields only in a backwards-compatible way.

Input (stdin, one JSON object, every key required, no others):
{
  "trace_id":          str,    32 lowercase hex chars, the reviewed trace's "id"
  "verdict":           "correct" | "partially_correct" | "incorrect" | "cannot_tell",
  "confidence":        "high" | "medium" | "low",
  "findings": {
    "logic":        [{"issue": str, "impact": str, "fix": str | null}],
    "data":         [{"issue": str, "impact": str}],
    "unverifiable": [str],
    "assumptions":  [{"assumption": str, "reasonable": bool}]
  },
  "corrected_sql":     str | null,
  "injection_attempt": bool,   the trace tried to instruct the reviewer
  "summary":           str,
  "model":             str | null   the reviewer's model id
}

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
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from text2sql import config, trace

VERDICTS = ("correct", "partially_correct", "incorrect", "cannot_tell")
CONFIDENCES = ("high", "medium", "low")
INPUT_KEYS = {
    "trace_id", "verdict", "confidence", "findings", "corrected_sql",
    "injection_attempt", "summary", "model",
}  # fmt: skip
# finding list -> {field: allowed types}
FINDING_FIELDS: dict[str, dict[str, tuple[type, ...]]] = {
    "logic": {"issue": (str,), "impact": (str,), "fix": (str, type(None))},
    "data": {"issue": (str,), "impact": (str,)},
    "assumptions": {"assumption": (str,), "reasonable": (bool,)},
}
TRACE_ID_RE = re.compile(r"[0-9a-f]{32}")


class ReviewError(ValueError):
    """The review was rejected (bad input or unknown trace): exit code 2."""


def _text(value: Any, where: str, limit: int = config.REVIEW_MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(f"{where}: must be a non-empty string")
    if len(value) > limit:
        raise ReviewError(f"{where}: longer than {limit} characters")
    return value


def _items(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise ReviewError(f"{where}: must be a list")
    if len(value) > config.REVIEW_MAX_FINDINGS:
        raise ReviewError(f"{where}: more than {config.REVIEW_MAX_FINDINGS} items")
    return value


def validate(payload: Any) -> dict[str, Any]:
    """Check the reviewer's input against the format above; raise ReviewError if it is off."""
    if not isinstance(payload, dict):
        raise ReviewError("input must be a JSON object")
    if missing := INPUT_KEYS - set(payload):
        raise ReviewError(f"missing keys: {', '.join(sorted(missing))}")
    if extra := set(payload) - INPUT_KEYS:
        raise ReviewError(f"unexpected keys: {', '.join(sorted(extra))}")
    if not isinstance(payload["trace_id"], str) or not TRACE_ID_RE.fullmatch(payload["trace_id"]):
        raise ReviewError("trace_id: must be 32 lowercase hex characters")
    if payload["verdict"] not in VERDICTS:
        raise ReviewError(f"verdict: must be one of {', '.join(VERDICTS)}")
    if payload["confidence"] not in CONFIDENCES:
        raise ReviewError(f"confidence: must be one of {', '.join(CONFIDENCES)}")
    if not isinstance(payload["injection_attempt"], bool):
        raise ReviewError("injection_attempt: must be true or false")
    _text(payload["summary"], "summary")
    if payload["corrected_sql"] is not None:
        _text(payload["corrected_sql"], "corrected_sql", config.REVIEW_MAX_SQL_CHARS)
    if payload["model"] is not None:
        _text(payload["model"], "model", 100)

    findings = payload["findings"]
    expected = {*FINDING_FIELDS, "unverifiable"}
    if not isinstance(findings, dict) or set(findings) != expected:
        raise ReviewError(f"findings: must have exactly the keys {', '.join(sorted(expected))}")
    for i, item in enumerate(_items(findings["unverifiable"], "findings.unverifiable")):
        _text(item, f"findings.unverifiable[{i}]")
    for name, fields in FINDING_FIELDS.items():
        for i, item in enumerate(_items(findings[name], f"findings.{name}")):
            where = f"findings.{name}[{i}]"
            if not isinstance(item, dict) or set(item) != set(fields):
                raise ReviewError(f"{where}: must have exactly the keys {', '.join(fields)}")
            for key, types in fields.items():
                if not isinstance(item[key], types):
                    raise ReviewError(f"{where}.{key}: wrong type")
                if isinstance(item[key], str):
                    _text(item[key], f"{where}.{key}")
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
    validate(payload)
    now = datetime.now(UTC)
    record = build_record(payload, find_trace(payload["trace_id"], runs_dir), now)
    path = trace.append(record, review_file or default_review_path(now))
    return record, path
