"""Review log (``python -m text2sql review``) and the reviewer's shell guard.

Offline: no database, no LLM.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from text2sql import cli, config, review_guard, review_log

ACCEPTED_ID = "a" * 32
REFUSED_ID = "b" * 32
RAW_ID = "c" * 32


def _trace(tid, **kw):
    t = {
        "id": tid, "timestamp": "2026-11-19T10:00:00+00:00", "input": "top 5 categories?",
        "input_type": "natural_language", "verdict": "accepted", "refused_by": None,
        "reason": None, "stages": [], "generated_sql": "SELECT 1", "normalized_sql": "SELECT 1",
        "result": None, "meta": {"model": "m", "git_commit": "abc1234"},
    }  # fmt: skip
    t.update(kw)
    return t


@pytest.fixture()
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    lines = [
        "not json",
        json.dumps(_trace(ACCEPTED_ID)),
        json.dumps(_trace(REFUSED_ID, verdict="refused", refused_by="generation",
                          generated_sql=None, normalized_sql=None)),
        json.dumps(_trace(RAW_ID, input_type="raw_sql", verdict="refused",
                          refused_by="validator", generated_sql=None, normalized_sql=None)),
    ]  # fmt: skip
    (d / "20261119.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d


def payload(**kw):
    p = {
        "trace_id": ACCEPTED_ID, "verdict": "correct", "confidence": "high",
        "findings": {
            "logic": [{"issue": "no DISTINCT", "impact": "double counts", "fix": None}],
            "data": [{"issue": "zero row", "impact": "suspicious"}],
            "unverifiable": ["rows past the preview"],
            "assumptions": [{"assumption": "revenue = price", "reasonable": True}],
        },
        "corrected_sql": None, "injection_attempt": False, "summary": "Looks right.",
        "model": "claude-opus-5-5",
    }  # fmt: skip
    p.update(kw)
    return p


def log(runs_dir, out, p):
    return review_log.log_review(json.dumps(p), runs_dir, out)


def test_logs_one_complete_record(runs_dir, tmp_path):
    out = tmp_path / "reviews.jsonl"
    record, path = log(runs_dir, out, payload())
    assert path == out
    assert json.loads(out.read_text(encoding="utf-8")) == record
    assert len(record["id"]) == 32 and record["id"] != ACCEPTED_ID
    assert record["timestamp"].endswith("+00:00")
    assert record["trace_id"] == ACCEPTED_ID
    assert record["trace_timestamp"] == "2026-11-19T10:00:00+00:00"
    assert record["input"] == "top 5 categories?" and record["trace_verdict"] == "accepted"
    assert record["review_type"] == "answer" and record["normalized_sql"] == "SELECT 1"
    assert record["meta"]["model"] == "claude-opus-5-5" and record["meta"]["git_commit"]


@pytest.mark.parametrize(
    ("tid", "review_type"), [(ACCEPTED_ID, "answer"), (REFUSED_ID, "refusal"), (RAW_ID, "sql_only")]
)
def test_review_type_comes_from_the_trace(runs_dir, tmp_path, tid, review_type):
    record, _ = log(runs_dir, tmp_path / "r.jsonl", payload(trace_id=tid))
    assert record["review_type"] == review_type


def test_appends_and_keeps_earlier_lines(runs_dir, tmp_path):
    out = tmp_path / "reviews.jsonl"
    first, _ = log(runs_dir, out, payload())
    before = out.read_text(encoding="utf-8")
    second, _ = log(runs_dir, out, payload(verdict="incorrect"))
    lines = out.read_text(encoding="utf-8").splitlines()
    assert out.read_text(encoding="utf-8").startswith(before)
    assert [json.loads(x)["id"] for x in lines] == [first["id"], second["id"]]


def test_default_path_is_under_question_reviews(runs_dir):
    from datetime import UTC, datetime

    path = review_log.default_review_path(datetime.now(UTC))
    assert path.parent.name == "question-reviews" and path.parent.parent.name == "logs"


def test_unknown_trace_is_rejected(runs_dir, tmp_path):
    with pytest.raises(review_log.ReviewError, match="no trace"):
        log(runs_dir, tmp_path / "r.jsonl", payload(trace_id="d" * 32))
    assert not (tmp_path / "r.jsonl").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"input": "spoofed question"},  # trace fields are looked up, never sent
        {"trace_verdict": "accepted"},
        {"review_type": "answer"},
        {"id": "e" * 32},
        {"trace_id": "A" * 32},
        {"trace_id": "a" * 31},
        {"verdict": "mostly fine"},
        {"confidence": 0.9},
        {"injection_attempt": "no"},
        {"summary": ""},
        {"summary": "x" * 2001},
        {"corrected_sql": 1},
        {"model": ["m"]},
        {"findings": []},
        {"findings": {"logic": [], "data": [], "unverifiable": []}},
        {"findings": {"logic": [{"issue": "x"}], "data": [], "unverifiable": [],
                      "assumptions": []}},
        {"findings": {"logic": [], "data": [], "unverifiable": [1], "assumptions": []}},
        {"findings": {"logic": [], "data": [], "unverifiable": ["x"] * 21, "assumptions": []}},
        {"findings": {"logic": [], "data": [], "unverifiable": [],
                      "assumptions": [{"assumption": "x", "reasonable": "yes"}]}},
    ],
)  # fmt: skip
def test_bad_input_is_rejected(runs_dir, tmp_path, change):
    with pytest.raises(review_log.ReviewError):
        log(runs_dir, tmp_path / "r.jsonl", payload(**change))
    assert not (tmp_path / "r.jsonl").exists()


def test_missing_model_is_logged_as_null(runs_dir, tmp_path):
    """model is optional: forgetting it must not cost the reviewer a rejected call."""
    p = payload()
    del p["model"]
    record, _ = log(runs_dir, tmp_path / "r.jsonl", p)
    assert record["meta"]["model"] is None


def test_missing_key_and_bad_json_are_rejected(runs_dir, tmp_path):
    p = payload()
    del p["summary"]
    with pytest.raises(review_log.ReviewError, match="missing"):
        log(runs_dir, tmp_path / "r.jsonl", p)
    for raw in ("{not json", "[]", "x" * 64_001):
        with pytest.raises(review_log.ReviewError):
            review_log.log_review(raw, runs_dir, tmp_path / "r.jsonl")


def test_cli_review_exit_codes(runs_dir, tmp_path, monkeypatch, capsys):
    out = tmp_path / "r.jsonl"
    argv = ["review", "--runs-dir", str(runs_dir), "--review-file", str(out)]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload())))
    assert cli.main(argv) == cli.EXIT_ACCEPTED
    assert json.loads(capsys.readouterr().out)["trace_id"] == ACCEPTED_ID
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload(verdict="?"))))
    assert cli.main(argv) == cli.EXIT_REFUSED
    assert "rejected" in capsys.readouterr().err
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1


# --- review_guard: the reviewer's PreToolUse hook --------------------------------------

GOOD = ".venv/Scripts/python.exe -m text2sql review <<'EOF'\n{\"a\": 1}\nEOF"


@pytest.mark.parametrize(
    "command",
    [
        GOOD,
        GOOD + "\n",
        "python -m text2sql review <<'EOF'\n{}\nEOF",
        "./.venv/Scripts/python -m text2sql review <<'JSON'\n{\n}\nJSON",
        '.venv/Scripts/python.exe -m text2sql review <<\'EOF\'\n{"s": "; rm -rf / $(x) `y`"}\nEOF',
    ],
)
def test_guard_allows_the_review_command(command):
    assert review_guard.decide("Bash", command)[0]


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        ".venv/Scripts/python.exe -m text2sql run --sql 'SELECT 1'",
        ".venv/Scripts/python.exe -m text2sql review",  # no heredoc
        ".venv/Scripts/python.exe -m text2sql review <<EOF\n{}\nEOF",  # unquoted: expanded
        '.venv/Scripts/python.exe -m text2sql review <<"EOF"\n{}\nEOF',
        ".venv/Scripts/python.exe -m text2sql review <<-'EOF'\n{}\nEOF",
        ".venv/Scripts/python.exe -m text2sql review <<'EOF'; ls\n{}\nEOF",
        ".venv/Scripts/python.exe -m text2sql review <<'EOF' && ls\n{}\nEOF",
        ".venv/Scripts/python.exe -m text2sql review <<'EOF'\n{}\nEOF\nls",  # after the end
        ".venv/Scripts/python.exe -m text2sql review <<'EOF'\n{}\nEOF\nrm x\nEOF",  # repeated
        ".venv/Scripts/python.exe -m text2sql review <<'EOF'\n{}\nEOF ; ls",
        ".venv/Scripts/python.exe -m text2sql review <<'EOF'\n{}",  # never closed
        ".venv/Scripts/python.exe -m text2sql review <<'EOF'\r\n{}\r\nEOF",
        "ls; .venv/Scripts/python.exe -m text2sql review <<'EOF'\n{}\nEOF",
        "C:/evil/python -m text2sql review <<'EOF'\n{}\nEOF",
        "",
        None,
    ],
)
def test_guard_denies_everything_else(command):
    assert not review_guard.decide("Bash", command)[0]


def test_guard_denies_other_shells():
    assert not review_guard.decide("PowerShell", GOOD)[0]


def _hook(stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed arguments
        [sys.executable, "-m", "text2sql.review_guard"],
        input=stdin, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip


@pytest.mark.parametrize(("command", "decision"), [(GOOD, "allow"), ("ls", "deny")])
def test_guard_hook_output(command, decision):
    p = _hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))
    assert p.returncode == 0
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == decision


def test_guard_hook_fails_closed():
    p = _hook("not json")
    assert p.returncode == 2 and "blocked" in p.stderr


# In-process runs of main(), so the hook entry point itself is covered.


def _main(monkeypatch, stdin: str) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return review_guard.main()


@pytest.mark.parametrize(
    ("event", "decision", "reason"),
    [
        ({"tool_name": "Bash", "tool_input": {"command": GOOD}}, "allow", "review log command"),
        ({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "deny", "the only command"),
        ({"tool_name": "Read", "tool_input": {"file_path": "x"}}, "deny", "may not use Read"),
        ({"tool_name": "Bash", "tool_input": {}}, "deny", "no command given"),
        ({"tool_name": "Bash"}, "deny", "no command given"),
        ({"tool_input": {"command": GOOD}}, "deny", "may not use"),
        ({"tool_name": "Bash", "tool_input": {"command": 42}}, "deny", "no command given"),
    ],
)
def test_guard_main_decision(monkeypatch, capsys, event, decision, reason):
    assert _main(monkeypatch, json.dumps(event)) == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == decision
    assert out["permissionDecisionReason"].startswith("review_guard: ")
    assert reason in out["permissionDecisionReason"]


@pytest.mark.parametrize(
    "stdin",
    [
        "",  # empty input
        "not json",
        "[]",  # not an object: no .get
        '"Bash"',
        json.dumps({"tool_name": "Bash", "tool_input": ["ls"]}),  # tool_input not an object
        json.dumps({"tool_name": ["Bash"], "tool_input": {"command": GOOD}}),  # unhashable
    ],
)
def test_guard_main_fails_closed(monkeypatch, capsys, stdin):
    assert _main(monkeypatch, stdin) == 2
    captured = capsys.readouterr()
    assert captured.out == ""  # no decision printed, so nothing can read as "allow"
    assert "review_guard: blocked" in captured.err


def test_guard_main_fails_closed_on_internal_error(monkeypatch, capsys):
    def boom(*_args):
        raise RuntimeError("bug")

    monkeypatch.setattr(review_guard, "decide", boom)
    event = {"tool_name": "Bash", "tool_input": {"command": GOOD}}
    assert _main(monkeypatch, json.dumps(event)) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "RuntimeError" in captured.err


# runpy warns because the module is already imported above; harmless here.
@pytest.mark.filterwarnings("ignore:'text2sql.review_guard' found in sys.modules:RuntimeWarning")
def test_guard_module_entry_point(monkeypatch, capsys):
    import runpy

    event = {"tool_name": "Bash", "tool_input": {"command": GOOD}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("text2sql.review_guard", run_name="__main__")
    assert exc.value.code == 0
    assert '"allow"' in capsys.readouterr().out


def _agent_hook_command() -> str:
    text = (config.PROJECT_ROOT / ".claude" / "agents" / "question-reviewer.md").read_text(
        encoding="utf-8"
    )
    front = yaml.safe_load(text.split("---")[1])
    return front["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def _find_bash() -> str | None:
    """A POSIX bash like the one Claude Code runs hooks with.

    On Windows, `shutil.which("bash")` can return the WSL launcher in System32, which fails
    when no Linux distribution is installed: use Git Bash (next to `git`) instead.
    """
    if os.name != "nt":
        return shutil.which("bash")
    candidates = []
    if git := shutil.which("git"):
        git_root = Path(git).resolve().parent.parent  # <Git>/cmd/git.exe or <Git>/bin/git.exe
        candidates.append(git_root / "bin" / "bash.exe")
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    candidates.append(Path(program_files) / "Git" / "bin" / "bash.exe")
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        candidates.insert(0, Path(found))
    return next((str(c) for c in candidates if c.is_file()), None)


BASH = _find_bash()


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_agent_hook_blocks_when_the_guard_cannot_start(tmp_path):
    """Exit 1 / 127 would not block the call: the hook command must turn them into 2."""
    p = subprocess.run(  # noqa: S603 - the hook command from the repo, fixed input
        [BASH, "-c", _agent_hook_command()],
        input="{}", capture_output=True, text=True, timeout=30, check=False,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)},  # no .venv there
    )  # fmt: skip
    assert p.returncode == 2 and "blocked" in p.stderr
