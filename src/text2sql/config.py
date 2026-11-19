"""Paths, limits and the data-access policy (allowed tables/columns, restricted columns).

The policy is read from ``data/schema.yaml`` so that the validator (layer 3) needs no
database access. ``tests/test_validator.py`` checks that the yaml matches the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "olist.duckdb"
SCHEMA_PATH = DATA_DIR / "schema.yaml"
LOGS_DIR = PROJECT_ROOT / "questions" / "logs"
RUNS_DIR = LOGS_DIR / "questions"  # /question traces
REVIEWS_DIR = LOGS_DIR / "question-reviews"  # /question-review records (review_log.py)

DIALECT = "duckdb"

# Layer 1
MAX_INPUT_CHARS = 500
# Layer 3
MAX_SQL_CHARS = 5_000  # the SQL itself (proposed SQL does not go through layer 1's limit)
MAX_ROWS = 100
MAX_JOINS = 4
MAX_SUBQUERY_DEPTH = 3
# Layer 4
STATEMENT_TIMEOUT_S = 5.0
MEMORY_LIMIT = "512MB"
THREADS = 2
# Layer 5
PREVIEW_ROWS = 20
MAX_CELL_CHARS = 1_000  # longer strings are cut (string_agg can pack a table in one cell)
# Review log (/question-review)
REVIEW_MAX_INPUT_CHARS = 64_000
REVIEW_MAX_TEXT_CHARS = 2_000
REVIEW_MAX_SQL_CHARS = 5_000
REVIEW_MAX_MODEL_CHARS = 100
REVIEW_MAX_FINDINGS = 20


@dataclass(frozen=True)
class Policy:
    """Which tables and columns queries may touch."""

    tables: dict[str, tuple[str, ...]]  # table -> all its columns (lowercase, doc order)
    restricted: frozenset[tuple[str, str]]  # (table, column) pairs that must never be read

    @property
    def restricted_names(self) -> frozenset[str]:
        """Bare restricted column names, used by layer 5 on result column names."""
        return frozenset(col for _, col in self.restricted)

    def is_restricted(self, table: str, column: str) -> bool:
        return (table.lower(), column.lower()) in self.restricted


def column_doc(value: Any) -> tuple[str, bool]:
    """A schema.yaml column entry is a description string or {description, sensitive}."""
    if isinstance(value, dict):
        return str(value.get("description", "")), bool(value.get("sensitive", False))
    return ("" if value is None else str(value)), False


def load_schema_docs(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def policy_from_docs(docs: dict[str, Any]) -> Policy:
    tables: dict[str, tuple[str, ...]] = {}
    restricted: set[tuple[str, str]] = set()
    for table, spec in (docs.get("tables") or {}).items():
        columns = (spec or {}).get("columns") or {}
        tables[table.lower()] = tuple(c.lower() for c in columns)
        for col, value in columns.items():
            if column_doc(value)[1]:
                restricted.add((table.lower(), col.lower()))
    return Policy(tables=tables, restricted=frozenset(restricted))


@lru_cache(maxsize=4)
def load_policy(path: Path = SCHEMA_PATH) -> Policy:
    return policy_from_docs(load_schema_docs(path))
