from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import redis


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

    def _key(self, task_id: str) -> str:
        return f"task:{task_id}"

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def add_task(self, job_id: str, payload: dict[str, str]) -> str:
        task_id = uuid.uuid4().hex
        self.client.hset(self._key(task_id), mapping={"status": "pending", "job_id": job_id})
        self.client.xadd(
            self.stream,
            {"task_id": task_id, "job_id": job_id, "payload": json.dumps(payload)},
        )
        return task_id

    def claim(self, consumer: str, block_ms: int = 5000) -> Task | None:
        response = self.client.xreadgroup(
            self.group, consumer, {self.stream: ">"}, count=1, block=block_ms
        )
        if not response:
            return None
        entry_id, fields = response[0][1][0]
        task = Task(entry_id, fields["task_id"], fields["job_id"], json.loads(fields["payload"]))
        self.client.hset(self._key(task.task_id), mapping={"status": "running", "worker": consumer})
        return task

    def complete(self, task: Task, result: str) -> None:
        self.client.hset(self._key(task.task_id), mapping={"status": "done", "result": result})
        self.client.xack(self.stream, self.group, task.entry_id)

    def fail(self, task: Task, error: str) -> None:
        self.client.hset(self._key(task.task_id), mapping={"status": "failed", "error": error})
        self.client.xack(self.stream, self.group, task.entry_id)

    def get(self, task_id: str) -> dict[str, str]:
        return self.client.hgetall(self._key(task_id))
