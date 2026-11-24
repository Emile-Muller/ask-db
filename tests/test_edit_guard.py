"""edit_guard: the PreToolUse hook that freezes the judging files during question runs."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from text2sql import config, edit_guard

ROOT = config.PROJECT_ROOT
SKILLS = ROOT / ".claude" / "skills"

FROZEN_FILES = [
    "data/schema.yaml",
    "tests/redteam_corpus.yaml",
    "questions/questions.yaml",
    "src/text2sql/schemas/question_trace.json",
    "src/text2sql/schemas/new.json",
    "src/text2sql/edit_guard.py",
    "src/text2sql/review_guard.py",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/skills/question/SKILL.md",
    ".claude/skills/question/scripts/validate.py",
    ".claude/skills/question-review/example.json",
    ".claude/skills/question-batch/SKILL.md",
    ".claude/agents/question-reviewer.md",
]
FREE_FILES = [
    "src/text2sql/validator.py",
    "tests/test_validator.py",
    "data/README.md",
    "questions/results.json",
    "showcase/results.json",
    ".claude/skills/other/SKILL.md",
    ".claude/agents/code-reviewer.md",
    "README.md",
]


@pytest.mark.parametrize("rel", FROZEN_FILES)
def test_frozen_paths_are_denied(rel):
    assert edit_guard.frozen_match(ROOT / rel)
    assert edit_guard.decide("Edit", {"file_path": str(ROOT / rel)})


@pytest.mark.parametrize("rel", FREE_FILES)
def test_other_paths_get_no_decision(rel):
    assert edit_guard.frozen_match(ROOT / rel) is None
    assert edit_guard.decide("Write", {"file_path": str(ROOT / rel)}) is None


@pytest.mark.parametrize(
    "path",
    [
        "data/schema.yaml",  # relative to cwd
        "src/../data/schema.yaml",  # dot-dot
        "data/./schema.yaml",
    ],
)
def test_relative_and_dotted_paths(path):
    assert edit_guard.decide("Edit", {"file_path": path}, cwd=str(ROOT))


def test_relative_path_from_subdirectory():
    assert edit_guard.decide("Edit", {"file_path": "../data/schema.yaml"}, cwd=str(ROOT / "src"))


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive file system")
def test_case_is_ignored_on_windows():
    assert edit_guard.decide("Edit", {"file_path": str(ROOT / "DATA" / "Schema.YAML")})


def test_outside_project_is_free(tmp_path):
    assert edit_guard.decide("Write", {"file_path": str(tmp_path / "manifest.json")}) is None


def test_symlink_into_frozen_file_is_denied(tmp_path):
    link = tmp_path / "link.yaml"
    try:
        link.symlink_to(ROOT / "data" / "schema.yaml")
    except OSError:
        pytest.skip("symlinks not available")
    assert edit_guard.decide("Edit", {"file_path": str(link)})


def test_notebook_path_is_checked():
    rel = ROOT / "src" / "text2sql" / "schemas" / "x.ipynb"
    assert edit_guard.decide("NotebookEdit", {"notebook_path": str(rel)})


@pytest.mark.parametrize("tool", ["Read", "Bash", "Grep", "Skill"])
def test_non_edit_tools_get_no_decision(tool):
    assert edit_guard.decide(tool, {"file_path": str(ROOT / "data" / "schema.yaml")}) is None


@pytest.mark.parametrize("tool_input", [{}, {"file_path": ""}, {"file_path": 42}])
def test_edit_without_path_is_denied(tool_input):
    assert "without a file path" in edit_guard.decide("Edit", tool_input)


# --- the hook entry point -------------------------------------------------------------


def _main(monkeypatch, stdin: str) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return edit_guard.main()


def test_main_denies_frozen(monkeypatch, capsys):
    event = {"tool_name": "Edit", "tool_input": {"file_path": str(ROOT / "data" / "schema.yaml")}}
    assert _main(monkeypatch, json.dumps(event)) == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"].startswith("edit_guard: ")


def test_main_never_says_allow(monkeypatch, capsys):
    event = {"tool_name": "Edit", "tool_input": {"file_path": str(ROOT / "README.md")}}
    assert _main(monkeypatch, json.dumps(event)) == 0
    assert capsys.readouterr().out == ""  # no decision: normal permission flow


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "[]",
        json.dumps({"tool_name": "Edit", "tool_input": ["x"]}),
        json.dumps({"tool_name": ["Edit"], "tool_input": {"file_path": "x"}}),
        json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "x"}, "cwd": 42}),
    ],
)
def test_main_fails_closed(monkeypatch, capsys, stdin):
    assert _main(monkeypatch, stdin) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "edit_guard: blocked" in captured.err


def test_hook_subprocess():
    event = {"tool_name": "Write", "tool_input": {"file_path": str(ROOT / "data" / "schema.yaml")}}
    p = subprocess.run(  # noqa: S603 - fixed arguments
        [sys.executable, "-m", "text2sql.edit_guard"],
        input=json.dumps(event), capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert p.returncode == 0
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- the skills actually declare the hook ---------------------------------------------

HOOK_RE = re.compile(
    r"^hooks:\n  PreToolUse:\n    - matcher: \"Edit\|Write\|MultiEdit\|NotebookEdit\"\n"
    r"(?:.*\n)*?\s+command: '.*-m text2sql\.edit_guard \|\| \{ .*exit 2; \}'$",
    re.MULTILINE,
)


@pytest.mark.parametrize("skill", ["question", "question-review", "question-batch"])
def test_question_skills_declare_edit_guard(skill):
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("---\n")[1]
    assert HOOK_RE.search(frontmatter), f"{skill} must declare the edit_guard hook"


def test_frozen_covers_every_question_skill():
    for d in SKILLS.iterdir():
        if d.name.startswith("question"):
            assert edit_guard.frozen_match(Path(d) / "SKILL.md"), d.name
