---
name: eval
description: Run the end-to-end text-to-SQL eval (eval/questions.yaml) - each showcase question goes through Claude, the SQL guardrail and DuckDB, and is graded against gold SQL. Use when the user asks to run the eval, check accuracy, or verify a prompt/schema change.
argument-hint: "[gold | <question-id> ...]"
disable-model-invocation: true
---

# Eval workflow

Arguments: `$ARGUMENTS`

Use the project venv: `.\.venv\Scripts\python.exe`.

## 0. Parse the argument
- `gold`: only check the gold SQL (`python -m agentic_query.eval --gold-only`). Free, no API calls. Go to step 2.
- Empty: run all questions.
- Otherwise: treat each word as a question id (`python -m agentic_query.eval --only <ids>`). Unknown ids make the
  script exit with an error: show it and list the ids from `eval/questions.yaml`.

## 1. Pre-flight
- `data/olist.duckdb` exists; otherwise tell the user to run `python -m agentic_query.build_db` and stop.
- An API credential is available: `ANTHROPIC_API_KEY` set in the environment (or `.env` loaded by
  the user). Check only whether it is set - **never print, log or echo its value**. If missing,
  stop and tell the user to set it (e.g. `$env:ANTHROPIC_API_KEY = "..."` in their own terminal).
- A full run makes one Claude API call per question (~30). Say so before running.

## 2. Run
- `.\.venv\Scripts\python.exe -m agentic_query.eval [--gold-only | --only <ids>]`
- Exit code 1 means at least one question failed; that is a result to report, not a crash.

## 3. Report
- Score `passed/total`, plus model and effort (from `eval/last_run.json`).
- For each failure: id, reason, the generated SQL, and your diagnosis: model mistake, an
  ambiguous question, a missing rule/description in `data/schema.yaml`, or a wrong gold answer.
  Check by running the gold and generated SQL yourself through `agentic_query.sql_guard.run_query` only.
- Suggest fixes (usually a `data/schema.yaml` rule or a clearer question) but do not edit files
  unless the user asks. Never change a gold answer just to make a run pass.
