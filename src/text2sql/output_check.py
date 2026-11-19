"""Layer 5: output check on the rows DuckDB returned.

- Drops every result column whose name matches a restricted column. If nothing is
  left, the result is refused.
- Masks email addresses and phone numbers in string cells, and restricted keys inside
  nested values (structs / maps).
- Keeps a preview of at most PREVIEW_ROWS rows for the trace, with row_count and truncated.
- Converts every cell to a JSON-safe value.

Defense in depth: the validator should already have rejected restricted columns, so a
drop here is worth investigating.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from text2sql import config

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Optional country code, 2-3 digit area code, then 3-5 + 4 digits (Brazilian, US and most
# international formats). Not preceded/followed by a word character, so hex ids and
# longer numbers do not match.
PHONE_RE = re.compile(
    r"(?<![\w+])(?:\+?\d{1,3}[\s.-]?)?\(?\d{2,3}\)?[\s.-]?\d{3,5}[\s.-]?\d{4}(?!\w)"
)
EMAIL_MASK = "[email]"
PHONE_MASK = "[phone]"
RESTRICTED_MASK = "[restricted]"


@dataclass
class OutputCheck:
    ok: bool
    columns: list[str]
    rows: list[list[Any]]  # preview rows, JSON-safe
    row_count: int
    truncated: bool
    dropped_columns: list[str] = field(default_factory=list)
    masked_cells: int = 0
    reason: str | None = None

    def result(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
        }

    def details(self) -> dict[str, Any]:
        return {
            "dropped_columns": self.dropped_columns,
            "masked_cells": self.masked_cells,
            "preview_rows": len(self.rows),
            "row_count": self.row_count,
            "truncated": self.truncated,
        }


def check(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    restricted_names: Iterable[str] | None = None,
    preview_rows: int = config.PREVIEW_ROWS,
) -> OutputCheck:
    restricted = {n.lower() for n in (restricted_names or config.load_policy().restricted_names)}
    keep = [i for i, c in enumerate(columns) if c.lower() not in restricted]
    dropped = [c for c in columns if c.lower() in restricted]
    all_rows = list(rows)
    out = OutputCheck(
        ok=True,
        columns=[columns[i] for i in keep],
        rows=[],
        row_count=len(all_rows),
        truncated=len(all_rows) > preview_rows,
        dropped_columns=dropped,
    )
    if columns and not keep:
        out.ok, out.reason = False, f"every result column is restricted: {', '.join(dropped)}"
        return out
    masker = _Masker(restricted)
    out.rows = [[masker.clean(row[i]) for i in keep] for row in all_rows[:preview_rows]]
    out.masked_cells = masker.count
    return out


class _Masker:
    def __init__(self, restricted: set[str]) -> None:
        self.restricted = restricted
        self.count = 0

    def clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._mask_text(value)
        if isinstance(value, dict):
            return {str(k): self._clean_item(str(k), v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self.clean(v) for v in value]
        return to_jsonable(value)

    def _clean_item(self, key: str, value: Any) -> Any:
        if key.lower() in self.restricted:
            self.count += 1
            return RESTRICTED_MASK
        return self.clean(value)

    def _mask_text(self, text: str) -> str:
        masked = PHONE_RE.sub(PHONE_MASK, EMAIL_RE.sub(EMAIL_MASK, text))
        if masked != text:
            self.count += 1
        return masked


def to_jsonable(value: Any) -> Any:
    """Scalar DuckDB value -> JSON-safe value."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)
