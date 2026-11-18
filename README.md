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

## Project layout

```
agentic_query/   Python package (run modules with `python -m agentic_query.<module>`)
  build_db.py    CSVs -> data/olist.duckdb (the only writer)
  db.py          read-only, locked-down DuckDB connection
  sql_guard.py   SQL guardrail: the only path for untrusted SQL
  schema.py      schema introspection + descriptions -> prompt schema block
  text_to_sql.py question -> Claude -> SQL -> guardrail -> results
  eval.py        end-to-end eval against gold SQL
data/            dataset CSVs, olist.duckdb, data_source.md, schema.yaml (table/column descriptions)
eval/            questions.yaml (eval set)
tests/           pytest suite
```

Run everything from the project root so `agentic_query` is importable.

## Setup

Requires Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Database

`agentic_query/build_db.py` loads every CSV in `data/` into `data/olist.duckdb`, one table per file, named after the file (e.g. `olist_products_dataset.csv` → table `olist_products_dataset`). Re-run it whenever the CSVs change.

```powershell
.\.venv\Scripts\python.exe -m agentic_query.build_db
```

`agentic_query/db.py` provides read-only access to the database:

- `get_connection()`: opens `data/olist.duckdb` with `read_only=True`, external access disabled (no file reads, `ATTACH`, extension install/load) and the configuration locked so SQL cannot re-enable it. All queries go through this connection; only `build_db` writes to the database.
- `first_products(n=5)`: returns the first `n` rows of `olist_products_dataset` as a list of dicts.

```powershell
.\.venv\Scripts\python.exe -m agentic_query.db   # prints the first 5 products
```

## SQL guardrail

`agentic_query/sql_guard.py` is the only way user- or LLM-generated SQL may reach the database. Call `run_query(sql, max_rows=1000)`. It returns a `QueryResult` (`sql`, `columns`, `rows`, `truncated`, `as_dicts()`) or raises `UnsafeQueryError` with a message you can show the user.

Checks, in order:

1. **Parsed with sqlglot** (DuckDB dialect). SQL that does not parse is rejected.
2. **One statement only.** Chained statements (`SELECT 1; DROP ...`) are rejected.
3. **SELECT only.** `WITH`, `UNION`, `INTERSECT` and `EXCEPT` are allowed. `INSERT`, `UPDATE`, `DELETE`, DDL, `PRAGMA`, `ATTACH`, `COPY`, `SET`, `INSTALL`, `LOAD`, `CALL`, `SELECT ... INTO`, `DESCRIBE` and similar are rejected anywhere in the query.
4. **Table allowlist.** Queries may only reference CTEs and tables in the database's `main` schema. Table functions (`read_csv`, `glob`, `query`, ...), file paths (`FROM 'x.csv'`) and other schemas are rejected.
5. **Row limit.** A missing `LIMIT`, a non-literal one, or one over `max_rows` is replaced, and at most `max_rows` rows are returned. `truncated` tells you whether there were more.
6. **Read-only execution.** The SQL that runs is the one sqlglot regenerates from the validated AST, not the raw input, and it runs on the locked-down connection above.

```powershell
.\.venv\Scripts\python.exe -m pytest -q   # guardrail tests
.\.venv\Scripts\python.exe -m pre_commit install          # once per clone: ruff lint + format on every commit
.\.venv\Scripts\python.exe -m pre_commit run --all-files  # run the hooks manually (config in ruff.toml)
```

## Text-to-SQL pipeline

```
question ─► prompt (instructions + schema context + question) ─► Claude ─► SQL ─► guardrail ─► DuckDB (read-only)
```

1. **Schema introspection** (`agentic_query/schema.py`): `introspect()` reads table names, column names and types from DuckDB's `information_schema`.
2. **Descriptions** (`data/schema.yaml`): what each table and column contains, primary/foreign keys (the CSV-built database declares none), and business rules that remove guesswork (e.g. *revenue = `SUM(price)` from order items*, *customers = distinct `customer_unique_id`*, *use English category names*). `render_schema()` merges it with the live schema into the prompt's schema block. A test checks that `data/schema.yaml` and the database stay in sync.
3. **Prompt** (`agentic_query/text_to_sql.py`): the system prompt starts with *"You write SQL for DuckDB. Use only the tables below. Return one SELECT query only."*, followed by the schema block (prompt-cached, since it never changes). The question goes in the user message, wrapped in `<question>` tags and treated as data.
4. **Generation + guardrail**: `generate_sql(question)` calls Claude (`claude-opus-5-5`, effort `medium`) and returns the SQL. `ask(question)` then runs it through `sql_guard.run_query`, so LLM output is never executed unchecked. If the model declines, a server-side fallback model is tried in the same call.

The SDK reads the API key from the `ANTHROPIC_API_KEY` environment variable; it is never in code. Optional overrides: `AGENTIC_QUERY_MODEL`, `AGENTIC_QUERY_EFFORT`.

```powershell
$env:ANTHROPIC_API_KEY = "..."   # in your own terminal; never commit it
.\.venv\Scripts\python.exe -m agentic_query.schema                    # print the schema context sent to the model
.\.venv\Scripts\python.exe -m agentic_query.text_to_sql "Which product categories generate the most revenue?"
```

## Eval

`eval/questions.yaml` holds 30 showcase questions: 28 with a hand-written gold SQL answer, plus 2 that must be refused (a delete request and a prompt injection). `agentic_query/eval.py` sends each question through the whole chain and compares the result with the gold result: same row count, and each gold column matches a generated column (numbers within 0.01 / 0.1%, order checked only for rankings and time series, column names and extra columns ignored). Refusal questions pass if the guardrail rejects the SQL or the model returns its *"Cannot answer"* row.

```powershell
.\.venv\Scripts\python.exe -m agentic_query.eval --gold-only   # check the gold SQL only (no API calls)
.\.venv\Scripts\python.exe -m agentic_query.eval               # full run (~30 API calls); report in eval/last_run.json
.\.venv\Scripts\python.exe -m agentic_query.eval --only top_categories_revenue avg_review_score
```

In Claude Code, `/eval` runs it and explains each failure (`/eval gold` for the free gold check, `/eval <id> ...` for some questions).

## Releasing

In Claude Code, run `/release major|minor|patch` (`.claude/skills/release/SKILL.md`). It runs the tests and the `CLAUDE.md` safety checks, bumps `VERSION` and this version history, stages all changes, makes a single release commit, and creates an annotated `vX.Y.Z` tag on it. It never pushes; publish with `git push origin main --follow-tags`.

## Version history

### 0.5.0 — 2026-11-18
- Add text-to-SQL pipeline `agentic_query/text_to_sql.py`: `generate_sql()` asks Claude for one SELECT, `ask()` runs it through the guardrail
- Add schema context: `agentic_query/schema.py` introspects DuckDB and merges `data/schema.yaml` (table/column descriptions, keys, business rules) into the prompt
- Add end-to-end eval `agentic_query/eval.py` with 30 questions in `eval/questions.yaml` (gold SQL + 2 refusals) and an `/eval` skill
- Move `build_db.py`, `db.py` and `sql_guard.py` into the `agentic_query` package (run with `python -m agentic_query.<module>`)
- Add offline tests `tests/test_text_to_sql.py` (fake Claude client); update imports in `tests/test_sql_guard.py`
- Add `anthropic` and `pyyaml` to requirements; ignore `eval/last_run.json`; update ruff config and `/release` skill paths
- README documents the project layout, text-to-SQL pipeline and eval

### 0.4.0 — 2026-11-18
- Add ruff linting and formatting (`ruff.toml`: py311, line length 100, rules incl. bugbear, bandit, simplify)
- Add pre-commit hooks (`.pre-commit-config.yaml`) running `ruff check --fix` and `ruff format`; add `ruff` and `pre-commit` to `requirements.txt`
- Reformat `sql_guard.py` and tests with ruff; use `zip(..., strict=True)` in `sql_guard.py` and `db.py`; simplify the LIMIT check
- Add read-only code-reviewer agent (`.claude/agents/code-reviewer.md`) for readability, performance, best practices and safety rules
- `/release` skill now handles the pre-commit hook (re-stage auto-fixes once, never `--no-verify`); README documents the hook setup

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
