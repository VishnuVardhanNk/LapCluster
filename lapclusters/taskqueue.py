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
# How long a task's live output is kept after the last piece was written.
OUTPUT_TTL_S = 3600
EVENTS_KEPT = 500


def connection_problem(exc: Exception) -> str:
    """A one-line explanation of a CONNECTION_ERRORS exception for the user."""
    if isinstance(exc, redis.AuthenticationError):
        return "Redis rejected the password. Check the password in REDIS_URL."
    return "Cannot reach Redis. Check REDIS_URL and that Redis is running."


class TaskError(Exception):
    """A task that cannot succeed as written. Its message becomes the task's error.

    `details` are extra fields to store with the failed task, such as the
    model's raw reply.
    """

    def __init__(self, message: str, details: dict[str, str] | None = None):
        super().__init__(message)
        self.details = details or {}


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
    # Name of the dead worker this task was taken over from, if any.
    taken_from: str = ""


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

    def _job_meta_key(self, job_id: str) -> str:
        return f"jobmeta:{job_id}"

    def _worker_key(self, worker: str) -> str:
        return f"worker:{worker}"

    def _output_key(self, task_id: str) -> str:
        return f"out:{task_id}"

    def _control_key(self, worker: str) -> str:
        return f"control:{worker}"

    def now(self) -> float:
        """The Redis server's clock, so every laptop stamps times the same way."""
        seconds, microseconds = self.client.time()
        return seconds + microseconds / 1_000_000

    def _stamp(self) -> str:
        return f"{self.now():.3f}"

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
        queued_at = self._stamp()
        # One transaction, so a worker never sees a task without its status record.
        with self.client.pipeline() as pipe:
            pipe.hset(
                self._task_key(task_id),
                mapping={
                    "status": PENDING,
                    "job_id": job_id,
                    "name": name,
                    "queued_at": queued_at,
                },
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
        fields = {"status": RUNNING, "worker": consumer, "started_at": self._stamp()}
        if task.taken_from:
            fields["taken_over_from"] = task.taken_from
        self.client.hset(self._task_key(task.task_id), mapping=fields)
        if task.taken_from:
            name = self.client.hget(self._task_key(task.task_id), "name") or "a task"
            self.log_event(
                "takeover",
                f"{consumer} took over {name} from {task.taken_from}, which stopped responding",
                worker=consumer,
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
            if self.client.hget(self._task_key(task.task_id), "status") not in FINISHED:
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
            if self.client.hget(self._task_key(task.task_id), "status") in FINISHED:
                # The previous worker stored its answer but died before acknowledging.
                self.client.xack(self.stream, self.group, task.entry_id)
                continue
            if entry["times_delivered"] >= MAX_DELIVERIES:
                self.fail(task, f"abandoned by {MAX_DELIVERIES} workers in a row")
                continue
            if owner != consumer:
                task.taken_from = owner
            return task
        return None

    def note(self, task: Task, **details: str) -> None:
        """Record something about a task while it is still running."""
        self.client.hset(self._task_key(task.task_id), mapping=details)

    def complete(self, task: Task, result: str, **details: str) -> None:
        self._finish(task, {**details, "status": DONE, "result": result})

    def fail(self, task: Task, error: str, **details: str) -> None:
        self._finish(task, {**details, "status": FAILED, "error": error})

    def _finish(self, task: Task, fields: dict[str, str]) -> None:
        fields["finished_at"] = self._stamp()
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

    def get_fields(self, task_ids: list[str], fields: list[str]) -> dict[str, dict[str, str]]:
        """Like get_many, but only the named fields. Use it to skip large ones
        such as the prompt and the model's reply."""
        if not task_ids:
            return {}
        with self.client.pipeline(transaction=False) as pipe:
            for task_id in task_ids:
                pipe.hmget(self._task_key(task_id), fields)
            rows = pipe.execute()
        return {
            task_id: {f: v for f, v in zip(fields, values) if v is not None}
            for task_id, values in zip(task_ids, rows)
        }

    # --- live output -----------------------------------------------------

    def reset_output(self, task_id: str) -> None:
        self.client.delete(self._output_key(task_id))

    def append_output(self, task_id: str, text: str) -> None:
        """Add a piece of the model's reply while it is still being written."""
        if not text:
            return
        with self.client.pipeline() as pipe:
            pipe.rpush(self._output_key(task_id), text)
            pipe.expire(self._output_key(task_id), OUTPUT_TTL_S)
            pipe.execute()

    def read_output(self, task_id: str, offset: int = 0) -> tuple[str, int]:
        """Text written since `offset`, and the offset to pass next time.

        An offset beyond the end means the output was restarted; reading then
        starts again from the beginning.
        """
        key = self._output_key(task_id)
        if offset > self.client.llen(key):
            offset = 0
        pieces = self.client.lrange(key, offset, -1)
        return "".join(pieces), offset + len(pieces)

    # --- jobs ------------------------------------------------------------

    def job_tasks(self, job_id: str) -> list[str]:
        return list(self.client.smembers(self._job_key(job_id)))

    def cancel_job(self, job_id: str) -> int:
        """Mark a job's unfinished tasks as failed so no worker starts them.

        A task already running on a worker is not interrupted. Returns how many
        tasks were cancelled.
        """
        cancelled = 0
        for task_id, info in self.get_fields(self.job_tasks(job_id), ["status"]).items():
            if info.get("status") not in FINISHED:
                self.client.hset(
                    self._task_key(task_id),
                    mapping={
                        "status": FAILED,
                        "error": "cancelled",
                        "finished_at": self._stamp(),
                    },
                )
                cancelled += 1
        return cancelled

    def create_job(self, job_id: str, **meta: str) -> None:
        """Record a job so it shows up in the dashboard and in history."""
        with self.client.pipeline() as pipe:
            pipe.hset(self._job_meta_key(job_id), mapping={"created_at": self._stamp(), **meta})
            pipe.zadd("jobs", {job_id: self.now()})
            pipe.execute()

    def update_job(self, job_id: str, **meta: str) -> None:
        self.client.hset(self._job_meta_key(job_id), mapping=meta)

    def close_job(self, job_id: str, **meta: str) -> bool:
        """Record that a job has ended. Returns False if it was already closed,
        so that only one caller announces it."""
        if not self.client.hsetnx(self._job_meta_key(job_id), "finished_at", meta["finished_at"]):
            return False
        self.client.hset(self._job_meta_key(job_id), mapping=meta)
        return True

    def job_meta(self, job_id: str) -> dict[str, str]:
        return self.client.hgetall(self._job_meta_key(job_id))

    def recent_jobs(self, count: int = 20) -> list[str]:
        """Job ids, newest first."""
        return list(self.client.zrevrange("jobs", 0, count - 1))

    # --- workers ---------------------------------------------------------

    def heartbeat(self, worker: str, info: str = "") -> None:
        """Mark a worker as alive for the next WORKER_TTL_S seconds."""
        self.client.set(self._worker_key(worker), info, ex=WORKER_TTL_S)

    def heartbeat_left_ms(self, worker: str) -> int:
        """Milliseconds until a worker's heartbeat expires; negative if it has none."""
        return self.client.pttl(self._worker_key(worker))

    def forget_worker(self, worker: str) -> None:
        """Remove a worker's heartbeat at once, for a clean shutdown."""
        self.client.delete(self._worker_key(worker))

    def workers(self) -> dict[str, str]:
        """Live workers, as {name: info}."""
        keys = sorted(self.client.scan_iter(match=self._worker_key("*")))
        if not keys:
            return {}
        prefix = len(self._worker_key(""))
        values = self.client.mget(keys)
        # A key can expire between the scan and the read; skip those.
        return {key[prefix:]: value for key, value in zip(keys, values) if value is not None}

    def worker_details(self) -> dict[str, dict]:
        """Live workers with their info decoded. Older workers report only a
        model name; newer ones report a JSON object."""
        details = {}
        for name, info in self.workers().items():
            try:
                data = json.loads(info)
            except ValueError:
                data = None
            details[name] = data if isinstance(data, dict) else {"model": info}
        return details

    def send_command(self, worker: str, command: dict) -> None:
        """Leave an instruction for a worker, such as a change of model."""
        with self.client.pipeline() as pipe:
            pipe.rpush(self._control_key(worker), json.dumps(command))
            pipe.expire(self._control_key(worker), 120)
            pipe.execute()

    def take_commands(self, worker: str) -> list[dict]:
        commands = []
        while True:
            raw = self.client.lpop(self._control_key(worker))
            if raw is None:
                return commands
            try:
                command = json.loads(raw)
            except ValueError:
                continue
            if isinstance(command, dict):
                commands.append(command)

    # --- activity feed ---------------------------------------------------

    def log_event(self, kind: str, text: str, **fields: str) -> None:
        self.client.xadd(
            "events",
            {"ts": self._stamp(), "kind": kind, "text": text, **fields},
            maxlen=EVENTS_KEPT,
            approximate=True,
        )

    def events(self, count: int = 50) -> list[dict[str, str]]:
        """The most recent events, newest first."""
        return [
            {"id": entry_id, **fields}
            for entry_id, fields in self.client.xrevrange("events", count=count)
        ]
