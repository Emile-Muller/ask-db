"""Build data/olist.duckdb from the Olist CSV files in data/.

One table per CSV, named after the file without the .csv extension
(e.g. data/olist_products_dataset.csv -> table olist_products_dataset).

This is the only place that opens the database for writing. It runs fixed,
trusted SQL only - never LLM-generated SQL.
"""

import duckdb

from agentic_query import DATA_DIR

DB_PATH = DATA_DIR / "olist.duckdb"


def build() -> None:
    csv_files = sorted(DATA_DIR.glob("*.csv"))
    if not csv_files:
        raise SystemExit(f"No CSV files in {DATA_DIR}. See data/data_source.md.")

    with duckdb.connect(str(DB_PATH)) as con:
        for csv in csv_files:
            table = csv.stem
            con.execute(
                f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM read_csv_auto(?)',
                [str(csv)],
            )
            rows = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            print(f"{table}: {rows:,} rows")

    print(f"Database written to {DB_PATH}")


if __name__ == "__main__":
    build()
