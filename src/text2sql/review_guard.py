"""PreToolUse hook for the question-reviewer agent: ``python -m text2sql.review_guard``.

The reviewer needs a shell for exactly one command, the review log writer, with its JSON
passed in a quoted heredoc:

    .venv/Scripts/python.exe -m text2sql review <<'EOF'
    {"trace_id": "...", ...}
    EOF

Every other shell call is denied, and so is anything that could run a second command:
an unquoted delimiter (the body would be expanded), the delimiter line repeated inside
the body (the shell would run what follows), or anything after the closing delimiter.
Reads the hook input (JSON) on stdin and prints a PreToolUse decision. Fails closed: an
unreadable input or an internal error blocks the call (exit 2).
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

SHELL_TOOLS = {"Bash"}
_PYTHON = r"(?:python|(?:\./)?\.venv/Scripts/python(?:\.exe)?)"
FIRST_LINE_RE = re.compile(_PYTHON + r" -m text2sql review <<'(?P<delim>[A-Z_]{1,20})'")


def decide(tool_name: str, command: Any) -> tuple[bool, str]:
    """Return (allowed, reason) for one tool call made by the reviewer."""
    if tool_name not in SHELL_TOOLS:
        return False, f"the reviewer may not use {tool_name}"
    if not isinstance(command, str):
        return False, "no command given"
    if "\r" in command or "\0" in command:
        return False, "carriage returns and NUL bytes are not allowed"
    lines = command.rstrip("\n").split("\n")
    first = FIRST_LINE_RE.fullmatch(lines[0])
    if first is None:
        return False, (
            "the only command the reviewer may run is "
            "`.venv/Scripts/python.exe -m text2sql review <<'EOF'` + JSON lines + `EOF`"
        )
    delim = first["delim"]
    if len(lines) < 3 or lines[-1] != delim:
        return False, f"the heredoc must end with a line that is exactly {delim}"
    if delim in lines[1:-1]:
        return False, f"the delimiter {delim} must not appear as a line inside the JSON"
    return True, "review log command"


def main() -> int:
    try:
        event = json.load(sys.stdin)
        allowed, reason = decide(
            event.get("tool_name", ""), event.get("tool_input", {}).get("command")
        )
    except Exception as e:  # fail closed  # noqa: BLE001
        print(
            f"review_guard: blocked, could not check the call: {type(e).__name__}", file=sys.stderr
        )
        return 2
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if allowed else "deny",
        "permissionDecisionReason": f"review_guard: {reason}",
    }
    print(json.dumps({"hookSpecificOutput": decision}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
