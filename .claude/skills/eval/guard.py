"""Send candidate SQL through agentic_query.sql_guard.run_query and report the result.

Usage (from the repo root):
    .\\.venv\\Scripts\\python.exe .claude\\skills\\eval\\guard.py <sql_file>

The SQL is read from a file so it never has to be quoted on the command line.
Prints JSON. Exit code 0: accepted (``sql`` is the guardrail-approved SQL that ran).
Exit code 1: rejected by the guardrail or failed in DuckDB (``error`` says why).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import duckdb  # noqa: E402

from agentic_query.sql_guard import UnsafeQueryError, run_query  # noqa: E402

PREVIEW_ROWS = 5


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: guard.py <sql_file>", file=sys.stderr)
        return 2
    sql = Path(sys.argv[1]).read_text(encoding="utf-8-sig")  # tolerate a BOM
    try:
        result = run_query(sql)  # default max_rows, same as the app
    except UnsafeQueryError as e:
        print(json.dumps({"ok": False, "stage": "guardrail", "error": str(e)}))
        return 1
    except duckdb.Error as e:
        print(json.dumps({"ok": False, "stage": "duckdb", "error": str(e)}))
        return 1
    out = {
        "ok": True,
        "sql": result.sql,
        "columns": result.columns,
        "preview": [list(r) for r in result.rows[:PREVIEW_ROWS]],
        "more_rows": result.truncated or len(result.rows) > PREVIEW_ROWS,
    }
    print(json.dumps(out, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
