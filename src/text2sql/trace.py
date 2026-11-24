"""Machine-readable trace of one pipeline run (consumed by the static showcase page).

Every run produces exactly one JSON object, printed to stdout and appended as one line
to a JSONL file (default ``questions/logs/questions/<YYYYMMDD>.jsonl``). The format is STABLE: add
fields only in a backwards-compatible way.

{
  "id":            str,      uuid4 hex
  "timestamp":     str,      UTC, ISO 8601, seconds
  "input":         str,      the raw input (question, or SQL on the raw-SQL path)
  "input_type":    "natural_language" | "raw_sql",
  "verdict":       "accepted" | "refused" | "error",
  "refused_by":    "input_screen" | "generation" | "validator" | "executor" |
                   "output_check" | null,          (null when accepted; on "error" the
                                                    layer that crashed, or null)
  "reason":        str | null,                     why it was refused / errored
  "stages": [                                      always 5, in layer order
    {"layer": 1..5, "name": str, "status": "pass" | "fail" | "skipped",
     "duration_ms": int, "details": {...}}
  ],
  "generated_sql":  str | null,   SQL proposed by the model (null on the raw-SQL path)
  "normalized_sql": str | null,   SQL that passed the validator and was executed
  "result": {"columns": [str], "rows": [[...]], "row_count": int, "truncated": bool} | null,
  "meta": {"model": str | null, "git_commit": str}
}

This block is a summary; the contract is ``schemas/question_trace.json`` plus
``cross_checks`` below. ``append`` checks the exact JSON line against both before writing
and raises ``TraceError`` (nothing written) when it does not match.

Stage names: input_screen, generation, validator, executor, output_check. A stage after
the first failure is "skipped"; generation is also "skipped" on the raw-SQL path, where
later stages still run. ``rows`` holds at most PREVIEW_ROWS rows; ``row_count`` is the
number of rows fetched and ``truncated`` tells whether the preview is shorter.

Stage details:
  input_screen  normalized, input_type, flags, reason, original_length, length, ok
  generation    proposed_sql | refusal_reason, model   (or reason when skipped)
  validator     checks: [{name, passed, detail}], reason
  executor      row_count, execution_ms | error_type, error
  output_check  dropped_columns, masked_cells, cut_cells, preview_rows, row_count, truncated
                (cut_cells: strings longer than MAX_CELL_CHARS, cut and marked "…[cut]")
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - only used for a fixed git command
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from text2sql import config, json_contract

LAYERS: tuple[str, ...] = ("input_screen", "generation", "validator", "executor", "output_check")
PASS, FAIL, SKIPPED = "pass", "fail", "skipped"
ACCEPTED, REFUSED, ERROR = "accepted", "refused", "error"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "question_trace.json"


class TraceError(ValueError):
    """The trace does not match its contract (a pipeline bug): it is not logged."""


@dataclass
class Stage:
    layer: int
    name: str
    status: str = SKIPPED
    duration_ms: int = 0
    details: dict[str, Any] = field(default_factory=dict)


class TraceBuilder:
    """Collects stages while the pipeline runs, then renders the final trace dict."""

    def __init__(self, input_text: str, input_type: str, model: str | None = None) -> None:
        self.input = input_text
        self.input_type = input_type
        self.model = model
        self.stages = {name: Stage(i + 1, name) for i, name in enumerate(LAYERS)}
        self.generated_sql: str | None = None
        self.normalized_sql: str | None = None

    @contextmanager
    def timed(self, name: str) -> Iterator[None]:
        """Time a stage: ``with b.timed("validator"): ...`` then ``b.record(...)``.

        The duration is recorded even if the stage raises.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name].duration_ms = int((time.perf_counter() - started) * 1000)

    def record(self, name: str, status: str, details: dict[str, Any] | None = None) -> None:
        stage = self.stages[name]
        stage.status = status
        stage.details = details or {}

    def accepted(self, result: dict[str, Any]) -> dict[str, Any]:
        return self._render(ACCEPTED, None, None, result)

    def refused(self, layer: str, reason: str) -> dict[str, Any]:
        return self._render(REFUSED, layer, reason, None)

    def error(self, layer: str | None, reason: str) -> dict[str, Any]:
        return self._render(ERROR, layer, reason, None)

    def _render(
        self, verdict: str, layer: str | None, reason: str | None, result: dict[str, Any] | None
    ) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "input": self.input,
            "input_type": self.input_type,
            "verdict": verdict,
            "refused_by": layer,
            "reason": reason,
            "stages": [vars(self.stages[name]).copy() for name in LAYERS],
            "generated_sql": self.generated_sql,
            "normalized_sql": self.normalized_sql,
            "result": result,
            "meta": {"model": self.model, "git_commit": git_commit()},
        }


@lru_cache(maxsize=1)
def git_commit() -> str:
    try:
        out = subprocess.run(  # noqa: S603 - fixed arguments, no user input
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git from PATH
            cwd=config.PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def default_trace_path() -> Path:
    return config.RUNS_DIR / f"{datetime.now(UTC):%Y%m%d}.jsonl"


def check_log_path(path: Path) -> Path:
    """Return ``path`` resolved if it is a ``.jsonl`` file under LOGS_DIR or the temp dir.

    The CLI is allow-listed for Claude Code, so an unchecked ``--trace-file`` would let a
    session append to any file, including the pipeline's own code.
    """
    resolved = Path(path).resolve()
    roots = (config.LOGS_DIR.resolve(), Path(tempfile.gettempdir()).resolve())
    if resolved.suffix != ".jsonl" or not any(resolved.is_relative_to(r) for r in roots):
        raise ValueError(f"log file must be a .jsonl file under {config.LOGS_DIR}: {path}")
    return resolved


def cross_checks(trace: dict[str, Any]) -> list[str]:
    """Rules between fields that the schema cannot express (run after it passes)."""
    errors: list[str] = []
    stages = trace["stages"]
    if [s["name"] for s in stages] != list(LAYERS) or [s["layer"] for s in stages] != list(
        range(1, len(LAYERS) + 1)
    ):
        errors.append(f"$.stages: must be layers 1-{len(LAYERS)} in order {', '.join(LAYERS)}")
    statuses = [s["status"] for s in stages]
    failed = statuses.index(FAIL) if FAIL in statuses else None
    if failed is not None and any(s != SKIPPED for s in statuses[failed + 1 :]):
        errors.append("$.stages: every stage after the first fail must be skipped")
    failed_name = None if failed is None else stages[failed]["name"]
    if trace["refused_by"] != failed_name:
        errors.append(f"$.refused_by: {trace['refused_by']}, yet the failed stage is {failed_name}")
    if result := trace["result"]:
        width = len(result["columns"])
        for i, row in enumerate(result["rows"]):
            if len(row) != width:
                errors.append(f"$.result.rows[{i}]: {len(row)} values for {width} columns")
        shown, expected = len(result["rows"]), min(result["row_count"], config.PREVIEW_ROWS)
        if shown != expected:
            errors.append(f"$.result.rows: {shown} rows, expected {expected}")
        if result["truncated"] != (result["row_count"] > shown):
            errors.append("$.result.truncated: must be true exactly when row_count > len(rows)")
    return errors


def schema_errors(trace: Any) -> list[str]:
    """Return the errors of a trace (in its JSON form) against the contract; empty when valid."""
    return json_contract.check_schema(trace, SCHEMA_PATH) or cross_checks(trace)


def append(trace: dict[str, Any], path: Path | None = None) -> Path:
    """Append the trace as one JSON line, after checking that exact line against the contract."""
    path = check_log_path(path or default_trace_path())
    line = json.dumps(trace, ensure_ascii=False, default=str)
    if errors := schema_errors(json.loads(line)):
        raise TraceError("; ".join(json_contract.capped(errors)))
    return _write_line(line, path)


def append_jsonl(record: dict[str, Any], path: Path) -> Path:
    """Append any record (e.g. a review, checked by its own writer) as one JSON line."""
    path = check_log_path(path)
    return _write_line(json.dumps(record, ensure_ascii=False, default=str), path)


def _write_line(line: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path


def dumps(trace: dict[str, Any]) -> str:
    return json.dumps(trace, ensure_ascii=False, indent=2, default=str)
