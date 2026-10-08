"""Command-line tool: send one prompt to the cluster and print the answer."""

from __future__ import annotations

import argparse
import sys
import time
import uuid

from lapclusters import config
from lapclusters.taskqueue import CONNECTION_ERRORS, FAILED, FINISHED, TaskQueue, connect


def submit_and_wait(
    queue: TaskQueue, prompt: str, timeout_s: float = 300.0, poll_s: float = 0.5
) -> dict[str, str]:
    task_id = queue.add_task(uuid.uuid4().hex, {"prompt": prompt})
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = queue.get(task_id)
        if info.get("status") in FINISHED:
            return info
        time.sleep(poll_s)
    raise TimeoutError(
        f"No worker finished the task within {timeout_s:g} seconds. Is a worker running?"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send one prompt to the cluster and print the answer."
    )
    parser.add_argument("prompt")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    queue = TaskQueue(connect(config.REDIS_URL))
    try:
        queue.ensure_group()
        info = submit_and_wait(queue, args.prompt, timeout_s=args.timeout)
    except CONNECTION_ERRORS:
        print("Cannot reach Redis. Check REDIS_URL and that Redis is running.", file=sys.stderr)
        return 1
    except TimeoutError as exc:
        print(exc, file=sys.stderr)
        return 1
    if info["status"] == FAILED:
        print(f"Task failed: {info.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    print(info["result"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
