"""Layer 4: the sandbox must hold on its own, with SQL that skipped the validator.

Every test runs on a throwaway database built by the fixture (fixed, trusted SQL on a
writable connection that is closed before any test SQL runs), never on data/olist.duckdb.
"""

from __future__ import annotations

import hashlib
import time

import duckdb
import pytest

from text2sql import executor
from text2sql.executor import ExecutionRefused, execute

ROWS = 500


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "sandbox.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute(
            "CREATE TABLE t AS SELECT range AS a, 'row ' || range AS s FROM range(?)", [ROWS]
        )
    return path


@pytest.fixture()
def csv_file(tmp_path):
    path = tmp_path / "secret.csv"
    path.write_text("token\nsk-not-a-real-key\n", encoding="utf-8")
    return path.as_posix()


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _row_count(db) -> int:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute("SELECT count(*) FROM t").fetchone()[0]


def test_select_works_and_respects_max_rows(db):
    res = execute("SELECT a, s FROM t ORDER BY a", db_path=db, max_rows=10)
    assert res.columns == ["a", "s"]
    assert res.row_count == 10 and res.rows[0] == (0, "row 0")
    assert res.duration_ms >= 0


def test_fetch_is_capped_even_without_limit(db):
    res = execute("SELECT a FROM t", db_path=db, max_rows=7)
    assert res.row_count == 7


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t",
        "DELETE FROM t",
        "UPDATE t SET a = 0",
        "INSERT INTO t VALUES (1, 'x')",
        "CREATE TABLE stolen AS SELECT * FROM t",
        "ALTER TABLE t ADD COLUMN b INT",
        "SELECT 1; DROP TABLE t",
        "WITH x AS (SELECT 1) DELETE FROM t",
    ],
)
def test_writes_fail_on_read_only_connection(db, sql):
    before = _digest(db)
    with pytest.raises(ExecutionRefused):
        execute(sql, db_path=db)
    assert _digest(db) == before
    assert _row_count(db) == ROWS


def test_checkpoint_is_a_no_op_on_read_only_connection(db):
    before = _digest(db)
    execute("CHECKPOINT", db_path=db)  # accepted by DuckDB, but nothing is written
    assert _digest(db) == before


@pytest.mark.parametrize(
    "template",
    [
        "SELECT * FROM read_csv('{f}')",
        "SELECT * FROM read_csv_auto('{f}')",
        "SELECT * FROM '{f}'",
        "SELECT * FROM read_text('{f}')",
        "SELECT * FROM glob('{d}/*')",
    ],
)
def test_file_reads_fail_with_external_access_off(db, csv_file, template):
    directory = csv_file.rsplit("/", 1)[0]
    with pytest.raises(ExecutionRefused):
        execute(template.format(f=csv_file, d=directory), db_path=db)


def test_copy_to_file_fails_and_writes_nothing(db, tmp_path):
    out = tmp_path / "out.csv"
    with pytest.raises(ExecutionRefused):
        execute(f"COPY t TO '{out.as_posix()}'", db_path=db)
    assert not out.exists()


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH ':memory:' AS other",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SELECT * FROM read_csv('https://example.com/x.csv')",
    ],
)
def test_external_resources_are_blocked(db, sql):
    with pytest.raises(ExecutionRefused):
        execute(sql, db_path=db)


@pytest.mark.parametrize(
    "sql",
    [
        "SET enable_external_access = true",
        "SET lock_configuration = false",
        "RESET lock_configuration",
        "SET memory_limit = '64GB'",
        "SET threads = 64",
        "PRAGMA threads = 64",
    ],
)
def test_configuration_is_locked(db, sql):
    with pytest.raises(ExecutionRefused):
        execute(sql, db_path=db)


def test_sandbox_settings_are_applied(db):
    res = execute(
        "SELECT current_setting('enable_external_access'), current_setting('lock_configuration'),"
        " current_setting('threads')",
        db_path=db,
    )
    assert res.rows[0] == (False, True, 2)


def test_slow_query_hits_timeout(db):
    sql = (
        "SELECT count(*) FROM generate_series(1, 100000000) AS a"
        " CROSS JOIN generate_series(1, 100000000) AS b"
    )
    start = time.perf_counter()
    with pytest.raises(ExecutionRefused) as info:
        execute(sql, db_path=db, timeout_s=0.5)
    assert info.value.error_type == "timeout"
    assert "timeout" in info.value.reason
    assert time.perf_counter() - start < 5


def test_sql_errors_are_reported_as_refusals(db):
    with pytest.raises(ExecutionRefused) as info:
        execute("SELECT no_such_column FROM t", db_path=db)
    assert info.value.error_type == "BinderException"


def test_missing_database_is_an_error_not_a_new_file(tmp_path):
    missing = tmp_path / "missing.duckdb"
    with pytest.raises(FileNotFoundError):
        execute("SELECT 1", db_path=missing)
    assert not missing.exists()


def test_statement_without_result_set(db):
    res = execute("SELECT 1 WHERE false", db_path=db)
    assert res.row_count == 0 and res.columns


def test_connect_is_read_only(db):
    con = executor.connect(db)
    try:
        with pytest.raises(duckdb.Error):
            con.execute("CREATE TABLE x (a INT)")
    finally:
        con.close()
