# Agentic Query

Ask questions in plain English, get SQL and results.

An AI model turns natural language into a SQL query, runs it against a DuckDB database, and shows the query and its results in a Streamlit app.

## Stack

- **Frontend:** Streamlit
- **Database:** DuckDB
- **AI:** LLM for text-to-SQL
- **Data:** [Brazilian E-Commerce Public Dataset by Olist](data/README.md) (Kaggle, ~100k orders, 2016–2018)

## Example

> "What are the top 5 product categories by revenue?"

The app generates the SQL, runs it, and displays the table.

## Data

CSV files live in `data/raw/` (not tracked; download them from Kaggle). See [data/README.md](data/README.md) for source, license and table descriptions.

> **Note:** the original `olist_products_dataset.csv` misspells two columns (`product_name_lenght`, `product_description_lenght`). The header has been fixed locally to `product_name_length` and `product_description_length`. If you re-download the dataset, fix the header again before building the database.

## Project layout

```
src/
  text2sql/      5-layer safe text-to-SQL pipeline (`python -m text2sql`, used by /question)
  olist_db/
    build.py     data/raw/*.csv -> data/olist.duckdb (the only writer)
data/
  README.md      dataset source, license and file descriptions
  schema.yaml    table/column descriptions, keys, rules and sensitive columns (tracked)
  olist.duckdb   built database (generated, not tracked)
  raw/           Kaggle CSVs (not tracked)
questions/       questions.yaml (test inputs), results.json (sample /question run), logs/questions/ (traces), logs/question-reviews/ (reviews)
tests/           offline tests: one file per layer, CLI, DB build; redteam_corpus.yaml
```

## Setup

Requires Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .              # makes text2sql and olist_db importable
.\.venv\Scripts\python.exe -m pre_commit install          # once per clone: ruff lint + format on every commit
```

## Database

`src/olist_db/build.py` loads every CSV in `data/raw/` into `data/olist.duckdb`, one table per file, named after the file (e.g. `olist_products_dataset.csv` → table `olist_products_dataset`). Re-run it whenever the CSVs change. It is the only code that opens the database for writing, and it runs fixed SQL only; everything else goes through the read-only, locked-down connection in `src/text2sql/executor.py`.

```powershell
.\.venv\Scripts\python.exe -m olist_db.build
```

## Test questions

`questions/questions.yaml` holds 30 test inputs in eight categories: 12 valid questions (simple, join, aggregation, date filter), 3 destructive requests, 4 prompt injections, 4 stacked or obfuscated raw SQL queries, 3 exfiltration attempts (system tables, file reads), 2 sensitive-data requests, 1 resource-abuse query (a cross join of the two largest tables) and 1 out-of-scope question. Each has `expect: answer` or `expect: refuse`; any prompt injection or chained harmful statement must be refused as a whole, even when a legitimate read-only request is mixed in.

In Claude Code, `/question <question>` runs one input through the 5-layer `text2sql` pipeline (see below); any input from `questions/questions.yaml` can be pasted as is. `questions/results.json` is a showcase from an earlier version of the skill: one run over all 30 inputs, with the approved SQL and a result preview for each (16 answered, 14 refused). Checking results against `expect` is not automated.

`/question-review [trace id]` then has an independent agent judge one run (default: the latest) from its trace and `data/schema.yaml` only, without database access. Its judgement goes through `python -m text2sql review`, which fills in the trace fields, id and timestamp and appends the record to `questions/logs/question-reviews/<YYYYMMDD>.jsonl` (format in `src/text2sql/review_log.py`). A `PreToolUse` hook (`src/text2sql/review_guard.py`) denies the agent every other shell command.

## Safe text-to-SQL pipeline (`src/text2sql`)

The model only *proposes* SQL; deterministic code decides whether it runs. Every run prints a JSON trace (format documented in `src/text2sql/trace.py`) and appends it to `questions/logs/questions/<YYYYMMDD>.jsonl`.

| Layer | Module | What it does |
|---|---|---|
| 1 input screen | `input_screen.py` | NFKC + strip zero-width/control chars, ≤ 500 chars, raw SQL vs question, prompt-injection heuristics (defense in depth) |
| 2 generation | `prompt.py` | schema context from DuckDB + `data/schema.yaml`, restricted columns hidden; no LLM call from Python |
| 3 validator | `validator.py` | sqlglot: one statement, no comments, SELECT only, table/column/**function allowlists**, restricted columns (incl. `*`, aliases, subqueries), ≤ 4 joins, depth ≤ 3, no cartesian or recursive queries, LIMIT forced ≤ 100 |
| 4 executor | `executor.py` | read-only DuckDB, external access off, config locked, memory/threads capped, 5 s timeout |
| 5 output check | `output_check.py` | drop restricted columns, mask emails/phones, 20-row preview |

Layers 3 and 4 are the security boundary. `tests/redteam_corpus.yaml` holds 120+ attack and benign queries.

```powershell
.\.venv\Scripts\python.exe -m text2sql run --sql "SELECT 1; DROP TABLE customers"   # refused by validator, exit 2
.\.venv\Scripts\python.exe -m text2sql context --question "Top 5 customers by revenue"
.\.venv\Scripts\python.exe -m pytest --cov                 # all offline tests, with coverage
.\.venv\Scripts\python.exe -m pre_commit run --all-files   # ruff lint + format (config in ruff.toml)
```

## Releasing

In Claude Code, run `/release major|minor|patch` (`.claude/skills/release/SKILL.md`). It runs the tests and the `CLAUDE.md` safety checks, bumps `VERSION` and this version history, stages all changes, makes a single release commit, and creates an annotated `vX.Y.Z` tag on it. It never pushes; publish with `git push origin main --follow-tags`.

## Version history

### 1.0.0 — 2026-11-19
- **Breaking:** `agentic_query/` package removed, replaced by `src/text2sql/` (`python -m text2sql`) and `src/olist_db/build.py` (`python -m olist_db.build`); new `pyproject.toml` (version read from `VERSION`)
- 5-layer safe pipeline: `input_screen`, `prompt`, `validator` (sqlglot allowlist, forced LIMIT), `executor` (read-only DuckDB, timeout, row cap), `output_check` (drops restricted columns, masks emails/phones)
- CLI commands `context`, `run`, `check` and `review`; JSONL traces (`trace.py`) logged to `questions/logs/`
- `data/schema.yaml` marks zip prefixes and review free text as `sensitive: true`
- **Changed:** `eval/` moved to `questions/`; `/question` skill drives the CLI (`guard.py` removed); Python no longer calls the LLM (`anthropic` dependency dropped)
- Add `question-reviewer` agent, `/question-review` skill, `review_log.py` and `review_guard.py` `PreToolUse` hook (reviewer may only run `text2sql review`)
- `.claude/settings.json` allows the pipeline and tests, denies DB CLIs and direct reads of the database and raw data
- New offline test suite per layer plus `tests/redteam_corpus.yaml`, with coverage (`pytest-cov`)

### 0.6.3 — 2026-11-19
- Renamed the `/eval` skill to `/question` (`.claude/skills/question/`); README, tests and `eval/questions.yaml` updated
- Raw Kaggle CSVs moved to `data/raw/` (gitignored); `build_db.py` reads from there
- `RAW_DIR`, `DB_PATH` and `SCHEMA_PATH` now live in `agentic_query/__init__.py`, shared by `build_db`, `db` and `schema`
- `data/data_source.md` renamed to `data/README.md`, with download/build steps; links and README layout updated

### 0.6.2 — 2026-11-19
- `.claude/skills/eval/guard.py` split into `check()` + `write_report()`; each run (timestamp, submitted SQL, result or error) is saved to `eval/last_skill_run.json`, overwritten every run
- A failed report write only prints a warning on stderr and does not change the guard result
- Ignore `eval/last_skill_run.json`; README `/eval` section documents the new report file

### 0.6.1 — 2026-11-19
- Refuse the whole input on any prompt injection, non-SELECT or `;`-chained statement, or file/secret request, even when a legitimate question is mixed in (system prompt in `text_to_sql.py` and `/eval` skill)
- Eval set: the fake-`</question>` admin-injection case now expects `refuse`; kind docs updated to match
- Add `eval/results.json`: sample `/eval` run over all 30 inputs (16 answered, 14 refused), documented in the README

### 0.6.0 — 2026-11-19
- Rework the eval set: 30 inputs in five kinds (`question`, `invalid_question`, `instruction`, `sql`, `invalid_sql`), each with gold SQL or `expect: refuse`
- `eval.py` validates each case (known kind, non-empty input, exactly one of `gold_sql` / `expect: refuse`) and prints a score per kind
- `/eval` skill now turns one question into guardrail-approved SQL via the new `.claude/skills/eval/guard.py` (runs `sql_guard.run_query`, prints JSON)
- **Changed:** `/eval` no longer runs the full eval set; run it with `python -m agentic_query.eval`
- Merge `tests/test_sql_guard.py` and `tests/test_text_to_sql.py` into `tests/test_agentic_query.py` (safety, pipeline, eval set and guard script checks)
- Remove `first_products()` demo from `db.py`; `build_db.py` reuses `db.DB_PATH`
- README updates for the eval set, `/eval` skill, tests and `db.py`
- `/release` skill writes the commit message to a file and commits with `git commit -F <file>` (fixes PowerShell here-string failure)

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
