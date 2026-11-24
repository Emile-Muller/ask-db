"""JSON contract checker: a small JSON Schema subset, standard library only.

Used by ``review_log`` (the /question-review contract, ``schemas/question_review.json``)
and by the skills' ``scripts/validate.py`` (the /question report schema stays in its skill).

A schema cannot silently outgrow the checker: ``lint`` walks the WHOLE schema (every
property, item, ``$defs`` entry and ``if``/``then`` branch, reached or not) before any value
is checked, and raises ValueError on a keyword, type name or argument the checker does not
implement. ``check_schema`` always lints first.

Semantics differ from full JSON Schema on purpose, in the strict direction: ``enum`` and
``const`` also compare types (``0`` is not ``false``, ``1.0`` is not ``1``). One Python quirk
remains: in ``pattern``, ``$`` also matches before a final newline, so fixed-format strings
must also set ``minLength``/``maxLength``.

``main`` is the skills' common command line: it reads a file (or stdin), prints "valid"
and returns 0, or prints one error per line and returns 2 (1 on unreadable input).
"""

from __future__ import annotations

import functools
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

Schema = dict[str, Any]
Rule = Callable[[Any, Any, Schema, Schema, str], list[str]]

ANNOTATIONS = {"$schema", "$id", "$defs", "title", "description"}
TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "boolean": bool,
    "null": type(None),
}
MAX_ERRORS = 20  # errors reported per value; the rest are counted


def _is_type(value: Any, name: str) -> bool:
    if name not in TYPES:
        raise ValueError(f"unsupported type name: {name!r}")
    if isinstance(value, bool) != (name == "boolean"):  # bool is an int subclass in Python
        return False
    return isinstance(value, TYPES[name])


def _same(a: Any, b: Any) -> bool:
    """JSON equality that also compares types (so 0 != false and 1.0 != 1)."""
    return type(a) is type(b) and a == b


def _resolve(ref: str, root: Schema) -> Schema:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref (only local '#/...' refs): {ref!r}")
    node: Any = root
    for part in ref.removeprefix("#/").split("/"):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"unresolvable $ref: {ref!r}")
        node = node[part]
    if not isinstance(node, dict):
        raise ValueError(f"$ref does not point to a schema: {ref!r}")
    return node


def check(value: Any, schema: Schema, root: Schema, path: str = "$") -> list[str]:
    """Return the schema errors of ``value``; empty when it is valid.

    Call ``lint(root)`` first (``check_schema`` does): ``check`` only re-checks the keywords
    of the subschemas it visits.
    """
    if not isinstance(schema, dict):
        raise ValueError(f"schema at {path} must be an object")
    if unknown := set(schema) - ANNOTATIONS - KEYWORDS.keys() - {"then"}:
        raise ValueError(f"schema at {path} uses unsupported keywords: {sorted(unknown)}")
    errors: list[str] = []
    for key, arg in schema.items():
        if key in KEYWORDS:
            errors += KEYWORDS[key](value, arg, schema, root, path)
    return errors


def _type(v: Any, arg: Any, _s: Schema, _r: Schema, path: str) -> list[str]:
    names = [arg] if isinstance(arg, str) else arg
    return [] if any(_is_type(v, n) for n in names) else [f"{path}: expected {' or '.join(names)}"]


def _enum(v: Any, arg: Any, _s: Schema, _r: Schema, path: str) -> list[str]:
    return [] if any(_same(v, a) for a in arg) else [f"{path}: must be one of {arg}"]


def _const(v: Any, arg: Any, _s: Schema, _r: Schema, path: str) -> list[str]:
    return [] if _same(v, arg) else [f"{path}: must be {json.dumps(arg)}"]


def _required(v: Any, arg: Any, _s: Schema, _r: Schema, path: str) -> list[str]:
    return [f"{path}: missing {k!r}" for k in arg if isinstance(v, dict) and k not in v]


def _properties(v: Any, arg: Any, _s: Schema, root: Schema, path: str) -> list[str]:
    if not isinstance(v, dict):
        return []
    return [e for k, sub in arg.items() if k in v for e in check(v[k], sub, root, f"{path}.{k}")]


def _additional(v: Any, arg: Any, schema: Schema, _r: Schema, path: str) -> list[str]:
    if not isinstance(arg, bool):
        raise ValueError(f"schema at {path}: additionalProperties must be true or false")
    if arg or not isinstance(v, dict):
        return []
    return [f"{path}: unexpected {k!r}" for k in v if k not in schema.get("properties", {})]


def _items(v: Any, arg: Any, _s: Schema, root: Schema, path: str) -> list[str]:
    if not isinstance(v, list):
        return []
    return [e for i, item in enumerate(v) for e in check(item, arg, root, f"{path}[{i}]")]


def _min_items(v: Any, arg: Any, _s: Schema, _r: Schema, path: str) -> list[str]:
    bad = isinstance(v, list) and len(v) < arg
    return [f"{path}: at least {arg} items"] if bad else []


def _max_items(v: Any, arg: Any, _s: Schema, _r: Schema, path: str) -> list[str]:
    bad = isinstance(v, list) and len(v) > arg
    return [f"{path}: at most {arg} items"] if bad else []


def _min_length(v: Any, arg: Any, _s: Schema, _r: Schema, path: str) -> list[str]:
    bad = isinstance(v, str) and len(v) < arg
    return [f"{path}: at least {arg} characters"] if bad else []


def _max_length(v: Any, arg: Any, _s: Schema, _r: Schema, path: str) -> list[str]:
    bad = isinstance(v, str) and len(v) > arg
    return [f"{path}: at most {arg} characters"] if bad else []


def _minimum(v: Any, arg: Any, _s: Schema, _r: Schema, path: str) -> list[str]:
    bad = _is_type(v, "integer") and v < arg
    return [f"{path}: must be >= {arg}"] if bad else []


def _pattern(v: Any, arg: Any, _s: Schema, _r: Schema, path: str) -> list[str]:
    bad = isinstance(v, str) and not re.search(arg, v)
    return [f"{path}: must match {arg}"] if bad else []


def _ref(v: Any, arg: Any, _s: Schema, root: Schema, path: str) -> list[str]:
    return check(v, _resolve(arg, root), root, path)


def _all_of(v: Any, arg: Any, _s: Schema, root: Schema, path: str) -> list[str]:
    return [e for sub in arg for e in check(v, sub, root, path)]


def _if(v: Any, arg: Any, schema: Schema, root: Schema, path: str) -> list[str]:
    if check(v, arg, root, path) or "then" not in schema:
        return []
    return check(v, schema["then"], root, path)


KEYWORDS: dict[str, Rule] = {
    "type": _type,
    "enum": _enum,
    "const": _const,
    "required": _required,
    "properties": _properties,
    "additionalProperties": _additional,
    "items": _items,
    "minItems": _min_items,
    "maxItems": _max_items,
    "minLength": _min_length,
    "maxLength": _max_length,
    "minimum": _minimum,
    "pattern": _pattern,
    "$ref": _ref,
    "allOf": _all_of,
    "if": _if,
}


def _is_count(arg: Any) -> bool:
    return isinstance(arg, int) and not isinstance(arg, bool) and arg >= 0


def lint(root: Schema) -> None:
    """Raise ValueError unless every subschema of ``root`` uses only what ``check`` implements."""

    def walk(schema: Any, path: str) -> None:
        if not isinstance(schema, dict):
            raise ValueError(f"schema at {path} must be an object")
        keys = set(schema)
        if unknown := keys - ANNOTATIONS - KEYWORDS.keys() - {"then"}:
            raise ValueError(f"schema at {path} uses unsupported keywords: {sorted(unknown)}")
        if "then" in keys and "if" not in keys:
            raise ValueError(f"schema at {path}: 'then' without 'if'")
        if "type" in keys:
            names = [schema["type"]] if isinstance(schema["type"], str) else schema["type"]
            if not isinstance(names, list) or not names or set(names) - TYPES.keys():
                raise ValueError(f"schema at {path}: unsupported type {schema['type']!r}")
        if "enum" in keys and not (isinstance(schema["enum"], list) and schema["enum"]):
            raise ValueError(f"schema at {path}: enum must be a non-empty list")
        if "required" in keys and not (
            isinstance(schema["required"], list)
            and all(isinstance(k, str) for k in schema["required"])
        ):
            raise ValueError(f"schema at {path}: required must be a list of strings")
        if "additionalProperties" in keys and not isinstance(schema["additionalProperties"], bool):
            raise ValueError(f"schema at {path}: additionalProperties must be true or false")
        for key in ("minItems", "maxItems", "minLength", "maxLength"):
            if key in keys and not _is_count(schema[key]):
                raise ValueError(f"schema at {path}: {key} must be a non-negative integer")
        if "minimum" in keys and not (
            isinstance(schema["minimum"], int) and not isinstance(schema["minimum"], bool)
        ):
            raise ValueError(f"schema at {path}: minimum must be an integer")
        if "pattern" in keys:
            try:
                re.compile(schema["pattern"])
            except (re.error, TypeError) as exc:
                raise ValueError(f"schema at {path}: bad pattern: {exc}") from None
        if "$ref" in keys:
            _resolve(schema["$ref"], root)
        for name in ("properties", "$defs"):
            if name in keys:
                if not isinstance(schema[name], dict):
                    raise ValueError(f"schema at {path}: {name} must be an object")
                for k, sub in schema[name].items():
                    walk(sub, f"{path}.{name}.{k}")
        if "allOf" in keys:
            if not isinstance(schema["allOf"], list):
                raise ValueError(f"schema at {path}: allOf must be a list")
            for i, sub in enumerate(schema["allOf"]):
                walk(sub, f"{path}.allOf[{i}]")
        for name in ("items", "if", "then"):
            if name in keys:
                walk(schema[name], f"{path}.{name}")

    walk(root, "$")


@functools.cache
def load_schema(schema_path: Path) -> Schema:
    """Read and lint a schema file (once per path)."""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    lint(schema)
    return schema


def check_schema(value: Any, schema_path: Path) -> list[str]:
    """Return the errors of ``value`` against the schema file; empty when it is valid."""
    schema = load_schema(schema_path)
    return check(value, schema, schema)


def capped(errors: list[str], limit: int = MAX_ERRORS) -> list[str]:
    """The first ``limit`` errors, plus a count of the rest."""
    if len(errors) <= limit:
        return errors
    return [*errors[:limit], f"... and {len(errors) - limit} more errors"]


def main(
    argv: list[str],
    validate: Callable[[Any], list[str]],
    name: str,
    max_chars: int | None = None,
) -> int:
    """Validate the JSON in file ``argv[0]`` (stdin when absent) as a ``name``."""
    try:
        text = Path(argv[0]).read_text(encoding="utf-8") if argv else sys.stdin.read()
        if max_chars is not None and len(text) > max_chars:
            print(f"$: longer than {max_chars} characters")
            return 2
        value = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        print(f"cannot read {name}: {type(exc).__name__}: {exc}")
        return 1
    errors = capped(validate(value))
    print("\n".join(errors) if errors else "valid")
    return 2 if errors else 0
