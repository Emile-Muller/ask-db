"""End-to-end eval: question -> LLM SQL -> guardrail -> DuckDB, graded against gold SQL.

    python -m agentic_query.eval                  # run all questions (calls the Claude API)
    python -m agentic_query.eval --only top_categories_revenue avg_review_score
    python -m agentic_query.eval --gold-only      # just check the gold SQL runs (no API calls)

Writes a JSON report to eval/last_run.json. Exit code 1 if any question fails.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from agentic_query import PROJECT_ROOT
from agentic_query.sql_guard import QueryResult, UnsafeQueryError, run_query

EVAL_DIR = PROJECT_ROOT / "eval"
QUESTIONS_PATH = EVAL_DIR / "questions.yaml"
REPORT_PATH = EVAL_DIR / "last_run.json"
CANNOT_ANSWER = "cannot answer from the available data"
MAX_ROWS = 1000
KINDS = ("question", "invalid_question", "instruction", "sql", "invalid_sql")


# --- grading ---------------------------------------------------------------


def _norm(v: Any) -> Any:
    """Make values comparable across equivalent SQL: numbers as floats, dates as text."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, str):
        return v.strip()
    return v


def _num_eq(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-3, abs_tol=0.01)


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return _num_eq(a, b)
    return a == b


def _sort_key(v: Any):
    return (v is None, type(v).__name__, v if v is not None else 0)


def _columns_match(gold: list[Any], pred: list[Any], ordered: bool) -> bool:
    if len(gold) != len(pred):
        return False
    if not ordered:
        gold, pred = sorted(gold, key=_sort_key), sorted(pred, key=_sort_key)
    return all(_eq(g, p) for g, p in zip(gold, pred, strict=True))


def compare(
    gold: QueryResult,
    pred: QueryResult,
    ordered: bool = False,
    check: list[str] | None = None,
) -> tuple[bool, str]:
    """Is ``pred`` the same answer as ``gold``? Returns (passed, reason)."""
    if len(gold.rows) != len(pred.rows):
        return False, f"row count {len(pred.rows)} != expected {len(gold.rows)}"
    check = check or gold.columns
    missing = [c for c in check if c not in gold.columns]
    if missing:
        raise ValueError(f"check columns not in gold result: {missing}")

    g_cols = {c: [_norm(r[i]) for r in gold.rows] for i, c in enumerate(gold.columns)}
    p_cols = [[_norm(r[i]) for r in pred.rows] for i in range(len(pred.columns))]

    # Map each checked gold column to a generated column with the same values.
    mapping: dict[str, int] = {}
    for c in check:
        idx = next(
            (
                i
                for i, col in enumerate(p_cols)
                if i not in mapping.values() and _columns_match(g_cols[c], col, ordered)
            ),
            None,
        )
        if idx is None:
            return False, f"no generated column matches expected column '{c}'"
        mapping[c] = idx

    # Unordered columns can match one by one yet pair up differently: check rows too.
    # (Ordered columns compared position by position are already aligned.)
    if not ordered and len(check) > 1:
        g_rows = Counter(tuple(_round(g_cols[c][r]) for c in check) for r in range(len(gold.rows)))
        p_rows = Counter(
            tuple(_round(p_cols[mapping[c]][r]) for c in check) for r in range(len(pred.rows))
        )
        if g_rows != p_rows:
            return False, "values match per column but not row by row"
    return True, "ok"


def _round(v: Any) -> Any:
    return round(v, 2) if isinstance(v, float) else v


def is_cannot_answer(res: QueryResult) -> bool:
    return (
        len(res.rows) == 1
        and len(res.columns) == 1
        and isinstance(res.rows[0][0], str)
        and res.rows[0][0].strip().lower().rstrip(".") == CANNOT_ANSWER
    )


# --- running ---------------------------------------------------------------


@dataclass
class Outcome:
    id: str
    kind: str
    question: str
    passed: bool
    reason: str
    generated_sql: str | None = None
    executed_sql: str | None = None
    seconds: float = 0.0
    preview: list[list[Any]] = field(default_factory=list)


def load_questions(path: Path = QUESTIONS_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        items = yaml.safe_load(f)["questions"]
    ids = [q["id"] for q in items]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    if dupes:
        raise ValueError(f"duplicate question ids: {dupes}")
    for q in items:
        if q.get("kind") not in KINDS:
            raise ValueError(f"{q['id']}: kind must be one of {KINDS}, got {q.get('kind')!r}")
        if not str(q.get("question") or "").strip():
            raise ValueError(f"{q['id']}: empty question")
        if (q.get("expect") == "refuse") == bool(q.get("gold_sql")):
            raise ValueError(f"{q['id']}: needs exactly one of gold_sql or expect: refuse")
    return items


def run_one(item: dict, client=None) -> Outcome:
    from agentic_query.text_to_sql import GenerationError, generate_sql  # imports the SDK

    start = time.perf_counter()
    out = Outcome(item["id"], item["kind"], item["question"], False, "")
    try:
        out.generated_sql = generate_sql(item["question"], client)
        pred = run_query(out.generated_sql, max_rows=MAX_ROWS)
        out.executed_sql = pred.sql
        out.preview = [list(map(_jsonable, r)) for r in pred.rows[:5]]
        if item.get("expect") == "refuse":
            out.passed = is_cannot_answer(pred)
            out.reason = "returned 'cannot answer'" if out.passed else "returned data"
        else:
            gold = run_query(item["gold_sql"], max_rows=MAX_ROWS)
            out.passed, out.reason = compare(
                gold, pred, item.get("ordered", False), item.get("check")
            )
    except UnsafeQueryError as e:
        out.passed = item.get("expect") == "refuse"
        out.reason = f"guardrail rejected: {e}"
    except GenerationError as e:
        out.passed = item.get("expect") == "refuse"
        out.reason = f"generation failed: {e}"
    except Exception as e:  # report any failure (SQL error, API error) per question
        out.reason = f"{type(e).__name__}: {e}"
    out.seconds = round(time.perf_counter() - start, 1)
    return out


def _jsonable(v: Any) -> Any:
    return v if isinstance(v, (int, float, str, bool, type(None))) else str(v)


def check_gold(items: list[dict]) -> int:
    failures = 0
    for item in items:
        if item.get("expect") == "refuse":
            continue
        try:
            res = run_query(item["gold_sql"], max_rows=MAX_ROWS)
            compare(res, res, item.get("ordered", False), item.get("check"))
            print(f"ok    {item['id']:<26} {len(res.rows):>4} rows  {res.rows[:2]}")
        except Exception as e:
            failures += 1
            print(f"FAIL  {item['id']:<26} {type(e).__name__}: {e}")
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--only", nargs="+", metavar="ID", help="run only these question ids")
    ap.add_argument("--gold-only", action="store_true", help="check gold SQL only, no API calls")
    ap.add_argument("--workers", type=int, default=4, help="parallel API calls (default 4)")
    args = ap.parse_args(argv)

    items = load_questions()
    if args.only:
        unknown = set(args.only) - {q["id"] for q in items}
        if unknown:
            ap.error(f"unknown ids: {', '.join(sorted(unknown))}")
        items = [q for q in items if q["id"] in args.only]

    if args.gold_only:
        return 1 if check_gold(items) else 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        outcomes = list(pool.map(run_one, items))

    for o in outcomes:
        mark = "PASS" if o.passed else "FAIL"
        print(f"{mark}  {o.id:<26} {o.seconds:>5}s  {o.reason}")
        if not o.passed and o.generated_sql:
            print("      " + o.generated_sql.replace("\n", "\n      "))
    passed = sum(o.passed for o in outcomes)
    print()
    for kind in KINDS:
        of_kind = [o for o in outcomes if o.kind == kind]
        if of_kind:
            print(f"  {kind:<17} {sum(o.passed for o in of_kind)}/{len(of_kind)}")
    print(f"{passed}/{len(outcomes)} passed ({100 * passed / max(1, len(outcomes)):.0f}%)")

    from agentic_query.text_to_sql import EFFORT, MODEL

    REPORT_PATH.write_text(
        json.dumps(
            {
                "run_at": datetime.now().isoformat(timespec="seconds"),
                "model": MODEL,
                "effort": EFFORT,
                "passed": passed,
                "total": len(outcomes),
                "results": [asdict(o) for o in outcomes],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"Report: {REPORT_PATH}")
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
