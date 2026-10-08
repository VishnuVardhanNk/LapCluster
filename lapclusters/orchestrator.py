"""Running a job on the cluster: split the work into tasks, and for jobs that
need it, piece the answers together at the end.

Kinds of job:
  review  find defects in each source file of a repository
  ask     answer a question from every file of a collection, then combine
  prompt  answer one prompt
  code    write code for one request
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Callable

from lapclusters import ask, config, discovery
from lapclusters.repo import ASK, REVIEW, Collected, RepoError, SourceFile, collect_files, open_repo
from lapclusters.review import SEVERITIES
from lapclusters.taskqueue import (
    CONNECTION_ERRORS,
    DONE,
    FAILED,
    FINISHED,
    TaskQueue,
    connect,
    connection_problem,
)

KINDS = ("review", "ask", "prompt", "code")
NEEDS_SOURCE = ("review", "ask")
NEEDS_QUESTION = ("ask", "prompt", "code")
DEFAULT_QUESTION = "Summarise what this file contains."

CODE_PROMPT = """You are a careful programmer. Write complete, working code for the request below.

Reply with the code in one fenced block, then a short note on how to run it and any \
assumption you made. Do not leave parts out or use placeholders.

Request: {request}
"""

NO_ANSWER = "none"


def _context() -> str:
    return str(config.MODEL_CONTEXT)


def _combine_budget() -> int:
    # Characters of notes that fit in one request alongside the instructions
    # and the reply, counted cautiously.
    return max((config.MODEL_CONTEXT - 3000) * 2, 4000)


# --- starting a job ----------------------------------------------------------


def start_job(
    queue: TaskQueue,
    files: list[SourceFile],
    job_id: str | None = None,
    kind: str = "review",
    question: str = "",
) -> str:
    """Queue one task per file (or per part of a long file)."""
    job_id = job_id or uuid.uuid4().hex
    for source in files:
        if kind == "ask":
            payload = {
                "type": "ask", "kind": source.kind, "path": source.path, "part": source.part,
                "content": source.content, "images": source.images, "context": source.context,
                "question": question or DEFAULT_QUESTION, "ctx": _context(),
            }
        else:
            payload = {
                "type": "review", "path": source.path, "part": source.part,
                "content": source.content, "first_line": str(source.first_line),
                "ctx": _context(),
            }
        queue.add_task(
            job_id,
            payload,
            name=source.name,
            # Anything with a picture can only go to a laptop whose model can see.
            lane="vision" if source.images else "",
            type=payload["type"],
            file=source.path,
            part=source.part,
        )
    return job_id


def prepare_job(
    queue: TaskQueue, source: str, started_by: str = "", kind: str = "review", question: str = ""
) -> str:
    """Record a job that is about to start, so it is visible straight away
    even while its repository is still being cloned."""
    job_id = uuid.uuid4().hex
    queue.create_job(
        job_id, source=source, status="preparing", started_by=started_by,
        kind=kind, question=question, context=_context(),
    )
    return job_id


def _someone_can_see(queue: TaskQueue) -> bool:
    return any(info.get("vision") for info in queue.worker_details().values())


def fill_job(queue: TaskQueue, job_id: str, source: str) -> Collected:
    """Queue the tasks of a prepared job.

    A source that cannot be read marks the job failed and raises RepoError.
    """
    meta = queue.job_meta(job_id)
    kind = meta.get("kind", "review")
    question = meta.get("question", "")

    if kind in ("prompt", "code"):
        prompt = CODE_PROMPT.format(request=question) if kind == "code" else question
        if queue.job_meta(job_id).get("status") != "cancelled":
            task_id = queue.add_task(
                job_id, {"type": "prompt", "prompt": prompt, "ctx": _context()},
                name="Answer", type="prompt",
            )
            queue.update_job(
                job_id, status="running", total="1", skipped="[]",
                queued_at=f"{queue.now():.3f}", answer_task=task_id,
            )
            queue.log_event("review", f"{'Code request' if kind == 'code' else 'Prompt'} started")
        return Collected()

    try:
        with open_repo(source) as root:
            if kind == "ask":
                # Pictures are only worth queueing if some laptop can look at them.
                collected = collect_files(root, mode=ASK, pictures=_someone_can_see(queue))
            else:
                collected = collect_files(root, mode=REVIEW)
        if not collected.files:
            raise RepoError(
                "No files this kind of job can read were found."
                if kind == "ask" else "No source files found to review."
            )
    except RepoError as exc:
        queue.close_job(job_id, status="failed", error=str(exc), finished_at=f"{queue.now():.3f}")
        raise
    if queue.job_meta(job_id).get("status") == "cancelled":
        # Cancelled while the repository was still being read: queue nothing.
        return collected
    start_job(queue, collected.files, job_id, kind=kind, question=question)
    queue.update_job(
        job_id,
        status="running",
        total=str(len(collected.files)),
        skipped=json.dumps(collected.skipped),
        queued_at=f"{queue.now():.3f}",
    )
    files = len({f.path for f in collected.files})
    queue.log_event(
        "review",
        f"{'Question about' if kind == 'ask' else 'Review of'} {source} started: "
        f"{files} file{'' if files == 1 else 's'} in {len(collected.files)} tasks",
    )
    return collected


# --- piecing answers together ------------------------------------------------


def advance(queue: TaskQueue, job_id: str) -> None:
    """Move an 'ask' job to its next stage if it is ready for one.

    Once every file has been answered, the answers are combined. When they do
    not fit in one request they are first condensed in groups, and the groups
    combined, for as many rounds as it takes. Only the host calls this.
    """
    meta = queue.job_meta(job_id)
    if meta.get("kind") != "ask" or meta.get("status") != "running" or meta.get("answer_task"):
        return
    # Two callers at once would each queue a combining task.
    if not queue.lock(f"advance:{job_id}"):
        return
    try:
        _advance(queue, job_id, queue.job_meta(job_id))
    finally:
        queue.unlock(f"advance:{job_id}")


def _advance(queue: TaskQueue, job_id: str, meta: dict[str, str]) -> None:
    if meta.get("status") != "running" or meta.get("answer_task"):
        return
    current = _loads(meta.get("combine_ids"), [])
    if current:
        rows = queue.get_fields(current, ["status", "name", "result"])
        if any(rows.get(task_id, {}).get("status") not in FINISHED for task_id in current):
            return
        notes = [
            (rows[task_id].get("name", "notes"), rows[task_id].get("result", ""))
            for task_id in current
            if rows.get(task_id, {}).get("status") == DONE
        ]
    else:
        rows = queue.get_fields(queue.job_tasks(job_id), ["status", "name", "result", "type"])
        if not rows or any(row.get("status") not in FINISHED for row in rows.values()):
            return
        notes = sorted(
            (row.get("name", ""), row.get("result", ""))
            for row in rows.values()
            if row.get("status") == DONE and row.get("type") == "ask"
        )
    if not notes:
        # Nothing was answered, so there is nothing to combine.
        queue.update_job(job_id, answer_task=NO_ANSWER)
        return

    question = meta.get("question") or DEFAULT_QUESTION
    batches = ask.plan_batches(notes, _combine_budget())
    final = len(batches) == 1
    round_number = int(meta.get("combine_round") or 0) + 1
    task_ids = []
    for index, batch in enumerate(batches, start=1):
        name = "Combined answer" if final else f"Summary of group {index} (round {round_number})"
        task_ids.append(
            queue.add_task(
                job_id,
                {
                    "type": "combine", "question": question, "notes": json.dumps(batch),
                    "final": "1" if final else "0", "ctx": _context(),
                },
                name=name,
                type="combine",
            )
        )
    if final:
        queue.update_job(job_id, answer_task=task_ids[0], combine_round=str(round_number))
    else:
        queue.update_job(job_id, combine_ids=json.dumps(task_ids), combine_round=str(round_number))


def retry_job(queue: TaskQueue, job_id: str) -> int:
    """Run a job's failed tasks again. For an 'ask' job the combining stage is
    thrown away and redone, since its input is about to change."""
    meta = queue.job_meta(job_id)
    if not meta:
        return 0
    if meta.get("kind") == "ask":
        rows = queue.get_fields(queue.job_tasks(job_id), ["type"])
        queue.remove_tasks(job_id, [t for t, row in rows.items() if row.get("type") == "combine"])
        queue.clear_job_fields(job_id, "answer_task", "combine_ids", "combine_round")
    retried = queue.retry_failed(job_id)
    if retried or meta.get("kind") == "ask":
        queue.clear_job_fields(job_id, "finished_at", "summary", "error")
        queue.update_job(job_id, status="running")
        queue.log_event("review", f"{retried} failed task{'' if retried == 1 else 's'} queued again")
    return retried


def _loads(text: str | None, fallback):
    try:
        return json.loads(text) if text else fallback
    except ValueError:
        return fallback


# --- waiting and reporting ---------------------------------------------------


def wait_for_job(
    queue: TaskQueue,
    job_id: str,
    timeout_s: float = 3600.0,
    poll_s: float = 1.0,
    on_progress: Callable[[int, int], None] | None = None,
    stages: bool = False,
) -> dict[str, dict[str, str]]:
    """Block until every task in the job is done or failed, then return them all.

    With `stages`, an 'ask' job is also moved through its combining stage.
    """
    deadline = time.monotonic() + timeout_s
    reported = (-1, -1)
    while True:
        if stages:
            advance(queue, job_id)
        task_ids = queue.job_tasks(job_id)
        # Only the status is read while waiting; the full records hold each
        # file's prompt and reply and are fetched once at the end.
        statuses = queue.get_fields(task_ids, ["status"])
        finished = sum(1 for info in statuses.values() if info.get("status") in FINISHED)
        if on_progress and (finished, len(task_ids)) != reported:
            on_progress(finished, len(task_ids))
            reported = (finished, len(task_ids))
        answered = not stages or bool(queue.job_meta(job_id).get("answer_task"))
        if finished == len(task_ids) and answered:
            return queue.get_many(task_ids)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Only {finished} of {len(task_ids)} tasks finished within "
                f"{timeout_s:g} seconds. Is a worker running?"
            )
        time.sleep(poll_s)


def _cell(text: str) -> str:
    # Keep a finding on one table row whatever the model wrote.
    return " ".join(text.split()).replace("|", "\\|")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _reviewers(infos: dict[str, dict[str, str]]) -> list[str]:
    per_worker: Counter[str] = Counter()
    for info in infos.values():
        if info.get("status") != DONE:
            continue
        reviewer = info.get("worker", "unknown")
        if info.get("model"):
            reviewer += f" ({info['model']})"
        per_worker[reviewer] += 1
    return [f"- {worker}: {_plural(count, 'file')}" for worker, count in sorted(per_worker.items())]


def build_report(
    source: str, infos: dict[str, dict[str, str]], skipped: list[tuple[str, str]]
) -> str:
    findings = []
    seen = set()
    not_reviewed = list(skipped)
    reviewed = 0
    for info in infos.values():
        name = info.get("name", "")
        if info.get("status") != DONE:
            not_reviewed.append((name, info.get("error", "not finished")))
            continue
        reviewed += 1
        for finding in json.loads(info.get("result") or "[]"):
            file = info.get("file") or name
            # Parts of a split file overlap, so the same line can be reported twice.
            key = (file, finding["line"], finding["severity"]) if finding["line"] else None
            if key is not None and key in seen:
                continue
            seen.add(key)
            findings.append({**finding, "file": file})

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

    reviewers = _reviewers(infos)
    if reviewers:
        lines += ["", "## Reviewed by", "", *reviewers]
    return "\n".join(lines) + "\n"


def build_answer_report(
    source: str,
    question: str,
    answer: str,
    infos: dict[str, dict[str, str]],
    skipped: list[tuple[str, str]],
) -> str:
    """The report for an 'ask', 'prompt' or 'code' job: the answer, then what
    each file contributed, so every claim can be traced."""
    lines = ["# LapClusters answer", ""]
    if source:
        lines += [f"- Source: {source}"]
    lines += [f"- Question: {question}", "", "## Answer", "", answer.strip() or "No answer was produced."]

    notes = sorted(
        (info.get("name", ""), info.get("result", ""))
        for info in infos.values()
        if info.get("status") == DONE and info.get("type") == "ask"
    )
    if notes:
        lines += ["", "## What each file contributed", ""]
        for name, text in notes:
            lines += [f"### {name}", "", text.strip(), ""]

    missing = list(skipped) + [
        (info.get("name", ""), info.get("error", "not finished"))
        for info in infos.values()
        if info.get("status") == FAILED
    ]
    if missing:
        lines += ["", "## Not included", ""]
        lines += [f"- `{name}`: {_cell(reason)}" for name, reason in sorted(missing)]

    reviewers = _reviewers(infos)
    if reviewers:
        lines += ["", "## Answered by", "", *reviewers]
    return "\n".join(lines).rstrip() + "\n"


def job_answer(queue: TaskQueue, job_id: str) -> str:
    """The final answer of an 'ask', 'prompt' or 'code' job, or ""."""
    task_id = queue.job_meta(job_id).get("answer_task", "")
    if not task_id or task_id == NO_ANSWER:
        return ""
    info = queue.get_fields([task_id], ["status", "result"]).get(task_id, {})
    return info.get("result", "") if info.get("status") == DONE else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a job on the cluster and write one report.")
    parser.add_argument("source", nargs="?", default="", help="a local folder or a git URL")
    parser.add_argument("--ask", metavar="QUESTION", help="answer this from every file, then combine")
    parser.add_argument("--prompt", metavar="TEXT", help="answer one prompt; no source needed")
    parser.add_argument("--code", metavar="REQUEST", help="write code for a request; no source needed")
    parser.add_argument("--output", default="review-report.md", help="where to write the report")
    parser.add_argument("--timeout", type=float, default=3600.0, help="seconds to wait")
    args = parser.parse_args()

    if args.prompt is not None:
        kind, question = "prompt", args.prompt
    elif args.code is not None:
        kind, question = "code", args.code
    elif args.ask is not None:
        kind, question = "ask", args.ask
    else:
        kind, question = "review", ""
    if kind in NEEDS_SOURCE and not args.source:
        parser.error("give a folder or a git URL")

    try:
        queue = TaskQueue(connect(discovery.resolve(config.REDIS_URL)))
    except discovery.DiscoveryError as exc:
        print(exc, file=sys.stderr)
        return 1
    job_id = None
    try:
        queue.ensure_group()
        job_id = prepare_job(queue, args.source, config.WORKER_NAME, kind=kind, question=question)
        try:
            collected = fill_job(queue, job_id, args.source)
        except RepoError as exc:
            print(exc, file=sys.stderr)
            return 1
        started = time.monotonic()
        workers = queue.workers()
        print(f"Queued {len(queue.job_tasks(job_id))} tasks. Live workers: {len(workers)}")
        if not workers:
            print("No worker is running yet. Start one with: python -m lapclusters.worker")
        infos = wait_for_job(
            queue,
            job_id,
            timeout_s=args.timeout,
            on_progress=lambda done, total: print(f"  {done}/{total} tasks finished"),
            stages=kind == "ask",
        )
    except CONNECTION_ERRORS as exc:
        print(connection_problem(exc), file=sys.stderr)
        return 1
    except (TimeoutError, KeyboardInterrupt) as exc:
        # Do not leave the rest of the job queued in front of the next one.
        cancelled = queue.cancel_job(job_id) if job_id else 0
        if job_id:
            queue.update_job(job_id, status="cancelled")
        reason = str(exc) or "Stopped."
        print(f"{reason} Cancelled {cancelled} unstarted tasks.", file=sys.stderr)
        return 1

    if kind == "review":
        report = build_report(args.source, infos, collected.skipped)
    else:
        answer = job_answer(queue, job_id)
        report = build_answer_report(args.source, question, answer, infos, collected.skipped)
        print()
        print(answer or "No answer was produced.")
        print()
    Path(args.output).write_text(report, encoding="utf-8")
    print(f"Finished in {time.monotonic() - started:.0f} seconds. Report: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
