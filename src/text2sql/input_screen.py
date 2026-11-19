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
_SQL_START = re.compile(
    r"^\(*\s*(select|with|insert|update|delete|drop|alter|create|pragma|attach|copy)\b", re.I
)
# A second SQL signal after the leading keyword, so that English such as
# "Select the top 5 sellers" or "With which payment type ..." stays natural language.
_SQL_BODY = re.compile(
    r"^\s*\d|[*()='\"]|\b(from|where|table|into|set|values|join|group\s+by|order\s+by|limit|"
    r"as|database|extension|returning)\b",
    re.I,
)
_SQL_KEYWORD = re.compile(
    r"\b(select|insert|update|delete|drop|alter|create|pragma|attach|copy|from|table)\b", re.I
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


def classify(text: str) -> str:
    if (start := _SQL_START.match(text)) and _SQL_BODY.search(text[start.end() :]):
        return RAW_SQL
    if ";" in text and _SQL_KEYWORD.search(text):
        return RAW_SQL
    return NATURAL_LANGUAGE


def detect_injection(text: str) -> tuple[list[str], list[str]]:
    """Return (strong flags, weak flags) matched in ``text``."""
    strong = [name for name, rx, is_strong in _PATTERNS if is_strong and rx.search(text)]
    weak = [name for name, rx, is_strong in _PATTERNS if not is_strong and rx.search(text)]
    return strong, weak


def screen(text: str, max_chars: int = config.MAX_INPUT_CHARS) -> ScreenResult:
    raw = text if isinstance(text, str) else ""
    normalized = normalize(raw)
    input_type = classify(normalized)
    strong, weak = detect_injection(normalized)
    flags = strong + weak
    result = ScreenResult(True, normalized, input_type, flags, None, len(raw))
    if not normalized:
        result.ok, result.reason = False, "empty input"
    elif len(normalized) > max_chars:
        result.ok, result.reason = False, f"input too long ({len(normalized)} > {max_chars} chars)"
    elif strong:
        result.ok, result.reason = False, f"possible prompt injection: {', '.join(strong)}"
    return result
