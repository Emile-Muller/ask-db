# CLAUDE.md

Text-to-SQL on the Olist Brazilian E-Commerce dataset (see `data/README.md`), built to show
how to make an AI-driven text-to-SQL tool safe. Natural language → SQL proposed by an LLM
→ 5 deterministic layers → DuckDB → results plus a machine-readable trace.

- `src/text2sql/`: the safe pipeline (`python -m text2sql`), used by the `/question` skill
- `src/olist_db/`: builds `data/olist.duckdb` from `data/raw/*.csv` (`python -m olist_db.build`),
  the only code that opens the database for writing (fixed, trusted SQL only)

## Core principle
The LLM (the Claude Code session) only PROPOSES SQL. Everything that decides whether SQL
runs is deterministic Python code, testable without any LLM. **The validator (layer 3) and
the executor (layer 4) are the security boundary.** They must hold on their own even if
layers 1 and 2 are bypassed. Layers 1 (input screen), 2 (prompt context) and 5 (output
check) are defense in depth.

| Layer | Module | Decides |
|---|---|---|
| 1 | `input_screen.py` | normalize, length ≤ 500, raw SQL vs question, injection heuristics |
| 2 | `prompt.py` | schema context without restricted columns; no LLM call from Python |
| 3 | `validator.py` | sqlglot: one SELECT, no comments, allowlisted tables/columns/functions, limits, forced LIMIT |
| 4 | `executor.py` | read-only DuckDB, external access off, config locked, 5 s timeout, row cap |
| 5 | `output_check.py` | drop restricted columns, mask emails/phones, 20-row preview |

Limits and paths live in `config.py`; allowed tables and restricted (`sensitive: true`)
columns in `data/schema.yaml`. The trace format is documented in `trace.py`; its contract is
`src/text2sql/schemas/question_trace.json` plus `trace.cross_checks`, checked by `trace.append`
before a trace is logged or printed (a trace that fails is neither, exit 1).

## CLI contract (`python -m text2sql`, venv: `.\.venv\Scripts\python.exe`)
- `context --question "..."`: layer 1; if it passes, print the layer-2 prompt, else the refusal trace
- `run --question "..." --sql "..."`: layers 1-5 with the proposed SQL; prints the trace
- `run --question "..." --refusal "..."`: the proposer declined (refused by `generation`)
- `run --sql "..."`: raw-SQL path (input_type `raw_sql`): layers 1, 3, 4, 5
- `check --sql "..."`: layer 3 only, no database
- `review` (review JSON on stdin): checks it against `src/text2sql/schemas/question_review.json`
  (the single source of the review contract; its limits mirror `config.REVIEW_MAX_*`,
  kept equal by a test) and logs a `/question-review` judgement to
  `questions/logs/question-reviews/<YYYYMMDD>.jsonl` (`review_log.py`); no database. The
  question-reviewer agent may run only this command, enforced by its `PreToolUse` hook
  (`review_guard.py`)
- Exit codes: 0 accepted, 2 refused, 1 internal error. Traces go to stdout and `questions/logs/questions/<YYYYMMDD>.jsonl`.

## Safety rules (must follow)

### 1. No secrets
- Never hardcode API keys, tokens or passwords in code, commits, logs or docs.
- Load secrets from environment variables or `.streamlit/secrets.toml` / `.env`.
- Keep `.env` and `.streamlit/secrets.toml` in `.gitignore`.

### 2. Database is read-only
- Always open DuckDB in read-only mode: `duckdb.connect(path, read_only=True)`.
- Generated SQL must only read data (`SELECT` / `WITH`). Reject anything else
  (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`, `ATTACH`, `COPY`, `PRAGMA`, `INSTALL`, `LOAD`, etc.).
- Allow a single statement per query (no `;`-chained statements).
- Never run LLM-generated SQL on a writable connection, even for testing.
- Treat all user input and LLM output as untrusted.

### 3. Query only through the pipeline
- Never execute SQL against the database yourself (no `duckdb` CLI, no Python snippets
  opening `data/olist.duckdb`). The only way to query is `python -m text2sql`.
- Never weaken a layer to make a query pass. If you find a guardrail bypass, fix it and
  keep the case in `tests/redteam_corpus.yaml`.

### 4. Tests
- `.\.venv\Scripts\python.exe -m pytest --cov` runs offline: no LLM, no network.
- Keep ruff clean (pre-commit runs `ruff check --fix` and `ruff format`).

### 5. The allowlist and red-team corpus are frozen for sessions
`.claude/settings.json` denies `Edit(./tests/redteam_corpus.yaml)` and
`Edit(./data/schema.yaml)`, so a `/question` run can never change the allowlist or the
red-team cases that judge it. It also denies `Edit(./.claude/skills/question/**)` and
`Edit(./src/text2sql/schemas/**)`, so a session cannot loosen the trace, report or review
contract it is checked against. To change them, a human removes those rules temporarily, makes
the change, and puts them back.
