"""Agentic Query: natural language -> SQL (Claude) -> guardrail -> DuckDB."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
