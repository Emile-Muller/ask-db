"""Read-only access to the Olist DuckDB database."""

import duckdb

from agentic_query import DB_PATH

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
        raise FileNotFoundError(
            f"{DB_PATH} not found. Run `python -m agentic_query.build_db` first."
        )
    return duckdb.connect(str(DB_PATH), read_only=True, config=dict(READ_ONLY_CONFIG))
