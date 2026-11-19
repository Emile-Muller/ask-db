"""Layer 3: static SQL validation with sqlglot (DuckDB dialect).

Deterministic: no LLM, no database access. Together with the executor (layer 4) this
is the security boundary: it must hold even if layers 1 and 2 are bypassed, so it
treats every SQL string as hostile and fails closed (any unexpected error = reject).

Checks run in order and stop at the first failure:

1. parse               - SQL is a non-empty string that sqlglot can parse
2. single_statement    - exactly one statement (a single trailing ``;`` is tolerated)
3. no_comments         - no ``--`` or ``/* */`` comments anywhere (rejected, not stripped)
4. select_only         - root is SELECT / UNION / INTERSECT / EXCEPT (CTEs allowed)
5. no_forbidden_nodes  - no DML, DDL, command, PRAGMA, SET, ATTACH, COPY, ... anywhere
6. function_allowlist  - every function call is on an explicit allowlist
7. table_allowlist     - only policy tables or CTEs defined in the query; no schemas
8. column_allowlist    - every column resolves to an allowed, non-restricted column
9. complexity          - join count, subquery depth, no cartesian / recursive queries
10. limit              - LIMIT added if missing, capped at MAX_ROWS

The SQL that runs is ``normalized_sql``: regenerated from the validated AST, never the
raw input.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.tokens import Token, TokenType

from text2sql import config
from text2sql.config import Policy

# sqlglot logs a warning when it falls back to an opaque Command node; we reject those.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

_FORBIDDEN_NAMES = (
    # DML / DDL
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable",
    # commands, session and database state
    "Command", "Pragma", "Set", "Use", "Transaction", "Commit", "Rollback",
    "Attach", "Detach", "Copy", "Install", "LoadData", "Export",
    "Describe", "Summarize", "Show", "Analyze", "Refresh", "Cache", "Uncache",
    "Grant", "Revoke",
    # SELECT ... INTO, prepared-statement parameters
    "Into", "Placeholder", "Parameter", "SessionParameter",
)  # fmt: skip
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n)
)

# Function ALLOWLIST, by sqlglot expression class. Everything else is rejected, which
# blocks read_csv / read_parquet / glob / getenv / duckdb_tables() / query() etc. by default.
ALLOWED_FUNCTION_CLASSES: frozenset[str] = frozenset(
    {
        # aggregates
        "Count", "CountIf", "Sum", "Avg", "Min", "Max", "Median", "Stddev", "StddevSamp",
        "StddevPop", "Variance", "VariancePop", "PercentileCont", "PercentileDisc",
        "ApproxDistinct", "AnyValue", "First", "Last", "GroupConcat",
        # window
        "RowNumber", "Rank", "DenseRank", "PercentRank", "CumeDist", "Ntile", "Lag", "Lead",
        "FirstValue", "LastValue",
        # math
        "Round", "Abs", "Floor", "Ceil", "Sqrt", "Pow", "Ln", "Log", "Exp", "Sign",
        "Greatest", "Least",
        # conditional / null handling / casts
        "Coalesce", "Nullif", "If", "Case", "Cast", "TryCast",
        # dates
        "DateTrunc", "TimestampTrunc", "Extract", "Year", "Month", "Day", "Quarter",
        "DayOfWeek", "DayOfYear", "WeekOfYear", "DateDiff", "DateAdd", "DateSub",
        "TimeToStr", "CurrentDate",
        # strings
        "Lower", "Upper", "Length", "Trim", "Concat", "Substring", "Left", "Right",
        # operators / predicates that sqlglot models as functions
        "And", "Or", "Xor", "Exists",
    }
)  # fmt: skip
# Functions sqlglot keeps as generic calls (exp.Anonymous), by lowercase name.
ALLOWED_ANONYMOUS_FUNCTIONS: frozenset[str] = frozenset({"date_part", "datepart"})


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class ValidationResult:
    ok: bool
    normalized_sql: str | None
    checks: list[Check] = field(default_factory=list)
    reason: str | None = None  # "<check>: <detail>" of the first failing check

    @property
    def failed_check(self) -> str | None:
        return next((c.name for c in self.checks if not c.passed), None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _Reject(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass
class _State:
    sql: str
    max_rows: int
    policy: Policy
    tokens: list[Token] = field(default_factory=list)
    statements: list[exp.Expression] = field(default_factory=list)
    tree: exp.Expression | None = None  # validated root (mutated by the limit check)
    qualified: exp.Expression | None = None  # qualified copy, used for column checks


# --- checks -----------------------------------------------------------------------------


def _check_parse(s: _State) -> str:
    if not isinstance(s.sql, str) or not s.sql.strip():
        raise _Reject("query is empty")
    try:
        s.tokens = sqlglot.Dialect.get_or_raise(config.DIALECT).tokenize(s.sql)
        s.statements = [st for st in sqlglot.parse(s.sql, read=config.DIALECT) if st is not None]
    except sqlglot.errors.SqlglotError as e:
        raise _Reject(f"could not parse SQL: {_first_line(e)}") from e
    if not s.statements:
        raise _Reject("no statement found")
    return f"{len(s.tokens)} tokens"


def _check_single_statement(s: _State) -> str:
    semis = [i for i, t in enumerate(s.tokens) if t.token_type == TokenType.SEMICOLON]
    if len(s.statements) != 1 or any(i != len(s.tokens) - 1 for i in semis):
        raise _Reject(f"exactly one statement is allowed (found {len(s.statements)})")
    return "1 statement"


def _check_no_comments(s: _State) -> str:
    if any(t.comments for t in s.tokens) or _tree_has_comments(s.statements[0]):
        raise _Reject("SQL comments are not allowed")
    return "none"


def _check_select_only(s: _State) -> str:
    root = s.statements[0]
    while isinstance(root, exp.Subquery):  # "(SELECT ...)" -> "SELECT ..."
        root = root.this
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        raise _Reject(f"only SELECT queries are allowed, got {type(root).__name__.upper()}")
    s.tree = root
    return type(root).__name__.upper()


def _check_no_forbidden_nodes(s: _State) -> str:
    for node in s.tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise _Reject(f"forbidden construct {type(node).__name__.upper()}")
    return "none"


def _check_function_allowlist(s: _State) -> str:
    used: set[str] = set()
    for node in s.tree.find_all(exp.Func):
        name, allowed = _function_status(node)
        if not allowed:
            raise _Reject(f"function {name}() is not on the allowlist")
        used.add(name)
    return ", ".join(sorted(used)) or "none"


def _check_table_allowlist(s: _State) -> str:
    cte_names = {cte.alias_or_name.lower() for cte in s.tree.find_all(exp.CTE)}
    used: set[str] = set()
    for table in s.tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise _Reject("table functions are not allowed in FROM / JOIN")
        if table.args.get("db") or table.args.get("catalog"):
            raise _Reject(f"schema-qualified tables are not allowed: {table.sql(config.DIALECT)}")
        name = table.name.lower()
        if name not in cte_names and name not in s.policy.tables:
            raise _Reject(f"table {table.name!r} is not allowed")
        used.add(name)
    return ", ".join(sorted(used)) or "none"


def _check_column_allowlist(s: _State) -> str:
    for table in s.tree.find_all(exp.Table):
        alias = table.args.get("alias")
        if isinstance(alias, exp.TableAlias) and alias.columns:
            # FROM t AS x(a, b, c) renames base columns and would hide restricted ones.
            raise _Reject(f"column aliases on table {table.name!r} are not allowed")
    schema = {t: dict.fromkeys(cols, "TEXT") for t, cols in s.policy.tables.items()}
    try:
        s.qualified = qualify(
            s.tree.copy(), schema=schema, dialect=config.DIALECT, validate_qualify_columns=True
        )
    except sqlglot.errors.SqlglotError as e:
        raise _Reject(f"unknown or ambiguous column: {_first_line(e)}") from e

    used: set[str] = set()
    for scope in traverse_scope(s.qualified):
        for col in scope.columns:
            if col.table:
                used.add(_check_qualified_column(s.policy, scope, col))
    _check_unqualified_columns(s.qualified)
    _check_no_leftover_star(s.qualified)
    return f"{len(used)} base columns, none restricted"


def _check_complexity(s: _State) -> str:
    joins = list(s.tree.find_all(exp.Join))
    if len(joins) > config.MAX_JOINS:
        raise _Reject(f"{len(joins)} joins (max {config.MAX_JOINS})")
    # Join type on the original tree: qualify() rewrites NATURAL JOIN into an explicit ON
    # (bypass found by the corpus: natural_join). Join conditions on the qualified tree,
    # where every column carries its table.
    for join in joins:
        _check_join_type(join)
    for join in s.qualified.find_all(exp.Join):
        _check_join_condition(join)
    depth = max((_select_depth(sel) for sel in s.tree.find_all(exp.Select)), default=0)
    if depth > config.MAX_SUBQUERY_DEPTH:
        raise _Reject(f"subquery depth {depth} (max {config.MAX_SUBQUERY_DEPTH})")
    if any(w.args.get("recursive") for w in s.tree.find_all(exp.With)):
        raise _Reject("recursive CTEs are not allowed")
    return f"{len(joins)} joins, subquery depth {depth}"


def _check_limit(s: _State) -> str:
    limit = s.tree.args.get("limit")
    current = _literal_limit(limit)
    if current is not None and current <= s.max_rows:
        return f"kept LIMIT {current}"
    s.tree.set("limit", None)  # also drops FETCH FIRST / LIMIT n% / non-literal LIMIT
    s.tree = s.tree.limit(s.max_rows, copy=False)
    if limit is None:
        return f"added LIMIT {s.max_rows}"
    shown = current if current is not None else limit.sql(config.DIALECT)
    return f"capped LIMIT {shown} to {s.max_rows}"


CHECKS: tuple[tuple[str, Callable[[_State], str]], ...] = (
    ("parse", _check_parse),
    ("single_statement", _check_single_statement),
    ("no_comments", _check_no_comments),
    ("select_only", _check_select_only),
    ("no_forbidden_nodes", _check_no_forbidden_nodes),
    ("function_allowlist", _check_function_allowlist),
    ("table_allowlist", _check_table_allowlist),
    ("column_allowlist", _check_column_allowlist),
    ("complexity", _check_complexity),
    ("limit", _check_limit),
)


def validate(
    sql: str, policy: Policy | None = None, max_rows: int = config.MAX_ROWS
) -> ValidationResult:
    """Validate untrusted SQL. On success ``normalized_sql`` is the only SQL to execute."""
    state = _State(sql=sql, max_rows=max_rows, policy=policy or config.load_policy())
    checks: list[Check] = []
    for name, fn in CHECKS:
        try:
            detail = fn(state)
        except _Reject as r:
            return _rejected(checks, name, r.detail)
        except Exception as e:  # fail closed on anything unexpected (e.g. sqlglot internals)
            return _rejected(checks, name, f"validator error: {type(e).__name__}: {e}")
        checks.append(Check(name, True, detail))
    return ValidationResult(True, state.tree.sql(dialect=config.DIALECT), checks, None)


# --- helpers ----------------------------------------------------------------------------


def _rejected(checks: list[Check], name: str, detail: str) -> ValidationResult:
    checks.append(Check(name, False, detail))
    return ValidationResult(False, None, checks, f"{name}: {detail}")


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__


def _tree_has_comments(tree: exp.Expression) -> bool:
    return any(node.comments for node in tree.walk())


def _function_status(node: exp.Func) -> tuple[str, bool]:
    if isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc)):
        name = str(node.name).lower()
        return name, name in ALLOWED_ANONYMOUS_FUNCTIONS
    return node.sql_name().lower(), type(node).__name__ in ALLOWED_FUNCTION_CLASSES


def _resolve_source(scope: Scope, name: str) -> Any:
    """Find the source (exp.Table or child Scope) a column qualifier refers to."""
    current: Scope | None = scope
    while current is not None:
        if name in current.sources:
            return current.sources[name]
        current = current.parent
    return None


def _check_qualified_column(policy: Policy, scope: Scope, col: exp.Column) -> str:
    source = _resolve_source(scope, col.table)
    if source is None:
        raise _Reject(f"column {col.sql(config.DIALECT)} does not resolve to a source")
    if not isinstance(source, exp.Table):
        return f"<derived>.{col.name}"  # a CTE / subquery output, validated in its own scope
    table, column = source.name.lower(), col.name.lower()
    if column not in policy.tables.get(table, ()):
        raise _Reject(f"column {table}.{column} is not allowed")
    if policy.is_restricted(table, column):
        raise _Reject(f"restricted column {table}.{column}")
    return f"{table}.{column}"


def _check_unqualified_columns(tree: exp.Expression) -> None:
    """After qualification, a bare column may only name an output alias (ORDER BY n).

    A bare column that names a table or alias is DuckDB's whole-row struct
    (``SELECT c FROM customers c``), which would leak every column of the row.
    """
    # qualify() rewrites such a reference to exp.TableColumn, invisible to exp.Column checks
    # (bypass found by the corpus: whole_row_struct).
    for node in tree.find_all(exp.TableColumn):
        raise _Reject(f"table {node.name!r} used as a column (whole-row access)")
    sources = {t.alias_or_name.lower() for t in tree.find_all(exp.Table)}
    sources |= {q.alias_or_name.lower() for q in tree.find_all(exp.Subquery) if q.alias_or_name}
    sources |= {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias)}
    for col in tree.find_all(exp.Column):
        if col.table:
            continue
        name = col.name.lower()
        if name in sources:
            raise _Reject(f"table {name!r} used as a column (whole-row access)")
        if name not in aliases:
            raise _Reject(f"column {name!r} could not be resolved")


def _check_no_leftover_star(tree: exp.Expression) -> None:
    for star in tree.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            raise _Reject("could not expand * to known columns")


def _check_join_type(join: exp.Join) -> None:
    kind = (join.args.get("kind") or "").upper()
    method = (join.args.get("method") or "").upper()
    if kind == "CROSS" or method in {"NATURAL", "POSITIONAL", "ASOF"}:
        raise _Reject(f"{kind or method} JOIN is not allowed")


def _check_join_condition(join: exp.Join) -> None:
    if join.args.get("using"):
        return
    on = join.args.get("on")
    if on is None:
        raise _Reject("JOIN without an ON condition (cartesian product)")
    tables = {c.table for c in on.find_all(exp.Column) if c.table}
    if len(tables) < 2:
        raise _Reject("JOIN condition must compare columns of both sides")


def _select_depth(select: exp.Select) -> int:
    depth, node = 0, select.parent
    while node is not None:
        if isinstance(node, exp.Select):
            depth += 1
        node = node.parent
    return depth


def _literal_limit(limit: Any) -> int | None:
    if not isinstance(limit, exp.Limit):
        return None
    options = limit.args.get("limit_options")
    if options is not None and options.args.get("percent"):
        return None
    value = limit.expression
    if isinstance(value, exp.Literal) and value.is_int:
        return int(value.this)
    return None
