"""The JSON Schema checker (text2sql.json_contract), used by review_log and the skills."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from text2sql import json_contract
from text2sql.json_contract import capped, check, lint

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = [
    ROOT / "src" / "text2sql" / "schemas" / "question_review.json",
    ROOT / "src" / "text2sql" / "schemas" / "question_trace.json",
    ROOT / "src" / "text2sql" / "schemas" / "question_results.json",
    ROOT / ".claude" / "skills" / "question" / "schema.json",
]


def ok(value, schema) -> bool:
    lint(schema)
    return check(value, schema, schema) == []


# --- the real schemas -------------------------------------------------------------------


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: p.name)
def test_real_schemas_lint_clean(path):
    json_contract.load_schema(path)  # lints, and loads the sibling files it $refs


# --- $ref to a sibling schema file ------------------------------------------------------


def _write(path: Path, schema: dict) -> Path:
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


def test_sibling_ref_resolves_in_the_sibling(tmp_path):
    _write(tmp_path / "b.json", {"$defs": {"t": {"type": "string"}}, "properties": {
        "a": {"$ref": "#/$defs/t"}}})  # fmt: skip
    root = _write(tmp_path / "a.json", {"properties": {
        "whole": {"$ref": "b.json"}, "part": {"$ref": "b.json#/$defs/t"}}})  # fmt: skip
    assert json_contract.check_schema({"whole": {"a": "x"}, "part": "y"}, root) == []
    assert json_contract.check_schema({"whole": {"a": 1}, "part": 2}, root) == [
        "$.whole.a: expected string",
        "$.part: expected string",
    ]


@pytest.mark.parametrize(
    "ref", ["../b.json", "sub/b.json", "b.yaml", "b.json#x", "b.json#/missing", "nope.json"]
)
def test_bad_sibling_refs_are_rejected(tmp_path, ref):
    _write(tmp_path / "b.json", {"type": "string"})
    root = _write(tmp_path / f"a-{abs(hash(ref))}.json", {"$ref": ref})
    with pytest.raises((ValueError, OSError)):
        json_contract.load_schema(root)


def test_sibling_ref_cycles_are_rejected(tmp_path):
    _write(tmp_path / "x.json", {"$ref": "y.json"})
    _write(tmp_path / "y.json", {"$ref": "x.json"})
    with pytest.raises(ValueError, match="cycle"):
        json_contract.load_schema(tmp_path / "x.json")


# --- lint: the whole schema, reached or not ---------------------------------------------


@pytest.mark.parametrize(
    "schema",
    [
        {"maximum": 3},
        {"properties": {"absent": {"format": "email"}}},  # never reached by a value
        {"items": {"uniqueItems": True}},
        {"$defs": {"unused": {"oneOf": []}}},
        {"allOf": [{"if": {"type": "string"}, "then": {"not": {}}}]},
        {"then": {"type": "string"}},  # then without if
        {"additionalProperties": {"type": "string"}},  # schema form not implemented
        {"type": "number"},
        {"type": []},
        {"enum": []},
        {"required": "a"},
        {"maxLength": -1},
        {"maxItems": True},
        {"minimum": 1.5},
        {"pattern": "("},
        {"$ref": "#/$defs/missing"},
        {"$ref": "other.json#/x"},
        {"properties": {"a": True}},  # boolean schemas not implemented
        {"allOf": {}},
    ],
)
def test_lint_rejects_what_check_does_not_implement(schema):
    with pytest.raises(ValueError):
        lint(schema)


def test_check_still_rejects_unknown_keywords_it_reaches():
    with pytest.raises(ValueError, match="unsupported"):
        check(1, {"maximum": 3}, {})


def test_load_schema_lints(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"properties": {"a": {"format": "x"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported"):
        json_contract.check_schema({}, bad)


# --- keywords ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "type_", "expected"),
    [
        (True, "boolean", True),
        (1, "boolean", False),
        (True, "integer", False),
        (1, "integer", True),
        (1.0, "integer", False),
        (None, "null", True),
        ([], "object", False),
        ({}, "array", False),
    ],
)
def test_type(value, type_, expected):
    assert ok(value, {"type": type_}) is expected


@pytest.mark.parametrize(("value", "expected"), [("a", True), (None, True), (1, False)])
def test_type_list(value, expected):
    assert ok(value, {"type": ["string", "null"]}) is expected


@pytest.mark.parametrize(
    ("value", "enum", "expected"),
    [
        (0, [False], False),
        (False, [0], False),
        (1, [True], False),
        (1.0, [1], False),
        (1, [1], True),
        (None, [None], True),
        ("a", ["a", "b"], True),
    ],
)
def test_enum_compares_types(value, enum, expected):
    assert ok(value, {"enum": enum}) is expected


@pytest.mark.parametrize(("value", "expected"), [(None, True), (0, False), (False, False)])
def test_const_compares_types(value, expected):
    assert ok(value, {"const": None}) is expected


def test_required_and_additional_properties():
    schema = {"required": ["a"], "properties": {"a": {}}, "additionalProperties": False}
    assert ok({"a": 1}, schema)
    assert check({}, schema, schema) == ["$: missing 'a'"]
    assert check({"a": 1, "b": 2}, schema, schema) == ["$: unexpected 'b'"]
    assert ok("not an object", schema)  # object keywords ignore non-objects


def test_items_and_counts():
    schema = {"items": {"type": "integer"}, "minItems": 1, "maxItems": 2}
    assert ok([1, 2], schema)
    assert check([], schema, schema) == ["$: at least 1 items"]
    assert check([1, "x", 3], schema, schema) == ["$[1]: expected integer", "$: at most 2 items"]


def test_lengths_and_minimum():
    assert ok("ab", {"minLength": 2, "maxLength": 2})
    assert not ok("a", {"minLength": 2})
    assert not ok("abc", {"maxLength": 2})
    assert ok(0, {"minimum": 0})
    assert not ok(-1, {"minimum": 0})
    assert ok("text", {"minimum": 0})  # numeric keywords ignore non-numbers


def test_pattern_is_a_search_and_needs_lengths_for_fixed_formats():
    assert ok("xx a xx", {"pattern": "a"})  # search, not full match
    hexid = {"pattern": "^[0-9a-f]{4}$"}
    assert ok("abcd\n", hexid)  # Python: $ matches before a final newline
    assert not ok("abcd\n", {**hexid, "maxLength": 4})  # ... so the schemas add lengths


def test_ref():
    schema = {"$defs": {"t": {"type": "string"}}, "properties": {"a": {"$ref": "#/$defs/t"}}}
    assert ok({"a": "x"}, schema)
    assert check({"a": 1}, schema, schema) == ["$.a: expected string"]


def test_all_of_and_if_then():
    schema = {
        "allOf": [
            {"if": {"properties": {"k": {"const": "a"}}}, "then": {"required": ["x"]}},
            {"if": {"properties": {"k": {"const": "b"}}}, "then": {"required": ["y"]}},
        ]
    }
    assert ok({"k": "a", "x": 1}, schema)
    assert ok({"k": "b", "y": 1}, schema)
    assert ok({"k": "c"}, schema)  # no branch applies
    assert check({"k": "a"}, schema, schema) == ["$: missing 'x'"]
    assert ok({"k": "a"}, {"if": {"properties": {"k": {"const": "a"}}}})  # if alone: no-op


# --- errors and the command line --------------------------------------------------------


def test_capped():
    assert capped(["e"] * 3, 5) == ["e"] * 3
    assert capped([str(i) for i in range(30)], 2) == ["0", "1", "... and 28 more errors"]


def _cli(tmp_path, capsys, text: str, max_chars=None) -> tuple[int, str]:
    f = tmp_path / "in.json"
    f.write_text(text, encoding="utf-8")
    code = json_contract.main([str(f)], lambda v: [] if v == {} else ["bad"], "thing", max_chars)
    return code, capsys.readouterr().out


def test_main(tmp_path, capsys):
    assert _cli(tmp_path, capsys, "{}") == (0, "valid\n")
    assert _cli(tmp_path, capsys, "[]") == (2, "bad\n")
    assert _cli(tmp_path, capsys, "{}  ", max_chars=2)[0] == 2
    assert _cli(tmp_path, capsys, "{")[0] == 1


def test_main_survives_hostile_input(tmp_path, capsys):
    code, out = _cli(tmp_path, capsys, "[" * 100_000)
    assert code == 1 and "RecursionError" in out
    f = tmp_path / "latin1.json"
    f.write_bytes(b'{"a": "\xe9"}')
    assert json_contract.main([str(f)], lambda v: [], "thing") == 1
    assert "UnicodeDecodeError" in capsys.readouterr().out


def test_main_caps_errors(tmp_path, capsys):
    f = tmp_path / "in.json"
    f.write_text("{}", encoding="utf-8")
    json_contract.main([str(f)], lambda v: ["e"] * 100, "thing")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == json_contract.MAX_ERRORS + 1
    assert lines[-1] == "... and 80 more errors"
