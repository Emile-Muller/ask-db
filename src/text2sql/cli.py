"""``python -m text2sql``: the only way to query the database from Claude Code.

  context --question "..."                     layer 1; on pass print the layer-2 prompt
  run --question "..." --sql "..."             layers 1-5 with the SQL the model proposed
  run --question "..." --refusal "..."         the model declined (refused by generation)
  run --sql "..."                              raw-SQL path: layers 1, 3, 4, 5
  check --sql "..."                            layer 3 only, no database (debugging)
  review < review.json                         log a /question-review judgement (no database)

Exit codes: 0 accepted, 2 refused, 1 internal or usage error. ``review``: 0 logged,
2 rejected (bad input or unknown trace id), 1 internal error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

from text2sql import config, executor, output_check, prompt, review_log
from text2sql.input_screen import NATURAL_LANGUAGE, RAW_SQL, screen
from text2sql.trace import FAIL, PASS, SKIPPED, TraceBuilder, append, check_log_path, dumps
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


def _emit(trace: dict[str, Any], trace_file: Path | None) -> int:
    print(dumps(trace))
    try:
        append(trace, trace_file)
    except (OSError, ValueError) as e:
        print(f"warning: could not append trace: {e}", file=sys.stderr)
    return {"accepted": EXIT_ACCEPTED, "refused": EXIT_REFUSED}.get(trace["verdict"], EXIT_ERROR)


def cmd_context(args: argparse.Namespace) -> int:
    s = screen(args.question)
    if not s.ok:
        b = TraceBuilder(args.question, s.input_type)
        b.record("input_screen", FAIL, s.details())
        return _emit(b.refused("input_screen", s.reason), args.trace_file)
    if s.input_type == RAW_SQL:
        # No ready-to-paste command: quoting untrusted SQL for a shell is the caller's job
        # (double quotes would still expand $(...)).
        print("The input was classified as raw SQL, so generation is skipped.")
        print("Run the input unchanged through the raw-SQL path: run --sql '<the input>'")
        return EXIT_ACCEPTED
    print(prompt.build_context(s.normalized, db_path=args.db).prompt, end="")
    return EXIT_ACCEPTED


def cmd_run(args: argparse.Namespace) -> int:
    trace = run_pipeline(args.question, args.sql, args.refusal, args.model, args.db)
    return _emit(trace, args.trace_file)


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


def _log_file(value: str) -> Path:
    """argparse type: a JSONL log path the CLI may append to (see trace.check_log_path)."""
    try:
        return check_log_path(Path(value))
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m text2sql", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", type=Path, default=config.DB_PATH, help=argparse.SUPPRESS)
        p.add_argument("--trace-file", type=_log_file, default=None, help="JSONL file to append to")

    p = sub.add_parser("context", help="screen a question and print the generation context")
    p.add_argument("--question", required=True)
    common(p)
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("run", help="run the full pipeline and print the trace")
    p.add_argument("--question")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--sql", help="SQL proposed for --question, or raw SQL on its own")
    group.add_argument("--refusal", help="the proposer's refusal reason for --question")
    p.add_argument("--model", default=os.environ.get("TEXT2SQL_MODEL"), help="proposer model id")
    common(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("check", help="validate SQL only (layer 3, no database)")
    p.add_argument("--sql", required=True)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("review", help="log a /question-review judgement (JSON on stdin)")
    p.add_argument("--runs-dir", type=Path, default=config.RUNS_DIR, help=argparse.SUPPRESS)
    p.add_argument("--review-file", type=_log_file, default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_review)
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
