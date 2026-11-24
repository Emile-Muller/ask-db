"""Batch results (``questions/results.json``): ``python -m text2sql results``.

The ``/question-batch`` skill runs every ``questions/questions.yaml`` entry through
``/question`` and ``/question-review`` and collects only ids. This module does the rest,
deterministically and without an LLM or the database:

  write   ``results [--ids FILE] [--questions PATH] [--out PATH] [--method TEXT]``
          reads the id manifest (``--ids`` or stdin), copies each trace and review VERBATIM
          from the JSONL logs by id, joins them with ``id``/``category``/``expect`` from
          questions.yaml, derives ``actual``/``pass``/``error``/``summary``, checks the whole
          document, and only then writes it (atomically). Any failure: nothing written, exit 1.
  list    ``results --list [--questions PATH]`` prints ``[{id, question}]`` in file order,
          the question text exactly as the run subagent must receive it.
          ``results --list --rerun ID [ID ...] [--out PATH]`` prints only what a partial
          re-run must cover: those ids, plus every entry of the earlier ``--out`` file that
          cannot be reused (``stale_reason``, printed on stderr).
  merge   ``results --ids FILE --merge [--out PATH]``: the manifest may cover a subset; each
          other id reuses its entry from the earlier ``--out`` file, only if it passes
          ``stale_reason`` (else nothing is written). Derived fields are recomputed.
  check   ``results --check PATH [--questions PATH] [--verify-logs]``
          checks a results file; ``--verify-logs`` also compares every embedded record
          with its line in the local logs (the logs are not tracked by git). Exit 0 valid, 1 not.

Manifest (JSON list, one item per question, any order):
  {"id": str, "trace_id": hex32 | null, "review_id": hex32 | null, "error": str | null}
``review_id`` may be null: the review is then looked up by ``trace_id`` (trace ids are fresh
uuid4s, so a review found by one belongs to this run). ``error`` is the orchestrator's note
when a run or review exited 1.

Format 2 (contract: ``schemas/question_results.json`` plus ``cross_checks`` below):
{
  "format": 2, "generated_at": str, "method": str,
  "summary": {total, pass, fail, errors, failed_ids, outcome_by_deciding_layer, review_verdicts},
  "results": [{id, category, expect, actual, pass, error, trace, review}]
}
Everything else a reader needs is inside the records: the question is ``trace.input``, the
deciding layer ``trace.refused_by`` + ``trace.reason``, the model ``trace.meta.model``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from text2sql import config, json_contract, review_log, trace

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "question_results.json"
FORMAT = 2
ANSWER, REFUSE, ERROR = "answer", "refuse", "error"
VERDICT_TO_ACTUAL = {trace.ACCEPTED: ANSWER, trace.REFUSED: REFUSE}
OUTCOME_ORDER = (trace.ACCEPTED, *trace.LAYERS, ERROR)
REVIEW_VERDICTS = ("correct", "partially_correct", "incorrect", "cannot_tell")
MAX_ERROR_CHARS = 2_000
MANIFEST_KEYS = {"id", "trace_id", "review_id", "error"}
HEX_ID = re.compile(r"[0-9a-f]{32}")
DEFAULT_METHOD = (
    "Each question ran through python -m text2sql by its questions.yaml id. Inputs refused "
    "by layer 1 and raw-SQL inputs needed no LLM. For the others, an sql-proposer agent "
    "could read only the layer-2 prompt file (schema, rules and the question; no id, "
    "category or expect) and its JSON reply was passed to the pipeline unchanged. Each "
    "trace was then judged by /question-review, forked into its own question-reviewer "
    "agent given only the trace id. The traces and reviews were copied verbatim from the "
    "JSONL logs by python -m text2sql results."
)


class ResultsError(ValueError):
    """The results document cannot be built or does not match its contract: exit 1."""


# --- inputs ---------------------------------------------------------------------------


def load_questions(path: Path = config.QUESTIONS_PATH) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        docs = yaml.safe_load(f) or {}
    questions = docs.get("questions") if isinstance(docs, dict) else None
    if not isinstance(questions, list) or not all(isinstance(q, dict) for q in questions):
        raise ResultsError(f"{path}: expected a 'questions' list")
    return questions


def question_list(questions: list[dict[str, Any]]) -> list[dict[str, str]]:
    """``[{id, question}]`` in file order, the text as sent to /question (YAML fold stripped)."""
    return [{"id": q.get("id"), "question": str(q.get("question", "")).strip()} for q in questions]


def load_manifest(raw: str) -> list[dict[str, Any]]:
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as e:
        raise ResultsError(f"manifest is not valid JSON: {e}") from None
    if not isinstance(items, list):
        raise ResultsError("manifest must be a JSON list")
    for i, item in enumerate(items):
        if not isinstance(item, dict) or set(item) - MANIFEST_KEYS or "id" not in item:
            raise ResultsError(f"manifest[{i}]: keys must be id, trace_id, review_id, error")
        for key in ("trace_id", "review_id"):
            value = item.get(key)
            if value is not None and not (isinstance(value, str) and HEX_ID.fullmatch(value)):
                raise ResultsError(f"manifest[{i}].{key}: must be a 32-char hex id or null")
        if not isinstance(item.get("error"), str | None):
            raise ResultsError(f"manifest[{i}].error: must be a string or null")
    return items


def find_records(log_dir: Path, key: str, value: str) -> list[dict[str, Any]]:
    """Every JSONL record in ``log_dir`` whose ``key`` equals ``value`` (oldest file first)."""
    needle = f'"{key}": "{value}"'
    found: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if needle not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get(key) == value:
                    found.append(record)
    return found


# --- derived fields -------------------------------------------------------------------


def actual_of(t: dict[str, Any] | None) -> str:
    return VERDICT_TO_ACTUAL.get(t["verdict"], ERROR) if t else ERROR


def outcome_of(t: dict[str, Any] | None) -> str:
    if not t or t["verdict"] == trace.ERROR:
        return ERROR
    return t["refused_by"] or trace.ACCEPTED


def review_copy_of(t: dict[str, Any]) -> dict[str, Any]:
    """The fields review_log.build_record copies from the trace (so they are rebuildable)."""
    return {
        "trace_id": t["id"],
        "trace_timestamp": t["timestamp"],
        "input": t["input"],
        "input_type": t["input_type"],
        "trace_verdict": t["verdict"],
        "refused_by": t["refused_by"],
        "review_type": review_log.review_type(t),
        "normalized_sql": t["normalized_sql"] or t["generated_sql"],
    }


def _ordered(counts: Counter[str], order: tuple[str, ...]) -> dict[str, int]:
    return {k: counts[k] for k in order if counts[k]}


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [e["id"] for e in entries if not e["pass"]]
    reviews = Counter(e["review"]["verdict"] for e in entries if e["review"])
    return {
        "total": len(entries),
        "pass": len(entries) - len(failed),
        "fail": len(failed),
        "errors": sum(e["error"] is not None for e in entries),
        "failed_ids": failed,
        "outcome_by_deciding_layer": _ordered(
            Counter(outcome_of(e["trace"]) for e in entries), OUTCOME_ORDER
        ),
        "review_verdicts": _ordered(reviews, REVIEW_VERDICTS),
    }


def _same_text(a: Any, b: Any) -> bool:
    """Question text equality up to whitespace (YAML folding, shell quoting of newlines)."""
    return isinstance(a, str) and isinstance(b, str) and a.split() == b.split()


# --- build ----------------------------------------------------------------------------


def build_entry(
    question: dict[str, Any], item: dict[str, Any], runs_dir: Path, reviews_dir: Path
) -> dict[str, Any]:
    problems: list[str] = [item["error"].strip()] if (item.get("error") or "").strip() else []
    t: dict[str, Any] | None = None
    review: dict[str, Any] | None = None

    if trace_id := item.get("trace_id"):
        found = find_records(runs_dir, "id", trace_id)
        if len(found) == 1:
            t = found[0]
        else:
            problems.append(f"trace {trace_id}: {len(found)} records in the trace log, need 1")
    elif not problems:
        problems.append("the run produced no trace")

    if t is not None:
        if not _same_text(t.get("input"), question.get("question")):
            raise ResultsError(f"{question['id']}: trace {t['id']} is for another input")
        if t.get("verdict") == trace.ERROR:
            problems.append(f"pipeline error at {t.get('refused_by')}: {t.get('reason')}")
        if review_id := item.get("review_id"):
            candidates = find_records(reviews_dir, "id", review_id)
        else:
            candidates = find_records(reviews_dir, "trace_id", t["id"])
        if len(candidates) == 1 and candidates[0].get("trace_id") == t["id"]:
            review = candidates[0]
        elif len(candidates) == 1:
            problems.append(f"review {review_id} is for trace {candidates[0].get('trace_id')}")
        else:
            which = f"review {review_id}" if review_id else f"reviews of trace {t['id']}"
            problems.append(f"{which}: {len(candidates)} records in the review log, need 1")

    actual = actual_of(t)
    error = "; ".join(problems)[:MAX_ERROR_CHARS] if problems else None
    return {
        "id": question["id"],
        "category": question["category"],
        "expect": question["expect"],
        "actual": actual,
        "pass": question["expect"] == actual,
        "error": error,
        "trace": t,
        "review": review,
    }


def stale_reason(entry: Any, question: dict[str, Any]) -> str | None:
    """Why an entry of an earlier results.json cannot be reused as it is; None if it can.

    Reusable means: run and review both succeeded, the trace is for this exact question
    text, and it was made by the pipeline as it is now (same git commit and same
    ``trace.pipeline_hash``, which also covers uncommitted edits and data/schema.yaml).
    """
    if not isinstance(entry, dict):
        return "no earlier entry"
    t, review = entry.get("trace"), entry.get("review")
    if entry.get("error") is not None or not isinstance(t, dict) or not isinstance(review, dict):
        return "the earlier run or review failed"
    if not _same_text(t.get("input"), question.get("question")):
        return "the question text changed"
    meta = t.get("meta") if isinstance(t.get("meta"), dict) else {}
    commit = trace.git_commit()
    if commit == "unknown" or meta.get("git_commit") != commit:
        return f"made at commit {meta.get('git_commit')}, now {commit}"
    if meta.get("pipeline_hash") != trace.pipeline_hash():
        return "made by a different pipeline (code, contracts or data/schema.yaml changed)"
    return None


def previous_entries(path: Path) -> dict[str, Any]:
    """The entries of an earlier results file by id; empty if there is none."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as e:
        raise ResultsError(f"cannot read the earlier results {path}: {e}") from None
    entries = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        raise ResultsError(f"{path}: not a results file (no results list)")
    return {e.get("id"): e for e in entries if isinstance(e, dict)}


def rerun_plan(
    questions: list[dict[str, Any]], requested: list[str], previous: dict[str, Any]
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """The ``[{id, question}]`` to run for ``/question-batch <ids>``: the requested ids plus
    every entry that cannot be reused, in file order; and why each of the latter is stale."""
    ids = {q.get("id") for q in questions}
    if unknown := sorted(set(requested) - ids):
        raise ResultsError(f"ids not in questions.yaml: {unknown}")
    stale = {
        q["id"]: reason
        for q in questions
        if q["id"] not in requested and (reason := stale_reason(previous.get(q["id"]), q))
    }
    plan = [q for q in question_list(questions) if q["id"] in requested or q["id"] in stale]
    return plan, stale


def reused_entry(question: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """An earlier entry's records, with every derived field recomputed from questions.yaml."""
    t = old["trace"]
    actual = actual_of(t)
    return {
        "id": question["id"],
        "category": question["category"],
        "expect": question["expect"],
        "actual": actual,
        "pass": question["expect"] == actual,
        "error": None,
        "trace": t,
        "review": old["review"],
    }


def build(
    manifest: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    runs_dir: Path = config.RUNS_DIR,
    reviews_dir: Path = config.REVIEWS_DIR,
    method: str = DEFAULT_METHOD,
    now: datetime | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the document. With ``previous`` (merge mode) the manifest may cover a subset:
    every other id reuses its earlier entry, which must pass ``stale_reason``."""
    by_id: dict[str, dict[str, Any]] = {}
    for item in manifest:
        if item["id"] in by_id:
            raise ResultsError(f"manifest: id {item['id']!r} appears twice")
        by_id[item["id"]] = item
    ids = [q.get("id") for q in questions]
    if extra := sorted(set(by_id) - set(ids)):
        raise ResultsError(f"manifest: ids not in questions.yaml: {extra}")
    missing = [q for q in questions if q.get("id") not in by_id]
    if missing and previous is None:
        raise ResultsError(f"manifest: no entry for {[q.get('id') for q in missing]}")
    previous = previous or {}
    if stale := {q["id"]: r for q in missing if (r := stale_reason(previous.get(q["id"]), q))}:
        listed = "; ".join(f"{i}: {r}" for i, r in stale.items())
        raise ResultsError(f"manifest: no entry, and no reusable earlier entry, for {listed}")
    entries = [
        build_entry(q, by_id[q["id"]], runs_dir, reviews_dir)
        if q["id"] in by_id
        else reused_entry(q, previous[q["id"]])
        for q in questions
    ]
    if missing:
        method += (
            f" {len(missing)} of {len(questions)} entries were reused from the previous"
            " results.json (same question text, git commit and pipeline hash)."
        )
    return {
        "format": FORMAT,
        "generated_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "method": method,
        "summary": summarize(entries),
        "results": entries,
    }


# --- contract -------------------------------------------------------------------------


def cross_checks(doc: dict[str, Any], questions: list[dict[str, Any]]) -> list[str]:
    """Rules between fields that the schema cannot express (run after it passes)."""
    errors: list[str] = []
    entries = doc["results"]
    ids = [e["id"] for e in entries]
    if dupes := sorted(i for i, n in Counter(ids).items() if n > 1):
        errors.append(f"$.results: duplicate ids {dupes}")
    expected_ids = [q.get("id") for q in questions]
    if ids != expected_ids:
        errors.append("$.results: ids must match questions.yaml one for one, in file order")
    by_id = {q.get("id"): q for q in questions}

    for i, e in enumerate(entries):
        at, t, r = f"$.results[{i}]", e["trace"], e["review"]
        if q := by_id.get(e["id"]):
            for key in ("category", "expect"):
                if e[key] != q.get(key):
                    errors.append(f"{at}.{key}: {e[key]!r}, questions.yaml says {q.get(key)!r}")
            if t is not None and not _same_text(t["input"], q.get("question")):
                errors.append(f"{at}.trace.input: not the questions.yaml question for {e['id']}")
        if e["actual"] != actual_of(t):
            errors.append(f"{at}.actual: {e['actual']!r}, the trace says {actual_of(t)!r}")
        if e["pass"] != (e["expect"] == e["actual"]):
            errors.append(f"{at}.pass: must be expect == actual")
        if e["error"] is None and t is not None and t["verdict"] == trace.ERROR:
            errors.append(f"{at}.error: the trace verdict is error, so error must be set")
        if t is not None:
            errors += [f"{at}.trace{err.removeprefix('$')}" for err in trace.cross_checks(t)]
        if r is not None:
            if t is None:
                errors.append(f"{at}.review: a review without a trace")
            else:
                for key, value in review_copy_of(t).items():
                    if r[key] != value:
                        errors.append(f"{at}.review.{key}: does not match the trace")

    if entries and doc["summary"] != summarize(entries):
        errors.append(f"$.summary: must be {json.dumps(summarize(entries))}")
    return errors


def schema_errors(doc: Any, questions: list[dict[str, Any]]) -> list[str]:
    """Return the errors of a results document against the contract; empty when valid."""
    return json_contract.check_schema(doc, SCHEMA_PATH) or cross_checks(doc, questions)


def log_errors(
    doc: dict[str, Any], runs_dir: Path = config.RUNS_DIR, reviews_dir: Path = config.REVIEWS_DIR
) -> list[str]:
    """Compare every embedded trace and review with its line in the local logs."""
    errors: list[str] = []
    for i, e in enumerate(doc["results"]):
        for key, log_dir in (("trace", runs_dir), ("review", reviews_dir)):
            record = e[key]
            if record is not None and find_records(log_dir, "id", record["id"]) != [record]:
                errors.append(f"$.results[{i}].{key}: not a verbatim copy of its log line")
    return errors


def check_out_path(path: Path) -> Path:
    """The writer may only create ``.json`` files under questions/ or the temp dir."""
    resolved = Path(path).resolve()
    roots = (config.QUESTIONS_DIR.resolve(), Path(tempfile.gettempdir()).resolve())
    if resolved.suffix != ".json" or not any(resolved.is_relative_to(r) for r in roots):
        raise ResultsError(f"output must be a .json file under {config.QUESTIONS_DIR}: {path}")
    return resolved


def dumps(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def write(doc: dict[str, Any], questions: list[dict[str, Any]], out: Path) -> Path:
    """Check the document, then write it atomically; on any error write nothing."""
    out = check_out_path(out)
    text = dumps(doc)
    if errors := schema_errors(json.loads(text), questions):
        raise ResultsError("; ".join(json_contract.capped(errors)))
    fd, tmp = tempfile.mkstemp(prefix=".results-", suffix=".json", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out


def check_file(
    path: Path,
    questions: list[dict[str, Any]],
    verify_logs: bool = False,
    runs_dir: Path = config.RUNS_DIR,
    reviews_dir: Path = config.REVIEWS_DIR,
) -> list[str]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as e:
        return [f"cannot read {path}: {type(e).__name__}: {e}"]
    errors = schema_errors(doc, questions)
    if not errors and verify_logs:
        errors = log_errors(doc, runs_dir, reviews_dir)
    return errors
