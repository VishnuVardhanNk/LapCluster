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

# A worker whose heartbeat is older than this is treated as gone.
WORKER_TTL_S = 15
# A task is given up on once this many workers in a row have died holding it.
MAX_DELIVERIES = 3


def connection_problem(exc: Exception) -> str:
    """A one-line explanation of a CONNECTION_ERRORS exception for the user."""
    if isinstance(exc, redis.AuthenticationError):
        return "Redis rejected the password. Check the password in REDIS_URL."
    return "Cannot reach Redis. Check REDIS_URL and that Redis is running."


class TaskError(Exception):
    """A task that cannot succeed as written. Its message becomes the task's error."""


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
    def __init__(
        self,
        client: redis.Redis,
        stream: str = "tasks",
        group: str = "workers",
        reclaim_idle_ms: int = 10_000,
    ):
        self.client = client
        self.stream = stream
        self.group = group
        # How long a claimed task must sit untouched before another worker may take it.
        self.reclaim_idle_ms = reclaim_idle_ms

    def _task_key(self, task_id: str) -> str:
        return f"task:{task_id}"

    def _job_key(self, job_id: str) -> str:
        return f"job:{job_id}"

    def _worker_key(self, worker: str) -> str:
        return f"worker:{worker}"

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    # --- tasks -----------------------------------------------------------

    def add_task(self, job_id: str, payload: dict[str, str], name: str = "") -> str:
        """Queue a task. `name` is a short label shown in reports and the dashboard."""
        task_id = uuid.uuid4().hex
        # One transaction, so a worker never sees a task without its status record.
        with self.client.pipeline() as pipe:
            pipe.hset(
                self._task_key(task_id),
                mapping={"status": PENDING, "job_id": job_id, "name": name},
            )
            pipe.sadd(self._job_key(job_id), task_id)
            pipe.xadd(
                self.stream,
                {"task_id": task_id, "job_id": job_id, "payload": json.dumps(payload)},
            )
            pipe.execute()
        return task_id

    def claim(self, consumer: str, block_ms: int = 5000) -> Task | None:
        """Take the next task: first one a dead worker left behind, else a new one."""
        try:
            task = self._reclaim_abandoned(consumer) or self._read_new(consumer, block_ms)
        except redis.ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            # Redis was restarted and lost the stream; recreate it and carry on.
            self.ensure_group()
            return None
        if task is None:
            return None
        self.client.hset(
            self._task_key(task.task_id), mapping={"status": RUNNING, "worker": consumer}
        )
        return task

    def _read_new(self, consumer: str, block_ms: int) -> Task | None:
        while True:
            response = self.client.xreadgroup(
                self.group, consumer, {self.stream: ">"}, count=1, block=block_ms
            )
            if not response:
                return None
            task = self._to_task(*response[0][1][0])
            if self.get(task.task_id).get("status") not in FINISHED:
                return task
            # Cancelled before any worker started it.
            self.client.xack(self.stream, self.group, task.entry_id)

    def _to_task(self, entry_id: str, fields: dict[str, str]) -> Task:
        return Task(entry_id, fields["task_id"], fields["job_id"], json.loads(fields["payload"]))

    def _reclaim_abandoned(self, consumer: str) -> Task | None:
        pending = self.client.xpending_range(self.stream, self.group, min="-", max="+", count=100)
        for entry in pending:
            owner = entry["consumer"]
            # A worker only asks for work when it is free, so anything still under
            # its own name is left over from before it restarted.
            if owner != consumer and self.client.exists(self._worker_key(owner)):
                continue
            claimed = self.client.xclaim(
                self.stream,
                self.group,
                consumer,
                min_idle_time=self.reclaim_idle_ms,
                message_ids=[entry["message_id"]],
            )
            if not claimed or claimed[0][1] is None:
                continue
            task = self._to_task(*claimed[0])
            if self.get(task.task_id).get("status") in FINISHED:
                # The previous worker stored its answer but died before acknowledging.
                self.client.xack(self.stream, self.group, task.entry_id)
                continue
            if entry["times_delivered"] >= MAX_DELIVERIES:
                self.fail(task, f"abandoned by {MAX_DELIVERIES} workers in a row")
                continue
            return task
        return None

    def complete(self, task: Task, result: str) -> None:
        self._finish(task, {"status": DONE, "result": result})

    def fail(self, task: Task, error: str) -> None:
        self._finish(task, {"status": FAILED, "error": error})

    def _finish(self, task: Task, fields: dict[str, str]) -> None:
        self.client.hset(self._task_key(task.task_id), mapping=fields)
        self.client.xack(self.stream, self.group, task.entry_id)

    def get(self, task_id: str) -> dict[str, str]:
        return self.client.hgetall(self._task_key(task_id))

    def get_many(self, task_ids: list[str]) -> dict[str, dict[str, str]]:
        if not task_ids:
            return {}
        with self.client.pipeline(transaction=False) as pipe:
            for task_id in task_ids:
                pipe.hgetall(self._task_key(task_id))
            return dict(zip(task_ids, pipe.execute()))

    def job_tasks(self, job_id: str) -> list[str]:
        return list(self.client.smembers(self._job_key(job_id)))

    def cancel_job(self, job_id: str) -> int:
        """Mark a job's unfinished tasks as failed so no worker starts them.

        A task already running on a worker is not interrupted. Returns how many
        tasks were cancelled.
        """
        cancelled = 0
        for task_id, info in self.get_many(self.job_tasks(job_id)).items():
            if info.get("status") not in FINISHED:
                self.client.hset(
                    self._task_key(task_id), mapping={"status": FAILED, "error": "cancelled"}
                )
                cancelled += 1
        return cancelled

    # --- workers ---------------------------------------------------------

    def heartbeat(self, worker: str, info: str = "") -> None:
        """Mark a worker as alive for the next WORKER_TTL_S seconds."""
        self.client.set(self._worker_key(worker), info, ex=WORKER_TTL_S)

    def workers(self) -> dict[str, str]:
        """Live workers, as {name: info}."""
        keys = sorted(self.client.scan_iter(match=self._worker_key("*")))
        if not keys:
            return {}
        prefix = len(self._worker_key(""))
        values = self.client.mget(keys)
        return {key[prefix:]: value or "" for key, value in zip(keys, values)}
