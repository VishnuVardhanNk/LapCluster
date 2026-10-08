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
# A task handed back this many times by laptops that could not run it is failed.
MAX_ATTEMPTS = 4
# Separate queues for work only some laptops can do. "" is the ordinary one.
LANES = ("", "vision")
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
    # Which queue it came from; see LANES.
    lane: str = ""


class TaskQueue:
    def __init__(
        self,
        client: redis.Redis,
        # Not "tasks", which earlier versions read: a laptop still running one
        # would take new-style tasks it cannot handle. On its own stream it
        # simply receives nothing until it is updated.
        stream: str = "work",
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

    def _payload_key(self, task_id: str) -> str:
        return f"payload:{task_id}"

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

    def _stream(self, lane: str) -> str:
        return f"{self.stream}:{lane}" if lane else self.stream

    def _lane_of(self, stream: str) -> str:
        return stream[len(self.stream) + 1:] if stream != self.stream else ""

    def now(self) -> float:
        """The Redis server's clock, so every laptop stamps times the same way."""
        seconds, microseconds = self.client.time()
        return seconds + microseconds / 1_000_000

    def _stamp(self) -> str:
        return f"{self.now():.3f}"

    def ensure_group(self) -> None:
        for lane in LANES:
            try:
                self.client.xgroup_create(self._stream(lane), self.group, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    # --- tasks -----------------------------------------------------------

    def add_task(
        self,
        job_id: str,
        payload: dict[str, str],
        name: str = "",
        lane: str = "",
        **fields: str,
    ) -> str:
        """Queue a task.

        `name` is a short label shown in reports and the dashboard. `lane`
        restricts it to laptops that can do that kind of work. `fields` are
        stored with the task's status for display.
        """
        task_id = uuid.uuid4().hex
        record = {
            **fields,
            "status": PENDING,
            "job_id": job_id,
            "name": name,
            "lane": lane,
            "attempts": "0",
            "queued_at": self._stamp(),
        }
        # One transaction, so a worker never sees a task without its records.
        with self.client.pipeline() as pipe:
            pipe.hset(self._task_key(task_id), mapping=record)
            pipe.set(self._payload_key(task_id), json.dumps(payload))
            pipe.sadd(self._job_key(job_id), task_id)
            pipe.xadd(self._stream(lane), {"task_id": task_id, "job_id": job_id})
            pipe.execute()
        return task_id

    def claim(
        self, consumer: str, block_ms: int = 5000, lanes: tuple[str, ...] | list[str] = ("",)
    ) -> Task | None:
        """Take the next task: first one a dead worker left behind, else a new one.

        Only the given lanes are looked at, so a laptop is never handed work it
        cannot do.
        """
        try:
            task = self._reclaim_abandoned(consumer, lanes) or self._read_new(
                consumer, block_ms, lanes
            )
        except redis.ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            # Redis was restarted and lost the streams; recreate them and carry on.
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

    def _read_new(self, consumer: str, block_ms: int, lanes) -> Task | None:
        streams = {self._stream(lane): ">" for lane in lanes}
        while True:
            response = self.client.xreadgroup(
                self.group, consumer, streams, count=1, block=block_ms
            )
            if not response:
                return None
            stream, entries = response[0]
            # Reading several lanes at once can deliver one entry from each.
            # A worker does one task at a time, so put the others straight back
            # for whichever laptop is free.
            for other_stream, other_entries in response[1:]:
                for entry_id, fields in other_entries:
                    with self.client.pipeline() as pipe:
                        pipe.xadd(other_stream, {k: fields[k] for k in ("task_id", "job_id")})
                        pipe.xack(other_stream, self.group, entry_id)
                        pipe.execute()
            task = self._to_task(stream, *entries[0])
            if task is not None:
                return task
            # Cancelled before any worker started it, or its data is gone.

    def _to_task(self, stream: str, entry_id: str, fields: dict[str, str]) -> Task | None:
        """Build a Task from a stream entry, or acknowledge and drop an entry
        that should not run: one already finished or cancelled, or one whose
        data has been deleted."""
        task_id = fields["task_id"]
        lane = self._lane_of(stream)
        status = self.client.hget(self._task_key(task_id), "status")
        # Entries written by an earlier version carry their payload inline.
        raw = fields.get("payload") or self.client.get(self._payload_key(task_id))
        if status in FINISHED or status is None or raw is None:
            self.client.xack(stream, self.group, entry_id)
            if status not in FINISHED and status is not None:
                self.client.hset(
                    self._task_key(task_id),
                    mapping={"status": FAILED, "error": "the task's data is missing",
                             "finished_at": self._stamp()},
                )
            return None
        return Task(entry_id, task_id, fields["job_id"], json.loads(raw), lane=lane)

    def _reclaim_abandoned(self, consumer: str, lanes) -> Task | None:
        for lane in lanes:
            stream = self._stream(lane)
            pending = self.client.xpending_range(stream, self.group, min="-", max="+", count=100)
            for entry in pending:
                owner = entry["consumer"]
                # A worker only asks for work when it is free, so anything still
                # under its own name is left over from before it restarted.
                if owner != consumer and self.client.exists(self._worker_key(owner)):
                    continue
                claimed = self.client.xclaim(
                    stream,
                    self.group,
                    consumer,
                    min_idle_time=self.reclaim_idle_ms,
                    message_ids=[entry["message_id"]],
                )
                if not claimed or claimed[0][1] is None:
                    continue
                # Also drops an entry whose previous worker stored its answer
                # but died before acknowledging.
                task = self._to_task(stream, *claimed[0])
                if task is None:
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
        self.client.xack(self._stream(task.lane), self.group, task.entry_id)

    def release(self, task: Task, reason: str) -> bool:
        """Hand a task back because this laptop could not run it.

        It returns to the queue for another laptop. Returns False if it has
        now been handed back too often and was failed instead.
        """
        attempts = self.client.hincrby(self._task_key(task.task_id), "attempts", 1)
        if attempts >= MAX_ATTEMPTS:
            self.fail(task, f"{reason}. Gave up after {attempts} attempts.")
            return False
        stream = self._stream(task.lane)
        with self.client.pipeline() as pipe:
            pipe.hset(
                self._task_key(task.task_id), mapping={"status": PENDING, "last_error": reason}
            )
            pipe.hdel(self._task_key(task.task_id), "started_at")
            pipe.xadd(stream, {"task_id": task.task_id, "job_id": task.job_id})
            pipe.xack(stream, self.group, task.entry_id)
            pipe.execute()
        return True

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

    def retry_failed(self, job_id: str) -> int:
        """Put a job's failed tasks back in the queue. Returns how many."""
        retried = 0
        rows = self.get_fields(self.job_tasks(job_id), ["status", "lane"])
        for task_id, row in rows.items():
            if row.get("status") != FAILED or not self.client.exists(self._payload_key(task_id)):
                continue
            with self.client.pipeline() as pipe:
                pipe.hset(self._task_key(task_id), mapping={"status": PENDING, "attempts": "0"})
                pipe.hdel(
                    self._task_key(task_id),
                    "error", "result", "raw", "worker", "model", "started_at", "finished_at",
                    "taken_over_from", "last_error",
                )
                pipe.delete(self._output_key(task_id))
                pipe.xadd(self._stream(row.get("lane", "")), {"task_id": task_id, "job_id": job_id})
                pipe.execute()
            retried += 1
        return retried

    def remove_tasks(self, job_id: str, task_ids: list[str]) -> None:
        """Delete tasks and everything stored for them."""
        if not task_ids:
            return
        with self.client.pipeline() as pipe:
            for task_id in task_ids:
                pipe.delete(
                    self._task_key(task_id), self._payload_key(task_id), self._output_key(task_id)
                )
                pipe.srem(self._job_key(job_id), task_id)
            pipe.execute()

    def create_job(self, job_id: str, **meta: str) -> None:
        """Record a job so it shows up in the dashboard and in history."""
        with self.client.pipeline() as pipe:
            pipe.hset(self._job_meta_key(job_id), mapping={"created_at": self._stamp(), **meta})
            pipe.zadd("jobs", {job_id: self.now()})
            pipe.execute()

    def update_job(self, job_id: str, **meta: str) -> None:
        self.client.hset(self._job_meta_key(job_id), mapping=meta)

    def clear_job_fields(self, job_id: str, *fields: str) -> None:
        self.client.hdel(self._job_meta_key(job_id), *fields)

    def close_job(self, job_id: str, **meta: str) -> bool:
        """Record that a job has ended. Returns False if it was already closed,
        so that only one caller announces it."""
        if not self.client.hsetnx(self._job_meta_key(job_id), "finished_at", meta["finished_at"]):
            return False
        self.client.hset(self._job_meta_key(job_id), mapping=meta)
        return True

    def lock(self, name: str, seconds: int = 30) -> bool:
        """Take a short-lived lock. Returns False if someone else holds it."""
        return bool(self.client.set(f"lock:{name}", "1", nx=True, ex=seconds))

    def unlock(self, name: str) -> None:
        self.client.delete(f"lock:{name}")

    def job_meta(self, job_id: str) -> dict[str, str]:
        return self.client.hgetall(self._job_meta_key(job_id))

    def recent_jobs(self, count: int = 20) -> list[str]:
        """Job ids, newest first."""
        return list(self.client.zrevrange("jobs", 0, count - 1))

    def delete_job(self, job_id: str) -> None:
        """Remove a job from history along with all of its tasks."""
        self.remove_tasks(job_id, self.job_tasks(job_id))
        with self.client.pipeline() as pipe:
            pipe.delete(self._job_key(job_id), self._job_meta_key(job_id))
            pipe.zrem("jobs", job_id)
            pipe.execute()

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
