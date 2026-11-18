# Agentic Query

Ask questions in plain English, get SQL and results.

An AI model turns natural language into a SQL query, runs it against a DuckDB database, and shows the query and its results in a Streamlit app.

## Stack

- **Frontend:** Streamlit
- **Database:** DuckDB
- **AI:** LLM for text-to-SQL
- **Data:** [Brazilian E-Commerce Public Dataset by Olist](data/data_source.md) (Kaggle, ~100k orders, 2016–2018)

## Example

> "What are the top 5 product categories by revenue?"

The app generates the SQL, runs it, and displays the table.

## Data

CSV files live in `data/`. See [data/data_source.md](data/data_source.md) for source, license and table descriptions.

> **Note:** the original `olist_products_dataset.csv` misspells two columns (`product_name_lenght`, `product_description_lenght`). The header has been fixed locally to `product_name_length` and `product_description_length`. If you re-download the dataset, fix the header again before building the database.

## Setup

Requires Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Database

`build_db.py` loads every CSV in `data/` into `data/olist.duckdb`, one table per file, named after the file (e.g. `olist_products_dataset.csv` → table `olist_products_dataset`). Re-run it whenever the CSVs change.

```powershell
.\.venv\Scripts\python.exe build_db.py
```

`db.py` provides read-only access to the database:

- `get_connection()`: opens `data/olist.duckdb` with `read_only=True`, external access disabled (no file reads, `ATTACH`, extension install/load) and the configuration locked so SQL cannot re-enable it. All queries go through this connection; only `build_db.py` writes to the database.
- `first_products(n=5)`: returns the first `n` rows of `olist_products_dataset` as a list of dicts.

```powershell
.\.venv\Scripts\python.exe db.py   # prints the first 5 products
```

## SQL guardrail

`sql_guard.py` is the only way user- or LLM-generated SQL may reach the database. Call `run_query(sql, max_rows=1000)`. It returns a `QueryResult` (`sql`, `columns`, `rows`, `truncated`, `as_dicts()`) or raises `UnsafeQueryError` with a message you can show the user.

Checks, in order:

1. **Parsed with sqlglot** (DuckDB dialect). SQL that does not parse is rejected.
2. **One statement only.** Chained statements (`SELECT 1; DROP ...`) are rejected.
3. **SELECT only.** `WITH`, `UNION`, `INTERSECT` and `EXCEPT` are allowed. `INSERT`, `UPDATE`, `DELETE`, DDL, `PRAGMA`, `ATTACH`, `COPY`, `SET`, `INSTALL`, `LOAD`, `CALL`, `SELECT ... INTO`, `DESCRIBE` and similar are rejected anywhere in the query.
4. **Table allowlist.** Queries may only reference CTEs and tables in the database's `main` schema. Table functions (`read_csv`, `glob`, `query`, ...), file paths (`FROM 'x.csv'`) and other schemas are rejected.
5. **Row limit.** A missing `LIMIT`, a non-literal one, or one over `max_rows` is replaced, and at most `max_rows` rows are returned. `truncated` tells you whether there were more.
6. **Read-only execution.** The SQL that runs is the one sqlglot regenerates from the validated AST, not the raw input, and it runs on the locked-down connection above.

```powershell
.\.venv\Scripts\python.exe -m pytest -q   # guardrail tests
```

## Releasing

In Claude Code, run `/release major|minor|patch` (`.claude/skills/release/SKILL.md`). It runs the tests and the `CLAUDE.md` safety checks, bumps `VERSION` and this version history, stages all changes, makes a single release commit, and creates an annotated `vX.Y.Z` tag on it. It never pushes; publish with `git push origin main --follow-tags`.

## Version history

### 0.3.0 — 2026-11-18
- Add SQL guardrail `sql_guard.py`: `run_query()` parses SQL with sqlglot and allows only one SELECT statement on allowlisted `main` tables
- Guardrail rejects writes, DDL, PRAGMA/ATTACH/COPY/SET/INSTALL/LOAD, file-reading table functions and file paths, and caps rows with LIMIT (`max_rows`, default 1000)
- `db.get_connection()` now turns off external access and extension autoload/autoinstall, and locks the configuration
- Add pytest suite `tests/test_sql_guard.py` for the guardrail and the locked-down connection
- Add `sqlglot` and `pytest` to `requirements.txt`; document the guardrail in the README

### 0.2.1 — 2026-11-18
- `/release` now makes one commit (pending work, `VERSION` and history together) and then tags it
- version-manager now only edits files: it checks the new version and writes the history from commits and uncommitted changes, and no longer stages, commits or tags
- Tag creation and its checks moved from version-manager into the `/release` skill
- README "Releasing" section updated to match the single-commit flow

### 0.2.0 — 2026-11-18
- Add `/release` skill (`.claude/skills/release/SKILL.md`) for the release workflow
- version-manager now validates and creates annotated `vX.Y.Z` tags (SemVer, VERSION match, no duplicate, version increase, clean tree)

### 0.1.0 — 2026-11-18
- Add `build_db.py` to load each Olist CSV in `data/` into `data/olist.duckdb` (one table per file)
- Add `db.py` with read-only `get_connection()` and `first_products(n)` helper
- Add dataset source and table docs (`data/data_source.md`)
- Add README, CLAUDE.md safety rules (no secrets, read-only DB, SELECT/WITH only), requirements and `.gitignore`
- Add version-manager agent (`.claude/agents/version-manager.md`)
