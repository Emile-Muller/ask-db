"""PreToolUse hook for question runs: ``python -m text2sql.edit_guard``.

Declared in the frontmatter of the ``/question``, ``/question-review`` and
``/question-batch`` skills. Claude Code registers a skill's hooks when the skill is
invoked and keeps them for the rest of that session, so once a question run starts, the
session can no longer edit the files that judge it: the allowlist, the red-team corpus, the
test questions, the JSON contracts, the question skills and agent, the settings, and the
guards themselves. A session that never runs a question skill is not restricted.

Only file-editing tools are checked (``FILE_TOOLS``). Other tools, and edits to any other
path, get no decision, so the normal permission flow applies (this hook never says
"allow"). Paths are resolved (``..`` and symlinks) against the project root found from
this file's location, not from the environment. Fails closed: an unreadable input, an
edit call without a path, or an internal error blocks the call (exit 2).
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path
from typing import Any

from text2sql import config

FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Project-relative POSIX globs; "*" also matches "/" (fnmatch), so "dir/*" is the whole tree.
FROZEN = (
    "data/schema.yaml",
    "tests/redteam_corpus.yaml",
    "questions/questions.yaml",
    "src/text2sql/schemas/*",
    "src/text2sql/edit_guard.py",
    "src/text2sql/review_guard.py",
    "src/text2sql/proposer_guard.py",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/skills/question/*",
    ".claude/skills/question-review/*",
    ".claude/skills/question-batch/*",
    ".claude/agents/question-reviewer.md",
    ".claude/agents/sql-proposer.md",
)


def frozen_match(
    path: str | Path, cwd: str | Path | None = None, root: Path | None = None
) -> str | None:
    """Return the FROZEN pattern that ``path`` falls under, or None."""
    root = (root or config.PROJECT_ROOT).resolve()
    p = Path(path)
    if not p.is_absolute():
        p = Path(cwd or root) / p
    try:
        rel = p.resolve().relative_to(root)
    except ValueError:
        return None  # outside the project
    rel_s = os.path.normcase(rel.as_posix())
    for pattern in FROZEN:
        if fnmatch.fnmatchcase(rel_s, os.path.normcase(pattern)):
            return pattern
    return None


def decide(tool_name: str, tool_input: dict[str, Any], cwd: str | None = None) -> str | None:
    """Return a deny reason for this call, or None to leave it to the normal permission flow."""
    if tool_name not in FILE_TOOLS:
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(path, str) or not path:
        return f"{tool_name} call without a file path"
    if pattern := frozen_match(path, cwd):
        return (
            f"{path} is frozen during question runs ({pattern}); "
            "edit it in a session that has not run /question, /question-review or /question-batch"
        )
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        tool_input = event.get("tool_input", {})
        if not isinstance(tool_input, dict):
            raise TypeError("tool_input is not an object")
        reason = decide(event.get("tool_name", ""), tool_input, event.get("cwd"))
    except Exception as e:  # fail closed  # noqa: BLE001
        print(f"edit_guard: blocked, could not check the call: {type(e).__name__}", file=sys.stderr)
        return 2
    if reason is None:
        return 0  # no decision: the normal permission rules apply
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"edit_guard: {reason}",
    }
    print(json.dumps({"hookSpecificOutput": decision}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
