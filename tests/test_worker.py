import json
import time

import pytest
import redis

from lapclusters.worker import process_one, run_forever, start_heartbeat


def test_process_one_completes_task(queue):
    task_id = queue.add_task("job1", {"prompt": "2+2?"})
    handled = process_one(queue, "w1", lambda prompt: f"echo:{prompt}", block_ms=100)
    assert handled is True
    info = queue.get(task_id)
    assert info["status"] == "done"
    assert info["result"] == "echo:2+2?"
    assert info["worker"] == "w1"


def test_process_one_returns_false_when_idle(queue):
    assert process_one(queue, "w1", lambda prompt: "x", block_ms=100) is False


def test_model_error_marks_task_failed(queue):
    def broken(prompt):
        raise RuntimeError("ollama unreachable")

    task_id = queue.add_task("job1", {"prompt": "hi"})
    handled = process_one(queue, "w1", broken, block_ms=100)
    assert handled is True
    info = queue.get(task_id)
    assert info["status"] == "failed"
    assert info["error"] == "RuntimeError: ollama unreachable"


def test_error_without_message_still_names_the_problem(queue):
    # Some errors, such as an HTTP read timeout, carry no message of their own.
    def silent(prompt):
        raise TimeoutError()

    task_id = queue.add_task("job1", {"prompt": "hi"})
    process_one(queue, "w1", silent, block_ms=100)
    assert queue.get(task_id)["error"] == "TimeoutError"


def test_missing_prompt_marks_task_failed(queue):
    task_id = queue.add_task("job1", {"text": "wrong key"})
    handled = process_one(queue, "w1", lambda prompt: "x", block_ms=100)
    assert handled is True
    info = queue.get(task_id)
    assert info["status"] == "failed"
    assert info["error"] == "task has no prompt"


def test_worker_continues_after_failure(queue):
    def broken_once(prompt):
        if prompt == "bad":
            raise RuntimeError("boom")
        return "ok"

    queue.add_task("job1", {"prompt": "bad"})
    good_id = queue.add_task("job1", {"prompt": "good"})
    process_one(queue, "w1", broken_once, block_ms=100)
    process_one(queue, "w1", broken_once, block_ms=100)
    assert queue.get(good_id)["status"] == "done"


class _StopLoop(Exception):
    pass


class _FlakyQueue:
    """Loses its Redis connection on the first claim, then ends the test."""

    def __init__(self):
        self.claims = 0

    def claim(self, consumer, block_ms=5000):
        self.claims += 1
        if self.claims == 1:
            raise redis.ConnectionError("connection dropped")
        raise _StopLoop()


def test_run_forever_survives_a_lost_redis_connection():
    flaky = _FlakyQueue()
    with pytest.raises(_StopLoop):
        run_forever(flaky, "w1", lambda prompt: "x", retry_delay_s=0)
    assert flaky.claims == 2


def test_review_task_stores_findings_as_json(queue):
    reply = '{"findings": [{"line": 2, "severity": "high", "message": "Divides by zero."}]}'
    task_id = queue.add_task(
        "job1", {"type": "review", "path": "calc.py", "content": "a\nb\n"}, name="calc.py"
    )
    process_one(queue, "w1", lambda prompt, schema=None: reply, block_ms=100)
    info = queue.get(task_id)
    assert info["status"] == "done"
    assert json.loads(info["result"]) == [
        {"line": 2, "severity": "high", "message": "Divides by zero."}
    ]


def test_review_task_with_unusable_replies_is_marked_failed(queue):
    task_id = queue.add_task("job1", {"type": "review", "path": "calc.py", "content": "a\n"})
    process_one(queue, "w1", lambda prompt, schema=None: "no json here", block_ms=100)
    info = queue.get(task_id)
    assert info["status"] == "failed"
    assert info["error"].startswith("model returned invalid JSON twice")


def test_heartbeat_keeps_worker_listed_until_stopped(queue):
    stop = start_heartbeat(queue, "laptop-1", "gemma4:e4b", interval_s=0.05)
    try:
        time.sleep(0.2)
        assert queue.workers() == {"laptop-1": "gemma4:e4b"}
    finally:
        stop.set()


class _HealthyQueue:
    def claim(self, consumer, block_ms=5000):
        raise _StopLoop()


def test_run_forever_switches_to_a_reopened_queue():
    flaky = _FlakyQueue()
    opened = []

    def reopen():
        opened.append(True)
        return _HealthyQueue()

    with pytest.raises(_StopLoop):
        run_forever(flaky, "w1", lambda prompt: "x", retry_delay_s=0, reopen=reopen)
    assert opened == [True]
    assert flaky.claims == 1


def test_run_forever_keeps_trying_when_the_host_cannot_be_found_yet():
    from lapclusters.discovery import DiscoveryError

    flaky = _FlakyQueue()

    def reopen():
        raise DiscoveryError("nobody home")

    with pytest.raises(_StopLoop):
        run_forever(flaky, "w1", lambda prompt: "x", retry_delay_s=0, reopen=reopen)
    assert flaky.claims == 2
