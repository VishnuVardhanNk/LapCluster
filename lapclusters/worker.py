from __future__ import annotations

from typing import Callable

from lapclusters import config, llm
from lapclusters.taskqueue import TaskQueue, connect


def process_one(
    queue: TaskQueue,
    consumer: str,
    generate: Callable[[str], str],
    block_ms: int = 5000,
) -> bool:
    task = queue.claim(consumer, block_ms=block_ms)
    if task is None:
        return False
    if "prompt" not in task.payload:
        queue.fail(task, "task has no prompt")
        return True
    try:
        result = generate(task.payload["prompt"])
    except Exception as exc:
        queue.fail(task, str(exc))
        return True
    queue.complete(task, result)
    return True


def main() -> None:
    queue = TaskQueue(connect(config.REDIS_URL))
    queue.ensure_group()
    print(f"Worker {config.WORKER_NAME} ready, model {config.MODEL}")
    while True:
        if process_one(queue, config.WORKER_NAME, llm.generate):
            print("Handled one task")


if __name__ == "__main__":
    main()
