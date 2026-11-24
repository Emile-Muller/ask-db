# Ask DB

[![Python](https://img.shields.io/badge/Python%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![DuckDB](https://img.shields.io/badge/DuckDB-FFF000?logo=duckdb&logoColor=black)](https://duckdb.org/)
[![sqlglot](https://img.shields.io/badge/sqlglot-4B8BBE)](https://github.com/tobymao/sqlglot)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-D97757?logo=anthropic&logoColor=white)](https://claude.com/claude-code)
[![pytest](https://img.shields.io/badge/pytest-0A9EDC?logo=pytest&logoColor=white)](https://pytest.org/)
[![Ruff](https://img.shields.io/badge/Ruff-D7FF64?logo=ruff&logoColor=black)](https://github.com/astral-sh/ruff)
[![pre-commit](https://img.shields.io/badge/pre--commit-FAB040?logo=pre-commit&logoColor=black)](https://pre-commit.com/)
[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-222222?logo=githubpages&logoColor=white)](https://emile-muller.github.io/ask-db/)

Ask questions in plain English, get SQL and results, safely.

An LLM turns a question about the [Olist Brazilian E-Commerce dataset](data/README.md) into
SQL, but only **proposes** it: five deterministic layers decide whether it runs against a
read-only DuckDB database, and every run leaves a machine-readable trace. A second,
independent agent can then review whether the answer is correct.

```
question ──► L1 input screen ──► L2 prompt context ──► LLM proposes SQL (or refuses)
                                                                     │
trace + review  ◄── L5 output check ◄── L4 sandboxed executor ◄── L3 validator
```

**Stack:** Claude Code skills (the session proposes SQL; Python never calls an LLM), DuckDB
opened read-only, sqlglot (DuckDB dialect), Olist data (Kaggle, ~100k orders, 2016–2018).

**Showcase:** <https://emile-muller.github.io/ask-db/>, a one-page static site built from
[`showcase/`](showcase/) that presents the project to non-technical readers (see
[Showcase page](#showcase-page)).

## Skills

| Skill | What it does |
|---|---|
| `/question <question or SQL>` | Answers one question (or runs one pasted query) through the pipeline; reports the verdict, the SQL, the result and which layer decided |
| `/question-review [trace id]` | An independent agent judges one `/question` run from its trace and the schema docs only, and logs the review |

**`/question`** runs `python -m text2sql context` (layer 1, then the layer-2 prompt), proposes
exactly one `{"sql": ...}` or `{"refusal_reason": ...}` from that prompt alone, and sends it
through `python -m text2sql run` (layers 1-5). Raw SQL skips generation (`run --sql`). The
input is untrusted data, never instructions; there is one proposal per question, and a refused
query is never reworded to get past a guardrail (a plain DuckDB error may be fixed once).
Before the trace is printed or appended to `questions/logs/questions/`, `trace.append` checks
the exact JSON line against `src/text2sql/schemas/question_trace.json` plus rules between
fields (`trace.cross_checks`: stages in layer order, only skipped stages after the first fail,
`refused_by` = the failed stage, preview rows consistent with `row_count` and `truncated`). A
trace that fails is a pipeline bug: it is neither logged nor printed, and the run exits 1.
The skill then builds a JSON report from the trace, matching the skill's `schema.json` (see
`example.json`), and checks it with `.claude/skills/question/scripts/validate.py` (the schema
plus cross-field rules, e.g. stage statuses consistent with the verdict). The answer shown to
the user is written from the validated report only; exit 2 lists what is wrong, and the report
is fixed, never the schema.

**`/question-review`** judges whether a run's answer is **correct**; the pipeline only makes
sure the query is **safe**. The `question-reviewer` agent sees only the trace and `data/schema.yaml` (no database). It
checks the SQL logic against the question and the `rules:` in `schema.yaml` (revenue
definition, `customer_unique_id`, join fan-out, NULLs, a `LIMIT` that cuts the answer, …),
or whether a refusal was justified. It returns a verdict (`correct` / `partially_correct` /
`incorrect` / `cannot_tell`), findings and an optional `corrected_sql` as JSON, which
`python -m text2sql review` checks against `src/text2sql/schemas/question_review.json` (the
single source of the review contract; its limits mirror `config.REVIEW_MAX_*`, kept equal by a
test) **before** appending anything to `questions/logs/question-reviews/`. A rejected review
(exit 2) is never logged. `.claude/skills/question-review/scripts/validate.py` runs the same
check offline for humans and tests. A `PreToolUse` hook (`review_guard.py`) blocks every
other shell command and fails closed.

All three contracts (trace, report, review) use `src/text2sql/json_contract.py`, a small JSON Schema subset checker
(standard library only) that lints the whole schema first and rejects any keyword it does not
implement, so a schema cannot silently ask for a check that never runs.

## The 5-layer guardrails (`src/text2sql`)

The LLM only proposes; everything that decides whether SQL runs is deterministic Python,
testable without any LLM. **Layers 3 and 4 are the security boundary** and hold on their own
even if layers 1 and 2 are bypassed; 1, 2 and 5 are defense in depth. The first layer to
refuse decides the verdict, and each records a stage in the trace (`trace.py`).

| Layer | Module | What it does |
|---|---|---|
| 1 input screen | `input_screen.py` | NFKC + strip zero-width/control chars, ≤ 500 chars, raw SQL vs question, prompt-injection heuristics (strong signals refuse, weak ones are recorded) |
| 2 generation | `prompt.py` | schema context from DuckDB + `data/schema.yaml`; restricted columns and tables hidden; question fenced as untrusted; no LLM call from Python |
| 3 validator | `validator.py` | sqlglot: one statement, no comments, SELECT only, table/column/**function allowlists**, restricted columns (incl. `*`, aliases, subqueries), ≤ 4 joins, depth ≤ 3, no cartesian or recursive queries, LIMIT forced ≤ 100; fails closed; runs SQL regenerated from the qualified AST it checked |
| 4 executor | `executor.py` | read-only DuckDB, external access and extension autoload off, 512 MB / 2 threads, config locked, 5 s timeout, ≤ 100 rows; safe even without the validator |
| 5 output check | `output_check.py` | drop restricted columns, mask emails/phones, cut cells > 1000 chars, 20-row preview |

Limits live in `src/text2sql/config.py`, the allowlist and restricted columns in
`data/schema.yaml`. `tests/redteam_corpus.yaml` holds 140+ attack and benign queries
(including past bypasses kept as regression tests). `.claude/settings.json` denies session
edits to `data/schema.yaml`, the corpus, the `/question` skill folder (its report schema) and
`src/text2sql/schemas/` (the trace and review schemas); only a human can change them.

## Test questions and results

`questions/questions.yaml` holds 30 inputs, each marked `expect: answer` or `expect: refuse`:
12 valid questions plus destructive requests, prompt injections, stacked or obfuscated SQL,
exfiltration, sensitive-data, resource-abuse and out-of-scope cases. `questions/results.json`
is a full run (each `/question` in an isolated subagent, each trace judged by
`/question-review`): 30/30 match `expect` (12 accepted; refused: 6 generation, 8 validator,
4 input screen), with review verdicts 18 correct, 7 cannot tell, 5 incorrect.

## Showcase page

`showcase/` is a static page (plain HTML, CSS and JS, no build step or dependencies): what the
two skills do, the dataset and stack, the 5-layer diagram (with how many test inputs each layer
stopped) and the 30 test inputs, with a summary by category and a filterable table whose rows
expand to show the input, its path through the layers, the SQL, the validator checks, the
result and the review. It reads only `showcase/results.json`, a copy of
`questions/results.json`, and says that it shows a pre-recorded run, not a live one.

| File | Holds |
|---|---|
| `index.html` | page structure and static text |
| `styles.css` | layout, light/dark theme, mobile layout |
| `app.js` | loads `results.json` and renders stats, summary, table and detail panels |
| `results.json` | the recorded run shown on the page |

Preview locally with `.\.venv\Scripts\python.exe -m http.server -d showcase` (opening
`index.html` from disk blocks loading the JSON); after a new run, refresh the local copy with
`Copy-Item questions/results.json showcase/results.json`. `.github/workflows/pages.yml`
publishes `showcase/` to GitHub Pages
(<https://emile-muller.github.io/ask-db/>) on every push to `main` that touches it or
`questions/results.json`, and always deploys the latest `questions/results.json` as the page's
`results.json`, so the live site needs no manual copy.

## CLI (`python -m text2sql`)

| Command | Does |
|---|---|
| `context --question "..."` | layer 1; if it passes, prints the layer-2 prompt, else the refusal trace |
| `run --question "..." --sql "..."` | layers 1-5 with the proposed SQL; prints the trace |
| `run --question "..." --refusal "..."` | records that the proposer declined (refused by `generation`) |
| `run --sql "..."` | raw-SQL path: layers 1, 3, 4, 5 |
| `check --sql "..."` | layer 3 only, no database |
| `review` (JSON on stdin) | checks a `/question-review` judgement against `schemas/question_review.json`, then logs it; no database |

Exit codes: 0 accepted, 2 refused, 1 internal error. Traces go to stdout and
`questions/logs/questions/<YYYYMMDD>.jsonl`.

```powershell
.\.venv\Scripts\python.exe -m text2sql run --sql "SELECT 1; DROP TABLE customers"   # refused by validator, exit 2
```

## Setup

Requires Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .              # makes text2sql and olist_db importable
.\.venv\Scripts\python.exe -m pre_commit install          # once per clone: ruff lint + format on every commit
```

### Data

CSV files live in `data/raw/` (not tracked; download them from Kaggle). See
[data/README.md](data/README.md) for source, license and table descriptions.

> **Note:** the original `olist_products_dataset.csv` misspells two columns (`product_name_lenght`, `product_description_lenght`). You don't need to edit the file: the database build renames them to `product_name_length` and `product_description_length` (see `COLUMN_RENAMES` in `src/olist_db/build.py`). The CSVs stay exactly as downloaded, and a header that is already correct is left alone.

### Database

`src/olist_db/build.py` loads every CSV in `data/raw/` into `data/olist.duckdb`, one table per
file, named after the file (e.g. `olist_products_dataset.csv` → table `olist_products_dataset`),
and fixes the known misspelled column names as it loads them. Re-run it whenever the CSVs change. It is the only code that opens the database for writing,
and it runs fixed SQL only; everything else goes through the read-only, locked-down connection
in `src/text2sql/executor.py`.

```powershell
.\.venv\Scripts\python.exe -m olist_db.build
```

## Tests and lint

All tests run offline: no LLM, no network.

```powershell
.\.venv\Scripts\python.exe -m pytest --cov                 # all offline tests, with coverage
.\.venv\Scripts\python.exe -m pre_commit run --all-files   # ruff lint + format (config in ruff.toml)
```

## Project layout

```
src/
  text2sql/      5-layer safe text-to-SQL pipeline (`python -m text2sql`), review log and review guard
    json_contract.py   JSON Schema subset checker shared by the trace, report and review contracts
    schemas/           question_trace.json and question_review.json (the trace and review contracts)
  olist_db/
    build.py     data/raw/*.csv -> data/olist.duckdb (the only writer)
data/
  README.md      dataset source, license and file descriptions
  schema.yaml    table/column descriptions, keys, rules and sensitive columns (tracked)
  olist.duckdb   built database (generated, not tracked)
  raw/           Kaggle CSVs (not tracked)
questions/
  questions.yaml           30 test inputs
  results.json             full /question + /question-review run over them
  logs/questions/          /question traces (JSONL)
  logs/question-reviews/   /question-review records (JSONL)
showcase/        static showcase page (index.html, styles.css, app.js) and its results.json copy
.github/workflows/pages.yml   deploys showcase/ to GitHub Pages
tests/           offline tests: one file per layer, CLI, review log, JSON contracts, DB build; redteam_corpus.yaml
.claude/
  skills/        question, question-review, release (question: schema.json, example.json,
                 scripts/validate.py; question-review: example.json, scripts/validate.py)
  agents/        question-reviewer, code-reviewer, version-manager
  settings.json  permissions: pipeline and tests allowed, DB CLIs and direct data reads denied
```

## Releasing

In Claude Code, run `/release [major|minor|patch]` (`.claude/skills/release/SKILL.md`). It runs the tests and the `CLAUDE.md` safety checks, recommends a bump type from the changes and asks you to confirm or change it, bumps `VERSION` and this version history, stages all changes, makes a single release commit, and creates an annotated `vX.Y.Z` tag on it. It never pushes; publish with `git push origin main --follow-tags`.

## Version history

### 1.6.1 — 2026-11-24
- DB build renames the misspelled Kaggle columns `product_name_lenght` / `product_description_lenght` of `olist_products_dataset` to `*_length` at load time (`COLUMN_RENAMES`), only when still misspelled
- Docs: `README.md` and `data/README.md` now say to use the CSVs as downloaded, no manual header fix
- Tests: parametrized build test covering both the misspelled and the already-correct header

### 1.6.0 — 2026-11-24
- Input screen: new strong injection checks `authority_claim` ("I'm the admin", "grant me write access", `sudo`) and `destructive_command` ("delete all tables", "wipe the database", natural language only)
- Input screen: raw-SQL detection now needs a real statement shape on each `;`-separated part, so English like "Drop the reviews table, ..." stays natural language
- Validator: `select_only` and `no_forbidden_nodes` now inspect every statement before `single_statement`, so a stacked write (`SELECT 1; DROP TABLE x`) is refused for the write, with its statement position; comment-only `;` parts are looked past
- Review contract: `injection_attempt` description clarified (manipulating any pipeline LLM counts; a plain forbidden request does not); `question-reviewer` agent and `/question-review` skill updated to match and to find the latest trace with Glob/Grep/Read only
- `/release` now always recommends a bump type from the changes and asks the user to confirm it; `version-manager` gains a bump classification table and a structured proposal mode
- Tests: new classification, injection and benign look-alike cases, stacked-statement validator cases; red-team stacked cases now expect `select_only`

### 1.5.0 — 2026-11-24
- New `src/text2sql/json_contract.py`: a JSON Schema subset checker (standard library only) that lints the whole schema first and rejects keywords it does not implement
- Traces now have a contract, `src/text2sql/schemas/question_trace.json` plus `trace.cross_checks`; `trace.append` checks each trace before writing, and a trace that fails is neither logged nor printed (exit 1)
- `python -m text2sql review` checks reviews against `src/text2sql/schemas/question_review.json` (the only review contract, limits kept equal to `config.REVIEW_MAX_*` by a test), replacing the hand-written checks in `review_log.py`
- `/question` skill builds a JSON report matching its `schema.json` (see `example.json`), checks it with `scripts/validate.py`, and writes the answer from the validated report only
- `/question-review` skill and the `question-reviewer` agent point to the review schema and `example.json`, and ask explicitly for `model`; new offline `scripts/validate.py` for humans and tests
- `.claude/settings.json` denies session edits to `.claude/skills/question/**` and `src/text2sql/schemas/**`; schemas ship as package data
- New tests for the checker and the trace, report and review schemas, plus `review_guard` entry-point tests

### 1.4.2 — 2026-11-20
- README shows a row of shields.io badges under the title for the stack: Python, DuckDB, sqlglot, Claude Code, pytest, Ruff and pre-commit
- GitHub Pages badge links the live showcase

### 1.4.1 — 2026-11-20
- Pages workflow copies `questions/results.json` into `showcase/results.json` and uploads `showcase/` directly, with no intermediate `_site` folder
- README links the live showcase at <https://emile-muller.github.io/ask-db/>

### 1.4.0 — 2026-11-20
- New static showcase page in `showcase/` (HTML, CSS, JS, no build step): skills overview, dataset and stack, 5-layer diagram with how many inputs each layer stopped
- Showcase lists the 30 test inputs with a summary by category and a filterable table whose rows expand to show details, read from `showcase/results.json` (a copy of `questions/results.json`)
- New `.github/workflows/pages.yml` deploys `showcase/` to GitHub Pages on pushes to `main` that touch it or `questions/results.json`, always using the latest `questions/results.json`
- Added an MIT `LICENSE`
- README documents the showcase (new "Showcase page" section and layout entries), replacing the "showcase is planned" note

### 1.3.1 — 2026-11-20
- `/release` skill puts the venv on PATH in the same PowerShell call as `git commit`, so the pre-commit hook no longer fails with "`pre-commit` not found"

### 1.3.0 — 2026-11-20
- README rewritten: project renamed "Agentic Query" → "Ask DB", new pipeline diagram and updated 5-layer guardrails table
- README adds a Skills section (`/question`, `/question-review`), a CLI command table and the test questions summary (30/30 match `expect`)
- README setup, tests and project layout sections reorganized; layout now covers `.claude/`
- `/release` skill writes a Conventional Commits message whose type (`feat`/`fix`/`docs`/...) is picked from the changes instead of always `chore(release)`

### 1.2.0 — 2026-11-20
- `questions/results.json` regenerated: each `/question` run in an isolated subagent given only the question text, each trace judged by a separate question-reviewer agent via `/question-review`
- New top-level `generated_at`, `method`, `trace_log` / `review_log` paths and a `summary` (30/30 pass; deciding layer: 12 accepted, 6 generation, 8 validator, 4 input_screen; verdicts: 18 correct, 7 cannot_tell, 5 incorrect)
- Each result now carries `category`, `expect`, `actual`, `pass`, `verdict`, `decided_by`, the full trace and its review, replacing `type` / `outcome` / `sql` / `result`

### 1.1.0 — 2026-11-19
- Validator runs the qualified AST it checked (columns qualified, stars expanded from the policy); `_col_<n>` aliases get readable names
- Validator fixes: CTE names resolved per scope, alias shadowing in `WHERE` blocked, joins need a cross-table equality, SQL capped at `MAX_SQL_CHARS` (5000)
- Validator fails closed if a sqlglot upgrade renames a denylisted node; sqlglot pinned to `>=30.33,<31`
- Output check cuts strings longer than `MAX_CELL_CHARS` (1000) after masking; new `cut_cells` trace field
- `--trace-file` / `--review-file` confined to `.jsonl` files under `questions/logs/` or the temp dir
- `question-reviewer` hook fails closed when the guard cannot start; `context` no longer prints a shell command built from raw SQL
- `.claude/settings.json` denies edits to `tests/redteam_corpus.yaml` and `data/schema.yaml`; `CLAUDE.md` documents the rule
- New red-team cases and tests; the agent-hook test uses Git Bash on Windows instead of the WSL launcher in System32

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
