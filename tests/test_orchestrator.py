import json
import threading
from pathlib import Path

import pytest

from lapclusters.orchestrator import build_report, start_job, wait_for_job
from lapclusters.repo import SourceFile, collect_files
from lapclusters.worker import process_one

SAMPLE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


def _work_until_idle(queue, generate):
    while process_one(queue, "w1", generate, block_ms=300):
        pass


def test_start_job_adds_one_named_review_task_per_file(queue):
    files = [SourceFile("a.py", "x = 1\n"), SourceFile("lib/b.js", "y\n")]
    job_id = start_job(queue, files)
    infos = queue.get_many(queue.job_tasks(job_id))
    assert sorted(info["name"] for info in infos.values()) == ["a.py", "lib/b.js"]
    task = queue.claim("w1", block_ms=100)
    assert task.payload["type"] == "review"
    assert task.payload["path"] in ("a.py", "lib/b.js")


def test_wait_for_job_returns_every_task_once_finished(queue):
    job_id = start_job(queue, [SourceFile("a.py", "x\n"), SourceFile("b.py", "y\n")])
    progress = []
    worker = threading.Thread(
        target=_work_until_idle, args=(queue, lambda prompt, schema=None: '{"findings": []}')
    )
    worker.start()
    infos = wait_for_job(
        queue, job_id, timeout_s=10, poll_s=0.05, on_progress=lambda d, t: progress.append((d, t))
    )
    worker.join()
    assert len(infos) == 2
    assert all(info["status"] == "done" for info in infos.values())
    assert progress[-1] == (2, 2)


def test_wait_for_job_times_out_when_nothing_finishes(queue):
    job_id = start_job(queue, [SourceFile("a.py", "x\n")])
    with pytest.raises(TimeoutError, match="0 of 1"):
        wait_for_job(queue, job_id, timeout_s=0.3, poll_s=0.05)


def _done(name, findings, worker="laptop-1"):
    return {"status": "done", "name": name, "worker": worker, "result": json.dumps(findings)}


def test_report_sorts_findings_by_severity_then_file_then_line():
    infos = {
        "t1": _done("b.py", [{"line": 9, "severity": "low", "message": "Minor."}]),
        "t2": _done(
            "a.py",
            [
                {"line": 7, "severity": "high", "message": "Second high."},
                {"line": 2, "severity": "high", "message": "First high."},
                {"line": 1, "severity": "medium", "message": "A medium."},
            ],
        ),
    }
    report = build_report("my-repo", infos, skipped=[])
    order = [report.index(text) for text in ("First high.", "Second high.", "A medium.", "Minor.")]
    assert order == sorted(order)
    assert "Files reviewed: 2" in report
    assert "Findings: 4 (2 high, 1 medium, 1 low)" in report


def test_report_lists_files_that_were_not_reviewed():
    infos = {
        "t1": _done("a.py", []),
        "t2": {"status": "failed", "name": "b.py", "error": "model returned invalid JSON twice"},
    }
    report = build_report("my-repo", infos, skipped=[("big.py", "larger than 20 KB")])
    assert "Files reviewed: 1" in report
    assert "Files not reviewed: 2" in report
    assert "`b.py`: model returned invalid JSON twice" in report
    assert "`big.py`: larger than 20 KB" in report


def test_report_says_so_when_there_are_no_findings():
    report = build_report("my-repo", {"t1": _done("a.py", [])}, skipped=[])
    assert "No problems found." in report


def test_report_keeps_table_intact_when_message_has_pipes_or_newlines():
    infos = {"t1": _done("a.py", [{"line": 1, "severity": "low", "message": "a | b\nc"}])}
    report = build_report("my-repo", infos, skipped=[])
    assert "a \\| b c" in report


def test_report_counts_files_per_worker():
    infos = {
        "t1": _done("a.py", [], worker="laptop-1"),
        "t2": _done("b.py", [], worker="laptop-2"),
        "t3": _done("c.py", [], worker="laptop-1"),
    }
    report = build_report("my-repo", infos, skipped=[])
    assert "laptop-1: 2 files" in report
    assert "laptop-2: 1 file" in report


def test_sample_repo_goes_through_the_whole_pipeline(queue):
    collected = collect_files(SAMPLE_REPO)
    assert [f.path for f in collected.files] == ["app.js", "calculator.py", "users.py"]
    job_id = start_job(queue, collected.files)
    reply = '{"findings": [{"line": 1, "severity": "high", "message": "Stub finding."}]}'
    worker = threading.Thread(target=_work_until_idle, args=(queue, lambda p, schema=None: reply))
    worker.start()
    infos = wait_for_job(queue, job_id, timeout_s=10, poll_s=0.05)
    worker.join()
    report = build_report("sample_repo", infos, collected.skipped)
    assert "Findings: 3 (3 high, 0 medium, 0 low)" in report
    assert "| high | `calculator.py` | 1 | Stub finding. |" in report
