"""A worker: takes tasks from the queue and answers them with the local model."""

from __future__ import annotations

import sys
import time
from typing import Callable

from lapclusters import config, llm
from lapclusters.taskqueue import CONNECTION_ERRORS, TaskQueue, connect


def _describe(exc: Exception) -> str:
    # Some errors, such as an HTTP read timeout, have an empty message.
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def process_one(
    queue: TaskQueue,
    consumer: str,
    generate: Callable[[str], str],
    block_ms: int = 5000,
) -> bool:
    """Handle at most one task. Returns False when none arrived in time."""
    task = queue.claim(consumer, block_ms=block_ms)
    if task is None:
        return False
    if "prompt" not in task.payload:
        queue.fail(task, "task has no prompt")
        return True
    try:
        result = generate(task.payload["prompt"])
    except Exception as exc:
        queue.fail(task, _describe(exc))
        return True
    queue.complete(task, result)
    return True


def run_forever(
    queue: TaskQueue,
    consumer: str,
    generate: Callable[[str], str],
    retry_delay_s: float = 3.0,
) -> None:
    while True:
        try:
            if process_one(queue, consumer, generate):
                print("Handled one task")
        except CONNECTION_ERRORS:
            print(f"Lost connection to Redis, retrying in {retry_delay_s:g} seconds")
            time.sleep(retry_delay_s)


def main() -> int:
    queue = TaskQueue(connect(config.REDIS_URL))
    try:
        queue.ensure_group()
    except CONNECTION_ERRORS:
        print("Cannot reach Redis. Check REDIS_URL and that Redis is running.", file=sys.stderr)
        return 1
    print(f"Worker {config.WORKER_NAME} ready, model {config.MODEL}")
    try:
        run_forever(queue, config.WORKER_NAME, llm.generate)
    except KeyboardInterrupt:
        print("Worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
