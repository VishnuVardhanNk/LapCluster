"""Turning what is stored in Redis into the shapes the dashboard shows."""

from __future__ import annotations

import json

from lapclusters.orchestrator import NO_ANSWER
from lapclusters.review import SEVERITIES
from lapclusters.taskqueue import DONE, FAILED, FINISHED, PENDING, RUNNING, TaskQueue

# Everything about a task except its large fields (prompt, reply, result).
TASK_FIELDS = [
    "status", "name", "worker", "model", "queued_at", "started_at", "finished_at",
    "error", "taken_over_from", "type", "file", "part", "lane", "attempts", "last_error",
    "preferred_model", "route_applied", "route_problem",
    *(f"count_{level}" for level in SEVERITIES),
]
REPORT_FIELDS = ["status", "name", "worker", "model", "result", "error", "type", "file"]
SUMMARY_KEYS = ("total", "done", "failed", "findings", "laptops", "duration")


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


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _subject(meta: dict[str, str]) -> str:
    """What a job is called in the activity feed."""
    kind = meta.get("kind", "review")
    if kind == "review":
        return f"Review of {meta.get('source', '')}"
    if kind == "ask":
        return f"Question about {meta.get('source', '')}"
    return "Code request" if kind == "code" else "Prompt"


def job_view(
    queue: TaskQueue, job_id: str, close: bool = False, with_tasks: bool = True
) -> dict | None:
    """One job: its progress, totals and, optionally, a row per task.

    With `close`, a job whose work has all finished is recorded as ended and
    announced in the activity feed. Only the host does that.
    """
    meta = queue.job_meta(job_id)
    if not meta:
        return None
    kind = meta.get("kind", "review")
    rows = queue.get_fields(queue.job_tasks(job_id), TASK_FIELDS)
    tasks = sorted(
        ({"id": task_id, **row} for task_id, row in rows.items()),
        # Combining steps come after the files they combine.
        key=lambda t: (t.get("type") == "combine", t.get("file") or t.get("name", ""),
                       _number(t.get("queued_at")) or 0.0),
    )

    by_status = {status: 0 for status in (PENDING, RUNNING, DONE, FAILED)}
    findings = {level: 0 for level in SEVERITIES}
    last_finished = 0.0
    laptops: dict[str, int] = {}
    waiting_for_sight = 0
    for task in tasks:
        status = task.get("status", PENDING)
        by_status[status] = by_status.get(status, 0) + 1
        if status in FINISHED:
            last_finished = max(last_finished, _number(task.get("finished_at")) or 0.0)
        if status == PENDING and task.get("lane") == "vision":
            waiting_for_sight += 1
        if status == DONE:
            worker = task.get("worker", "unknown")
            laptops[worker] = laptops.get(worker, 0) + 1
            for level in SEVERITIES:
                findings[level] += int(_number(task.get(f"count_{level}")) or 0)

    total = len(tasks)
    finished = by_status[DONE] + by_status[FAILED]
    answer_task = meta.get("answer_task", "")
    status = meta.get("status", "running")
    if status == "running" and total and finished == total:
        # A question is not answered until its answers have been combined.
        if kind != "ask" or answer_task:
            status = "done"
    queued_at = _number(meta.get("queued_at"))
    ended_at = _number(meta.get("finished_at"))
    if ended_at is None and status != "running" and last_finished:
        ended_at = last_finished
    duration = None
    if queued_at is not None and ended_at is not None:
        duration = max(ended_at - queued_at, 0.0)

    answer = ""
    if answer_task and answer_task != NO_ANSWER:
        row = queue.get_fields([answer_task], ["status", "result"]).get(answer_task, {})
        if row.get("status") == DONE:
            answer = row.get("result", "")

    view = {
        "id": job_id,
        "kind": kind,
        "source": meta.get("source", ""),
        "question": meta.get("question", ""),
        "context": int(_number(meta.get("context")) or 0),
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
        "waiting_for_sight": waiting_for_sight,
        "answer": answer,
        "answer_task": answer_task if answer_task != NO_ANSWER else "",
        "combining": kind == "ask" and status == "running" and total > 0
        and all(t.get("status") in FINISHED for t in tasks if t.get("type") != "combine"),
    }
    if close and status == "done" and "finished_at" not in meta and ended_at is not None:
        summary = {k: view[k] for k in SUMMARY_KEYS}
        if queue.close_job(
            job_id, status="done", finished_at=f"{ended_at:.3f}", summary=json.dumps(summary)
        ):
            queue.log_event(
                "review",
                f"{_subject(meta)} finished: {view['done']} of {_plural(total, 'task')} in "
                f"{duration or 0:.0f} seconds on {_plural(len(laptops), 'laptop')}",
            )
    if with_tasks:
        view["tasks"] = tasks
    return view


def job_summary(queue: TaskQueue, job_id: str) -> dict | None:
    """A job as one line of history. Ended jobs are read from their stored
    summary, so listing many of them stays cheap."""
    meta = queue.job_meta(job_id)
    if not meta:
        return None
    stored = _loads(meta.get("summary"), None)
    if not isinstance(stored, dict):
        return job_view(queue, job_id, with_tasks=False)
    return {
        "id": job_id,
        "kind": meta.get("kind", "review"),
        "source": meta.get("source", ""),
        "question": meta.get("question", ""),
        "status": meta.get("status", "done"),
        "error": meta.get("error", ""),
        "started_by": meta.get("started_by", ""),
        "created_at": _number(meta.get("created_at")),
        "queued_at": _number(meta.get("queued_at")),
        "finished_at": _number(meta.get("finished_at")),
        **stored,
    }


def cancel_job(queue: TaskQueue, job_id: str) -> int:
    """Cancel a job that is still going. Returns how many tasks were dropped;
    a job that has already ended is left as it is."""
    before = job_view(queue, job_id, with_tasks=False)
    if before is None or before["status"] not in ("running", "preparing"):
        return 0
    # Mark the job first: once its tasks are all cancelled they count as
    # finished, and it must not then be mistaken for one that completed.
    queue.update_job(job_id, status="cancelled")
    cancelled = queue.cancel_job(job_id)
    view = job_view(queue, job_id, with_tasks=False)
    summary = {k: view[k] for k in SUMMARY_KEYS}
    if queue.close_job(
        job_id, status="cancelled", finished_at=f"{queue.now():.3f}", summary=json.dumps(summary)
    ):
        queue.log_event(
            "review",
            f"{_subject(queue.job_meta(job_id))} was cancelled with "
            f"{_plural(cancelled, 'task')} not started",
        )
    queue.update_job(job_id, status="cancelled")
    return cancelled


def clear_history(queue: TaskQueue) -> int:
    """Delete every job that has ended, with all its stored tasks. A job that
    is still running is kept. Returns how many were deleted."""
    deleted = 0
    for job_id in queue.recent_jobs(10_000):
        view = job_view(queue, job_id, with_tasks=False)
        if view is not None and view["status"] in ("running", "preparing"):
            continue
        queue.delete_job(job_id)
        deleted += 1
    return deleted


def task_view(queue: TaskQueue, task_id: str) -> dict | None:
    """Everything known about one task, including the prompt and raw reply."""
    info = queue.get(task_id)
    if not info:
        return None
    findings = None
    if info.get("status") == DONE and info.get("type", "review") == "review":
        parsed = _loads(info.get("result"), None)
        if isinstance(parsed, list) and all(isinstance(f, dict) for f in parsed):
            findings = parsed
    return {
        "id": task_id,
        "name": info.get("name", ""),
        "type": info.get("type", "review"),
        "status": info.get("status", PENDING),
        "worker": info.get("worker", ""),
        "model": info.get("model", ""),
        "preferred_model": info.get("preferred_model", ""),
        "route_applied": info.get("route_applied", ""),
        "route_problem": info.get("route_problem", ""),
        "queued_at": _number(info.get("queued_at")),
        "started_at": _number(info.get("started_at")),
        "finished_at": _number(info.get("finished_at")),
        "error": info.get("error", ""),
        "last_error": info.get("last_error", ""),
        "attempts": int(_number(info.get("attempts")) or 0),
        "needs_sight": info.get("lane") == "vision",
        "taken_over_from": info.get("taken_over_from", ""),
        "findings": findings,
        "result": "" if findings is not None else info.get("result", ""),
        "raw": info.get("raw", ""),
        "prompt": info.get("prompt", ""),
    }


def findings_view(queue: TaskQueue, job_id: str) -> dict:
    """What a job produced, in one place.

    For a review: every finding, de-duplicated across the overlapping parts of
    split files. For other jobs: each file's answer. Both list what was left out.
    """
    meta = queue.job_meta(job_id)
    rows = queue.get_fields(queue.job_tasks(job_id), REPORT_FIELDS)
    findings = []
    notes = []
    seen = set()
    left_out = [{"file": name, "reason": reason} for name, reason in _loads(meta.get("skipped"), [])]
    for task_id, row in rows.items():
        status = row.get("status")
        kind = row.get("type", "review")
        if status == FAILED:
            left_out.append({"file": row.get("name", ""), "reason": row.get("error", "")})
        if status != DONE:
            continue
        if kind == "ask":
            notes.append(
                {
                    "task": task_id, "file": row.get("name", ""), "text": row.get("result", ""),
                    "worker": row.get("worker", ""), "model": row.get("model", ""),
                }
            )
        if kind != "review":
            continue
        file = row.get("file") or row.get("name", "")
        for finding in _loads(row.get("result"), []):
            if not isinstance(finding, dict):
                continue
            line = finding.get("line", 0)
            key = (file, line, finding.get("severity"))
            if line and key in seen:
                continue
            seen.add(key)
            findings.append(
                {
                    "task": task_id,
                    "file": file,
                    "line": line,
                    "severity": finding.get("severity", "medium"),
                    "message": finding.get("message", ""),
                    "worker": row.get("worker", ""),
                    "model": row.get("model", ""),
                }
            )
    order = {level: index for index, level in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (order.get(f["severity"], len(order)), f["file"], f["line"]))
    notes.sort(key=lambda n: n["file"])
    left_out.sort(key=lambda item: item["file"])
    return {"findings": findings, "notes": notes, "not_reviewed": left_out}


def report_rows(queue: TaskQueue, job_id: str) -> dict[str, dict[str, str]]:
    """The fields the report builders need, without each task's prompt and raw reply."""
    return queue.get_fields(queue.job_tasks(job_id), REPORT_FIELDS)
