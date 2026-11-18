import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from sql_guard import UnsafeQueryError, run_query, validate_sql  # noqa: E402

TABLES = {"olist_orders_dataset", "olist_products_dataset"}


def ok(sql, max_rows=100):
    return validate_sql(sql, TABLES, max_rows=max_rows)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM olist_orders_dataset",
    "select order_id from OLIST_ORDERS_DATASET where order_status = 'delivered'",
    "WITH o AS (SELECT * FROM olist_orders_dataset) SELECT count(*) FROM o",
    "SELECT 1 UNION ALL SELECT 2",
    "SELECT * FROM main.olist_products_dataset",
    "(SELECT 1)",
    "SELECT 1;",
    "SELECT ';DROP TABLE x' AS s",
    "SELECT 1 -- ; DROP TABLE x",
])
def test_allows_selects(sql):
    assert "LIMIT" in ok(sql).upper()


@pytest.mark.parametrize("sql", [
    "", "   ", "not sql at all (((",
    "SELECT 1; SELECT 2",
    "SELECT 1; DROP TABLE olist_orders_dataset",
    "DROP TABLE olist_orders_dataset",
    "DELETE FROM olist_orders_dataset",
    "UPDATE olist_orders_dataset SET order_status = 'x'",
    "INSERT INTO olist_orders_dataset SELECT * FROM olist_orders_dataset",
    "CREATE TABLE t AS SELECT 1",
    "ALTER TABLE olist_orders_dataset ADD COLUMN x INT",
    "ATTACH 'other.db'",
    "COPY olist_orders_dataset TO 'out.csv'",
    "PRAGMA show_tables",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SET enable_external_access = true",
    "CALL pragma_version()",
    "SELECT * INTO t FROM olist_orders_dataset",
    "DESCRIBE olist_orders_dataset",
    "SUMMARIZE olist_orders_dataset",
    "SELECT * FROM read_csv('data/olist_orders_dataset.csv')",
    "SELECT * FROM 'data/olist_orders_dataset.csv'",
    "SELECT * FROM read_parquet('x.parquet')",
    "SELECT * FROM glob('*')",
    "SELECT * FROM query('DROP TABLE x')",
    "SELECT getenv('PATH')",
    "SELECT * FROM secret_table",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM other_db.main.olist_orders_dataset",
    "SELECT * FROM olist_orders_dataset WHERE order_id IN (SELECT x FROM read_csv('a.csv'))",
    "WITH x AS (SELECT * FROM read_csv('a.csv')) SELECT * FROM x",
])
def test_rejects_unsafe(sql):
    with pytest.raises(UnsafeQueryError):
        ok(sql)


def test_adds_limit_when_missing():
    assert ok("SELECT * FROM olist_orders_dataset", max_rows=50).endswith("LIMIT 51")


def test_caps_large_limit():
    assert ok("SELECT * FROM olist_orders_dataset LIMIT 100000", max_rows=50).endswith("LIMIT 51")


def test_keeps_small_limit():
    assert ok("SELECT * FROM olist_orders_dataset LIMIT 5", max_rows=50).endswith("LIMIT 5")


def test_caps_non_literal_limit():
    assert ok("SELECT * FROM olist_orders_dataset LIMIT (SELECT 10 ** 9)", max_rows=50).endswith("LIMIT 51")


def test_limit_applies_to_whole_union():
    out = ok("SELECT 1 UNION ALL SELECT 2", max_rows=1)
    assert out.endswith("LIMIT 2")


# --- integration: real database ------------------------------------------

needs_db = pytest.mark.skipif(not db.DB_PATH.exists(), reason="run build_db.py first")


@needs_db
def test_run_query_truncates():
    res = run_query("SELECT order_id FROM olist_orders_dataset", max_rows=10)
    assert len(res.rows) == 10 and res.truncated
    assert res.columns == ["order_id"]


@needs_db
def test_run_query_not_truncated():
    res = run_query("SELECT count(*) AS n FROM olist_orders_dataset", max_rows=10)
    assert len(res.rows) == 1 and not res.truncated


@needs_db
def test_run_query_rejects_before_executing():
    with pytest.raises(UnsafeQueryError):
        run_query("DROP TABLE olist_orders_dataset")


@needs_db
def test_connection_is_read_only_and_locked():
    import duckdb
    with db.get_connection() as con:
        with pytest.raises(duckdb.Error):
            con.execute("CREATE TABLE t AS SELECT 1")
        with pytest.raises(duckdb.Error):
            con.execute("SELECT * FROM read_csv('data/olist_orders_dataset.csv')")
        with pytest.raises(duckdb.Error):
            con.execute("SET enable_external_access = true")
