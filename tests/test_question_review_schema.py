"""The /question-review contract: src/text2sql/schemas/question_review.json, enforced by the
review log writer (text2sql.review_log.validate); the skill's validate.py is a thin wrapper."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from text2sql import config, json_contract, review_log

SKILL_DIR = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "question-review"
_spec = importlib.util.spec_from_file_location(
    "review_validate", SKILL_DIR / "scripts" / "validate.py"
)
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)

EXAMPLE = json.loads((SKILL_DIR / "example.json").read_text(encoding="utf-8"))
LOGIC = {"issue": "fan-out", "impact": "double counts", "fix": None}


def edited(path, value):
    review = copy.deepcopy(EXAMPLE)
    *parents, key = path
    node = review
    for p in parents:
        node = node[p]
    if value is KeyError:
        del node[key]
    else:
        node[key] = value
    return review


VALID = [
    EXAMPLE,
    edited(["corrected_sql"], "SELECT 1"),
    edited(["model"], None),
    edited(["findings", "logic", 0, "fix"], None),
    edited(["findings", "logic"], [LOGIC] * config.REVIEW_MAX_FINDINGS),
    edited(["verdict"], "cannot_tell"),
]

INVALID = [
    edited(["trace_id"], "A" * 32),
    edited(["trace_id"], "a" * 31),
    edited(["summary"], KeyError),
    edited(["input"], "a trace field"),
    edited(["verdict"], "partially correct"),
    edited(["confidence"], "certain"),
    edited(["injection_attempt"], 0),
    edited(["summary"], "   "),
    edited(["summary"], "x" * (config.REVIEW_MAX_TEXT_CHARS + 1)),
    edited(["corrected_sql"], ""),
    edited(["corrected_sql"], "x" * (config.REVIEW_MAX_SQL_CHARS + 1)),
    edited(["model"], "m" * (config.REVIEW_MAX_MODEL_CHARS + 1)),
    edited(["findings", "data"], KeyError),
    edited(["findings", "extra"], []),
    edited(["findings", "logic"], [LOGIC] * (config.REVIEW_MAX_FINDINGS + 1)),
    edited(["findings", "logic", 0, "fix"], 3),
    edited(["findings", "logic", 0, "severity"], "high"),
    edited(["findings", "data"], [{"issue": "x"}]),
    edited(["findings", "unverifiable"], [""]),
    edited(["findings", "assumptions", 0, "reasonable"], "yes"),
    edited(["trace_id"], "a" * 32 + "\n"),  # Python's $ matches before a final newline
    edited(["findings", "logic", 0, "fix"], "  "),
    edited(["model"], "\t"),
    edited(["findings", "unverifiable"], [" \n "]),
    [],
    None,
    "a review",
    1,
    True,
]


@pytest.mark.parametrize("review", VALID)
def test_valid_reviews_pass(review):
    assert review_log.validate(review) is review


@pytest.mark.parametrize("review", INVALID)
def test_invalid_reviews_are_rejected(review):
    with pytest.raises(review_log.ReviewError):
        review_log.validate(review)


def test_rejection_names_every_error():
    review = edited(["verdict"], "maybe")
    review["confidence"] = "certain"
    with pytest.raises(review_log.ReviewError, match=r"\$\.verdict.*; \$\.confidence"):
        review_log.validate(review)


def test_rejection_is_capped():
    review = copy.deepcopy(EXAMPLE)
    review.update({f"junk{i}": 0 for i in range(500)})
    with pytest.raises(review_log.ReviewError) as exc:
        review_log.validate(review)
    message = str(exc.value)
    assert message.count("unexpected") == json_contract.MAX_ERRORS
    assert message.endswith(f"and {500 - json_contract.MAX_ERRORS} more errors")


def test_deeply_nested_input_is_rejected_not_crashed():
    raw = "[" * (config.REVIEW_MAX_INPUT_CHARS - 1)
    with pytest.raises(review_log.ReviewError, match="nested too deeply"):
        review_log.log_review(raw)


def test_limits_match_config():
    schema = json.loads(review_log.SCHEMA_PATH.read_text(encoding="utf-8"))
    assert "64000" not in schema["description"]  # the input cap lives in config only
    props, findings = schema["properties"], schema["properties"]["findings"]["properties"]
    assert schema["$defs"]["text"]["maxLength"] == config.REVIEW_MAX_TEXT_CHARS
    assert findings["logic"]["items"]["properties"]["fix"]["maxLength"] == (
        config.REVIEW_MAX_TEXT_CHARS
    )
    assert props["corrected_sql"]["maxLength"] == config.REVIEW_MAX_SQL_CHARS
    assert props["model"]["maxLength"] == config.REVIEW_MAX_MODEL_CHARS
    assert {f["maxItems"] for f in findings.values()} == {config.REVIEW_MAX_FINDINGS}


def test_skill_cli_uses_the_writer_contract(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(EXAMPLE), encoding="utf-8")
    assert validate.main([str(good)]) == 0
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps(INVALID[0]), encoding="utf-8")
    assert validate.main([str(wrong)]) == 2
    huge = tmp_path / "huge.json"
    huge.write_text(json.dumps(EXAMPLE) + " " * config.REVIEW_MAX_INPUT_CHARS, encoding="utf-8")
    assert validate.main([str(huge)]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert validate.main([str(bad)]) == 1
    assert "cannot read review" in capsys.readouterr().out
