"""The queue, model and worker features the dashboard is built on."""

import json
import time

import httpx
import pytest

from lapclusters import config, llm
from lapclusters.review import run_detailed
from lapclusters.taskqueue import TaskError
from lapclusters.worker import Runtime, process_one, start_heartbeat

FINDINGS = '{"findings": [{"line": 1, "severity": "high", "message": "Stub."}]}'


def streaming_model(prompt, schema=None, on_chunk=None):
    if on_chunk:
        on_chunk(FINDINGS[:20])
        on_chunk(FINDINGS[20:])
    return FINDINGS


# --- queue -----------------------------------------------------------------


def test_task_times_come_from_one_clock_and_are_in_order(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"})
    queue.complete(queue.claim("w1", block_ms=100), "answer", model="m1")
    info = queue.get(task_id)
    assert float(info["queued_at"]) <= float(info["started_at"]) <= float(info["finished_at"])
    assert abs(float(info["finished_at"]) - queue.now()) < 5
    assert info["model"] == "m1"


def test_get_fields_leaves_out_everything_else(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"}, name="a.py")
    queue.complete(queue.claim("w1", block_ms=100), "answer", prompt="x" * 1000)
    rows = queue.get_fields([task_id], ["status", "name", "error"])
    assert rows == {task_id: {"status": "done", "name": "a.py"}}
    assert queue.get_fields([], ["status"]) == {}


def test_live_output_is_read_in_steps(queue):
    queue.append_output("t1", "héllo ")
    text, offset = queue.read_output("t1")
    assert (text, offset) == ("héllo ", 1)
    queue.append_output("t1", "wörld")
    queue.append_output("t1", "")
    assert queue.read_output("t1", offset) == ("wörld", 2)
    assert queue.read_output("t1", 2) == ("", 2)


def test_reading_past_a_restarted_output_starts_again(queue):
    queue.append_output("t1", "first attempt")
    queue.append_output("t1", " continued")
    queue.reset_output("t1")
    queue.append_output("t1", "second attempt")
    assert queue.read_output("t1", 2) == ("second attempt", 1)


def test_events_are_listed_newest_first(queue):
    queue.log_event("join", "a joined", worker="a")
    queue.log_event("leave", "a left", worker="a")
    events = queue.events()
    assert [e["text"] for e in events] == ["a left", "a joined"]
    assert events[0]["kind"] == "leave" and events[0]["worker"] == "a"
    assert float(events[0]["ts"]) > 0


def test_commands_reach_only_their_worker_and_only_once(queue):
    queue.send_command("w1", {"cmd": "set_model", "model": "m2"})
    queue.client.rpush("control:w1", "not json")
    assert queue.take_commands("w2") == []
    assert queue.take_commands("w1") == [{"cmd": "set_model", "model": "m2"}]
    assert queue.take_commands("w1") == []


def test_worker_details_reads_old_and_new_heartbeats(queue):
    queue.heartbeat("old", "gemma4:e4b")
    queue.heartbeat("new", json.dumps({"model": "m1", "models": ["m1", "m2"]}))
    assert queue.worker_details() == {
        "new": {"model": "m1", "models": ["m1", "m2"]},
        "old": {"model": "gemma4:e4b"},
    }


def test_forgetting_a_worker_removes_it_at_once(queue):
    queue.heartbeat("w1", "m")
    queue.forget_worker("w1")
    assert queue.workers() == {}


def test_jobs_are_recorded_newest_first_and_closed_once(queue):
    queue.create_job("j1", source="a", status="running")
    time.sleep(0.01)
    queue.create_job("j2", source="b", status="running")
    assert queue.recent_jobs() == ["j2", "j1"]
    assert queue.job_meta("j1")["source"] == "a"
    assert queue.close_job("j1", status="done", finished_at="5.0") is True
    assert queue.close_job("j1", status="cancelled", finished_at="9.0") is False
    assert queue.job_meta("j1")["status"] == "done"
    assert queue.job_meta("j1")["finished_at"] == "5.0"


def test_takeover_is_recorded_on_the_task_and_in_the_feed(queue):
    queue.reclaim_idle_ms = 0
    task_id = queue.add_task("job1", {"prompt": "hi"}, name="a.py")
    queue.claim("dead-worker", block_ms=100)
    queue.claim("w2", block_ms=100)
    assert queue.get(task_id)["taken_over_from"] == "dead-worker"
    assert "w2 took over a.py from dead-worker" in queue.events()[0]["text"]


def test_cancelled_tasks_get_a_finish_time(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"})
    queue.cancel_job("job1")
    assert float(queue.get(task_id)["finished_at"]) > 0


# --- model -----------------------------------------------------------------


class _Stream:
    def __init__(self, lines, status=200):
        self._lines = lines
        self.status_code = status
        self.request = httpx.Request("POST", "http://localhost:11434/api/generate")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=self.request, response=self)

    def iter_lines(self):
        yield from self._lines


def _lines(*pieces):
    out = [json.dumps({"response": p, "done": False}) for p in pieces]
    return [*out, "", json.dumps({"response": "", "done": True})]


def test_streaming_passes_each_piece_on_and_returns_the_whole(monkeypatch):
    sent = {}

    def stream(method, url, json, timeout):
        sent.update(json)
        return _Stream(_lines("Hel", "lo"))

    monkeypatch.setattr(llm.httpx, "stream", stream)
    seen = []
    assert llm.generate("hi", on_chunk=seen.append) == "Hello"
    assert seen == ["Hel", "lo"]
    assert sent["stream"] is True


def test_streaming_retry_tells_the_listener_to_start_over(monkeypatch):
    attempts = iter([_Stream([], status=500), _Stream(_lines("ok"))])
    monkeypatch.setattr(llm.httpx, "stream", lambda *a, **k: next(attempts))
    monkeypatch.setattr(llm, "RETRY_DELAY_S", 0)
    seen = []
    assert llm.generate("hi", on_chunk=seen.append) == "ok"
    assert seen == [None, "ok"]


def test_streaming_reports_an_error_sent_inside_the_stream(monkeypatch):
    lines = [json.dumps({"error": "model ran out of memory"})]
    monkeypatch.setattr(llm.httpx, "stream", lambda *a, **k: _Stream(lines))
    with pytest.raises(llm.ModelError, match="out of memory"):
        llm.generate("hi", on_chunk=lambda piece: None)


def test_list_models_returns_sorted_names(monkeypatch):
    reply = httpx.Response(
        200,
        json={"models": [{"name": "gemma4:e4b"}, {"name": "alpha:1b"}]},
        request=httpx.Request("GET", "http://localhost:11434/api/tags"),
    )
    monkeypatch.setattr(llm.httpx, "get", lambda url, timeout: reply)
    assert llm.list_models() == ["alpha:1b", "gemma4:e4b"]


# --- review and worker -----------------------------------------------------


def test_review_keeps_the_prompt_the_reply_and_severity_counts():
    result, details = run_detailed({"path": "a.py", "content": "x\n"}, lambda p, s=None: FINDINGS)
    assert json.loads(result)[0]["message"] == "Stub."
    assert "a.py" in details["prompt"]
    assert details["raw"] == FINDINGS
    assert (details["count_high"], details["count_medium"], details["count_low"]) == ("1", "0", "0")


def test_failed_review_keeps_the_unusable_reply_for_inspection():
    with pytest.raises(TaskError) as caught:
        run_detailed({"path": "a.py", "content": "x\n"}, lambda p, s=None: "not json at all")
    assert caught.value.details["raw"] == "not json at all"


def test_worker_streams_the_reply_and_stores_everything(queue, monkeypatch):
    monkeypatch.setattr(config, "MODEL", "model-a")
    task_id = queue.add_task(
        "job1", {"type": "review", "path": "a.py", "content": "x\n"}, name="a.py"
    )
    process_one(queue, "w1", streaming_model, block_ms=100)
    info = queue.get(task_id)
    assert info["status"] == "done"
    assert info["model"] == "model-a"
    assert info["raw"] == FINDINGS
    assert info["count_high"] == "1"
    assert "a.py" in info["prompt"]
    assert queue.read_output(task_id)[0] == FINDINGS


def test_the_model_is_visible_while_a_file_is_still_being_reviewed(queue, monkeypatch):
    monkeypatch.setattr(config, "MODEL", "model-a")
    task_id = queue.add_task("job1", {"prompt": "hi"})
    seen = {}

    def model(prompt):
        seen.update(queue.get(task_id))
        return "answer"

    process_one(queue, "w1", model, block_ms=100)
    assert (seen["status"], seen["model"]) == ("running", "model-a")


def test_worker_keeps_the_bad_reply_when_a_review_fails(queue):
    task_id = queue.add_task("job1", {"type": "review", "path": "a.py", "content": "x\n"})
    process_one(queue, "w1", lambda prompt, schema=None: "garbage", block_ms=100)
    info = queue.get(task_id)
    assert info["status"] == "failed"
    assert info["raw"] == "garbage"
    assert info["model"] == config.MODEL


def test_second_attempt_replaces_the_first_attempts_live_output(queue):
    replies = iter(["garbage", FINDINGS])

    def model(prompt, schema=None, on_chunk=None):
        text = next(replies)
        on_chunk(text)
        return text

    task_id = queue.add_task("job1", {"type": "review", "path": "a.py", "content": "x\n"})
    process_one(queue, "w1", model, block_ms=100)
    assert queue.read_output(task_id)[0] == FINDINGS


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(llm, "list_models", lambda timeout=5.0: ["model-a", "model-b"])
    monkeypatch.setattr(config, "MODEL", "model-a")


def test_runtime_switches_only_to_an_installed_model(installed):
    runtime = Runtime("w1")
    assert runtime.set_model("model-b") == ""
    assert config.MODEL == "model-b"
    assert runtime.set_model("model-z") == "model-z is not installed on w1"
    assert config.MODEL == "model-b"


def test_runtime_reports_when_ollama_is_down(monkeypatch):
    def down(timeout=5.0):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(llm, "list_models", down)
    runtime = Runtime("w1")
    assert runtime.set_model("model-b") == "Ollama is not running on this laptop"
    assert json.loads(runtime.info())["ollama"] is False


def test_heartbeat_reports_models_and_obeys_a_switch_from_another_laptop(queue, installed):
    runtime = Runtime("w1")
    stop = start_heartbeat(queue, "w1", runtime, interval_s=0.05)
    try:
        time.sleep(0.2)
        assert queue.worker_details()["w1"]["models"] == ["model-a", "model-b"]
        queue.send_command("w1", {"cmd": "set_model", "model": "model-b", "by": "host-pc"})
        time.sleep(0.3)
        assert config.MODEL == "model-b"
        assert queue.worker_details()["w1"]["model"] == "model-b"
        assert queue.events()[0]["text"] == "host-pc switched w1 to model-b"
    finally:
        stop.set()


def test_switch_to_a_missing_model_is_refused_and_explained(queue, installed):
    runtime = Runtime("w1")
    runtime.handle({"cmd": "set_model", "model": "model-z", "by": "host-pc"}, queue)
    assert config.MODEL == "model-a"
    assert "but model-z is not installed on w1" in queue.events()[0]["text"]
