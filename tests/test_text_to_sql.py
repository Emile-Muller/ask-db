"""Offline tests for schema context, prompt, SQL extraction and eval grading.
No API calls: the Claude client is replaced by a fake."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentic_query import db, schema  # noqa: E402
from agentic_query import eval as ev  # noqa: E402
from agentic_query import text_to_sql as t2s  # noqa: E402
from agentic_query.sql_guard import QueryResult, UnsafeQueryError  # noqa: E402

needs_db = pytest.mark.skipif(
    not db.DB_PATH.exists(), reason="run python -m agentic_query.build_db first"
)


class FakeClient:
    """Mimics client.beta.messages.create and records the request."""

    def __init__(self, text="SELECT 1", stop_reason="end_turn"):
        self.calls = []
        self._response = SimpleNamespace(
            stop_reason=stop_reason,
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text=text),
            ],
        )
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


# --- schema -----------------------------------------------------------------


@needs_db
def test_schema_yaml_matches_database():
    with db.get_connection() as con:
        live = schema.introspect(con)
    assert schema.schema_mismatches(live, schema.load_descriptions()) == []


def test_schema_mismatches_detects_drift():
    live = {"t": [("a", "INTEGER"), ("b", "VARCHAR")]}
    docs = {"tables": {"t": {"columns": {"a": "x", "z": "y"}, "foreign_keys": {"a": "u.id"}}}}
    problems = schema.schema_mismatches(live, docs)
    assert "t.b is not described" in problems
    assert "t.z described but missing" in problems
    assert "t.a references unknown u.id" in problems


def test_render_schema_includes_types_keys_and_rules():
    live = {"orders": [("id", "VARCHAR"), ("cust", "VARCHAR")], "cust": [("id", "VARCHAR")]}
    docs = {
        "rules": ["Revenue = SUM(price)."],
        "tables": {
            "orders": {
                "description": "One row per order.",
                "primary_key": ["id"],
                "foreign_keys": {"cust": "cust.id"},
                "columns": {"id": "Order id.", "cust": "Customer."},
            }
        },
    }
    text = schema.render_schema(schema.build_tables(live, docs), docs)
    assert "TABLE orders -- One row per order." in text
    assert "primary key: (id)" in text
    assert "cust VARCHAR -> cust.id -- Customer." in text
    assert "TABLE cust" in text  # undocumented tables are still listed
    assert "- Revenue = SUM(price)." in text


# --- generation -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, sql",
    [
        ("SELECT 1", "SELECT 1"),
        ("SELECT 1;", "SELECT 1"),
        ("```sql\nSELECT 1;\n```", "SELECT 1"),
        ("Here you go:\n```\nSELECT a FROM t\n```\nDone.", "SELECT a FROM t"),
    ],
)
def test_extract_sql(raw, sql):
    assert t2s.extract_sql(raw) == sql


@needs_db
def test_prompt_has_instructions_schema_and_question():
    client = FakeClient("SELECT count(*) FROM olist_orders_dataset")
    sql = t2s.generate_sql("How many orders?", client)
    assert sql == "SELECT count(*) FROM olist_orders_dataset"
    (call,) = client.calls
    system = call["system"][0]["text"]
    assert system.startswith("You write SQL for DuckDB. Use only the tables below.")
    assert "TABLE olist_orders_dataset" in system
    assert "product_category_name_english" in system
    assert "How many orders?" in call["messages"][0]["content"]
    assert "How many orders?" not in system  # question stays out of the cached prefix


@pytest.mark.parametrize("question", ["", "   ", "x" * (t2s.MAX_QUESTION_CHARS + 1)])
def test_rejects_bad_questions_without_calling_api(question):
    client = FakeClient()
    with pytest.raises(t2s.GenerationError):
        t2s.generate_sql(question, client)
    assert client.calls == []


@needs_db
@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_bad_stop_reasons_raise(stop_reason):
    with pytest.raises(t2s.GenerationError):
        t2s.generate_sql("q", FakeClient("SELECT 1", stop_reason))


@needs_db
def test_ask_runs_through_guardrail():
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
def test_ask_blocks_unsafe_llm_output(llm_sql):
    with pytest.raises(UnsafeQueryError):
        t2s.ask("q", FakeClient(llm_sql))


# --- eval grading ------------------------------------------------------------


def qr(columns, rows):
    return QueryResult(sql="", columns=columns, rows=rows, truncated=False)


def test_compare_ignores_names_extra_columns_and_order():
    gold = qr(["cat", "rev"], [("a", 10.0), ("b", 5.0)])
    pred = qr(["category_pt", "category", "revenue"], [("y", "b", 5.004), ("x", "a", 10)])
    assert ev.compare(gold, pred)[0]


def test_compare_ordered():
    gold = qr(["cat", "rev"], [("a", 10.0), ("b", 5.0)])
    assert not ev.compare(gold, qr(["c", "r"], [("b", 5.0), ("a", 10.0)]), ordered=True)[0]
    assert ev.compare(gold, qr(["c", "r"], [("a", 10.0), ("b", 5.0)]), ordered=True)[0]


def test_compare_rejects_wrong_values_and_row_mismatch():
    gold = qr(["cat", "rev"], [("a", 10.0), ("b", 5.0)])
    assert not ev.compare(gold, qr(["c", "r"], [("a", 11.0), ("b", 5.0)]))[0]
    assert not ev.compare(gold, qr(["c", "r"], [("a", 5.0), ("b", 10.0)]))[0]  # swapped pairs
    assert not ev.compare(gold, qr(["c", "r"], [("a", 10.0)]))[0]


def test_compare_check_subset():
    gold = qr(["month", "n"], [("2017-01", 1), ("2017-02", 2)])
    pred = qr(["m", "n"], [("2017-01-01T00:00:00", 1), ("2017-02-01T00:00:00", 2)])
    assert not ev.compare(gold, pred, ordered=True)[0]
    assert ev.compare(gold, pred, ordered=True, check=["n"])[0]


def test_cannot_answer_detection():
    assert ev.is_cannot_answer(qr(["error"], [("Cannot answer from the available data",)]))
    assert not ev.is_cannot_answer(qr(["n"], [(1,)]))


def test_questions_file_is_valid():
    items = ev.load_questions()
    assert 20 <= len(items) <= 30
    for q in items:
        assert q["question"].strip()
        assert q.get("expect") == "refuse" or q.get("gold_sql")
