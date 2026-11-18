"""Read-only access to the Olist DuckDB database."""

from pathlib import Path

import duckdb

DB_PATH = Path(__file__).parent / "data" / "olist.duckdb"


# Read-only alone still lets SQL read arbitrary local files (read_csv, FROM 'x.csv'),
# ATTACH other databases or INSTALL/LOAD extensions. Disable external access and
# lock the configuration so SQL cannot SET it back.
READ_ONLY_CONFIG = {
    "enable_external_access": False,
    "autoload_known_extensions": False,
    "autoinstall_known_extensions": False,
    "lock_configuration": True,
}


def get_connection() -> duckdb.DuckDBPyConnection:
    """Open the database in read-only mode with external access disabled."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"{DB_PATH} not found. Run `python build_db.py` first.")
    return duckdb.connect(str(DB_PATH), read_only=True, config=dict(READ_ONLY_CONFIG))


def first_products(n: int = 5):
    """Return the first n rows of olist_products_dataset as a list of dicts."""
    with get_connection() as con:
        cur = con.execute("SELECT * FROM olist_products_dataset LIMIT ?", [n])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


if __name__ == "__main__":
    for row in first_products():
        print(row)
