"""A worker: takes tasks from the queue and answers them with the local model."""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from lapclusters import config, llm, review
from lapclusters.taskqueue import (
    CONNECTION_ERRORS,
    TaskError,
    TaskQueue,
    connect,
    connection_problem,
)


def _describe(exc: Exception) -> str:
    # Some errors, such as an HTTP read timeout, have an empty message.
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def run_task(payload: dict[str, str], generate: Callable[..., str]) -> str:
    """Do the work a task describes and return its result as text."""
    if payload.get("type") == "review":
        return review.run(payload, generate)
    if "prompt" not in payload:
        raise TaskError("task has no prompt")
    return generate(payload["prompt"])


def process_one(
    queue: TaskQueue,
    consumer: str,
    generate: Callable[..., str],
    block_ms: int = 5000,
) -> bool:
    """Handle at most one task. Returns False when none arrived in time."""
    task = queue.claim(consumer, block_ms=block_ms)
    if task is None:
        return False
    try:
        result = run_task(task.payload, generate)
    except TaskError as exc:
        queue.fail(task, str(exc))
    except Exception as exc:
        queue.fail(task, _describe(exc))
    else:
        queue.complete(task, result)
    return True


def start_heartbeat(
    queue: TaskQueue, name: str, info: str = "", interval_s: float = 5.0
) -> threading.Event:
    """Keep this worker listed as alive from a background thread.

    The model call blocks the main loop for a long time, so the heartbeat
    cannot live there. Set the returned event to stop it.
    """
    stop = threading.Event()

    def beat() -> None:
        while not stop.is_set():
            try:
                queue.heartbeat(name, info)
            except CONNECTION_ERRORS:
                pass
            stop.wait(interval_s)

    threading.Thread(target=beat, daemon=True).start()
    return stop


def run_forever(
    queue: TaskQueue,
    consumer: str,
    generate: Callable[..., str],
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
        queue.heartbeat(config.WORKER_NAME, config.MODEL)
    except CONNECTION_ERRORS as exc:
        print(connection_problem(exc), file=sys.stderr)
        return 1
    start_heartbeat(queue, config.WORKER_NAME, config.MODEL)
    print(f"Worker {config.WORKER_NAME} ready, model {config.MODEL}")
    try:
        run_forever(queue, config.WORKER_NAME, llm.generate)
    except KeyboardInterrupt:
        print("Worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
