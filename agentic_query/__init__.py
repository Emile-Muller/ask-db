"""Agentic Query: natural language -> SQL (Claude) -> guardrail -> DuckDB."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"  # Kaggle CSVs (not tracked, see data/README.md)
DB_PATH = DATA_DIR / "olist.duckdb"  # built from RAW_DIR by build_db (not tracked)
SCHEMA_PATH = DATA_DIR / "schema.yaml"  # table/column descriptions for the prompt
