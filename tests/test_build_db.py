"""olist_db.build: CSVs -> DuckDB, on a temporary folder (never touches data/olist.duckdb)."""

import pytest

from olist_db import build
from text2sql import executor


def test_build_loads_one_table_per_csv(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "olist_a.csv").write_text("id,name\n1,x\n2,y\n", encoding="utf-8")
    (raw / "olist_b.csv").write_text("id\n7\n", encoding="utf-8")
    db = tmp_path / "test.duckdb"

    assert build.build(raw, db) == {"olist_a": 2, "olist_b": 1}
    res = executor.execute("SELECT name FROM olist_a ORDER BY id", db_path=db)
    assert res.rows == [("x",), ("y",)]

    (raw / "olist_b.csv").write_text("id\n7\n8\n", encoding="utf-8")
    assert build.build(raw, db)["olist_b"] == 2  # rebuild replaces tables


@pytest.mark.parametrize(
    "header",
    [
        "product_id,product_name_lenght,product_description_lenght",  # as on Kaggle
        "product_id,product_name_length,product_description_length",  # already fixed
    ],
)
def test_build_fixes_misspelled_product_columns(tmp_path, header):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "olist_products_dataset.csv").write_text(f"{header}\np1,10,200\n", encoding="utf-8")
    db = tmp_path / "test.duckdb"

    build.build(raw, db)
    res = executor.execute(
        "SELECT product_name_length, product_description_length FROM olist_products_dataset",
        db_path=db,
    )
    assert res.columns == ["product_name_length", "product_description_length"]
    assert res.rows == [(10, 200)]


def test_build_without_csvs_exits(tmp_path):
    with pytest.raises(SystemExit, match="No CSV files"):
        build.build(tmp_path, tmp_path / "test.duckdb")
