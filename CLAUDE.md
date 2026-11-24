# CLAUDE.md

Text-to-SQL on the Olist Brazilian E-Commerce dataset (see `data/README.md`), built to show
how to make an AI-driven text-to-SQL tool safe. Natural language → SQL proposed by an LLM
→ 5 deterministic layers → DuckDB → results plus a machine-readable trace.

- `src/text2sql/`: the safe pipeline (`python -m text2sql`), used by the `/question` skill
- `src/olist_db/`: builds `data/olist.duckdb` from `data/raw/*.csv` (`python -m olist_db.build`),
  the only code that opens the database for writing (fixed, trusted SQL only)

## Core principle
The LLM (an `sql-proposer` agent that can only Read its layer-2 prompt file and Write its
reply once to its proposal file, spawned by `/question` and `/question-batch`) only PROPOSES SQL. Everything that decides whether SQL
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
- `results --ids <manifest.json>` (or the manifest on stdin): writes `questions/results.json`
  (`results.py`, format 2) from a list of `{id, trace_id, review_id, error}`. It copies each
  trace and review verbatim from the logs by id, joins `id/category/expect` from
  `questions/questions.yaml`, derives `actual`, `pass`, `error` and `summary`, and checks the
  document against `src/text2sql/schemas/question_results.json` plus `results.cross_checks`
  before writing; on failure it writes nothing (exit 1). Output only to `.json` under
  `questions/` or the temp dir. `results --check <file> [--verify-logs]` checks a file (exit
  0 valid, 1 not); `results --list` prints `[{id, question}]`; `results --list --rerun <ids>`
  prints those plus every stale entry; `results --ids <m> --merge` reuses the entries not in
  the manifest, only if their trace has the current `git_commit` and `meta.pipeline_hash`
  (`trace.pipeline_hash`: the package code, contracts and `data/schema.yaml`). No database,
  no LLM
- Batch options (used by `/question-batch`, so no untrusted text is quoted for a shell;
  `/question` uses `--brief` and `--proposal-file` with `--question`):
  `context`/`run --question-id <id>` take the text of that `questions.yaml` entry;
  `--brief` prints one JSON line (`context`: `status` refused/raw_sql/prompt, the prompt
  written to `questions/logs/prompts/<hex>.txt`, and the `proposal_file` /
  `retry_proposal_file` the proposer may write, `<temp>/text2sql-proposals/<hex>[-2].json`;
  `run`: `trace_id`, `verdict`, `refused_by`, `sql_error`); `run --proposal-file <f>` reads
  the proposer's JSON reply (`.json` under the temp dir) instead of `--sql`/`--refusal`; with `--prompt-file <p>`
  (only a `<hex>.txt` in the prompts dir) it deletes that prompt after the run
- `/question-batch [ids]`: the orchestrator runs `context`/`run` itself by id (layer-1
  refusals and raw SQL need no LLM), in parallel waves; a `sql-proposer` agent may only Read
  its prompt file and Write its JSON reply once to its proposal file (`PreToolUse` hook
  `proposer_guard.py`), so the orchestrator never copies SQL text; each trace is
  reviewed by `/question-review <trace id> --terse` (forked), given only the trace id
- Log appends (`trace._write_line`, traces and reviews) hold a lock on `<log>.lock`, so
  parallel runs and reviews are safe
- Exit codes: 0 accepted, 2 refused, 1 internal error. Traces go to stdout and `questions/logs/questions/<YYYYMMDD>.jsonl`.

## JSON contracts (`src/text2sql/schemas/`, checked by `json_contract.py`)
- `question_trace.json`: one trace (plus `trace.cross_checks`)
- `question_review.json`: the reviewer's input to `review`
- `question_results.json`: `questions/results.json` (plus `results.cross_checks`); `$ref`s
  the two above (sibling-file refs) for the embedded trace and the review record's fields

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

### 5. Question runs cannot edit what judges them
`/question`, `/question-review` and `/question-batch` declare a `PreToolUse` hook
(`src/text2sql/edit_guard.py`, list in `FROZEN`). From the first call of one of them until the
session ends, edit tools are denied on the allowlist (`data/schema.yaml`), the red-team corpus,
`questions/questions.yaml`, `src/text2sql/schemas/**`, the question skills and agents,
`.claude/settings*.json` and the guards. To change those files, use a session that has not run
a question skill. Do not bypass the guard with shell writes.
