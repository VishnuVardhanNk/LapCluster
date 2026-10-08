"""Review a whole repository: one task per file, merged into one report."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Callable

from lapclusters import config, discovery
from lapclusters.repo import Collected, RepoError, SourceFile, collect_files, open_repo
from lapclusters.review import SEVERITIES
from lapclusters.taskqueue import (
    CONNECTION_ERRORS,
    DONE,
    FINISHED,
    TaskQueue,
    connect,
    connection_problem,
)


def start_job(queue: TaskQueue, files: list[SourceFile], job_id: str | None = None) -> str:
    job_id = job_id or uuid.uuid4().hex
    for source in files:
        queue.add_task(
            job_id,
            {"type": "review", "path": source.path, "content": source.content},
            name=source.path,
        )
    return job_id


def prepare_job(queue: TaskQueue, source: str, started_by: str = "") -> str:
    """Record a review that is about to start, so it is visible straight away
    even while its repository is still being cloned."""
    job_id = uuid.uuid4().hex
    queue.create_job(job_id, source=source, status="preparing", started_by=started_by)
    return job_id


def fill_job(queue: TaskQueue, job_id: str, source: str) -> Collected:
    """Read the repository and queue one task per file for a prepared job.

    A repository that cannot be read marks the job failed and raises RepoError.
    """
    try:
        with open_repo(source) as root:
            collected = collect_files(root)
        if not collected.files:
            raise RepoError("No source files found to review.")
    except RepoError as exc:
        queue.close_job(job_id, status="failed", error=str(exc), finished_at=f"{queue.now():.3f}")
        raise
    if queue.job_meta(job_id).get("status") == "cancelled":
        # Cancelled while the repository was still being read: queue nothing.
        return collected
    start_job(queue, collected.files, job_id)
    queue.update_job(
        job_id,
        status="running",
        total=str(len(collected.files)),
        skipped=json.dumps(collected.skipped),
        queued_at=f"{queue.now():.3f}",
    )
    queue.log_event("review", f"Review of {source} started: {len(collected.files)} files")
    return collected


def wait_for_job(
    queue: TaskQueue,
    job_id: str,
    timeout_s: float = 3600.0,
    poll_s: float = 1.0,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, dict[str, str]]:
    """Block until every task in the job is done or failed, then return them all."""
    task_ids = queue.job_tasks(job_id)
    deadline = time.monotonic() + timeout_s
    reported = -1
    while True:
        # Only the status is read while waiting; the full records hold each
        # file's prompt and reply and are fetched once at the end.
        statuses = queue.get_fields(task_ids, ["status"])
        finished = sum(1 for info in statuses.values() if info.get("status") in FINISHED)
        if on_progress and finished != reported:
            on_progress(finished, len(task_ids))
            reported = finished
        if finished == len(task_ids):
            return queue.get_many(task_ids)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Only {finished} of {len(task_ids)} files were reviewed within "
                f"{timeout_s:g} seconds. Is a worker running?"
            )
        time.sleep(poll_s)


def _cell(text: str) -> str:
    # Keep a finding on one table row whatever the model wrote.
    return " ".join(text.split()).replace("|", "\\|")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def build_report(
    source: str, infos: dict[str, dict[str, str]], skipped: list[tuple[str, str]]
) -> str:
    findings = []
    not_reviewed = list(skipped)
    per_worker: Counter[str] = Counter()
    reviewed = 0
    for info in infos.values():
        name = info.get("name", "")
        if info.get("status") != DONE:
            not_reviewed.append((name, info.get("error", "not finished")))
            continue
        reviewed += 1
        reviewer = info.get("worker", "unknown")
        if info.get("model"):
            reviewer += f" ({info['model']})"
        per_worker[reviewer] += 1
        for finding in json.loads(info.get("result") or "[]"):
            findings.append({**finding, "file": name})

    findings.sort(key=lambda f: (SEVERITIES.index(f["severity"]), f["file"], f["line"]))
    counts = Counter(f["severity"] for f in findings)
    by_severity = ", ".join(f"{counts[level]} {level}" for level in SEVERITIES)

    lines = [
        f"# LapClusters review: {source}",
        "",
        f"- Files reviewed: {reviewed}",
        f"- Files not reviewed: {len(not_reviewed)}",
        f"- Findings: {len(findings)} ({by_severity})",
        "",
        "## Findings",
        "",
    ]
    if findings:
        lines += ["| Severity | File | Line | Problem |", "| --- | --- | --- | --- |"]
        lines += [
            f"| {f['severity']} | `{f['file']}` | {f['line'] or '-'} | {_cell(f['message'])} |"
            for f in findings
        ]
    else:
        lines.append("No problems found.")

    if not_reviewed:
        lines += ["", "## Not reviewed", ""]
        lines += [f"- `{name}`: {_cell(reason)}" for name, reason in sorted(not_reviewed)]

    if per_worker:
        lines += ["", "## Reviewed by", ""]
        lines += [
            f"- {worker}: {_plural(count, 'file')}" for worker, count in sorted(per_worker.items())
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review a repository on the cluster and write one report."
    )
    parser.add_argument("source", help="a local folder or a git URL")
    parser.add_argument("--output", default="review-report.md", help="where to write the report")
    parser.add_argument("--timeout", type=float, default=3600.0, help="seconds to wait")
    args = parser.parse_args()

    try:
        queue = TaskQueue(connect(discovery.resolve(config.REDIS_URL)))
    except discovery.DiscoveryError as exc:
        print(exc, file=sys.stderr)
        return 1
    job_id = None
    try:
        queue.ensure_group()
        job_id = prepare_job(queue, args.source, started_by=config.WORKER_NAME)
        try:
            collected = fill_job(queue, job_id, args.source)
        except RepoError as exc:
            print(exc, file=sys.stderr)
            return 1
        started = time.monotonic()
        workers = queue.workers()
        print(f"Queued {len(collected.files)} files. Live workers: {len(workers)}")
        if not workers:
            print("No worker is running yet. Start one with: python -m lapclusters.worker")
        infos = wait_for_job(
            queue,
            job_id,
            timeout_s=args.timeout,
            on_progress=lambda done, total: print(f"  {done}/{total} files reviewed"),
        )
    except CONNECTION_ERRORS as exc:
        print(connection_problem(exc), file=sys.stderr)
        return 1
    except (TimeoutError, KeyboardInterrupt) as exc:
        # Do not leave the rest of the job queued in front of the next review.
        cancelled = queue.cancel_job(job_id) if job_id else 0
        if job_id:
            queue.update_job(job_id, status="cancelled")
        reason = str(exc) or "Stopped."
        print(f"{reason} Cancelled {cancelled} unstarted files.", file=sys.stderr)
        return 1

    Path(args.output).write_text(
        build_report(args.source, infos, collected.skipped), encoding="utf-8"
    )
    print(f"Finished in {time.monotonic() - started:.0f} seconds. Report: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
