"""SQL guardrail: the only way untrusted (user / LLM) SQL may reach DuckDB.

Layers, all enforced by ``run_query``:

1. Parse with sqlglot (DuckDB dialect). Unparseable SQL is rejected.
2. Exactly one statement, and it must be a SELECT (optionally WITH / UNION /
   INTERSECT / EXCEPT). Any write, DDL, PRAGMA, ATTACH, COPY, SET, INSTALL,
   LOAD, SELECT ... INTO, etc. anywhere in the tree is rejected.
3. Table allowlist: every table reference must be a CTE name or a real table
   in the database's ``main`` schema. Table functions (``read_csv``, ``glob``,
   ``query``...) and file paths (``FROM 'x.csv'``) are rejected.
4. Row limit: the query's LIMIT is capped at ``max_rows`` and at most
   ``max_rows`` rows are fetched.
5. The SQL actually executed is the one regenerated from the validated AST,
   on a read-only connection with external access disabled (see ``db.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from db import get_connection

DIALECT = "duckdb"
DEFAULT_MAX_ROWS = 1000

# Node types that must never appear anywhere in an accepted query.
_FORBIDDEN_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "TruncateTable",
    "Command",
    "Pragma",
    "Copy",
    "Attach",
    "Detach",
    "Install",
    "LoadData",
    "Set",
    "Use",
    "Transaction",
    "Commit",
    "Rollback",
    "Into",
    "Describe",
    "Summarize",
    "Refresh",
    "Cache",
    "Uncache",
    "Grant",
    "Revoke",
    "Analyze",
    "Export",
)
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name) for name in _FORBIDDEN_NAMES if hasattr(exp, name)
)

# Functions that reach outside the allowed tables (files, env, dynamic SQL).
FORBIDDEN_FUNCTIONS = {
    "query",
    "query_table",
    "glob",
    "getenv",
    "read_text",
    "read_blob",
    "read_csv",
    "read_csv_auto",
    "read_json",
    "read_json_auto",
    "read_ndjson",
    "read_parquet",
    "parquet_scan",
    "parquet_metadata",
    "parquet_schema",
    "sniff_csv",
    "iceberg_scan",
    "delta_scan",
    "sqlite_scan",
    "postgres_scan",
    "mysql_scan",
    "current_setting",
}


class UnsafeQueryError(ValueError):
    """Raised when SQL fails the guardrail. The message is safe to show users."""


@dataclass(frozen=True)
class QueryResult:
    sql: str  # the SQL that was actually executed
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool  # True if more than max_rows rows were available

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]


def _parse_single(sql: str) -> exp.Expression:
    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeQueryError("Query is empty.")
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except ParseError as e:
        raise UnsafeQueryError(f"Could not parse SQL: {e}") from e
    if len(statements) != 1:
        raise UnsafeQueryError("Exactly one SQL statement is allowed.")
    return statements[0]


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    return (node.sql_name() or "").lower()


def validate_sql(
    sql: str,
    allowed_tables: set[str],
    max_rows: int = DEFAULT_MAX_ROWS,
) -> str:
    """Validate untrusted SQL and return the safe, LIMIT-capped SQL to execute.

    ``allowed_tables`` are the table names (case-insensitive) queries may read.
    The returned LIMIT is at most ``max_rows + 1`` so callers can detect
    truncation. Raises ``UnsafeQueryError`` on any violation.
    """
    if max_rows < 1:
        raise ValueError("max_rows must be >= 1")

    root = _parse_single(sql)
    while isinstance(root, exp.Subquery):  # "(SELECT ...)" -> "SELECT ..."
        root = root.this

    if not isinstance(root, (exp.Select, exp.SetOperation)):
        raise UnsafeQueryError("Only SELECT queries are allowed.")

    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeQueryError(f"Forbidden SQL construct: {type(node).__name__.upper()}.")
        if isinstance(node, exp.Func) and _function_name(node) in FORBIDDEN_FUNCTIONS:
            raise UnsafeQueryError(f"Forbidden function: {_function_name(node)}().")

    allowed = {t.lower() for t in allowed_tables}
    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeQueryError("Table functions are not allowed in FROM / JOIN.")
        name = table.name.lower()
        if table.catalog or (table.db and table.db.lower() != "main"):
            raise UnsafeQueryError(
                f"Only tables in the main schema are allowed: {table.sql(DIALECT)}."
            )
        if name not in allowed and not (name in cte_names and not table.db):
            raise UnsafeQueryError(f"Unknown or disallowed table: {table.name}.")

    # Row limit: keep a small literal LIMIT, otherwise cap it.
    limit = root.args.get("limit")
    current = None
    if (
        isinstance(limit, exp.Limit)
        and isinstance(limit.expression, exp.Literal)
        and limit.expression.is_int
    ):
        current = int(limit.expression.this)
    if current is None or current > max_rows:
        root = root.limit(max_rows + 1, copy=False)

    return root.sql(dialect=DIALECT)


def list_tables(con) -> set[str]:
    """Names of the base tables / views in the main schema (trusted, fixed SQL)."""
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    return {r[0] for r in rows}


def run_query(sql: str, max_rows: int = DEFAULT_MAX_ROWS) -> QueryResult:
    """Validate untrusted SQL, then run it on a read-only, locked-down connection."""
    with get_connection() as con:
        safe_sql = validate_sql(sql, list_tables(con), max_rows=max_rows)
        cur = con.execute(safe_sql)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    return QueryResult(sql=safe_sql, columns=columns, rows=rows[:max_rows], truncated=truncated)
