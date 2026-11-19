"""Send candidate SQL through agentic_query.sql_guard.run_query and report the result.

Usage (from the repo root):
    .\\.venv\\Scripts\\python.exe .claude\\skills\\question\\guard.py <sql_file>

The SQL is read from a file so it never has to be quoted on the command line.
Prints JSON and saves it, with the submitted SQL and a timestamp, to
eval/last_skill_run.json (overwritten on every run).
Exit code 0: accepted (``sql`` is the guardrail-approved SQL that ran).
Exit code 1: rejected by the guardrail or failed in DuckDB (``error`` says why).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import duckdb  # noqa: E402

from agentic_query.sql_guard import UnsafeQueryError, run_query  # noqa: E402

PREVIEW_ROWS = 5
REPORT_PATH = REPO_ROOT / "eval" / "last_skill_run.json"


def check(sql: str) -> dict:
    """Run ``sql`` through the guardrail and return the result as a JSON-ready dict."""
    try:
        result = run_query(sql)  # default max_rows, same as the app
    except UnsafeQueryError as e:
        return {"ok": False, "stage": "guardrail", "error": str(e)}
    except duckdb.Error as e:
        return {"ok": False, "stage": "duckdb", "error": str(e)}
    return {
        "ok": True,
        "sql": result.sql,
        "columns": result.columns,
        "preview": [list(r) for r in result.rows[:PREVIEW_ROWS]],
        "more_rows": result.truncated or len(result.rows) > PREVIEW_ROWS,
    }


def write_report(sql: str, out: dict) -> None:
    """Save the last run to eval/last_skill_run.json. A failed write never hides the result."""
    report = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "submitted_sql": sql,
        **out,
    }
    try:
        REPORT_PATH.parent.mkdir(exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as e:
        print(f"warning: could not write {REPORT_PATH}: {e}", file=sys.stderr)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: guard.py <sql_file>", file=sys.stderr)
        return 2
    sql = Path(sys.argv[1]).read_text(encoding="utf-8-sig")  # tolerate a BOM
    out = check(sql)
    write_report(sql, out)
    print(json.dumps(out, default=str, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
