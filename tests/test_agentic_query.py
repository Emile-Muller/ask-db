"""Offline checks of the three things this project promises. No API calls (fake Claude client).

1. Safety (CLAUDE.md): untrusted SQL can only read the Olist tables, never write, chain
   statements, touch files or settings, and it runs on a read-only, locked connection.
2. Pipeline: question -> prompt (instructions + schema) -> SQL -> guardrail -> DuckDB.
3. Eval: every case in eval/questions.yaml is consistent, and grading passes a correct
   answer and fails a wrong one. The /eval skill's guard.py reports what the guardrail says.

Tests marked needs_db are skipped until `python -m agentic_query.build_db` has been run.
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentic_query import db, schema  # noqa: E402
from agentic_query import eval as ev  # noqa: E402
from agentic_query import text_to_sql as t2s  # noqa: E402
from agentic_query.sql_guard import QueryResult, UnsafeQueryError, run_query, validate_sql  # noqa: E402

GUARD_SCRIPT = ROOT / ".claude" / "skills" / "eval" / "guard.py"
CANNOT_ANSWER_SQL = "SELECT 'Cannot answer from the available data' AS error"
TABLES = {"olist_orders_dataset", "olist_products_dataset"}

needs_db = pytest.mark.skipif(
    not db.DB_PATH.exists(), reason="run python -m agentic_query.build_db first"
)


class FakeClient:
    """Stands in for anthropic.Anthropic: records requests, answers with ``text``."""

    def __init__(self, text="SELECT 1", stop_reason="end_turn"):
        self.calls = []
        self._response = SimpleNamespace(
            stop_reason=stop_reason,
            content=[
                SimpleNamespace(type="thinking", thinking=""),  # must be ignored
                SimpleNamespace(type="text", text=text),
            ],
        )
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def qr(columns, rows):
    return QueryResult(sql="", columns=columns, rows=rows, truncated=False)


# --- 1. Safety ----------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM olist_orders_dataset",
        "select order_id from OLIST_ORDERS_DATASET where order_status = 'delivered'",
        "WITH o AS (SELECT * FROM olist_orders_dataset) SELECT count(*) FROM o",
        "SELECT 1 UNION ALL SELECT 2",
        "SELECT * FROM main.olist_products_dataset",
        "(SELECT 1)",
        "SELECT 1;",
        "SELECT ';DROP TABLE x' AS s",
        "SELECT 1 -- ; DROP TABLE x",
    ],
)
def test_guard_allows_read_only_selects(sql):
    assert "LIMIT" in validate_sql(sql, TABLES).upper()


@pytest.mark.parametrize(
    "sql",
    [
        # not exactly one parseable statement
        "",
        "   ",
        "not sql at all (((",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE olist_orders_dataset",
        # writes, DDL, settings, extensions
        "DROP TABLE olist_orders_dataset",
        "DELETE FROM olist_orders_dataset",
        "UPDATE olist_orders_dataset SET order_status = 'x'",
        "INSERT INTO olist_orders_dataset SELECT * FROM olist_orders_dataset",
        "CREATE TABLE t AS SELECT 1",
        "ALTER TABLE olist_orders_dataset ADD COLUMN x INT",
        "SELECT * INTO t FROM olist_orders_dataset",
        "ATTACH 'other.db'",
        "COPY olist_orders_dataset TO 'out.csv'",
        "PRAGMA show_tables",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET enable_external_access = true",
        "CALL pragma_version()",
        "DESCRIBE olist_orders_dataset",
        "SUMMARIZE olist_orders_dataset",
        # files, environment, dynamic SQL
        "SELECT * FROM read_csv('data/olist_orders_dataset.csv')",
        "SELECT * FROM 'data/olist_orders_dataset.csv'",
        "SELECT * FROM read_parquet('x.parquet')",
        "SELECT * FROM glob('*')",
        "SELECT * FROM query('DROP TABLE x')",
        "SELECT getenv('PATH')",
        "SELECT * FROM olist_orders_dataset WHERE order_id IN (SELECT x FROM read_csv('a.csv'))",
        "WITH x AS (SELECT * FROM read_csv('a.csv')) SELECT * FROM x",
        # tables outside the allowlist
        "SELECT * FROM secret_table",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM other_db.main.olist_orders_dataset",
    ],
)
def test_guard_rejects_everything_else(sql):
    with pytest.raises(UnsafeQueryError):
        validate_sql(sql, TABLES)


@pytest.mark.parametrize(
    "sql, limit",
    [
        ("SELECT * FROM olist_orders_dataset", 51),  # added
        ("SELECT * FROM olist_orders_dataset LIMIT 100000", 51),  # capped
        ("SELECT * FROM olist_orders_dataset LIMIT (SELECT 10 ** 9)", 51),  # non-literal: capped
        ("SELECT * FROM olist_orders_dataset LIMIT 5", 5),  # small: kept
    ],
)
def test_guard_caps_row_limit(sql, limit):
    assert validate_sql(sql, TABLES, max_rows=50).endswith(f"LIMIT {limit}")


@needs_db
def test_run_query_fetches_at_most_max_rows():
    res = run_query("SELECT order_id FROM olist_orders_dataset", max_rows=10)
    assert len(res.rows) == 10 and res.truncated and res.columns == ["order_id"]
    assert not run_query("SELECT count(*) FROM olist_orders_dataset", max_rows=10).truncated


@needs_db
def test_connection_is_read_only_and_locked():
    """Second line of defence: even SQL that slipped past the guardrail cannot do harm."""
    with db.get_connection() as con:
        for sql in (
            "CREATE TABLE t AS SELECT 1",
            "SELECT * FROM read_csv('data/olist_orders_dataset.csv')",
            "SET enable_external_access = true",
        ):
            with pytest.raises(duckdb.Error):
                con.execute(sql)


# --- 2. Pipeline ----------------------------------------------------------------


@needs_db
def test_schema_yaml_matches_database():
    with db.get_connection() as con:
        live = schema.introspect(con)
    assert schema.schema_mismatches(live, schema.load_descriptions()) == []


def test_schema_drift_is_detected():
    live = {"t": [("a", "INTEGER"), ("b", "VARCHAR")]}
    docs = {"tables": {"t": {"columns": {"a": "x", "z": "y"}, "foreign_keys": {"a": "u.id"}}}}
    assert set(schema.schema_mismatches(live, docs)) == {
        "t.b is not described",
        "t.z described but missing",
        "t.a references unknown u.id",
    }


@needs_db
def test_prompt_has_instructions_schema_and_question():
    client = FakeClient("SELECT count(*) FROM olist_orders_dataset")
    assert (
        t2s.generate_sql("How many orders?", client) == "SELECT count(*) FROM olist_orders_dataset"
    )
    (call,) = client.calls
    system = call["system"][0]["text"]
    assert system.startswith(t2s.INSTRUCTIONS)
    assert "TABLE olist_orders_dataset" in system and "Rules:" in system
    assert "How many orders?" in call["messages"][0]["content"]
    assert "How many orders?" not in system  # question stays out of the cached prefix


@pytest.mark.parametrize(
    "raw, sql",
    [
        ("SELECT 1", "SELECT 1"),
        ("SELECT 1;", "SELECT 1"),
        ("```sql\nSELECT 1;\n```", "SELECT 1"),
        ("Here you go:\n```\nSELECT a FROM t\n```\nDone.", "SELECT a FROM t"),
    ],
)
def test_extract_sql_strips_fences_and_semicolon(raw, sql):
    assert t2s.extract_sql(raw) == sql


@pytest.mark.parametrize("question", ["", "   ", "x" * (t2s.MAX_QUESTION_CHARS + 1)])
def test_empty_or_long_question_never_reaches_the_api(question):
    client = FakeClient()
    with pytest.raises(t2s.GenerationError):
        t2s.generate_sql(question, client)
    assert client.calls == []


@needs_db
@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_unusable_model_answer_raises(stop_reason):
    with pytest.raises(t2s.GenerationError):
        t2s.generate_sql("q", FakeClient("SELECT 1", stop_reason))


@needs_db
def test_ask_runs_model_sql_through_guardrail():
    a = t2s.ask("orders?", FakeClient("SELECT count(*) AS n FROM olist_orders_dataset"))
    assert a.result.columns == ["n"] and a.result.rows[0][0] > 0
    assert "LIMIT" in a.result.sql.upper()


@needs_db
@pytest.mark.parametrize(
    "llm_sql",
    [
        "DROP TABLE olist_orders_dataset",
        "SELECT 1; DELETE FROM olist_orders_dataset",
        "SELECT * FROM read_csv('data/olist_orders_dataset.csv')",
        "Sure! The query is: SELECT 1",
    ],
)
def test_ask_blocks_unsafe_model_output(llm_sql):
    with pytest.raises(UnsafeQueryError):
        t2s.ask("q", FakeClient(llm_sql))


# --- 3. Eval --------------------------------------------------------------------

ITEMS = ev.load_questions()  # also validates ids, kinds and gold_sql / expect: refuse
GOLD = [q for q in ITEMS if q.get("gold_sql")]
REFUSE = [q for q in ITEMS if q.get("expect") == "refuse"]
INVALID_SQL = [q for q in ITEMS if q["kind"] == "invalid_sql"]


def _ids(items):
    return [q["id"] for q in items]


def test_eval_set_has_30_cases_of_every_kind():
    assert len(ITEMS) == 30
    assert {q["kind"] for q in ITEMS} == set(ev.KINDS)


@needs_db
@pytest.mark.parametrize("item", GOLD, ids=_ids(GOLD))
def test_gold_sql_passes_guardrail(item):
    gold = run_query(item["gold_sql"], max_rows=ev.MAX_ROWS)
    assert gold.rows and not gold.truncated
    assert ev.compare(gold, gold, item.get("ordered", False), item.get("check"))[0]
    if item["kind"] == "sql":  # a pasted valid query must itself be the answer
        pasted = run_query(t2s.extract_sql(item["question"]), max_rows=ev.MAX_ROWS)
        assert ev.compare(gold, pasted, item.get("ordered", False), item.get("check"))[0]


@needs_db
@pytest.mark.parametrize("item", INVALID_SQL, ids=_ids(INVALID_SQL))
def test_invalid_sql_input_fails_as_written(item):
    """Otherwise the case would not test anything: the input must not run verbatim."""
    with pytest.raises((UnsafeQueryError, duckdb.Error)):
        run_query(item["question"])


@needs_db
@pytest.mark.parametrize("item", ITEMS, ids=_ids(ITEMS))
def test_grading_passes_right_answer_and_fails_wrong_one(item):
    right = CANNOT_ANSWER_SQL if item.get("expect") == "refuse" else item["gold_sql"]
    wrong = CANNOT_ANSWER_SQL if item.get("gold_sql") else "SELECT 1 AS n"
    assert ev.run_one(item, FakeClient(right)).passed
    assert not ev.run_one(item, FakeClient(wrong)).passed


@needs_db
def test_grading_counts_guardrail_block_as_refusal():
    out = ev.run_one(REFUSE[0], FakeClient("DROP TABLE olist_orders_dataset"))
    assert out.passed and out.reason.startswith("guardrail rejected")


def test_compare_ignores_column_names_extra_columns_and_row_order():
    gold = qr(["cat", "rev"], [("a", 10.0), ("b", 5.0)])
    pred = qr(["category_pt", "category", "revenue"], [("y", "b", 5.004), ("x", "a", 10)])
    assert ev.compare(gold, pred)[0]


def test_compare_rejects_wrong_answers():
    gold = qr(["cat", "rev"], [("a", 10.0), ("b", 5.0)])
    assert not ev.compare(gold, qr(["c", "r"], [("a", 11.0), ("b", 5.0)]))[0]  # value
    assert not ev.compare(gold, qr(["c", "r"], [("a", 5.0), ("b", 10.0)]))[0]  # pairs swapped
    assert not ev.compare(gold, qr(["c", "r"], [("a", 10.0)]))[0]  # row count
    assert not ev.compare(gold, qr(["c", "r"], [("b", 5.0), ("a", 10.0)]), ordered=True)[0]


def test_compare_check_limits_columns():
    gold = qr(["month", "n"], [("2017-01", 1), ("2017-02", 2)])
    pred = qr(["m", "n"], [("2017-01-01T00:00:00", 1), ("2017-02-01T00:00:00", 2)])
    assert not ev.compare(gold, pred, ordered=True)[0]
    assert ev.compare(gold, pred, ordered=True, check=["n"])[0]


@needs_db
@pytest.mark.parametrize(
    "sql, ok, stage",
    [
        ("SELECT order_id FROM olist_orders_dataset", True, None),
        ("DELETE FROM olist_orders_dataset", False, "guardrail"),
        ("SELECT no_such_column FROM olist_orders_dataset", False, "duckdb"),
    ],
)
def test_skill_guard_script(tmp_path, sql, ok, stage):
    """The /eval skill runs its SQL only through guard.py, from a file."""
    sql_file = tmp_path / "query.sql"
    sql_file.write_text(sql, encoding="utf-8-sig")  # editors on Windows may add a BOM
    proc = subprocess.run(  # noqa: S603 - fixed args: this interpreter + repo script
        [sys.executable, str(GUARD_SCRIPT), str(sql_file)], capture_output=True, text=True, cwd=ROOT
    )
    out = json.loads(proc.stdout)
    assert (proc.returncode == 0) is ok and out["ok"] is ok
    if ok:
        assert "LIMIT" in out["sql"].upper() and len(out["preview"]) == 5 and out["more_rows"]
    else:
        assert out["stage"] == stage and out["error"]
