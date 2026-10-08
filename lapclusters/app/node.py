"""This laptop's place in a cluster: its connection, its worker, and, on the
host, the announcer and the watcher that records who joins and leaves."""

from __future__ import annotations

import threading
import time
from typing import Callable
from urllib.parse import quote, urlsplit

import redis

from lapclusters import config, discovery, llm
from lapclusters.app import views
from lapclusters.taskqueue import CONNECTION_ERRORS, TaskQueue, connect, connection_problem
from lapclusters.worker import Link, Runtime, run_forever

HOST = "host"
MEMBER = "member"

REDIS_NOT_RUNNING = (
    "Redis is not reachable on this laptop. Start Docker Desktop, then run "
    "'docker start redis'. If the container does not exist yet, create it with "
    "'docker run -d --name redis -p 6379:6379 redis:7 redis-server --requirepass YOUR_PASSWORD'."
)


class ConnectError(Exception):
    pass


def redis_url(password: str, host: str, port: int, db: int = 0) -> str:
    credentials = f":{quote(password, safe='')}@" if password else ""
    return f"redis://{credentials}{host}:{port}/{db}"


class Node:
    def __init__(
        self,
        db: int = 0,
        generate: Callable[..., str] = llm.generate,
        announce: bool = True,
        run_worker: bool = True,
    ):
        self.db = db
        self.generate = generate
        self.announce = announce
        self.run_worker = run_worker
        self.name = config.WORKER_NAME
        self.runtime = Runtime(self.name, via_app=True)
        self.role: str | None = None
        self.host: dict | None = None
        self.announcing = False
        self.worker_note = ""
        self._lock = threading.RLock()
        self._queue: TaskQueue | None = None
        self._queue_failed_at = 0.0
        self._worker: threading.Thread | None = None
        self._worker_stop = threading.Event()
        self._beacon_stop: threading.Event | None = None
        self._watch_stop: threading.Event | None = None

    # --- connecting ------------------------------------------------------

    def _open(self, url: str, unreachable: str) -> TaskQueue:
        queue = TaskQueue(connect(url))
        try:
            queue.client.ping()
            queue.ensure_group()
        except redis.AuthenticationError as exc:
            raise ConnectError("That password was not accepted.") from exc
        except CONNECTION_ERRORS as exc:
            raise ConnectError(unreachable) from exc
        return queue

    def connect_host(self, password: str, port: int = 6379) -> None:
        """Make this laptop the host of a cluster, using the Redis running on it."""
        url = redis_url(password, "localhost", port, self.db)
        queue = self._open(url, REDIS_NOT_RUNNING)
        with self._lock:
            self.disconnect()
            config.REDIS_URL = url
            config.CLUSTER_HOST = ""
            self.role = HOST
            self.host = {"name": self.name, "address": "localhost", "port": port}
            self._queue = queue
            if self.announce:
                try:
                    self._beacon_stop, _ = discovery.start_beacon(self.name, port)
                    self.announcing = True
                except OSError:
                    # 'python -m lapclusters.host' is already announcing from a terminal.
                    self.announcing = False
            self._watch_stop = threading.Event()
            threading.Thread(target=self._watch, args=(self._watch_stop,), daemon=True).start()
            queue.log_event("cluster", f"{self.name} is hosting the cluster", worker=self.name)
            if self.run_worker:
                self.start_worker()

    def connect_member(
        self, password: str, name: str, address: str, port: int = 6379, follow: bool = True
    ) -> None:
        """Join another laptop's cluster.

        With `follow`, the host is looked up by name again whenever the
        connection drops, so a change of address is survived. Without it the
        address is fixed, for networks where discovery does not work.
        """
        url = redis_url(password, address, port, self.db)
        queue = self._open(
            url,
            f"Could not reach {name or address}. Check that both laptops are on the same "
            "network and that the host allows connections on its Redis port.",
        )
        with self._lock:
            self.disconnect()
            if follow:
                config.REDIS_URL = redis_url(password, discovery.AUTO, port, self.db)
                config.CLUSTER_HOST = name
            else:
                config.REDIS_URL = url
                config.CLUSTER_HOST = ""
            self.role = MEMBER
            self.host = {"name": name or address, "address": address, "port": port}
            self._queue = queue
            if self.run_worker:
                self.start_worker()

    def disconnect(self) -> None:
        with self._lock:
            self.stop_worker()
            for stop in (self._beacon_stop, self._watch_stop):
                if stop is not None:
                    stop.set()
            self._beacon_stop = self._watch_stop = None
            self.announcing = False
            self.role = None
            self.host = None
            self._queue = None
            self.worker_note = ""

    def queue(self) -> TaskQueue:
        """The connection the dashboard reads through, reopened after a failure."""
        with self._lock:
            if self.role is None:
                raise redis.ConnectionError("not connected")
            if self._queue is None:
                # Do not hammer a host that is away; try again every few seconds.
                if time.monotonic() - self._queue_failed_at < 3:
                    raise redis.ConnectionError("reconnecting")
                try:
                    url = discovery.resolve(config.REDIS_URL, ask=_never_ask)
                    self._queue = TaskQueue(connect(url))
                    self._queue.client.ping()
                    if self.role == MEMBER and self.host is not None:
                        # The host may have come back on a different address.
                        self.host["address"] = urlsplit(url).hostname or self.host["address"]
                except Exception:
                    self._queue = None
                    self._queue_failed_at = time.monotonic()
                    raise
            return self._queue

    def lost_connection(self) -> None:
        with self._lock:
            self._queue = None
            self._queue_failed_at = time.monotonic()

    # --- this laptop's worker --------------------------------------------

    @property
    def worker_state(self) -> str:
        if self._worker is None or not self._worker.is_alive():
            return "stopped"
        return "stopping" if self._worker_stop.is_set() else "running"

    def start_worker(self) -> str:
        """Start reviewing files on this laptop. Returns a problem, or ""."""
        with self._lock:
            if self.role is None:
                return "Connect to a cluster first."
            if self._worker is not None and self._worker.is_alive():
                self._worker_stop.clear()  # it was finishing its last file; carry on instead
                return ""
            self.worker_note = ""
            self._worker_stop = threading.Event()
            self._worker = threading.Thread(
                target=self._work, args=(self._worker_stop,), daemon=True
            )
            self._worker.start()
            return ""

    def stop_worker(self) -> None:
        """Stop taking new files. A file already being reviewed is finished first."""
        self._worker_stop.set()

    def _name_in_use(self, stop: threading.Event) -> bool:
        """Is another worker with this laptop's name alive right now?

        A heartbeat may simply be left over from this app's previous run, which
        happens whenever the app is restarted within a few seconds. A leftover
        only counts down; a live worker keeps pushing its expiry back up. So
        watch it: wait for a leftover to clear, refuse only a live one.
        """
        try:
            queue = self.queue()
            left = queue.heartbeat_left_ms(self.name)
            if left < 0:
                return False
            self.worker_note = "Waiting a few seconds for this laptop's previous worker to clear."
            while not stop.is_set():
                stop.wait(0.5)
                now_left = queue.heartbeat_left_ms(self.name)
                if now_left < 0:
                    self.worker_note = ""
                    return False
                if now_left > left:
                    self.worker_note = (
                        f"A worker named {self.name} is already running, probably in a terminal "
                        "on this laptop. Stop that one, then choose Start reviewing."
                    )
                    return True
                left = now_left
        except (*CONNECTION_ERRORS, discovery.DiscoveryError):
            return False  # opening the link below reports the connection problem
        return True

    def _work(self, stop: threading.Event) -> None:
        if self._name_in_use(stop):
            return
        link = Link(self.runtime)
        try:
            queue = None
            while queue is None and not stop.is_set():
                try:
                    queue = link.open()
                    self.worker_note = ""
                except CONNECTION_ERRORS as exc:
                    self.worker_note = connection_problem(exc)
                    stop.wait(3)
                except discovery.DiscoveryError as exc:
                    self.worker_note = str(exc)
                    stop.wait(3)
            if queue is not None:
                run_forever(
                    queue,
                    self.name,
                    self.generate,
                    reopen=link.open,
                    should_stop=stop.is_set,
                    block_ms=2000,
                )
        except Exception as exc:  # a bug here must be visible, not a silent dead thread
            self.worker_note = f"The worker stopped unexpectedly: {type(exc).__name__}: {exc}"
        finally:
            link.close()

    def set_own_model(self, model: str) -> str:
        """Switch this laptop's model. Returns a problem, or ""."""
        problem = self.runtime.set_model(model)
        if problem:
            return problem
        try:
            queue = self.queue()
            if self.worker_state != "stopped":
                queue.heartbeat(self.name, self.runtime.info())
            queue.log_event(
                "model", f"{self.name} switched to {model}", worker=self.name, by=self.name
            )
        except CONNECTION_ERRORS:
            self.lost_connection()
        return ""

    # --- host duties -----------------------------------------------------

    def _watch(self, stop: threading.Event) -> None:
        """Record laptops joining and leaving, and close reviews that have ended."""
        known: set[str] | None = None
        while not stop.wait(2):
            try:
                queue = self.queue()
                # The host's own worker is announced by "is hosting the cluster".
                names = set(queue.workers()) - {self.name}
                if known is not None:
                    for name in sorted(names - known):
                        queue.log_event("join", f"{name} joined the cluster", worker=name)
                    for name in sorted(known - names):
                        queue.log_event("leave", f"{name} left the cluster", worker=name)
                known = names
                for job_id in queue.recent_jobs(1):
                    views.job_view(queue, job_id, close=True, with_tasks=False)
            except CONNECTION_ERRORS:
                self.lost_connection()
            except discovery.DiscoveryError:
                pass


def _never_ask(hosts: list[discovery.Host]) -> discovery.Host:
    raise discovery.DiscoveryError(
        "Several hosts are on this network. Choose one on the Connect screen."
    )
