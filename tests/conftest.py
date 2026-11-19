"""Shared fixtures. Tests that need the real Olist database are skipped when it is missing."""

import pytest

from text2sql import config

needs_db = pytest.mark.skipif(
    not config.DB_PATH.is_file(),
    reason="data/olist.duckdb not built (python -m olist_db.build)",
)
