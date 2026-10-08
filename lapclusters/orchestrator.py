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

from lapclusters import config
from lapclusters.repo import RepoError, SourceFile, collect_files, open_repo
from lapclusters.review import SEVERITIES
from lapclusters.taskqueue import CONNECTION_ERRORS, DONE, FINISHED, TaskQueue, connect


def start_job(queue: TaskQueue, files: list[SourceFile]) -> str:
    job_id = uuid.uuid4().hex
    for source in files:
        queue.add_task(
            job_id,
            {"type": "review", "path": source.path, "content": source.content},
            name=source.path,
        )
    return job_id


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
        infos = queue.get_many(task_ids)
        finished = sum(1 for info in infos.values() if info.get("status") in FINISHED)
        if on_progress and finished != reported:
            on_progress(finished, len(task_ids))
            reported = finished
        if finished == len(task_ids):
            return infos
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
        per_worker[info.get("worker", "unknown")] += 1
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
        with open_repo(args.source) as root:
            collected = collect_files(root)
    except RepoError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not collected.files:
        print("No source files found to review.", file=sys.stderr)
        return 1

    queue = TaskQueue(connect(config.REDIS_URL))
    started = time.monotonic()
    try:
        queue.ensure_group()
        job_id = start_job(queue, collected.files)
        workers = queue.workers()
        print(f"Queued {len(collected.files)} files. Live workers: {len(workers)}")
        infos = wait_for_job(
            queue,
            job_id,
            timeout_s=args.timeout,
            on_progress=lambda done, total: print(f"  {done}/{total} files reviewed"),
        )
    except CONNECTION_ERRORS:
        print("Cannot reach Redis. Check REDIS_URL and that Redis is running.", file=sys.stderr)
        return 1
    except TimeoutError as exc:
        print(exc, file=sys.stderr)
        return 1

    Path(args.output).write_text(
        build_report(args.source, infos, collected.skipped), encoding="utf-8"
    )
    print(f"Finished in {time.monotonic() - started:.0f} seconds. Report: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
