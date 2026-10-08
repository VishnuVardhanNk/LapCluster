"""Command-line tool: send one prompt to the cluster and print the answer."""

from __future__ import annotations

import argparse
import sys
import time
import uuid

from lapclusters import config, discovery
from lapclusters.taskqueue import (
    CONNECTION_ERRORS,
    FAILED,
    FINISHED,
    TaskQueue,
    connect,
    connection_problem,
)


def submit_and_wait(
    queue: TaskQueue,
    prompt: str,
    timeout_s: float = 300.0,
    poll_s: float = 0.5,
    preferred_model: str = "",
) -> dict[str, str]:
    payload = {"prompt": prompt}
    if preferred_model:
        payload["preferred_model"] = preferred_model
    task_id = queue.add_task(uuid.uuid4().hex, payload)
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
    parser.add_argument("--model", default="", help="prefer a specific model for this prompt")
    args = parser.parse_args()

    try:
        queue = TaskQueue(connect(discovery.resolve(config.REDIS_URL)))
        queue.ensure_group()
        info = submit_and_wait(
            queue, args.prompt, timeout_s=args.timeout, preferred_model=args.model
        )
    except discovery.DiscoveryError as exc:
        print(exc, file=sys.stderr)
        return 1
    except CONNECTION_ERRORS as exc:
        print(connection_problem(exc), file=sys.stderr)
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
