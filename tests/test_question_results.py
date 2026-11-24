"""The batch results contract: src/text2sql/schemas/question_results.json plus
results.cross_checks, enforced by the writer (``python -m text2sql results``). Offline."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from text2sql import cli, config, json_contract, results, review_log, trace
from text2sql.trace import FAIL, PASS, SKIPPED, TraceBuilder

NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)

QUESTIONS = [
    {"id": "order_count", "category": "valid", "question": "How many orders?\n",
     "expect": "answer"},
    {"id": "drop_it", "category": "destructive", "question": "Drop the table.", "expect": "refuse"},
    {"id": "stacked", "category": "stacked_sql", "question": "SELECT 1;\nDROP TABLE x\n",
     "expect": "refuse"},
    {"id": "crash", "category": "runtime_error", "question": "SELECT 2", "expect": "refuse"},
    {"id": "lost", "category": "valid", "question": "Average price?", "expect": "answer"},
]  # fmt: skip


def _accepted(text: str) -> dict:
    b = TraceBuilder(text, "natural_language", model="claude-opus-5-5")
    for name in trace.LAYERS:
        b.record(name, PASS, {})
    b.generated_sql = "SELECT count(*) AS n FROM o"
    b.normalized_sql = 'SELECT COUNT(*) AS "n" FROM "o" LIMIT 100'
    return b.accepted({"columns": ["n"], "rows": [[99441]], "row_count": 1, "truncated": False})


def _refused_by_generation(text: str) -> dict:
    b = TraceBuilder(text, "natural_language", model="claude-opus-5-5")
    b.record("input_screen", PASS, {})
    b.record("generation", FAIL, {"refusal_reason": "a write", "model": "claude-opus-5-5"})
    return b.refused("generation", "a write")


def _refused_by_validator(text: str) -> dict:
    b = TraceBuilder(text, "raw_sql")
    b.record("input_screen", PASS, {})
    b.record("generation", SKIPPED, {"reason": "raw SQL input: generation not used"})
    b.record("validator", FAIL, {"checks": [], "reason": "two statements"})
    return b.refused("validator", "two statements")


def _errored(text: str) -> dict:
    b = TraceBuilder(text, "raw_sql")
    b.record("input_screen", PASS, {})
    b.record("generation", SKIPPED, {})
    b.record("validator", PASS, {})
    b.record("executor", FAIL, {"error": "RuntimeError: boom"})
    return b.error("executor", "internal error: RuntimeError: boom")


def _review(t: dict, verdict: str = "correct") -> dict:
    payload = {
        "trace_id": t["id"], "verdict": verdict, "confidence": "high",
        "findings": {"logic": [], "data": [], "unverifiable": [], "assumptions": []},
        "corrected_sql": None, "injection_attempt": False, "summary": "Fine.",
        "model": "claude-opus-5-5",
    }  # fmt: skip
    return review_log.build_record(payload, t, NOW)


def _jsonl(path: Path, records: list[dict]) -> None:
    lines = ["not json", *(json.dumps(r, ensure_ascii=False) for r in records)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture()
def logs(tmp_path):
    """Trace and review logs for QUESTIONS, plus the matching manifest."""
    traces = [
        _accepted("How many orders?"),
        _refused_by_generation("Drop the table."),
        _refused_by_validator("SELECT 1; DROP TABLE x"),  # whitespace differs from the yaml
        _errored("SELECT 2"),
    ]
    reviews = [_review(traces[0]), _review(traces[1]), _review(traces[2], "incorrect"),
               _review(traces[3], "cannot_tell")]  # fmt: skip
    runs, revs = tmp_path / "runs", tmp_path / "reviews"
    runs.mkdir()
    revs.mkdir()
    _jsonl(runs / "20261124.jsonl", traces)
    _jsonl(revs / "20261124.jsonl", reviews)
    manifest = [
        {"id": "order_count", "trace_id": traces[0]["id"], "review_id": reviews[0]["id"],
         "error": None},
        {"id": "drop_it", "trace_id": traces[1]["id"], "review_id": None, "error": None},
        {"id": "stacked", "trace_id": traces[2]["id"], "review_id": reviews[2]["id"],
         "error": None},
        {"id": "crash", "trace_id": traces[3]["id"], "review_id": None, "error": "run exited 1"},
        {"id": "lost", "trace_id": None, "review_id": None, "error": "no trace id returned"},
    ]  # fmt: skip
    return {"runs": runs, "reviews": revs, "manifest": manifest, "traces": traces}


def built(logs) -> dict:
    doc = results.build(logs["manifest"], QUESTIONS, logs["runs"], logs["reviews"], now=NOW)
    return json.loads(results.dumps(doc))


def errors(doc) -> list[str]:
    return results.schema_errors(doc, QUESTIONS)


# --- a valid document ------------------------------------------------------------------


def test_valid_document_passes(logs):
    doc = built(logs)
    assert errors(doc) == []
    assert [e["actual"] for e in doc["results"]] == ["answer", "refuse", "refuse", "error", "error"]
    assert [e["pass"] for e in doc["results"]] == [True, True, True, False, False]
    assert doc["summary"] == {
        "total": 5, "pass": 3, "fail": 2, "errors": 2, "failed_ids": ["crash", "lost"],
        "outcome_by_deciding_layer": {"accepted": 1, "generation": 1, "validator": 1, "error": 2},
        "review_verdicts": {"correct": 2, "incorrect": 1, "cannot_tell": 1},
    }  # fmt: skip


def test_records_are_verbatim_copies_of_the_logs(logs):
    doc = built(logs)
    assert doc["results"][0]["trace"] == logs["traces"][0]
    assert results.log_errors(doc, logs["runs"], logs["reviews"]) == []
    # The review of drop_it was found by its trace id (review_id null in the manifest).
    assert doc["results"][1]["review"]["trace_id"] == logs["traces"][1]["id"]


def test_error_entries_explain_what_failed(logs):
    by_id = {e["id"]: e for e in built(logs)["results"]}
    assert by_id["order_count"]["error"] is None
    assert by_id["crash"]["error"] == (
        "run exited 1; pipeline error at executor: internal error: RuntimeError: boom"
    )
    assert by_id["crash"]["review"]["verdict"] == "cannot_tell"
    assert by_id["lost"] == {
        "id": "lost", "category": "valid", "expect": "answer", "actual": "error", "pass": False,
        "error": "no trace id returned", "trace": None, "review": None,
    }  # fmt: skip


def test_unknown_trace_id_becomes_an_error_entry(logs):
    logs["manifest"][0]["trace_id"] = "f" * 32
    entry = built(logs)["results"][0]
    assert entry["trace"] is None and entry["actual"] == "error"
    assert "0 records in the trace log" in entry["error"]


def test_missing_review_becomes_an_error_entry(logs):
    logs["manifest"][1]["review_id"] = "e" * 32
    entry = built(logs)["results"][1]
    assert entry["review"] is None and entry["pass"] is True
    assert entry["error"] == f"review {'e' * 32}: 0 records in the review log, need 1"


def test_review_of_another_trace_becomes_an_error_entry(logs):
    other = built(logs)["results"][2]["review"]["id"]
    logs["manifest"][0]["review_id"] = other
    entry = built(logs)["results"][0]
    assert entry["review"] is None and "is for trace" in entry["error"]


def test_trace_of_another_question_is_refused(logs):
    logs["manifest"][0]["trace_id"] = logs["traces"][1]["id"]
    with pytest.raises(results.ResultsError, match="another input"):
        built(logs)


# --- the schema --------------------------------------------------------------------------


def test_schema_lints_and_refs_the_record_schemas():
    schema = json_contract.load_schema(results.SCHEMA_PATH)
    assert schema["$defs"]["entry"]["properties"]["trace"]["then"] == {
        "$ref": "question_trace.json"
    }
    text = results.SCHEMA_PATH.read_text(encoding="utf-8")
    assert '"question_review.json#/properties/findings"' in text


def edited(doc: dict, path: list, value) -> dict:
    doc = copy.deepcopy(doc)
    *parents, key = path
    node = doc
    for p in parents:
        node = node[p]
    if value is KeyError:
        del node[key]
    else:
        node[key] = value
    return doc


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["format"], 1),
        (["trace_log"], "questions/logs/questions/20261124.jsonl"),  # dropped in format 2
        (["results", 0, "question"], "How many orders?"),  # dropped: it is trace.input
        (["results", 0, "decided_by"], None),  # dropped: trace.refused_by + reason
        (["results", 0, "error"], KeyError),
        (["results", 4, "error"], None),  # no trace and no error
        (["results", 0, "trace", "meta"], KeyError),  # the trace contract applies
        (["results", 0, "review", "meta", "model"], ""),  # the review contract applies
        (["results", 0, "review", "verdict"], "fine"),
        (["summary", "outcome_by_deciding_layer", "executor"], 0),  # zeros are left out
    ],
)
def test_schema_rejects(logs, path, value):
    assert json_contract.check_schema(edited(built(logs), path, value), results.SCHEMA_PATH)


# --- cross-checks: one test per rule -----------------------------------------------------


def _swap_first_two(doc):
    doc["results"][0], doc["results"][1] = doc["results"][1], doc["results"][0]
    return doc


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: edited(d, ["summary", "pass"], 4), "$.summary"),
        (lambda d: edited(d, ["summary", "failed_ids"], ["crash"]), "$.summary"),
        (lambda d: edited(d, ["summary", "review_verdicts"], {"correct": 3}), "$.summary"),
        (lambda d: edited(d, ["results", 0, "pass"], False), "$.results[0].pass"),
        (lambda d: edited(d, ["results", 1, "actual"], "answer"), "$.results[1].actual"),
        (lambda d: edited(d, ["results", 4, "id"], "order_count"), "duplicate ids"),
        (lambda d: edited(d, ["results", 4, "id"], "renamed"), "one for one"),
        (_swap_first_two, "one for one"),
        (lambda d: edited(d, ["results", 0, "category"], "destructive"), "category"),
        (lambda d: edited(d, ["results", 1, "expect"], "answer"), "expect"),
        (lambda d: edited(d, ["results", 0, "trace", "input"], "Other?"), "trace.input"),
        (lambda d: edited(d, ["results", 0, "review", "trace_id"], "f" * 32), "review.trace_id"),
        (
            lambda d: edited(d, ["results", 2, "review", "input_type"], "natural_language"),
            "review.input_type",
        ),
        (
            lambda d: edited(d, ["results", 0, "review", "review_type"], "refusal"),
            "review.review_type",
        ),
        (
            lambda d: edited(d, ["results", 1, "trace", "refused_by"], "validator"),
            "$.results[1].trace.refused_by",
        ),
        (lambda d: edited(d, ["results", 3, "error"], None), "error must be set"),
        (
            lambda d: edited(d, ["results", 4, "review"], d["results"][0]["review"]),
            "a review without a trace",
        ),
    ],
)
def test_cross_checks_reject(logs, mutate, message):
    found = errors(mutate(built(logs)))
    assert any(message in e for e in found), found


def test_cross_checks_use_the_questions_file(logs):
    doc = built(logs)
    assert any("one for one" in e for e in results.schema_errors(doc, QUESTIONS[:-1]))


def test_log_errors_detect_an_edited_record(logs):
    doc = edited(built(logs), ["results", 0, "review", "summary"], "Edited.")
    assert results.log_errors(doc, logs["runs"], logs["reviews"]) == [
        "$.results[0].review: not a verbatim copy of its log line"
    ]


# --- manifest ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"id": "x"}',
        '[{"trace_id": null}]',
        '[{"id": "x", "extra": 1}]',
        '[{"id": "x", "trace_id": "ABC"}]',
        '[{"id": "x", "review_id": 5}]',
        '[{"id": "x", "error": 5}]',
    ],
)
def test_bad_manifest_is_refused(raw):
    with pytest.raises(results.ResultsError):
        results.load_manifest(raw)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m + [m[0]], "twice"),
        (lambda m: m[1:], "no entry"),
        (lambda m: m + [{"id": "extra", "trace_id": None}], "not in questions.yaml"),
    ],
)
def test_manifest_must_match_the_questions(logs, mutate, message):
    with pytest.raises(results.ResultsError, match=message):
        results.build(mutate(logs["manifest"]), QUESTIONS, logs["runs"], logs["reviews"])


# --- writer and CLI ----------------------------------------------------------------------


@pytest.fixture()
def questions_file(tmp_path):
    path = tmp_path / "questions.yaml"
    path.write_text(json.dumps({"questions": QUESTIONS}), encoding="utf-8")  # JSON is YAML
    return path


def _cli(logs, questions_file, *args):
    return cli.main([
        "results", "--questions", str(questions_file), "--runs-dir", str(logs["runs"]),
        "--reviews-dir", str(logs["reviews"]), *args,
    ])  # fmt: skip


def test_cli_writes_then_checks(logs, questions_file, tmp_path, capsys):
    ids, out = tmp_path / "ids.json", tmp_path / "results.json"
    ids.write_text(json.dumps(logs["manifest"]), encoding="utf-8")
    assert _cli(logs, questions_file, "--ids", str(ids), "--out", str(out)) == 0
    assert "3/5 pass, 2 errors" in capsys.readouterr().err
    assert errors(json.loads(out.read_text(encoding="utf-8"))) == []
    assert _cli(logs, questions_file, "--check", str(out), "--verify-logs") == 0
    assert capsys.readouterr().out.strip() == "valid"


def test_cli_reads_the_manifest_from_stdin(logs, questions_file, tmp_path, monkeypatch):
    import io

    out = tmp_path / "results.json"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(logs["manifest"])))
    assert _cli(logs, questions_file, "--out", str(out)) == 0 and out.is_file()


def test_cli_check_reports_errors(logs, questions_file, tmp_path, capsys):
    bad = tmp_path / "results.json"
    bad.write_text(json.dumps(edited(built(logs), ["summary", "total"], 9)), encoding="utf-8")
    assert _cli(logs, questions_file, "--check", str(bad)) == 1
    assert "$.summary" in capsys.readouterr().out
    assert _cli(logs, questions_file, "--check", str(tmp_path / "missing.json")) == 1


def test_cli_lists_questions(logs, questions_file, capsys):
    assert _cli(logs, questions_file, "--list") == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[2] == {"id": "stacked", "question": "SELECT 1;\nDROP TABLE x"}


def test_cli_writes_nothing_when_invalid(logs, questions_file, tmp_path, capsys):
    ids, out = tmp_path / "ids.json", tmp_path / "results.json"
    ids.write_text(json.dumps(logs["manifest"][1:]), encoding="utf-8")  # one id missing
    assert _cli(logs, questions_file, "--ids", str(ids), "--out", str(out)) == 1
    assert "nothing written" in capsys.readouterr().err and not out.exists()
    assert _cli(logs, questions_file, "--verify-logs") == 1


def test_writer_validates_before_writing(logs, tmp_path):
    out = tmp_path / "results.json"
    bad = edited(built(logs), ["results", 0, "pass"], False)
    with pytest.raises(results.ResultsError, match="pass"):
        results.write(bad, QUESTIONS, out)
    assert not out.exists() and not list(tmp_path.glob(".results-*"))


@pytest.mark.parametrize(
    "out", [config.PROJECT_ROOT / "src" / "text2sql" / "x.json", config.QUESTIONS_DIR / "x.txt"]
)
def test_writer_only_writes_json_under_questions(logs, out):
    with pytest.raises(results.ResultsError, match="must be a .json file"):
        results.write(built(logs), QUESTIONS, out)


# --- partial re-runs: --list --rerun and --merge -------------------------------------------


@pytest.fixture()
def previous(logs, tmp_path):
    """A full earlier run, written to disk as the file a merge reuses."""
    out = tmp_path / "results.json"
    results.write(results.build(logs["manifest"], QUESTIONS, logs["runs"], logs["reviews"],
                                now=NOW), QUESTIONS, out)  # fmt: skip
    return out


def test_entries_of_this_pipeline_are_reusable(logs, previous):
    by_id = results.previous_entries(previous)
    q = {q["id"]: q for q in QUESTIONS}
    assert results.stale_reason(by_id["order_count"], q["order_count"]) is None
    assert results.stale_reason(by_id["crash"], q["crash"]) == "the earlier run or review failed"
    assert results.stale_reason(None, q["order_count"]) == "no earlier entry"
    edited_q = {**q["order_count"], "question": "How many sellers?"}
    assert results.stale_reason(by_id["order_count"], edited_q) == "the question text changed"


@pytest.mark.parametrize(
    ("meta", "reason"),
    [
        ({"git_commit": "0000000"}, "made at commit 0000000"),
        ({"pipeline_hash": "0" * 16}, "different pipeline"),
        ({"pipeline_hash": None}, "different pipeline"),  # a trace older than the hash
    ],
)
def test_entries_of_another_pipeline_are_stale(logs, previous, meta, reason):
    entry = results.previous_entries(previous)["order_count"]
    entry["trace"]["meta"].update(meta)
    assert reason in results.stale_reason(entry, QUESTIONS[0])


def test_unknown_commit_is_never_reusable(logs, previous, monkeypatch):
    monkeypatch.setattr(trace, "git_commit", lambda: "unknown")
    entry = results.previous_entries(previous)["order_count"]
    entry["trace"]["meta"]["git_commit"] = "unknown"
    assert "now unknown" in results.stale_reason(entry, QUESTIONS[0])


def test_rerun_plan_adds_the_stale_entries(logs, previous):
    plan, stale = results.rerun_plan(QUESTIONS, ["drop_it"], results.previous_entries(previous))
    assert [p["id"] for p in plan] == ["drop_it", "crash", "lost"]  # file order
    assert stale == {"crash": "the earlier run or review failed",
                     "lost": "the earlier run or review failed"}  # fmt: skip
    with pytest.raises(results.ResultsError, match="not in questions.yaml"):
        results.rerun_plan(QUESTIONS, ["nope"], {})
    plan, _ = results.rerun_plan(QUESTIONS, [], {})  # no earlier file: everything
    assert len(plan) == len(QUESTIONS)


def test_merge_reuses_and_recomputes(logs, previous):
    # Re-run only the two failed entries; this time both succeed.
    t_crash, t_lost = _refused_by_validator("SELECT 2"), _accepted("Average price?")
    r_crash, r_lost = _review(t_crash), _review(t_lost)
    with open(logs["runs"] / "20261124.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(t_crash) + "\n" + json.dumps(t_lost) + "\n")
    with open(logs["reviews"] / "20261124.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(r_crash) + "\n" + json.dumps(r_lost) + "\n")
    manifest = [
        {"id": "crash", "trace_id": t_crash["id"], "review_id": r_crash["id"], "error": None},
        {"id": "lost", "trace_id": t_lost["id"], "review_id": r_lost["id"], "error": None},
    ]
    questions = copy.deepcopy(QUESTIONS)
    questions[1]["expect"] = "answer"  # changed in the yaml: pass is recomputed
    doc = results.build(manifest, questions, logs["runs"], logs["reviews"], now=NOW,
                        previous=results.previous_entries(previous))  # fmt: skip
    doc = json.loads(results.dumps(doc))
    assert results.schema_errors(doc, questions) == []
    assert results.log_errors(doc, logs["runs"], logs["reviews"]) == []
    by_id = {e["id"]: e for e in doc["results"]}
    assert by_id["order_count"]["trace"] == logs["traces"][0]  # reused verbatim
    assert by_id["drop_it"]["pass"] is False  # expect changed to answer, actual refuse
    assert by_id["crash"]["trace"] == t_crash and by_id["lost"]["actual"] == "answer"
    assert "3 of 5 entries were reused" in doc["method"]


def test_merge_refuses_a_stale_entry(logs, previous):
    manifest = [m for m in logs["manifest"] if m["id"] not in ("crash", "lost")][:2]
    with pytest.raises(results.ResultsError, match="no reusable earlier entry") as exc:
        results.build(manifest, QUESTIONS, logs["runs"], logs["reviews"],
                      previous=results.previous_entries(previous))  # fmt: skip
    assert "stacked" not in str(exc.value)  # reusable, so not listed
    assert "crash: the earlier run or review failed" in str(exc.value)


def test_previous_entries_of_a_bad_file(tmp_path):
    assert results.previous_entries(tmp_path / "missing.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(results.ResultsError, match="cannot read"):
        results.previous_entries(bad)
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(results.ResultsError, match="not a results file"):
        results.previous_entries(bad)


def test_cli_rerun_and_merge(logs, questions_file, previous, capsys):
    out = str(previous)
    assert _cli(logs, questions_file, "--list", "--rerun", "drop_it", "--out", out) == 0
    captured = capsys.readouterr()
    assert [q["id"] for q in json.loads(captured.out)] == ["drop_it", "crash", "lost"]
    assert "also re-run crash: the earlier run or review failed" in captured.err
    assert _cli(logs, questions_file, "--rerun", "drop_it") == 1  # needs --list
    assert _cli(logs, questions_file, "--list", "--merge") == 1
    # A merge whose manifest leaves out a stale entry writes nothing.
    before = previous.read_bytes()
    ids = previous.parent / "ids.json"
    ids.write_text(json.dumps(logs["manifest"][:1]), encoding="utf-8")
    assert _cli(logs, questions_file, "--ids", str(ids), "--merge", "--out", out) == 1
    assert "nothing written" in capsys.readouterr().err and previous.read_bytes() == before
    # With every non-reusable entry in the manifest it writes.
    ids.write_text(json.dumps(logs["manifest"][3:]), encoding="utf-8")
    assert _cli(logs, questions_file, "--ids", str(ids), "--merge", "--out", out) == 0


# --- the committed file -------------------------------------------------------------------


def test_committed_results_validate():
    questions = results.load_questions(config.QUESTIONS_PATH)
    assert results.check_file(config.RESULTS_PATH, questions) == []
