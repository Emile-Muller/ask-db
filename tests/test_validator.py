"""Layer 3: the validator, driven by the red-team corpus. No LLM, no network, no database."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot
import yaml
from sqlglot import exp

from text2sql import config, validator
from text2sql.validator import validate

CORPUS_PATH = Path(__file__).with_name("redteam_corpus.yaml")
CORPUS: list[dict] = yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))


def test_corpus_shape():
    ids = [e["id"] for e in CORPUS]
    assert len(ids) == len(set(ids)), "duplicate corpus ids"
    assert len(CORPUS) >= 60
    assert sum(e["expected"] == "allow" for e in CORPUS) >= 15
    for e in CORPUS:
        assert e["expected"] in {"allow", "block"}, e["id"]
        assert e["layer"] == 3 and e["why"] and isinstance(e["sql"], str), e["id"]
        assert "check" not in e or e["check"] in {name for name, _ in validator.CHECKS}, e["id"]


@pytest.mark.parametrize("entry", CORPUS, ids=[e["id"] for e in CORPUS])
def test_redteam_corpus(entry):
    result = validate(entry["sql"])
    if entry["expected"] == "allow":
        assert result.ok, f"over-blocked: {result.reason}"
        assert result.normalized_sql
        # What runs must itself pass validation unchanged (no drift on regeneration).
        again = validate(result.normalized_sql)
        assert again.ok, again.reason
        assert again.normalized_sql == result.normalized_sql
    else:
        assert not result.ok, f"bypass: {entry['why']}"
        assert result.normalized_sql is None
        assert result.reason and result.reason.startswith(f"{result.failed_check}:")
        if "check" in entry:
            assert result.failed_check == entry["check"], result.reason


def _limit_of(sql: str) -> int:
    tree = sqlglot.parse_one(sql, read="duckdb")
    return int(tree.args["limit"].expression.this)


@pytest.mark.parametrize(
    "sql, expected_limit, detail",
    [
        ("SELECT order_id FROM olist_orders_dataset", 100, "added LIMIT 100"),
        ("SELECT order_id FROM olist_orders_dataset LIMIT 7", 7, "kept LIMIT 7"),
        ("SELECT order_id FROM olist_orders_dataset LIMIT 100", 100, "kept LIMIT 100"),
        ("SELECT order_id FROM olist_orders_dataset LIMIT 101", 100, "capped LIMIT 101 to 100"),
        ("SELECT order_id FROM olist_orders_dataset LIMIT 99999999", 100, "capped LIMIT"),
        ("SELECT order_id FROM olist_orders_dataset LIMIT 50%", 100, "capped LIMIT"),
        ("SELECT order_id FROM olist_orders_dataset FETCH FIRST 5000 ROWS ONLY", 100, "capped"),
        ("SELECT order_id FROM olist_orders_dataset LIMIT (SELECT 5)", 100, "capped LIMIT"),
        ("SELECT seller_state FROM olist_sellers_dataset UNION SELECT 'x'", 100, "added"),
    ],
)
def test_limit_is_forced_and_capped(sql, expected_limit, detail):
    result = validate(sql)
    assert result.ok, result.reason
    assert _limit_of(result.normalized_sql) == expected_limit
    assert detail in result.checks[-1].detail


def test_custom_max_rows():
    result = validate("SELECT order_id FROM olist_orders_dataset LIMIT 50", max_rows=10)
    assert _limit_of(result.normalized_sql) == 10


def test_checks_are_reported_in_order():
    result = validate("SELECT count(*) FROM olist_orders_dataset")
    assert [c.name for c in result.checks] == [name for name, _ in validator.CHECKS]
    assert all(c.passed for c in result.checks)
    assert result.failed_check is None and result.reason is None


def test_checks_stop_at_first_failure():
    result = validate("SELECT 1; DROP TABLE olist_orders_dataset")
    assert [c.name for c in result.checks] == ["parse", "select_only"]
    assert [c.passed for c in result.checks] == [True, False]
    assert result.to_dict()["checks"][-1]["name"] == "select_only"


@pytest.mark.parametrize(
    "sql, check, detail",
    [
        # A stacked write is refused for the write, before the statement count.
        ("SELECT 1; DROP TABLE x", "select_only", "got DROP (statement 2 of 2)"),
        ("SELECT 1;DELETE FROM olist_orders_dataset", "select_only", "got DELETE (statement 2"),
        ("SELECT 1; SELECT * INTO t FROM x", "no_forbidden_nodes", "INTO (statement 2 of 2)"),
        # A comment after ';' becomes a comment-only Semicolon node; look past it.
        ("SELECT 1;/**/dRoP/**/TaBlE/**/x", "select_only", "got DROP (statement 2 of 2)"),
        ("SELECT 1; /* x */ DELETE FROM x", "select_only", "got DELETE (statement 2 of 2)"),
        ("SELECT 1 /* a */; DELETE FROM x -- b", "select_only", "got DELETE (statement 2 of 2)"),
        ("SELECT 1; /* x */", "single_statement", "found 2"),
        # Harmless stacked statements still fail the statement count.
        ("SELECT 1; SELECT 2", "single_statement", "found 2"),
    ],
)
def test_stacked_statements_report_the_write_first(sql, check, detail):
    result = validate(sql)
    assert not result.ok and result.failed_check == check
    assert detail in result.reason


@pytest.mark.parametrize("value", [None, 42, "   ", "\n\t"])
def test_non_string_or_blank_input_is_rejected(value):
    result = validate(value)
    assert not result.ok and result.failed_check == "parse"


def test_unexpected_error_fails_closed(monkeypatch):
    def boom(state):
        raise RuntimeError("sqlglot internals changed")

    checks = tuple((n, boom if n == "table_allowlist" else f) for n, f in validator.CHECKS)
    monkeypatch.setattr(validator, "CHECKS", checks)
    result = validate("SELECT count(*) FROM olist_orders_dataset")
    assert not result.ok and result.failed_check == "table_allowlist"
    assert "RuntimeError" in result.reason


def test_comments_are_also_detected_on_the_ast():
    """Comments are checked on tokens and on the AST, so either path alone is enough."""
    state = validator._State(sql="SELECT 1", max_rows=10, policy=config.load_policy())
    state.statements = [sqlglot.parse_one("SELECT 1 /* x */", read="duckdb")]
    with pytest.raises(validator._Reject):
        validator._check_no_comments(state)


def test_restricted_columns_follow_the_policy():
    policy = config.Policy(tables={"t": ("a", "secret")}, restricted=frozenset({("t", "secret")}))
    assert validate("SELECT a FROM t", policy=policy).ok
    assert validate("SELECT secret FROM t", policy=policy).failed_check == "column_allowlist"
    assert validate("SELECT * FROM t", policy=policy).failed_check == "column_allowlist"
    assert validate("SELECT * FROM olist_orders_dataset", policy=policy).failed_check == (
        "table_allowlist"
    )


def test_normalized_sql_is_the_qualified_tree():
    """What runs is what was checked: stars expanded from the policy, columns qualified.

    A column that exists in the database but not in schema.yaml can then never be returned
    by a star, since DuckDB no longer expands it.
    """
    policy = config.Policy(tables={"t": ("a", "b")}, restricted=frozenset())
    result = validate("SELECT * FROM t", policy=policy)
    assert result.ok, result.reason
    assert "*" not in result.normalized_sql
    assert '"t"."a"' in result.normalized_sql and '"t"."b"' in result.normalized_sql


def test_unaliased_outputs_keep_readable_names():
    result = validate(
        "SELECT order_status, count(*) FROM olist_orders_dataset GROUP BY 1 ORDER BY 2 DESC"
    )
    assert result.ok, result.reason
    assert 'COUNT(*) AS "count(*)"' in result.normalized_sql
    assert 'ORDER BY "count(*)" DESC' in result.normalized_sql
    assert "_col_" not in result.normalized_sql


def test_sql_length_is_capped():
    sql = "SELECT " + "1 + " * config.MAX_SQL_CHARS + "1"
    result = validate(sql)
    assert not result.ok and result.failed_check == "parse" and "longer than" in result.reason


def test_bare_column_alias_only_counts_in_its_own_order_by():
    # An alias defined in another scope, or used outside ORDER BY, does not resolve a bare column.
    other_scope = sqlglot.parse_one("SELECT (SELECT 1 AS a) AS x FROM t WHERE a = 1", read="duckdb")
    with pytest.raises(validator._Reject, match="could not be resolved"):
        validator._check_unqualified_columns(other_scope)
    window = sqlglot.parse_one("SELECT 1 AS a, rank() OVER (ORDER BY a) AS r FROM t", read="duckdb")
    with pytest.raises(validator._Reject, match="could not be resolved"):
        validator._check_unqualified_columns(window)
    ok = sqlglot.parse_one("SELECT t.x AS a FROM t ORDER BY a", read="duckdb")
    validator._check_unqualified_columns(ok)


def test_policy_marks_expected_columns_restricted():
    policy = config.load_policy()
    assert policy.is_restricted("olist_customers_dataset", "CUSTOMER_ZIP_CODE_PREFIX")
    assert not policy.is_restricted("olist_customers_dataset", "customer_unique_id")
    assert {"customer_zip_code_prefix", "review_comment_message"} <= policy.restricted_names


# --- defense in depth: each helper must fail closed even if an earlier check is removed ----


def _state(sql: str) -> validator._State:
    s = validator._State(sql=sql, max_rows=10, policy=config.load_policy())
    s.tree = sqlglot.parse_one(sql, read="duckdb")
    return s


def test_table_check_alone_rejects_table_functions():
    with pytest.raises(validator._Reject, match="table functions"):
        validator._check_table_allowlist(_state("SELECT * FROM read_csv('x.csv')"))


def test_column_helpers_alone_reject_bad_references():
    tree = sqlglot.parse_one("SELECT a FROM t", read="duckdb")
    with pytest.raises(validator._Reject, match="could not be resolved"):
        validator._check_unqualified_columns(tree)  # bare column that is not an output alias
    struct = sqlglot.parse_one("SELECT x.a FROM t AS x ORDER BY x", read="duckdb")
    with pytest.raises(validator._Reject, match="whole-row"):
        validator._check_unqualified_columns(struct)
    star = sqlglot.parse_one("SELECT * FROM t", read="duckdb")
    with pytest.raises(validator._Reject, match="expand"):
        validator._check_no_leftover_star(star)


def test_qualified_column_helper_rejects_unknown_source_and_column():
    from sqlglot.optimizer.scope import build_scope

    policy = config.load_policy()
    tree = sqlglot.parse_one("SELECT o.nope, z.order_id FROM olist_orders_dataset AS o")
    scope = build_scope(tree)
    nope, ghost = list(tree.find_all(exp.Column))
    with pytest.raises(validator._Reject, match="not allowed"):
        validator._check_qualified_column(policy, scope, nope)
    with pytest.raises(validator._Reject, match="does not resolve"):
        validator._check_qualified_column(policy, scope, ghost)


def test_join_condition_helper():
    using = sqlglot.parse_one("SELECT 1 FROM a JOIN b USING (id)", read="duckdb")
    validator._check_join_condition(next(using.find_all(exp.Join)))  # accepted
    bare = sqlglot.parse_one("SELECT 1 FROM a JOIN b", read="duckdb")
    with pytest.raises(validator._Reject, match="without an ON"):
        validator._check_join_condition(next(bare.find_all(exp.Join)))


def test_function_allowlist_names_exist_in_sqlglot():
    missing = [n for n in validator.ALLOWED_FUNCTION_CLASSES if not hasattr(exp, n)]
    assert not missing, f"allowlist names unknown to this sqlglot version: {missing}"
    assert all(issubclass(getattr(exp, n), exp.Func) for n in validator.ALLOWED_FUNCTION_CLASSES)


def test_forbidden_nodes_cover_writes_and_commands():
    # Every denylisted name must exist: a rename in sqlglot must not shrink the denylist.
    assert len(validator.FORBIDDEN_NODES) == len(validator._FORBIDDEN_NAMES)
    names = {t.__name__ for t in validator.FORBIDDEN_NODES}
    assert {"Insert", "Update", "Delete", "Drop", "Create", "Command", "Pragma", "Set"} <= names
    assert {"Attach", "Copy", "Use", "Transaction", "Into"} <= names
