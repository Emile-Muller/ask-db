"""PreToolUse hook for the sql-proposer agent: ``python -m text2sql.proposer_guard``.

The proposer gets one thing: the layer-2 prompt that ``context --brief`` (with
``--question`` or ``--question-id``) wrote to ``questions/logs/prompts/<32 hex>.txt``.
It may make exactly two kinds of call, so that nothing passes through the orchestrator as
text in either direction (the orchestrator never copies, and so can never alter, the
prompt or the reply):

- a Read of that prompt file;
- a Write of its JSON reply to ``<PROPOSALS_DIR>/<hex>.json``, the paths ``context --brief``
  printed (``proposal_file``, and ``retry_proposal_file`` = ``<hex>-2.json`` for the one
  ``sql_error`` retry). ``<hex>.json`` needs its prompt ``<hex>.txt`` to exist, ``<hex>-2.json``
  needs ``<hex>.json`` to exist (the prompt is deleted after the first run), and neither may
  exist yet: each reply is written once and never overwritten. The content must be a string
  of at most ``config.MAX_PROPOSAL_CHARS``; ``run --proposal-file`` checks its shape.

Every other tool call is denied: any other tool, any other path (resolved, so ``..`` and
symlinks cannot leave the folder), a directory, a file that is not a prompt. The proposer
cannot see the repo, the test questions, their expected outcomes or other runs. Reads the
hook input (JSON) on stdin and prints a PreToolUse decision. Fails closed: an unreadable
input or an internal error blocks the call (exit 2).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from text2sql import config

PROMPT_NAME = re.compile(r"[0-9a-f]{32}\.txt")
PROPOSAL_NAME = re.compile(r"(?P<hex>[0-9a-f]{32})(?P<retry>-2)?\.json")


def proposal_paths(prompt_file: Path, proposals_dir: Path | None = None) -> tuple[Path, Path]:
    """The (first, retry) reply paths the proposer of ``prompt_file`` may write."""
    proposals_dir = Path(proposals_dir or config.PROPOSALS_DIR).resolve()
    hex_id = Path(prompt_file).stem
    return proposals_dir / f"{hex_id}.json", proposals_dir / f"{hex_id}-2.json"


def _decide_read(path: Path, prompts_dir: Path) -> tuple[bool, str]:
    if path.parent != prompts_dir or not PROMPT_NAME.fullmatch(path.name):
        return False, "the proposer may only Read the prompt file named in its task"
    if not path.is_file():
        return False, "no such prompt file"
    return True, "prompt file"


def _decide_write(
    path: Path, content: Any, prompts_dir: Path, proposals_dir: Path
) -> tuple[bool, str]:
    m = PROPOSAL_NAME.fullmatch(path.name)
    if path.parent != proposals_dir or not m:
        return False, "the proposer may only Write the proposal file named in its task"
    if not isinstance(content, str) or len(content) > config.MAX_PROPOSAL_CHARS:
        return False, f"the reply must be text of at most {config.MAX_PROPOSAL_CHARS} characters"
    if path.exists() or path.is_symlink():
        return False, "this proposal was already written; each reply is written once"
    if m["retry"]:
        if not (proposals_dir / f"{m['hex']}.json").is_file():
            return False, "a retry proposal needs the first proposal of the same run"
        return True, "retry proposal file"
    if not (prompts_dir / f"{m['hex']}.txt").is_file():
        return False, "no prompt for this proposal file"
    return True, "proposal file"


def decide(
    tool_name: str,
    tool_input: Any,
    prompts_dir: Path | None = None,
    proposals_dir: Path | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason) for one tool call made by the proposer."""
    prompts_dir = Path(prompts_dir or config.PROMPTS_DIR).resolve()
    proposals_dir = Path(proposals_dir or config.PROPOSALS_DIR).resolve()
    if tool_name not in ("Read", "Write"):
        return False, (
            f"the proposer may not use {tool_name}; it may only Read its prompt file "
            "and Write its proposal file"
        )
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(path, str) or not path:
        return False, "no file path given"
    resolved = Path(path).resolve()
    if tool_name == "Read":
        return _decide_read(resolved, prompts_dir)
    return _decide_write(resolved, tool_input.get("content"), prompts_dir, proposals_dir)


def main() -> int:
    try:
        event = json.load(sys.stdin)
        allowed, reason = decide(event.get("tool_name", ""), event.get("tool_input", {}))
    except Exception as e:  # fail closed  # noqa: BLE001
        print(
            f"proposer_guard: blocked, could not check the call: {type(e).__name__}",
            file=sys.stderr,
        )
        return 2
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if allowed else "deny",
        "permissionDecisionReason": f"proposer_guard: {reason}",
    }
    print(json.dumps({"hookSpecificOutput": decision}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
