"""Check a /question-review judgement offline, without logging it.

Usage:
    python validate.py [review.json]     (reads stdin when no file is given)

Prints "valid" and exits 0, or prints one error per line and exits 2 (1 on unreadable
input). This is a thin wrapper: the contract is src/text2sql/schemas/question_review.json,
checked by the same function the review log writer (text2sql.review_log) runs. Whether
`trace_id` names an existing trace is checked only by `python -m text2sql review`.
"""

from __future__ import annotations

import sys

from text2sql import config, json_contract, review_log


def main(argv: list[str]) -> int:
    return json_contract.main(
        argv, review_log.schema_errors, "review", config.REVIEW_MAX_INPUT_CHARS
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
