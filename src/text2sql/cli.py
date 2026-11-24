"""``python -m text2sql``: the only way to query the database from Claude Code.

  context --question "..."                     layer 1; on pass print the layer-2 prompt
  run --question "..." --sql "..."             layers 1-5 with the SQL the model proposed
  run --question "..." --refusal "..."         the model declined (refused by generation)
  run --sql "..."                              raw-SQL path: layers 1, 3, 4, 5
  check --sql "..."                            layer 3 only, no database (debugging)
  review < review.json                         log a /question-review judgement (no database)
  results [--ids FILE] < ids.json              write questions/results.json from the logs
  results --check PATH [--verify-logs]         check a results file (no database)
  results --list [--rerun ID ...]              [{id, question}] to run (all, or ids + stale)
  results --ids FILE --merge                   reuse the up-to-date entries not in FILE

Batch options (/question-batch), so no untrusted text has to be quoted for a shell:
  --question-id ID     (context, run) the question is that questions.yaml entry's text
  --proposal-file F    (run) the proposer's reply {"sql": ..., "refusal_reason": ...}, a
                       .json file under the temp dir, instead of --sql / --refusal (the
                       proposer writes it itself, to the path context --brief printed)
  --prompt-file P      (run, with --proposal-file) the prompt_file context printed; it is
                       deleted after the run, whatever the outcome
  --brief              (context, run) print one JSON line instead of the trace or prompt:
                       context: {"status": "prompt", "prompt_file", "proposal_file",
                       "retry_proposal_file"} (the prompt is written to
                       questions/logs/prompts/<hex>.txt; the proposer may write its reply
                       to <temp>/text2sql-proposals/<hex>.json, and <hex>-2.json for the
                       sql_error retry) | {"status": "raw_sql"} |
                       {"status": "refused", trace fields...}
                       run:     {"trace_id", "verdict", "refused_by", "sql_error"}
                       ``sql_error`` is the DuckDB message when the executor refused a
                       proposal for a plain SQL mistake (executor.SQL_MISTAKES), else null.
                       The full trace is logged either way.

Exit codes: 0 accepted, 2 refused, 1 internal or usage error. ``review``: 0 logged,
2 rejected (bad input or unknown trace id), 1 internal error. ``results``: 0 written or
valid, 1 invalid (nothing written) or internal error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, NoReturn

import yaml

from text2sql import config, executor, json_contract, output_check, prompt, results, review_log
from text2sql.input_screen import NATURAL_LANGUAGE, RAW_SQL, screen
from text2sql.proposer_guard import PROMPT_NAME, proposal_paths
from text2sql.trace import (
    FAIL,
    LAYERS,
    PASS,
    SKIPPED,
    TraceBuilder,
    TraceError,
    append,
    check_log_path,
    dumps,
)
from text2sql.validator import validate

EXIT_ACCEPTED, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2
MAX_REFUSAL_CHARS = 500


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """argparse exits with 2 on bad usage, which would read as "refused"; use 1."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: error: {message}\n")


def run_pipeline(
    question: str | None = None,
    sql: str | None = None,
    refusal: str | None = None,
    model: str | None = None,
    db_path: Path = config.DB_PATH,
) -> dict[str, Any]:
    """Run layers 1-5 and return the trace. Never raises for refusals or SQL errors."""
    if question is None and sql is None:
        raise UsageError("give --question and/or --sql")
    if sql is not None and refusal is not None:
        raise UsageError("--sql and --refusal are mutually exclusive")
    if refusal is not None and question is None:
        raise UsageError("--refusal needs the --question it answers")

    primary = question if question is not None else sql
    # Best guess until layer 1 classifies the input (kept if screen() itself raises).
    b = TraceBuilder(primary, RAW_SQL if question is None else NATURAL_LANGUAGE)
    layer = "input_screen"
    try:
        with b.timed(layer):
            s = screen(primary)
        raw_path = question is None or (s.input_type == RAW_SQL and sql is None and refusal is None)
        b.input_type = RAW_SQL if raw_path else s.input_type
        b.model = None if raw_path else model
        b.record(layer, PASS if s.ok else FAIL, s.details())
        if not s.ok:
            return b.refused(layer, s.reason)
        if not raw_path and sql is None and refusal is None:
            raise UsageError("a natural-language question needs --sql or --refusal")

        layer = "generation"
        if raw_path:
            b.record(layer, SKIPPED, {"reason": "raw SQL input: generation not used"})
            candidate = s.normalized
        elif refusal is not None:
            reason = refusal.strip()[:MAX_REFUSAL_CHARS] or "proposer declined"
            b.record(layer, FAIL, {"refusal_reason": reason, "model": model})
            return b.refused(layer, reason)
        else:
            b.record(layer, PASS, {"proposed_sql": sql, "model": model})
            b.generated_sql = candidate = sql

        layer = "validator"
        with b.timed(layer):
            v = validate(candidate)
        b.record(
            layer, PASS if v.ok else FAIL, {"checks": v.to_dict()["checks"], "reason": v.reason}
        )
        if not v.ok:
            return b.refused(layer, v.reason)
        b.normalized_sql = v.normalized_sql

        layer = "executor"
        try:
            with b.timed(layer):
                r = executor.execute(v.normalized_sql, db_path=db_path)
        except executor.ExecutionRefused as e:
            b.record(layer, FAIL, {"error_type": e.error_type, "error": e.reason})
            return b.refused(layer, e.reason)
        b.record(layer, PASS, {"row_count": r.row_count, "execution_ms": r.duration_ms})

        layer = "output_check"
        with b.timed(layer):
            o = output_check.check(r.columns, r.rows)
        b.record(layer, PASS if o.ok else FAIL, o.details())
        if not o.ok:
            return b.refused(layer, o.reason)
        return b.accepted(o.result())
    except UsageError:
        raise
    except Exception as e:  # internal error: still produce a trace
        b.record(layer, FAIL, {"error": f"{type(e).__name__}: {e}"})
        return b.error(layer, f"internal error: {type(e).__name__}: {e}")


def brief(trace: dict[str, Any]) -> dict[str, Any]:
    """The one-line summary ``--brief`` prints (the full trace is still logged)."""
    executor_details = trace["stages"][LAYERS.index("executor")]["details"]
    sql_error = None
    if (
        trace["refused_by"] == "executor"
        and trace["generated_sql"] is not None
        and executor_details.get("error_type") in executor.SQL_MISTAKES
    ):
        sql_error = trace["reason"]
    return {
        "trace_id": trace["id"],
        "verdict": trace["verdict"],
        "refused_by": trace["refused_by"],
        "sql_error": sql_error,
    }


def _print_line(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def _emit(
    trace: dict[str, Any], trace_file: Path | None, brief_output: dict[str, Any] | None = None
) -> int:
    """Log the trace, then print it (or, with ``brief_output``, those keys + its brief).
    A trace that fails its schema is neither logged nor printed (exit 1)."""
    try:
        append(trace, trace_file)
    except TraceError as e:
        print(f"python -m text2sql: trace rejected, not logged: {e}", file=sys.stderr)
        return EXIT_ERROR
    except (OSError, ValueError) as e:
        print(f"warning: could not append trace: {e}", file=sys.stderr)
    if brief_output is not None:
        _print_line({**brief_output, **brief(trace)})
    else:
        print(dumps(trace))
    return {"accepted": EXIT_ACCEPTED, "refused": EXIT_REFUSED}.get(trace["verdict"], EXIT_ERROR)


def _under(path: Path, *roots: Path) -> bool:
    return any(path.is_relative_to(r.resolve()) for r in roots)


def question_text(
    question: str | None, question_id: str | None, questions_path: Path
) -> str | None:
    """``--question``, or the text of the questions.yaml entry ``--question-id`` names."""
    if question_id is None:
        return question
    try:
        listed = results.question_list(results.load_questions(questions_path))
    except (results.ResultsError, OSError, yaml.YAMLError) as e:
        raise UsageError(f"cannot read {questions_path}: {e}") from None
    for q in listed:
        if q["id"] == question_id:
            return q["question"]
    raise UsageError(f"no question with id {question_id!r} in {questions_path}")


def write_prompt(text: str, prompts_dir: Path) -> Path:
    """Write the layer-2 prompt to a new ``<prompts_dir>/<hex>.txt`` for the sql-proposer."""
    prompts_dir = Path(prompts_dir).resolve()
    if not _under(prompts_dir, config.LOGS_DIR, Path(tempfile.gettempdir())):
        raise UsageError(f"prompts dir must be under {config.LOGS_DIR}: {prompts_dir}")
    prompts_dir.mkdir(parents=True, exist_ok=True)
    path = prompts_dir / f"{uuid.uuid4().hex}.txt"
    with open(path, "x", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def read_proposal(path: Path) -> tuple[str | None, str | None]:
    """Return (sql, refusal) from the proposer's JSON reply. The content is untrusted and
    never echoed back in an error message."""
    resolved = Path(path).resolve()
    if resolved.suffix != ".json" or not _under(resolved, Path(tempfile.gettempdir())):
        raise UsageError("--proposal-file must be a .json file under the temp dir")
    try:
        raw = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise UsageError("--proposal-file is not UTF-8") from None
    if len(raw) > config.MAX_PROPOSAL_CHARS:
        raise UsageError(f"--proposal-file is longer than {config.MAX_PROPOSAL_CHARS} characters")
    body = raw.strip()
    if body.startswith("```") and body.endswith("```") and "\n" in body:
        body = body[body.index("\n") + 1 : -3]  # a Markdown fence around the JSON
    try:
        proposal = json.loads(body)
    except (json.JSONDecodeError, RecursionError):
        raise UsageError("--proposal-file is not valid JSON") from None
    shape = '{"sql": string|null, "refusal_reason": string|null}, exactly one non-null'
    if not isinstance(proposal, dict) or set(proposal) - {"sql", "refusal_reason"}:
        raise UsageError(f"--proposal-file must be {shape}")
    sql, refusal = proposal.get("sql"), proposal.get("refusal_reason")
    if not all(isinstance(v, str | None) for v in (sql, refusal)) or (sql is None) == (
        refusal is None
    ):
        raise UsageError(f"--proposal-file must be {shape}")
    return sql, refusal


def cmd_context(args: argparse.Namespace) -> int:
    question = question_text(args.question, args.question_id, args.questions)
    s = screen(question)
    if not s.ok:
        b = TraceBuilder(question, s.input_type)
        b.record("input_screen", FAIL, s.details())
        status = {"status": "refused"} if args.brief else None
        return _emit(b.refused("input_screen", s.reason), args.trace_file, status)
    if s.input_type == RAW_SQL:
        if args.brief:
            _print_line({"status": "raw_sql"})
            return EXIT_ACCEPTED
        # No ready-to-paste command: quoting untrusted SQL for a shell is the caller's job
        # (double quotes would still expand $(...)).
        print("The input was classified as raw SQL, so generation is skipped.")
        print("Run the input unchanged through the raw-SQL path: run --sql '<the input>'")
        return EXIT_ACCEPTED
    text = prompt.build_context(s.normalized, db_path=args.db).prompt
    if args.brief:
        prompt_file = write_prompt(text, args.prompts_dir)
        first, retry = proposal_paths(prompt_file, args.proposals_dir)
        first.parent.mkdir(parents=True, exist_ok=True)
        _print_line(
            {
                "status": "prompt",
                "prompt_file": str(prompt_file),
                "proposal_file": str(first),
                "retry_proposal_file": str(retry),
            }
        )
    else:
        print(text, end="")
    return EXIT_ACCEPTED


def prompt_file_path(path: Path, prompts_dir: Path) -> Path:
    """Return ``path`` resolved if it is a prompt ``context --brief`` could have written
    (``<prompts_dir>/<32 hex>.txt``, the names proposer_guard allows), so ``run`` never
    deletes any other file."""
    resolved = Path(path).resolve()
    if resolved.parent != Path(prompts_dir).resolve() or not PROMPT_NAME.fullmatch(resolved.name):
        raise UsageError(f"--prompt-file must be a <hex>.txt prompt in {prompts_dir}")
    return resolved


def cmd_run(args: argparse.Namespace) -> int:
    prompt_file = None
    if args.prompt_file is not None:
        if args.proposal_file is None:
            raise UsageError("--prompt-file needs --proposal-file")
        prompt_file = prompt_file_path(args.prompt_file, args.prompts_dir)
    try:
        question = question_text(args.question, args.question_id, args.questions)
        sql, refusal = args.sql, args.refusal
        if args.proposal_file is not None:
            if question is None:
                raise UsageError("--proposal-file needs --question or --question-id")
            sql, refusal = read_proposal(args.proposal_file)
        trace = run_pipeline(question, sql, refusal, args.model, args.db)
        return _emit(trace, args.trace_file, {} if args.brief else None)
    finally:
        # The proposer has replied, so its prompt is no longer needed (a retry after
        # sql_error goes to the same proposer, which already has the prompt in context).
        if prompt_file is not None:
            try:
                prompt_file.unlink(missing_ok=True)
            except OSError as e:
                print(f"warning: could not delete the prompt file: {e}", file=sys.stderr)


def cmd_check(args: argparse.Namespace) -> int:
    v = validate(args.sql)
    print(json.dumps(v.to_dict(), ensure_ascii=False, indent=2))
    return EXIT_ACCEPTED if v.ok else EXIT_REFUSED


def cmd_review(args: argparse.Namespace) -> int:
    try:
        record, path = review_log.log_review(sys.stdin.read(), args.runs_dir, args.review_file)
    except review_log.ReviewError as e:
        print(f"python -m text2sql review: rejected: {e}", file=sys.stderr)
        return EXIT_REFUSED
    print(dumps(record))
    print(f"logged to {path}", file=sys.stderr)
    return EXIT_ACCEPTED


def cmd_results(args: argparse.Namespace) -> int:
    try:
        questions = results.load_questions(args.questions)
        if args.rerun is not None and not args.list:
            raise UsageError("--rerun needs --list")
        if args.merge and (args.list or args.check is not None):
            raise UsageError("--merge only applies when writing (--ids)")
        if args.list:
            listed = results.question_list(questions)
            if args.rerun is not None:
                listed, stale = results.rerun_plan(
                    questions, args.rerun, results.previous_entries(args.out)
                )
                for qid, reason in stale.items():
                    print(f"also re-run {qid}: {reason}", file=sys.stderr)
            print(json.dumps(listed, ensure_ascii=False, indent=2))
            return EXIT_ACCEPTED
        if args.check is not None:
            errors = results.check_file(
                args.check, questions, args.verify_logs, args.runs_dir, args.reviews_dir
            )
            print("\n".join(json_contract.capped(errors)) if errors else "valid")
            return EXIT_ERROR if errors else EXIT_ACCEPTED
        if args.verify_logs:
            raise UsageError("--verify-logs needs --check")
        raw = args.ids.read_text(encoding="utf-8") if args.ids else sys.stdin.read()
        previous = results.previous_entries(args.out) if args.merge else None
        doc = results.build(
            results.load_manifest(raw),
            questions,
            args.runs_dir,
            args.reviews_dir,
            args.method,
            previous=previous,
        )
        out = results.write(doc, questions, args.out)
    except (results.ResultsError, OSError, yaml.YAMLError) as e:
        print(f"python -m text2sql results: nothing written: {e}", file=sys.stderr)
        return EXIT_ERROR
    s = doc["summary"]
    print(f"wrote {out}: {s['pass']}/{s['total']} pass, {s['errors']} errors", file=sys.stderr)
    return EXIT_ACCEPTED


def _log_file(value: str) -> Path:
    """argparse type: a JSONL log path the CLI may append to (see trace.check_log_path)."""
    try:
        return check_log_path(Path(value))
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m text2sql", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def common(p: argparse.ArgumentParser, question_required: bool) -> None:
        q = p.add_mutually_exclusive_group(required=question_required)
        q.add_argument("--question")
        q.add_argument("--question-id", help="use the text of this questions.yaml entry")
        p.add_argument(
            "--questions", type=Path, default=config.QUESTIONS_PATH, help=argparse.SUPPRESS
        )
        p.add_argument("--brief", action="store_true", help="print one JSON line (batch use)")
        p.add_argument("--db", type=Path, default=config.DB_PATH, help=argparse.SUPPRESS)
        p.add_argument("--trace-file", type=_log_file, default=None, help="JSONL file to append to")

    p = sub.add_parser("context", help="screen a question and print the generation context")
    common(p, question_required=True)
    p.add_argument("--prompts-dir", type=Path, default=config.PROMPTS_DIR, help=argparse.SUPPRESS)
    p.add_argument(
        "--proposals-dir", type=Path, default=config.PROPOSALS_DIR, help=argparse.SUPPRESS
    )
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("run", help="run the full pipeline and print the trace")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--sql", help="SQL proposed for --question, or raw SQL on its own")
    group.add_argument("--refusal", help="the proposer's refusal reason for --question")
    group.add_argument("--proposal-file", type=Path, help="the proposer's JSON reply (temp dir)")
    p.add_argument(
        "--prompt-file", type=Path, help="with --proposal-file: the prompt to delete after the run"
    )
    p.add_argument("--prompts-dir", type=Path, default=config.PROMPTS_DIR, help=argparse.SUPPRESS)
    p.add_argument("--model", default=os.environ.get("TEXT2SQL_MODEL"), help="proposer model id")
    common(p, question_required=False)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("check", help="validate SQL only (layer 3, no database)")
    p.add_argument("--sql", required=True)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("review", help="log a /question-review judgement (JSON on stdin)")
    p.add_argument("--runs-dir", type=Path, default=config.RUNS_DIR, help=argparse.SUPPRESS)
    p.add_argument("--review-file", type=_log_file, default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("results", help="write or check questions/results.json (no database)")
    p.add_argument("--list", action="store_true", help="print [{id, question}] in file order")
    p.add_argument(
        "--rerun", nargs="+", metavar="ID", help="with --list: these ids plus the stale entries"
    )
    p.add_argument(
        "--merge", action="store_true", help="reuse up-to-date entries of --out for other ids"
    )
    p.add_argument("--check", type=Path, help="check this results file instead of writing one")
    p.add_argument("--verify-logs", action="store_true", help="with --check: compare with logs")
    p.add_argument("--ids", type=Path, help="id manifest (JSON list; default stdin)")
    p.add_argument("--questions", type=Path, default=config.QUESTIONS_PATH)
    p.add_argument("--out", type=Path, default=config.RESULTS_PATH)
    p.add_argument("--method", default=results.DEFAULT_METHOD, help=argparse.SUPPRESS)
    p.add_argument("--runs-dir", type=Path, default=config.RUNS_DIR, help=argparse.SUPPRESS)
    p.add_argument("--reviews-dir", type=Path, default=config.REVIEWS_DIR, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_results)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UsageError as e:
        print(f"python -m text2sql: error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as e:
        print(f"python -m text2sql: error: {e}", file=sys.stderr)
        return EXIT_ERROR
