---
name: question-batch
description: Run the entries of questions/questions.yaml through the safe pipeline in parallel waves (the orchestrator runs the deterministic CLI steps; an sql-proposer agent sees only the layer-2 prompt and writes its reply to a proposal file itself; each trace is judged by /question-review, forked, given only the trace id), then write questions/results.json with the deterministic writer `python -m text2sql results`. With ids, re-runs only those (plus stale entries) and merges. Use when the user asks to re-run the test questions or regenerate results.json.
argument-hint: "[id ...]  (none: all of questions/questions.yaml; ids: re-run those and merge)"
disable-model-invocation: true
hooks:
  PreToolUse:
    - matcher: "Edit|Write|MultiEdit|NotebookEdit"
      hooks:
        - type: command
          # Freezes the files that judge a run for the rest of the session (edit_guard.py).
          # Fail closed: a guard that cannot even start must still block.
          command: '"$CLAUDE_PROJECT_DIR/.venv/Scripts/python.exe" -m text2sql.edit_guard || { echo "edit_guard: blocked, the guard did not run cleanly" >&2; exit 2; }'
---

# /question-batch: run the test questions in parallel, collect ids, let the writer build results.json

Input: `$ARGUMENTS`

You are the orchestrator. You run the deterministic CLI steps yourself and pass ids, file
paths and the proposer's reply around. You never decide whether SQL runs: the pipeline does.
Deterministic code reads the records from the logs and builds the file.

## Hard rules
- The only commands you run are, from the repo root in **Bash**:
  - `.venv/Scripts/python.exe -m text2sql context --question-id <id> --brief`
  - `.venv/Scripts/python.exe -m text2sql run --question-id <id> --brief [--proposal-file <path> --prompt-file <path> --model <id>]`
  - `.venv/Scripts/python.exe -m text2sql results ...`

  Do not copy `results.json` to `showcase/`: `.github/workflows/pages.yml` deploys the
  latest `questions/results.json` as the page's `results.json`.

  Always `--question-id`, never `--question` or `--sql`: no question or SQL text ever goes
  through a shell. Never run SQL any other way.
- You see only `{id, question}`. Never read `questions/questions.yaml`, `questions/results.json`,
  the logs or the prompt files, and never give anyone `category`, `expect`, other questions or
  earlier results.
- A proposer gets ONLY `PROMPT_FILE=<path>` and `PROPOSAL_FILE=<path>` (and, at most once,
  the `sql_error` of its own run with the retry path). A reviewer gets ONLY the trace id and
  `--terse`.
- Never reword a refused question, and never ask a proposer to try again after a refusal.
  The only second proposal is the one `sql_error` retry below. A step that exits 1 (or a
  reply with no id) becomes an error entry: note it and go on.
- Never write, edit or read a proposal file (the proposer writes it, the CLI reads it), a
  trace or a review, and never write `results.json` by hand.
- Never edit `.claude/**`, `questions/**` (except through the writer), `src/` or `data/`.

## 1. List what to run

    .venv/Scripts/python.exe -m text2sql results --list                       # no arguments
    .venv/Scripts/python.exe -m text2sql results --list --rerun <id> [<id> ...] # with ids

It prints `[{id, question}]` in file order. With `--rerun`, it also adds every entry of the
current `results.json` that cannot be reused (made by another commit or pipeline, the
question text changed, or the earlier run failed) and says why on stderr: report those.

## 2. Run in waves of up to 8 questions
DuckDB allows many read-only processes, and every log append is locked
(`trace._write_line`), so runs and reviews may overlap freely. Put all the calls of one
step for the whole wave into ONE message, and move each question on as soon as its
previous step is done. Do not wait for the whole wave between steps.

**a. Context.** `context --question-id <id> --brief` prints one JSON line:
- `{"status": "refused", "trace_id": ...}` (exit 2): layer 1 refused it. The trace exists:
  go to c.
- `{"status": "raw_sql"}`: no proposer is needed. Run `run --question-id <id> --brief`, take
  `trace_id`, go to c.
- `{"status": "prompt", "prompt_file", "proposal_file", "retry_proposal_file"}`: go to b.

**b. Propose, then run.** Spawn an `sql-proposer` agent (Agent tool,
`subagent_type: "sql-proposer"`) whose whole prompt is the two lines
`PROMPT_FILE=<prompt_file>` and `PROPOSAL_FILE=<proposal_file>`. It can only Read that
prompt and Write its JSON reply once to that proposal file (its `PreToolUse` guard,
`proposer_guard.py`, denies everything else), then replies `written`. You never see or
copy the SQL. When it replies:
1. `run --question-id <id> --proposal-file <proposal_file> --prompt-file <prompt_file>
   --model <your model id> --brief` (the proposer inherits your model; the CLI deletes the
   prompt file after the run, so pass the same `--prompt-file` on the retry). It prints
   `{"trace_id", "verdict", "refused_by", "sql_error"}`; exit 1 means the file is missing
   or not a valid proposal: error entry `proposal rejected` (also when the proposer
   replied `not written: ...`).

*One retry, only for a plain SQL mistake:* if `sql_error` is not null (the executor refused
a proposal for a DuckDB binder, type or cast error, never for a guardrail), send that
proposer ONE message with SendMessage: `DuckDB error: <sql_error>. PROPOSAL_FILE=<retry_proposal_file>`.
When it replies, run as above with `--proposal-file <retry_proposal_file>`. That second trace
is the one that counts, whatever its outcome.

**c. Review.** As soon as a question has its final trace id, call the Skill tool with
`skill: "question-review"` and `args: "<trace id> --terse"`. The skill has `context: fork` and
`agent: question-reviewer`, so it runs in its own agent with no access to this conversation.
It replies `REVIEW_ID=<hex>` or `REVIEW_ID=none` (null review id, error note
`review not logged`).

*Isolation check (first review only):* the Skill result must show that the skill ran as a
forked agent (a sub-agent result, not the skill text loaded into your own turn). If it
loaded inline, stop calling the skill. From then on spawn the `question-reviewer` agent
(Agent tool, `subagent_type: "question-reviewer"`) with a prompt of only: "Follow
.claude/skills/question-review/SKILL.md for trace id <id> --terse." Report which path you used.

**d. Record** one manifest item, ids only:

    {"id": "<questions.yaml id>", "trace_id": "<hex>" | null, "review_id": "<hex>" | null,
     "error": null | "<short note, e.g. context exited 1 / proposal rejected / review not logged>"}

## 3. Write results.json
Write the manifest (a JSON list, one item per question you ran) to a file in your
scratchpad, then:

    .venv/Scripts/python.exe -m text2sql results --ids <manifest file>           # full run
    .venv/Scripts/python.exe -m text2sql results --ids <manifest file> --merge   # with ids

The writer looks up each record by id in `questions/logs/`, joins it with
`id`/`category`/`expect` from questions.yaml, derives `actual`, `pass`, `error` and
`summary`, and checks the whole document against `src/text2sql/schemas/question_results.json`
plus `results.cross_checks` before writing `questions/results.json`. With `--merge`, every
id not in the manifest reuses its current entry, but only if it is still up to date; if one
is not, nothing is written and the error names it. Exit 1 means nothing was written: report
the errors, do not hand-patch the file. Then check it, including that every record is a
verbatim copy of its log line:

    .venv/Scripts/python.exe -m text2sql results --check questions/results.json --verify-logs

## 4. Report
- Pass/fail per question (`id`, expect, actual, deciding layer, review verdict), from the
  writer's file (read it only now, after it is written).
- Every `expect` mismatch, every `incorrect` review verdict, every error entry, every
  `sql_error` retry, and (partial run) which entries were reused.
- Whether the reviewer fork was confirmed or the fallback agent was used.
