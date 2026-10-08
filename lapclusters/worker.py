"""A worker: takes tasks from the queue and answers them with the local model."""

from __future__ import annotations

import inspect
import json
import sys
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx

from lapclusters import config, discovery, llm, review
from lapclusters.taskqueue import (
    CONNECTION_ERRORS,
    TaskError,
    TaskQueue,
    connect,
    connection_problem,
)

# How often the list of installed models is re-read, in heartbeats.
MODEL_REFRESH_BEATS = 6


def _describe(exc: Exception) -> str:
    # Some errors, such as an HTTP read timeout, have an empty message.
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class Runtime:
    """What this laptop's worker tells the cluster about itself, and the model
    it is currently using."""

    def __init__(self, name: str, via_app: bool = False):
        self.name = name
        self.via_app = via_app
        self.models: list[str] = []
        self.ollama_ok = False

    def refresh_models(self) -> None:
        try:
            self.models = llm.list_models()
            self.ollama_ok = True
        except (httpx.HTTPError, ValueError, KeyError):
            self.ollama_ok = False

    def info(self) -> str:
        return json.dumps(
            {
                "model": config.MODEL,
                "models": self.models,
                "ollama": self.ollama_ok,
                "app": self.via_app,
            }
        )

    def set_model(self, model: str) -> str:
        """Switch model. Returns an error message, or "" when it worked.

        The switch applies to the next file; one already being reviewed finishes
        on the model it started with.
        """
        self.refresh_models()
        if not self.ollama_ok:
            return "Ollama is not running on this laptop"
        if model not in self.models:
            return f"{model} is not installed on {self.name}"
        config.MODEL = model
        return ""

    def handle(self, command: dict, queue: TaskQueue) -> None:
        """Carry out an instruction another laptop sent through Redis."""
        if command.get("cmd") != "set_model":
            return
        model = str(command.get("model", ""))
        sender = str(command.get("by", "another laptop"))
        problem = self.set_model(model)
        if problem:
            queue.log_event(
                "model", f"{sender} asked {self.name} to use {model}, but {problem}",
                worker=self.name, by=sender,
            )
        else:
            queue.log_event(
                "model", f"{sender} switched {self.name} to {model}",
                worker=self.name, by=sender,
            )
        queue.heartbeat(self.name, self.info())


class _LiveOutput:
    """Passes the model's reply to Redis while it is being written, in small
    batches so a fast model does not flood the network."""

    def __init__(self, queue: TaskQueue, task_id: str, interval_s: float = 0.25):
        self.queue = queue
        self.task_id = task_id
        self.interval_s = interval_s
        self._buffer: list[str] = []
        self._last_flush = time.monotonic()

    def __call__(self, piece: str | None) -> None:
        if piece is None:
            self.restart()
            return
        self._buffer.append(piece)
        if time.monotonic() - self._last_flush >= self.interval_s:
            self.flush()

    def restart(self) -> None:
        self._buffer.clear()
        try:
            self.queue.reset_output(self.task_id)
        except CONNECTION_ERRORS:
            pass

    def flush(self) -> None:
        text, self._buffer = "".join(self._buffer), []
        self._last_flush = time.monotonic()
        try:
            self.queue.append_output(self.task_id, text)
        except CONNECTION_ERRORS:
            pass  # live output is a nicety; the stored result is what matters


def _streams(generate: Callable[..., str]) -> bool:
    try:
        return "on_chunk" in inspect.signature(generate).parameters
    except (TypeError, ValueError):
        return False


def run_task(
    payload: dict[str, str],
    generate: Callable[..., str],
    live: Callable[[str | None], None] | None = None,
) -> tuple[str, dict[str, str]]:
    """Do the work a task describes.

    Returns the result as text, plus extra fields worth keeping with the task.
    """

    def ask(prompt: str, schema: dict | None = None) -> str:
        if live is None or not _streams(generate):
            return generate(prompt) if schema is None else generate(prompt, schema)
        live(None)  # a second attempt replaces the first one's output
        return generate(prompt, schema, on_chunk=live)

    if payload.get("type") == "review":
        return review.run_detailed(payload, ask)
    if "prompt" not in payload:
        raise TaskError("task has no prompt")
    return ask(payload["prompt"]), {"prompt": payload["prompt"]}


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
    live = _LiveOutput(queue, task.task_id)
    # The model in use when the task started is the one that reviews it.
    model = {"model": config.MODEL}
    queue.note(task, **model)
    try:
        result, details = run_task(task.payload, generate, live)
    except TaskError as exc:
        live.flush()
        queue.fail(task, str(exc), **exc.details, **model)
    except Exception as exc:
        live.flush()
        queue.fail(task, _describe(exc), **model)
    else:
        live.flush()
        queue.complete(task, result, **details, **model)
    return True


def start_heartbeat(
    queue: TaskQueue,
    name: str,
    info: str | Runtime = "",
    interval_s: float = 3.0,
) -> threading.Event:
    """Keep this worker listed as alive from a background thread.

    The model call blocks the main loop for a long time, so the heartbeat
    cannot live there. Given a Runtime, the same thread also keeps its list of
    installed models fresh and carries out instructions sent by other laptops.
    Set the returned event to stop it.
    """
    stop = threading.Event()
    runtime = info if isinstance(info, Runtime) else None

    def beat() -> None:
        beats = 0
        while not stop.is_set():
            try:
                if runtime is None:
                    queue.heartbeat(name, info)
                else:
                    if beats % MODEL_REFRESH_BEATS == 0:
                        runtime.refresh_models()
                    queue.heartbeat(name, runtime.info())
                    for command in queue.take_commands(name):
                        runtime.handle(command, queue)
            except CONNECTION_ERRORS:
                pass
            beats += 1
            stop.wait(interval_s)

    threading.Thread(target=beat, daemon=True).start()
    return stop


def run_forever(
    queue: TaskQueue,
    consumer: str,
    generate: Callable[..., str],
    retry_delay_s: float = 3.0,
    reopen: Callable[[], TaskQueue] | None = None,
    should_stop: Callable[[], bool] | None = None,
    block_ms: int = 5000,
) -> None:
    """Handle tasks until interrupted, or until `should_stop` says so.

    After a lost connection, `reopen` is asked for a fresh queue. That lets a
    worker follow a host whose address has changed.
    """
    while not (should_stop and should_stop()):
        try:
            if process_one(queue, consumer, generate, block_ms=block_ms):
                print("Handled one task")
        except CONNECTION_ERRORS:
            print(f"Lost connection to Redis, retrying in {retry_delay_s:g} seconds")
            time.sleep(retry_delay_s)
            if reopen is not None:
                try:
                    queue = reopen()
                except (*CONNECTION_ERRORS, discovery.DiscoveryError):
                    pass  # still unreachable; keep the old queue and try again


class Link:
    """The worker's connection to the cluster, replaceable when the host moves."""

    def __init__(self, runtime: Runtime | None = None) -> None:
        self.runtime = runtime or Runtime(config.WORKER_NAME)
        self.queue: TaskQueue | None = None
        self._stop_heartbeat: threading.Event | None = None

    def open(self) -> TaskQueue:
        url = discovery.resolve(config.REDIS_URL)
        queue = TaskQueue(connect(url))
        queue.ensure_group()
        self.runtime.refresh_models()
        queue.heartbeat(self.runtime.name, self.runtime.info())
        if self._stop_heartbeat is not None:
            self._stop_heartbeat.set()
        self._stop_heartbeat = start_heartbeat(queue, self.runtime.name, self.runtime)
        self.queue = queue
        if discovery.is_auto(config.REDIS_URL):
            print(f"Connected to the host at {urlsplit(url).hostname}")
        return queue

    def close(self) -> None:
        """Stop the heartbeat and leave the cluster's list of workers at once."""
        if self._stop_heartbeat is not None:
            self._stop_heartbeat.set()
            self._stop_heartbeat = None
        if self.queue is not None:
            try:
                self.queue.forget_worker(self.runtime.name)
            except CONNECTION_ERRORS:
                pass


def main() -> int:
    link = Link()
    try:
        queue = link.open()
    except discovery.DiscoveryError as exc:
        print(exc, file=sys.stderr)
        return 1
    except CONNECTION_ERRORS as exc:
        print(connection_problem(exc), file=sys.stderr)
        return 1
    print(f"Worker {config.WORKER_NAME} ready, model {config.MODEL}")
    try:
        run_forever(queue, config.WORKER_NAME, llm.generate, reopen=link.open)
    except KeyboardInterrupt:
        link.close()
        print("Worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
