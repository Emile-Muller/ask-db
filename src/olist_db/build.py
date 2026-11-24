"""Build data/olist.duckdb from the Olist CSV files in data/raw/.

One table per CSV, named after the file without the .csv extension
(e.g. data/raw/olist_products_dataset.csv -> table olist_products_dataset).

    python -m olist_db.build

This is the only place that opens the database for writing. It runs fixed,
trusted SQL only - never user- or LLM-generated SQL.
"""

from pathlib import Path

import duckdb

from text2sql.config import DATA_DIR, DB_PATH

RAW_DIR = DATA_DIR / "raw"  # Kaggle CSVs (not tracked, see data/README.md)

# Column names misspelled in the Kaggle CSVs, renamed at load time: {table: {raw: fixed}}.
# The CSVs stay as downloaded; a header that is already fixed is left alone.
COLUMN_RENAMES: dict[str, dict[str, str]] = {
    "olist_products_dataset": {
        "product_name_lenght": "product_name_length",
        "product_description_lenght": "product_description_length",
    },
}


def _fix_column_names(con: duckdb.DuckDBPyConnection, table: str) -> None:
    """Rename the known misspelled columns of ``table`` that are present."""
    renames = COLUMN_RENAMES.get(table, {})
    columns = {row[0] for row in con.execute(f'DESCRIBE "{table}"').fetchall()}
    for old, new in renames.items():
        if old in columns:
            con.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')
            print(f"{table}: renamed column {old} -> {new}")


def build(raw_dir: Path = RAW_DIR, db_path: Path = DB_PATH) -> dict[str, int]:
    """Load every CSV in ``raw_dir`` into ``db_path``. Returns {table: row count}."""
    csv_files = sorted(Path(raw_dir).glob("*.csv"))
    if not csv_files:
        raise SystemExit(f"No CSV files in {raw_dir}. See data/README.md.")

    counts: dict[str, int] = {}
    with duckdb.connect(str(db_path)) as con:
        for csv in csv_files:
            table = csv.stem
            con.execute(
                f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM read_csv_auto(?)',
                [str(csv)],
            )
            _fix_column_names(con, table)
            counts[table] = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            print(f"{table}: {counts[table]:,} rows")

    print(f"Database written to {db_path}")
    return counts


if __name__ == "__main__":
    build()
