---
name: question-review
description: Independently review one /question run, judging from its trace and the schema docs only (no database access) whether the SQL logic answers the question and whether the returned data is plausible. Logs the review to questions/logs/question-reviews/. Use after a /question run.
argument-hint: "[trace id]  (default: the most recent run)"
disable-model-invocation: true
context: fork
agent: question-reviewer
---

# /question-review: judge one run from its trace

Input: `$ARGUMENTS`

## 1. Find the trace
Traces are JSON lines in `questions/logs/questions/<YYYYMMDD>.jsonl`, one object per run.
- With a trace id: Grep for `"id": "<id>"` in `questions/logs/questions/`. If nothing matches, say so and stop (log nothing).
- Without one: take the last line of the newest file in `questions/logs/questions/`.

Then read `data/schema.yaml`.

## 2. Decide what can be reviewed
- `input_type` is `raw_sql`: there is no question to compare against. Report only obvious
  logic problems in the SQL (for example double counting), then go to step 5.
- `verdict` is `refused` by `generation`: judge the refusal. Could the schema have answered
  the question (wrong refusal), or was it right to decline?
- `verdict` is `refused` by another layer, or `error`: there is no result. Review the logic
  of `generated_sql` only, and do not judge whether the guardrail was right to refuse.
- `verdict` is `accepted`: do steps 3 and 4.

## 3. SQL logic (`input` vs `normalized_sql`)
Break the question into its parts (measure, filters, grouping, time range, ordering,
top-N) and check that each one maps to the SQL. Then check the SQL against the schema:
- Every rule in `schema.yaml` `rules:` that applies (revenue definition, no unrequested
  `order_status` filter, English category names, `customer_unique_id` for customers,
  purchase timestamp as the order date, delivery-days formula, rounding).
- Joins follow the declared keys. No fan-out double counting (items × payments, items
  × reviews), and DISTINCT is used where needed.
- NULL handling, inner vs outer joins that silently drop rows, integer division, date bucketing.
- The forced `LIMIT` does not cut an answer the question needs in full (for example a
  "for each" question with more groups than the limit).

## 4. Returned data (`result`, `stages[4].details`)
- The columns answer the question (right measure, readable labels, units).
- Values are plausible for this dataset (~100k orders, 2016-09 to 2018-10, BRL): no
  impossible dates, negative amounts, percentages over 100, or suspicious NULL / zero / empty results.
- The ordering and top-N match the question.
- Note any `dropped_columns` or `masked_cells` from the output check, and say if they hide
  something the question asked for.
- If `truncated` is true, you only see a preview of `row_count` rows. Say which claims you
  cannot check because of that.

## 5. Log the review
Send your judgement to the review log writer, as one JSON object in a quoted heredoc:

    .venv/Scripts/python.exe -m text2sql review <<'EOF'
    {"trace_id": "<id>", "verdict": "correct", "confidence": "high",
     "findings": {"logic": [], "data": [], "unverifiable": [], "assumptions": []},
     "corrected_sql": null, "injection_attempt": false,
     "summary": "<one or two sentences>", "model": "<your model id>"}
    EOF

Every key is required and no others are accepted. The full format is in the docstring
of `src/text2sql/review_log.py`:
- `verdict`: `correct` / `partially_correct` / `incorrect` / `cannot_tell`. For a
  refusal review, `correct` means the refusal was right.
- `confidence`: `high` / `medium` / `low`.
- `findings.logic`: `[{"issue", "impact", "fix"}]` (`fix` may be null);
  `findings.data`: `[{"issue", "impact"}]`; `findings.unverifiable`: `[str]`;
  `findings.assumptions`: `[{"assumption", "reasonable": bool}]`.
- `corrected_sql`: only when a short fix is clear, else null.

Do not send trace fields (`input`, verdicts, SQL, timestamps): the writer copies them
from the trace, derives `review_type`, generates `id` and `timestamp`, and appends the
record to `questions/logs/question-reviews/<YYYYMMDD>.jsonl`. It prints the record.
Exit 2 means the JSON was rejected: read the reason, fix it and retry once.

## 6. Report
- **Verdict**: `correct` / `partially correct` / `incorrect` / `cannot tell`, with confidence (high / medium / low).
- **Question**, **trace id**, **timestamp**, and the `normalized_sql` in a ```sql block.
- **Logic findings**: each issue, why it changes the answer, and the fix (corrected SQL if short).
- **Data findings**: what looks plausible, what looks wrong, and what cannot be checked.
- **Assumptions**: interpretations the SQL made, and whether they are reasonable.
- **Logged to**: the review log path and the review `id` (from the writer's output).

Keep it short when the answer is clearly correct. Do not pad the report with praise.
