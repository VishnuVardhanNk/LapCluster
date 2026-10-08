"""The shared task queue. This is the only module that talks to Redis."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import redis

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
FINISHED = (DONE, FAILED)

# Errors that mean Redis could not be reached, as opposed to a bad command.
CONNECTION_ERRORS = (redis.ConnectionError, redis.TimeoutError)


def connect(url: str) -> redis.Redis:
    # The read timeout must stay longer than any block_ms passed to claim(),
    # otherwise an idle worker's wait is cut off with a TimeoutError.
    return redis.Redis.from_url(
        url, decode_responses=True, socket_timeout=60, socket_connect_timeout=5
    )


@dataclass
class Task:
    entry_id: str
    task_id: str
    job_id: str
    payload: dict[str, str]


class TaskQueue:
    def __init__(self, client: redis.Redis, stream: str = "tasks", group: str = "workers"):
        self.client = client
        self.stream = stream
        self.group = group

    def _task_key(self, task_id: str) -> str:
        return f"task:{task_id}"

    def _job_key(self, job_id: str) -> str:
        return f"job:{job_id}"

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def add_task(self, job_id: str, payload: dict[str, str]) -> str:
        task_id = uuid.uuid4().hex
        # One transaction, so a worker never sees a task without its status record.
        with self.client.pipeline() as pipe:
            pipe.hset(self._task_key(task_id), mapping={"status": PENDING, "job_id": job_id})
            pipe.sadd(self._job_key(job_id), task_id)
            pipe.xadd(
                self.stream,
                {"task_id": task_id, "job_id": job_id, "payload": json.dumps(payload)},
            )
            pipe.execute()
        return task_id

    def claim(self, consumer: str, block_ms: int = 5000) -> Task | None:
        response = self.client.xreadgroup(
            self.group, consumer, {self.stream: ">"}, count=1, block=block_ms
        )
        if not response:
            return None
        entry_id, fields = response[0][1][0]
        task = Task(entry_id, fields["task_id"], fields["job_id"], json.loads(fields["payload"]))
        self.client.hset(
            self._task_key(task.task_id), mapping={"status": RUNNING, "worker": consumer}
        )
        return task

    def complete(self, task: Task, result: str) -> None:
        self._finish(task, {"status": DONE, "result": result})

    def fail(self, task: Task, error: str) -> None:
        self._finish(task, {"status": FAILED, "error": error})

    def _finish(self, task: Task, fields: dict[str, str]) -> None:
        self.client.hset(self._task_key(task.task_id), mapping=fields)
        self.client.xack(self.stream, self.group, task.entry_id)

    def get(self, task_id: str) -> dict[str, str]:
        return self.client.hgetall(self._task_key(task_id))

    def job_tasks(self, job_id: str) -> list[str]:
        return list(self.client.smembers(self._job_key(job_id)))
