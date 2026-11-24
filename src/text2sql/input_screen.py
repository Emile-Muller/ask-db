"""Layer 1: input screen.

Normalizes the raw input, enforces a length limit, classifies it as a natural-language
question or raw SQL, and flags likely prompt-injection attempts.

This layer is HEURISTIC DEFENSE-IN-DEPTH, NOT the security boundary. Regexes cannot
recognize every injection, and a question that passes here is still untrusted. Whatever
SQL comes out of the pipeline is decided by the validator (layer 3) and the sandboxed
executor (layer 4), which must hold even if this layer is bypassed entirely.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from text2sql import config

NATURAL_LANGUAGE = "natural_language"
RAW_SQL = "raw_sql"

# Unicode categories removed before anything else: controls, format characters
# (zero-width spaces/joiners, bidi overrides), surrogates and private-use characters.
_STRIP_CATEGORIES = {"Cc", "Cf", "Cs", "Co"}

# --- classification -----------------------------------------------------------------
# Misclassifying either way is safe (the validator judges every SQL text on both paths),
# but it changes which layer explains the refusal, so the shapes below aim to be precise.
_QUERY_START = re.compile(r"^\(*\s*(select|with)\b", re.I)
# A second SQL signal after SELECT / WITH, so that English such as
# "Select the top 5 sellers" or "With which payment type ..." stays natural language.
_SQL_BODY = re.compile(
    r"^\s*\d|[*()='\"]|\b(from|where|table|into|set|values|join|group\s+by|order\s+by|limit|"
    r"as|database|extension|returning)\b",
    re.I,
)
# Apostrophes inside English contractions ("don't", "seller's") are not string quotes.
_CONTRACTION = re.compile(r"(?<=[a-z])'(?=[a-z])", re.I)
# Other statements must start with their real SQL shape: "DROP TABLE x" is SQL, while
# "Drop the reviews table, ..." is English (and is judged by the injection heuristics).
_OBJECT = r"(?:table|view|schema|database|index|sequence|macro|function|type|secret)\b"
_ENGLISH_DETERMINER = r"(?:the|a|an|all|any|every|each|my|our|your|this|that|these|those)\b"
_STATEMENT_START = re.compile(
    r"^\(*\s*(?:"
    rf"drop\s+(?:temp(?:orary)?\s+)?{_OBJECT}|"
    rf"create\s+(?:or\s+replace\s+)?(?:(?:temp|temporary|persistent|unique)\s+)?{_OBJECT}|"
    rf"alter\s+{_OBJECT}|"
    r"insert\s+(?:or\s+(?:replace|ignore)\s+)?into\b|"
    r"delete\s+from\b|"
    rf"update\s+(?!{_ENGLISH_DETERMINER})[\w.\"]+\s+set\b|"
    r"pragma\s+\w+|"
    r"attach\s+(?:database\s+)?(?:if\s+not\s+exists\s+)?['\"]|"
    rf"copy\s+(?:\(|(?!{_ENGLISH_DETERMINER})[\w.\"]+\s+(?:to|from)\b)"
    r")",
    re.I,
)

# --- prompt-injection heuristics ----------------------------------------------------
# (flag, regex, strong). A strong match refuses the input; weak matches are only recorded.
_W = r"(?:\W+\w+){0,4}?\W+"  # up to four filler words between two key words
_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = tuple(
    (name, re.compile(rx, re.I), strong)
    for name, rx, strong in (
        (
            "ignore_instructions",
            rf"\b(ignore|disregard|forget|override|bypass|skip)\b{_W}"
            r"(instructions?|rules?|prompts?|directions?|guidelines?|guardrails?|context)\b",
            True,
        ),
        ("system_prompt", r"\b(system|developer|hidden|initial)\s+(prompt|message)s?\b", True),
        (
            "reveal_instructions",
            rf"\b(reveal|show|print|repeat|output|leak|display|tell\s+me)\b{_W}"
            r"(instructions?|system\s+prompt|prompt|guidelines|rules\s+you)\b",
            True,
        ),
        (
            "role_override",
            r"\byou\s+are\s+(now|no\s+longer)\b|\bfrom\s+now\s+on\b|"
            r"\bpretend\s+(to\s+be|you\s+are|that)\b|\bact\s+as\s+(an?|the|my|if)\b|"
            r"\brole[\s-]?play\b|\bdeveloper\s+mode\b|\bjailbreak|\bdo\s+anything\s+now\b",
            True,
        ),
        (
            "new_instructions",
            r"\bnew\s+(instructions?|rules|task|role)\b|"
            r"\b(admin|administrator|developer|system|root)\s+"
            r"(override|command|instructions?|message|note|access)\b",
            True,
        ),
        (
            # Claims of authority or requests for more of it ("I'm the admin", "grant me
            # write access"). A user's claimed role never changes what the pipeline allows.
            "authority_claim",
            r"\b(?:i\s*['’]?\s*a?m|i\s+am)\s+(?:now\s+|really\s+|actually\s+)?"
            r"(?:the\s+|an?\s+|your\s+)?(?:system\s+|database\s+|db\s+|site\s+|super\s*)?"
            r"(?:admin|administrator|sysadmin|dba|root|superuser|super\s+user|developer)\b|"
            r"\bas\s+(?:the|an?|your)\s+(?:system\s+|database\s+|db\s+)?"
            r"(?:admin|administrator|sysadmin|dba|root|superuser)\b|"
            r"\bi\s+(?:have|got|own)\s+(?:full\s+)?(?:admin|administrator|root|superuser|write|"
            r"full|elevated|special)\s+(?:access|rights|privileges?|permissions?|clearance)\b|"
            r"\b(?:i\s*['’]?\s*a?m|i\s+am)\s+(?:authori[sz]ed|allowed|permitted|cleared)\s+to\b|"
            r"\b(?:grant|give)\s+(?:me|us)\b(?:\W+\w+){0,2}?\W+"
            r"(?:access|rights|privileges?|permissions?)\b|"
            r"\b(?:elevate|escalate)\b(?:\W+\w+){0,3}?\W+(?:privileges?|permissions?|rights)\b|"
            r"\bsudo\b",
            True,
        ),
        (
            # Destructive commands written in plain English ("delete all tables", "wipe the
            # database"). SQL keywords are still matched by the weaker write_intent below.
            "destructive_command",
            r"\b(?:delete|drop|remove|erase|wipe|truncate|destroy|purge|nuke|trash)\b"
            r"(?:\W+\w+){0,3}?\W+(?:tables?|databases?|db|schemas?|views?|indexes|everything|"
            r"all\s+(?:the\s+|of\s+the\s+)?(?:data|rows|records|entries))\b|"
            r"\b(?:wipe|erase|purge|destroy|nuke)\b(?:\W+\w+){0,2}?\W+(?:out|clean|data)\b",
            True,
        ),
        (
            "fake_delimiter",
            r"</?\s*(question|system|user|assistant|instructions?|untrusted\w*)\s*>|"
            r"<<<|>>>|untrusted_question|\[/?(inst|system)\]",
            True,
        ),
        (
            "secrets",
            r"\b(api[\s_-]?keys?|passwords?|secrets?|tokens?|credentials?)\b|\.env\b",
            False,
        ),
        (
            "file_access",
            r"\b(read_csv\w*|read_parquet|read_text|read_json\w*|glob|getenv)\b|"
            r"/etc/|file://|\b[a-z]:[\\/]",
            False,
        ),
        (
            "write_intent",
            r"\b(drop|truncate|delete\s+from|insert\s+into|alter\s+table)\b|\bupdate\s+\w+\s+set\b",
            False,
        ),
    )
)


@dataclass
class ScreenResult:
    ok: bool
    normalized: str
    input_type: str
    flags: list[str] = field(default_factory=list)
    reason: str | None = None
    original_length: int = 0

    def details(self) -> dict[str, Any]:
        d = asdict(self)
        d["length"] = len(self.normalized)
        return d


def normalize(text: str) -> str:
    """NFKC, drop control / zero-width / format characters, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(
        " " if ch in "\t\n\r\v\f" else ch
        for ch in text
        if ch in "\t\n\r\v\f" or unicodedata.category(ch) not in _STRIP_CATEGORIES
    )
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_statement(segment: str) -> bool:
    segment = segment.strip()
    if start := _QUERY_START.match(segment):
        return bool(_SQL_BODY.search(_CONTRACTION.sub("", segment[start.end() :])))
    return bool(_STATEMENT_START.match(segment))


def classify(text: str) -> str:
    """RAW_SQL if the text, or any ``;``-separated part of it, starts like a SQL statement."""
    if any(_looks_like_statement(segment) for segment in text.split(";")):
        return RAW_SQL
    return NATURAL_LANGUAGE


# Patterns aimed at plain English only. On raw SQL, write statements are left to the
# validator (layer 3), which refuses them with a precise check name.
_NATURAL_LANGUAGE_ONLY = frozenset({"destructive_command"})


def detect_injection(text: str, input_type: str = NATURAL_LANGUAGE) -> tuple[list[str], list[str]]:
    """Return (strong flags, weak flags) matched in ``text``."""
    active = [
        (name, rx, is_strong)
        for name, rx, is_strong in _PATTERNS
        if input_type == NATURAL_LANGUAGE or name not in _NATURAL_LANGUAGE_ONLY
    ]
    strong = [name for name, rx, is_strong in active if is_strong and rx.search(text)]
    weak = [name for name, rx, is_strong in active if not is_strong and rx.search(text)]
    return strong, weak


def screen(text: str, max_chars: int = config.MAX_INPUT_CHARS) -> ScreenResult:
    raw = text if isinstance(text, str) else ""
    normalized = normalize(raw)
    input_type = classify(normalized)
    strong, weak = detect_injection(normalized, input_type)
    flags = strong + weak
    result = ScreenResult(True, normalized, input_type, flags, None, len(raw))
    if not normalized:
        result.ok, result.reason = False, "empty input"
    elif len(normalized) > max_chars:
        result.ok, result.reason = False, f"input too long ({len(normalized)} > {max_chars} chars)"
    elif strong:
        result.ok, result.reason = False, f"possible prompt injection: {', '.join(strong)}"
    return result
