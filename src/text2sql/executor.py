"""Layer 4: sandboxed execution on DuckDB.

Second half of the security boundary. It must stay safe even when called directly
with SQL that never went through the validator:

- the database is opened ``read_only=True``: no writes, DDL or CHECKPOINT;
- ``enable_external_access=false``: no file / URL reads, COPY, ATTACH, INSTALL, LOAD;
- extension autoload / autoinstall off;
- ``memory_limit`` and ``threads`` cap resources;
- then ``lock_configuration=true``, so SQL cannot SET any of this back;
- a watchdog thread calls ``connection.interrupt()`` after the statement timeout;
- at most ``max_rows`` rows are fetched.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from text2sql import config


@dataclass
class ExecutionResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    row_count: int
    duration_ms: int


# DuckDB error classes that mean "the SQL itself is wrong" (bad column, type, cast), not a
# guardrail (permission, timeout, memory). Only these let /question-batch send the error
# back to the proposer for one corrected proposal (run --brief: "sql_error").
SQL_MISTAKES = frozenset({
    "BinderException", "CatalogException", "ConversionException", "InvalidInputException",
    "OutOfRangeException", "ParserException", "TypeMismatchException",
})  # fmt: skip


class ExecutionRefused(Exception):
    """DuckDB refused or aborted the statement (permission, timeout, SQL error)."""

    def __init__(self, reason: str, error_type: str, duration_ms: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error_type = error_type
        self.duration_ms = duration_ms


def connect(db_path: Path | str = config.DB_PATH) -> duckdb.DuckDBPyConnection:
    """Open the locked-down, read-only connection. The only way this package opens a DB."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    con = duckdb.connect(
        str(path),
        read_only=True,
        config={
            "enable_external_access": False,
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
            "memory_limit": config.MEMORY_LIMIT,
            "threads": config.THREADS,
        },
    )
    con.execute("SET lock_configuration = true")
    return con


def execute(
    sql: str,
    db_path: Path | str = config.DB_PATH,
    max_rows: int = config.MAX_ROWS,
    timeout_s: float = config.STATEMENT_TIMEOUT_S,
) -> ExecutionResult:
    """Run ``sql`` in the sandbox. Raises ExecutionRefused if DuckDB rejects or times out."""
    con = connect(db_path)
    timed_out = threading.Event()

    def watchdog() -> None:
        timed_out.set()
        con.interrupt()

    timer = threading.Timer(timeout_s, watchdog)
    timer.daemon = True
    start = time.perf_counter()
    try:
        timer.start()
        cursor = con.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchmany(max_rows) if cursor.description else []
    except duckdb.Error as e:
        elapsed = _ms_since(start)
        if timed_out.is_set():
            raise ExecutionRefused(
                f"statement timeout after {timeout_s:g} s", "timeout", elapsed
            ) from e
        raise ExecutionRefused(_first_line(e), type(e).__name__, elapsed) from e
    finally:
        timer.cancel()
        con.close()
    return ExecutionResult(columns, rows, len(rows), _ms_since(start))


def _ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _first_line(e: Exception) -> str:
    text = str(e).strip()
    return text.splitlines()[0] if text else type(e).__name__
