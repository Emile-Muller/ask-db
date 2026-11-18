"""Schema introspection + human descriptions -> the schema context given to the LLM.

1. ``introspect(con)`` reads table names, column names and types from DuckDB
   (fixed, trusted SQL on the read-only connection).
2. ``load_descriptions()`` reads ``data/schema.yaml``: table/column descriptions,
   primary/foreign keys (the CSV-built database declares none) and business rules.
3. ``render_schema()`` merges both into the compact text block used in the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from agentic_query import DATA_DIR
from agentic_query.db import get_connection

DESCRIPTIONS_PATH = DATA_DIR / "schema.yaml"


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    description: str = ""


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    description: str = ""
    primary_key: tuple[str, ...] = ()
    foreign_keys: dict[str, str] = field(default_factory=dict)  # column -> "table.column"


def introspect(con) -> dict[str, list[tuple[str, str]]]:
    """Return {table: [(column, type), ...]} for the main schema, in column order."""
    rows = con.execute(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'main'
        ORDER BY table_name, ordinal_position
        """
    ).fetchall()
    tables: dict[str, list[tuple[str, str]]] = {}
    for table, column, dtype in rows:
        tables.setdefault(table, []).append((column, dtype))
    return tables


def load_descriptions(path: Path = DESCRIPTIONS_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_tables(live: dict[str, list[tuple[str, str]]], docs: dict) -> list[Table]:
    """Merge the live schema with the descriptions. Live schema is the source of truth
    for which tables/columns exist; undocumented ones are still included."""
    doc_tables = docs.get("tables") or {}
    tables = []
    for name, cols in live.items():
        d = doc_tables.get(name) or {}
        col_docs = d.get("columns") or {}
        tables.append(
            Table(
                name=name,
                columns=tuple(Column(c, t, str(col_docs.get(c, ""))) for c, t in cols),
                description=str(d.get("description", "")).strip(),
                primary_key=tuple(d.get("primary_key") or ()),
                foreign_keys=dict(d.get("foreign_keys") or {}),
            )
        )
    return tables


def schema_mismatches(live: dict[str, list[tuple[str, str]]], docs: dict) -> list[str]:
    """Differences between schema.yaml and the database (empty list = in sync)."""
    problems = []
    doc_tables = docs.get("tables") or {}
    for t in sorted(set(live) - set(doc_tables)):
        problems.append(f"table {t} is not described in schema.yaml")
    for t in sorted(set(doc_tables) - set(live)):
        problems.append(f"schema.yaml describes unknown table {t}")
    for t in sorted(set(live) & set(doc_tables)):
        live_cols = {c for c, _ in live[t]}
        d = doc_tables[t] or {}
        doc_cols = set(d.get("columns") or {})
        problems += [f"{t}.{c} is not described" for c in sorted(live_cols - doc_cols)]
        problems += [f"{t}.{c} described but missing" for c in sorted(doc_cols - live_cols)]
        keyed = set(d.get("primary_key") or ()) | set(d.get("foreign_keys") or {})
        problems += [f"{t}: key column {c} missing" for c in sorted(keyed - live_cols)]
        for col, ref in (d.get("foreign_keys") or {}).items():
            ref_table, _, ref_col = str(ref).partition(".")
            if ref_col not in {c for c, _ in live.get(ref_table, [])}:
                problems.append(f"{t}.{col} references unknown {ref}")
    return problems


def render_schema(tables: list[Table], docs: dict) -> str:
    """Compact, LLM-friendly description of the tables, keys and rules."""
    lines = []
    if docs.get("dataset"):
        lines += [f"Dataset: {' '.join(str(docs['dataset']).split())}", ""]
    for t in tables:
        lines.append(f"TABLE {t.name}" + (f" -- {t.description}" if t.description else ""))
        if t.primary_key:
            lines.append(f"  primary key: ({', '.join(t.primary_key)})")
        for c in t.columns:
            ref = t.foreign_keys.get(c.name)
            parts = [f"  {c.name} {c.type}"]
            if ref:
                parts.append(f"-> {ref}")
            if c.description:
                parts.append(f"-- {c.description}")
            lines.append(" ".join(parts))
        lines.append("")
    rules = docs.get("rules") or []
    if rules:
        lines.append("Rules:")
        lines += [f"- {r}" for r in rules]
    return "\n".join(lines).rstrip() + "\n"


@lru_cache(maxsize=1)
def schema_context() -> str:
    """Schema text for the prompt, built once per process from the live database."""
    with get_connection() as con:
        live = introspect(con)
    docs = load_descriptions()
    return render_schema(build_tables(live, docs), docs)


if __name__ == "__main__":
    print(schema_context())
