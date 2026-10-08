"""Turning what is stored in Redis into the shapes the dashboard shows."""

from __future__ import annotations

import json

from lapclusters.review import SEVERITIES
from lapclusters.taskqueue import DONE, FAILED, FINISHED, PENDING, RUNNING, TaskQueue

# Everything about a task except its large fields (prompt, reply, result).
TASK_FIELDS = [
    "status", "name", "worker", "model", "queued_at", "started_at", "finished_at",
    "error", "taken_over_from", *(f"count_{level}" for level in SEVERITIES),
]
REPORT_FIELDS = ["status", "name", "worker", "model", "result", "error"]


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _loads(text: str | None, fallback):
    try:
        return json.loads(text) if text else fallback
    except ValueError:
        return fallback


def job_view(
    queue: TaskQueue, job_id: str, close: bool = False, with_tasks: bool = True
) -> dict | None:
    """One review: its progress, totals and, optionally, a row per file.

    With `close`, a review whose files have all finished is recorded as ended
    and announced in the activity feed. Only the host does that.
    """
    meta = queue.job_meta(job_id)
    if not meta:
        return None
    rows = queue.get_fields(queue.job_tasks(job_id), TASK_FIELDS)
    tasks = sorted(({"id": task_id, **row} for task_id, row in rows.items()),
                   key=lambda t: t.get("name", ""))

    by_status = {status: 0 for status in (PENDING, RUNNING, DONE, FAILED)}
    findings = {level: 0 for level in SEVERITIES}
    last_finished = 0.0
    laptops: dict[str, int] = {}
    for task in tasks:
        status = task.get("status", PENDING)
        by_status[status] = by_status.get(status, 0) + 1
        if status in FINISHED:
            last_finished = max(last_finished, _number(task.get("finished_at")) or 0.0)
        if status == DONE:
            worker = task.get("worker", "unknown")
            laptops[worker] = laptops.get(worker, 0) + 1
            for level in SEVERITIES:
                findings[level] += int(_number(task.get(f"count_{level}")) or 0)

    total = len(tasks)
    finished = by_status[DONE] + by_status[FAILED]
    status = meta.get("status", "running")
    if status == "running" and total and finished == total:
        status = "done"
    queued_at = _number(meta.get("queued_at"))
    ended_at = _number(meta.get("finished_at"))
    if ended_at is None and status != "running" and last_finished:
        ended_at = last_finished
    duration = None
    if queued_at is not None and ended_at is not None:
        duration = max(ended_at - queued_at, 0.0)

    view = {
        "id": job_id,
        "source": meta.get("source", ""),
        "status": status,
        "error": meta.get("error", ""),
        "started_by": meta.get("started_by", ""),
        "created_at": _number(meta.get("created_at")),
        "queued_at": queued_at,
        "finished_at": ended_at,
        "duration": duration,
        "total": total,
        "done": by_status[DONE],
        "failed": by_status[FAILED],
        "running": by_status[RUNNING],
        "pending": by_status[PENDING],
        "findings": findings,
        "laptops": laptops,
        "skipped": _loads(meta.get("skipped"), []),
    }
    if close and status == "done" and "finished_at" not in meta and ended_at is not None:
        summary = {k: view[k] for k in ("total", "done", "failed", "findings", "laptops", "duration")}
        if queue.close_job(
            job_id, status="done", finished_at=f"{ended_at:.3f}", summary=json.dumps(summary)
        ):
            queue.log_event(
                "review",
                f"Review of {view['source']} finished: {view['done']} of {total} files in "
                f"{duration or 0:.0f} seconds on {_plural(len(laptops), 'laptop')}",
            )
    if with_tasks:
        view["tasks"] = tasks
    return view


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def job_summary(queue: TaskQueue, job_id: str) -> dict | None:
    """A review as one line of history. Ended reviews are read from their
    stored summary, so listing many of them stays cheap."""
    meta = queue.job_meta(job_id)
    if not meta:
        return None
    stored = _loads(meta.get("summary"), None)
    if not isinstance(stored, dict):
        return job_view(queue, job_id, with_tasks=False)
    return {
        "id": job_id,
        "source": meta.get("source", ""),
        "status": meta.get("status", "done"),
        "error": meta.get("error", ""),
        "started_by": meta.get("started_by", ""),
        "created_at": _number(meta.get("created_at")),
        "queued_at": _number(meta.get("queued_at")),
        "finished_at": _number(meta.get("finished_at")),
        **stored,
    }


def cancel_job(queue: TaskQueue, job_id: str) -> int:
    """Cancel a review that is still going. Returns how many files were dropped;
    a review that has already ended is left as it is."""
    before = job_view(queue, job_id, with_tasks=False)
    if before is None or before["status"] not in ("running", "preparing"):
        return 0
    # Mark the review first: once its files are all cancelled they count as
    # finished, and it must not then be mistaken for one that completed.
    queue.update_job(job_id, status="cancelled")
    cancelled = queue.cancel_job(job_id)
    view = job_view(queue, job_id, with_tasks=False)
    summary = {k: view[k] for k in ("total", "done", "failed", "findings", "laptops", "duration")}
    if queue.close_job(
        job_id, status="cancelled", finished_at=f"{queue.now():.3f}", summary=json.dumps(summary)
    ):
        queue.log_event(
            "review",
            f"Review of {view['source']} was cancelled with {_plural(cancelled, 'file')} not started",
        )
    queue.update_job(job_id, status="cancelled")
    return cancelled


def task_view(queue: TaskQueue, task_id: str) -> dict | None:
    """Everything known about one file, including the prompt and raw reply."""
    info = queue.get(task_id)
    if not info:
        return None
    findings = _loads(info.get("result"), None) if info.get("status") == DONE else None
    is_findings = isinstance(findings, list) and all(isinstance(f, dict) for f in findings)
    return {
        "id": task_id,
        "name": info.get("name", ""),
        "status": info.get("status", PENDING),
        "worker": info.get("worker", ""),
        "model": info.get("model", ""),
        "queued_at": _number(info.get("queued_at")),
        "started_at": _number(info.get("started_at")),
        "finished_at": _number(info.get("finished_at")),
        "error": info.get("error", ""),
        "taken_over_from": info.get("taken_over_from", ""),
        "findings": findings if is_findings else None,
        "result": "" if is_findings else info.get("result", ""),
        "raw": info.get("raw", ""),
        "prompt": info.get("prompt", ""),
    }


def findings_view(queue: TaskQueue, job_id: str) -> dict:
    """Every finding of a review in one list, plus the files that were not reviewed."""
    meta = queue.job_meta(job_id)
    rows = queue.get_fields(queue.job_tasks(job_id), REPORT_FIELDS)
    findings = []
    not_reviewed = [{"file": name, "reason": reason} for name, reason in _loads(meta.get("skipped"), [])]
    for task_id, row in rows.items():
        status = row.get("status")
        if status == FAILED:
            not_reviewed.append({"file": row.get("name", ""), "reason": row.get("error", "")})
        if status != DONE:
            continue
        for finding in _loads(row.get("result"), []):
            if not isinstance(finding, dict):
                continue
            findings.append(
                {
                    "task": task_id,
                    "file": row.get("name", ""),
                    "line": finding.get("line", 0),
                    "severity": finding.get("severity", "medium"),
                    "message": finding.get("message", ""),
                    "worker": row.get("worker", ""),
                    "model": row.get("model", ""),
                }
            )
    order = {level: index for index, level in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (order.get(f["severity"], len(order)), f["file"], f["line"]))
    not_reviewed.sort(key=lambda item: item["file"])
    return {"findings": findings, "not_reviewed": not_reviewed}


def report_rows(queue: TaskQueue, job_id: str) -> dict[str, dict[str, str]]:
    """The fields build_report needs, without each file's prompt and raw reply."""
    return queue.get_fields(queue.job_tasks(job_id), REPORT_FIELDS)
