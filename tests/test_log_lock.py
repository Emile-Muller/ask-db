"""Parallel appends to one JSONL log (trace._write_line's lock). Offline: no database, no LLM.

/question-batch runs several pipelines and reviews at once, all appending to the same
day's trace and review logs. Without the lock, large lines written by several processes
can interleave on Windows and break the JSONL file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from text2sql import trace

PROCESSES = 8
LINES_EACH = 15
PAD = 64_000  # far past any single-write atomicity a file system might give

WRITER = f"""
import sys, time
from pathlib import Path
from text2sql import trace
log, go, worker = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
while not go.exists():
    time.sleep(0.001)
for i in range({LINES_EACH}):
    trace.append_jsonl({{"worker": worker, "i": i, "pad": str(worker) * {PAD}}}, log)
"""


def test_parallel_appends_give_one_valid_line_each(tmp_path):
    log, go = tmp_path / "parallel.jsonl", tmp_path / "go"
    procs = [
        subprocess.Popen(  # noqa: S603 - fixed arguments
            [sys.executable, "-c", WRITER, str(log), str(go), str(w)],
            stderr=subprocess.PIPE,
            text=True,
        )
        for w in range(PROCESSES)
    ]
    time.sleep(0.5)  # let every process start and wait at the gate
    go.touch()
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == PROCESSES * LINES_EACH
    records = [json.loads(line) for line in lines]  # every line is whole, valid JSON
    assert all(r["pad"] == str(r["worker"]) * PAD for r in records)
    assert sorted((r["worker"], r["i"]) for r in records) == [
        (w, i) for w in range(PROCESSES) for i in range(LINES_EACH)
    ]
    # Each worker's own lines stay in order.
    for w in range(PROCESSES):
        assert [r["i"] for r in records if r["worker"] == w] == list(range(LINES_EACH))


@pytest.mark.skipif(os.name != "nt", reason="flock has no timeout; it waits")
def test_lock_times_out_instead_of_hanging(tmp_path):
    log = tmp_path / "held.jsonl"
    with trace._locked(log):
        started = time.monotonic()
        try:
            with trace._locked(log, timeout_s=0.2):
                raise AssertionError("a second holder got the lock")
        except TimeoutError as e:
            assert "could not lock" in str(e)
        assert time.monotonic() - started < 5


def test_lock_is_a_sidecar_file(tmp_path):
    log = tmp_path / "x.jsonl"
    trace.append_jsonl({"a": 1}, log)
    assert (tmp_path / "x.jsonl.lock").is_file()
    assert log.read_text(encoding="utf-8") == '{"a": 1}\n'
