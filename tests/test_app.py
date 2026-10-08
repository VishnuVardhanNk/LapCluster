"""The local web server, driven the way the browser drives it."""

import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from lapclusters import config, llm
from lapclusters.app.node import Node
from lapclusters.app.server import create_app
from tests.conftest import TEST_REDIS_URL

SAMPLE_REPO = str(Path(__file__).parent / "fixtures" / "sample_repo")
FINDINGS = '{"findings": [{"line": 1, "severity": "high", "message": "Stub."}]}'
PASSWORD = unquote(urlsplit(TEST_REDIS_URL).password or "")
PORT = urlsplit(TEST_REDIS_URL).port or 6379


def stub_model(prompt, schema=None, on_chunk=None):
    if on_chunk:
        on_chunk(FINDINGS)
    return FINDINGS


def wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition was not met in time")


@pytest.fixture
def make_app(queue, monkeypatch):
    """Build an app on the test database; everything is torn down afterwards."""
    monkeypatch.setattr(config, "REDIS_URL", config.REDIS_URL)
    monkeypatch.setattr(config, "CLUSTER_HOST", config.CLUSTER_HOST)
    monkeypatch.setattr(config, "MODEL", "model-a")
    monkeypatch.setattr(llm, "list_models", lambda timeout=5.0: ["model-a", "model-b"])
    monkeypatch.setattr(llm, "can_see", lambda model, timeout=5.0: True)
    nodes = []

    def build(run_worker=True):
        node = Node(db=15, generate=stub_model, announce=False, run_worker=run_worker)
        nodes.append(node)
        return TestClient(create_app(node), headers={"x-lapclusters": "1"}), node

    yield build
    for node in nodes:
        worker = node._worker
        node.disconnect()
        if worker is not None:
            worker.join(timeout=10)


def host(client):
    reply = client.post("/api/connect", json={"role": "host", "password": PASSWORD, "port": PORT})
    assert reply.status_code == 200, reply.text


def finished_job(client):
    def done():
        job = client.get("/api/state").json().get("job")
        return job if job and job["status"] != "running" and job["status"] != "preparing" else None

    return wait_for(done)


# --- protection ------------------------------------------------------------


def test_changes_need_the_dashboards_own_header(make_app):
    client, _ = make_app()
    plain = TestClient(client.app)
    assert plain.post("/api/disconnect").status_code == 403
    assert plain.get("/api/state").status_code == 200


def test_requests_for_another_host_name_are_refused(make_app):
    client, _ = make_app()
    reply = client.get("/api/state", headers={"host": "evil.example.com"})
    assert reply.status_code == 403


# --- connecting ------------------------------------------------------------


def test_state_before_connecting(make_app):
    client, node = make_app()
    state = client.get("/api/state").json()
    assert state["role"] is None
    assert state["connected"] is False
    assert state["me"] == node.name
    assert state["models"] == ["model-a", "model-b"]
    assert state["ollama"] is True


def test_hosting_starts_this_laptops_worker(make_app):
    client, node = make_app()
    host(client)
    state = wait_for(lambda: (s := client.get("/api/state").json())["workers"] and s)
    assert state["role"] == "host"
    assert state["connected"] is True
    assert state["worker"]["state"] == "running"
    assert state["workers"][node.name]["model"] == "model-a"
    assert state["workers"][node.name]["models"] == ["model-a", "model-b"]
    assert state["workers"][node.name]["app"] is True


def test_wrong_password_is_explained(make_app):
    client, _ = make_app()
    reply = client.post(
        "/api/connect", json={"role": "host", "password": PASSWORD + "-wrong", "port": PORT}
    )
    assert reply.status_code == 400
    assert reply.json()["error"] == "That password was not accepted."


def test_hosting_without_redis_says_how_to_start_it(make_app):
    client, _ = make_app()
    reply = client.post("/api/connect", json={"role": "host", "password": "", "port": 1})
    assert reply.status_code == 400
    assert "docker start redis" in reply.json()["error"]


def test_disconnecting_stops_the_worker_and_leaves_the_cluster(make_app, queue):
    client, node = make_app()
    host(client)
    wait_for(lambda: node.name in queue.workers())
    client.post("/api/disconnect")
    wait_for(lambda: node.name not in queue.workers())
    assert client.get("/api/state").json()["role"] is None


# --- reviews ---------------------------------------------------------------


def test_a_review_runs_from_start_to_report(make_app):
    client, node = make_app()
    host(client)
    started = client.post("/api/reviews", json={"source": SAMPLE_REPO})
    assert started.status_code == 200
    job = finished_job(client)
    assert job["id"] == started.json()["job"]
    assert job["status"] == "done"
    assert (job["total"], job["done"], job["failed"]) == (3, 3, 0)
    assert job["findings"] == {"high": 3, "medium": 0, "low": 0}
    assert job["laptops"] == {node.name: 3}
    assert job["duration"] is not None and job["duration"] >= 0
    assert [t["name"] for t in job["tasks"]] == ["app.js", "calculator.py", "users.py"]
    assert all(t["model"] == "model-a" and t["worker"] == node.name for t in job["tasks"])

    task = client.get(f"/api/tasks/{job['tasks'][0]['id']}").json()
    assert task["findings"] == [{"line": 1, "severity": "high", "message": "Stub."}]
    assert task["raw"] == FINDINGS
    assert "app.js" in task["prompt"]
    assert task["queued_at"] <= task["started_at"] <= task["finished_at"]

    output = client.get(f"/api/tasks/{job['tasks'][0]['id']}/output").json()
    assert output == {"text": FINDINGS, "next": 1, "restarted": False}

    findings = client.get(f"/api/reviews/{job['id']}/findings").json()
    assert len(findings["findings"]) == 3
    assert findings["findings"][0]["model"] == "model-a"
    assert findings["not_reviewed"] == []

    report = client.get(f"/api/reviews/{job['id']}/report.md")
    assert report.headers["content-type"].startswith("text/markdown")
    assert "Findings: 3 (3 high, 0 medium, 0 low)" in report.text
    assert f"{node.name} (model-a): 3 files" in report.text


def test_a_finished_review_is_announced_once_and_summarised(make_app):
    client, node = make_app()
    host(client)
    client.post("/api/reviews", json={"source": SAMPLE_REPO})
    finished_job(client)
    client.get("/api/state")
    state = client.get("/api/state").json()
    announcements = [e for e in state["events"] if "finished" in e["text"]]
    assert len(announcements) == 1
    assert "3 of 3 tasks" in announcements[0]["text"]
    assert state["jobs"][0]["status"] == "done"
    assert state["jobs"][0]["laptops"] == {node.name: 3}


def test_a_review_of_a_missing_folder_fails_with_the_reason(make_app):
    client, _ = make_app()
    host(client)
    client.post("/api/reviews", json={"source": "C:/no/such/folder"})
    job = finished_job(client)
    assert job["status"] == "failed"
    assert "Not a folder" in job["error"]
    assert job["total"] == 0


def test_an_empty_source_is_rejected(make_app):
    client, _ = make_app()
    host(client)
    assert client.post("/api/reviews", json={"source": "   "}).status_code == 400


def test_cancelling_a_review_stops_unstarted_files(make_app):
    client, _ = make_app(run_worker=False)
    host(client)
    job_id = client.post("/api/reviews", json={"source": SAMPLE_REPO}).json()["job"]
    wait_for(lambda: (j := client.get("/api/state").json()["job"]) and j["total"] == 3)
    reply = client.post(f"/api/reviews/{job_id}/cancel")
    assert reply.json() == {"ok": True, "cancelled": 3}
    job = client.get("/api/state").json()["job"]
    assert job["status"] == "cancelled"
    assert job["failed"] == 3


def test_older_reviews_can_be_selected(make_app):
    client, _ = make_app()
    host(client)
    first = client.post("/api/reviews", json={"source": SAMPLE_REPO}).json()["job"]
    finished_job(client)
    second = client.post("/api/reviews", json={"source": "C:/no/such/folder"}).json()["job"]
    wait_for(lambda: client.get("/api/state").json()["job"]["id"] == second)
    assert client.get("/api/state", params={"job": first}).json()["job"]["id"] == first
    assert [j["id"] for j in client.get("/api/state").json()["jobs"]] == [second, first]


def test_unknown_review_and_file_are_not_found(make_app):
    client, _ = make_app()
    host(client)
    assert client.get("/api/reviews/nope/findings").status_code == 404
    assert client.get("/api/reviews/nope/report.md").status_code == 404
    assert client.post("/api/reviews/nope/cancel").status_code == 404
    assert client.get("/api/tasks/nope").status_code == 404


def test_only_the_host_starts_and_cancels_reviews(make_app):
    client, _ = make_app(run_worker=False)
    reply = client.post(
        "/api/connect",
        json={
            "role": "member", "password": PASSWORD, "name": "SomeHost",
            "address": "localhost", "port": PORT, "follow": False,
        },
    )
    assert reply.status_code == 200, reply.text
    assert client.get("/api/state").json()["role"] == "member"
    assert client.post("/api/reviews", json={"source": SAMPLE_REPO}).status_code == 403
    assert client.post("/api/reviews/x/cancel").status_code == 403


# --- worker and models -----------------------------------------------------


def test_worker_can_be_stopped_and_started_again(make_app, queue):
    client, node = make_app()
    host(client)
    wait_for(lambda: node.name in queue.workers())
    client.post("/api/worker", json={"action": "stop"})
    wait_for(lambda: client.get("/api/state").json()["worker"]["state"] == "stopped")
    wait_for(lambda: node.name not in queue.workers())
    assert client.post("/api/worker", json={"action": "start"}).status_code == 200
    wait_for(lambda: node.name in queue.workers())


def test_a_second_live_worker_with_the_same_name_is_refused(make_app, queue):
    from lapclusters.worker import start_heartbeat

    client, node = make_app(run_worker=False)
    host(client)
    other = start_heartbeat(queue, node.name, "gemma4:e4b", interval_s=0.2)
    try:
        time.sleep(0.3)
        assert client.post("/api/worker", json={"action": "start"}).status_code == 200
        state = wait_for(
            lambda: (s := client.get("/api/state").json())["worker"]["state"] == "stopped"
            and "already running" in s["worker"]["note"]
            and s
        )
        assert state["workers"][node.name] == {"model": "gemma4:e4b"}  # the other one, untouched
    finally:
        other.set()


def test_a_leftover_heartbeat_from_a_restart_does_not_block_the_worker(make_app, queue):
    client, node = make_app(run_worker=False)
    host(client)
    queue.client.set(f"worker:{node.name}", "left over", px=1500)
    client.post("/api/worker", json={"action": "start"})
    assert "previous worker" in wait_for(lambda: client.get("/api/state").json()["worker"]["note"])
    state = wait_for(
        lambda: (s := client.get("/api/state").json())["workers"].get(node.name, {}).get("app") and s
    )
    assert state["worker"] == {"state": "running", "note": ""}


def test_switching_this_laptops_model(make_app, queue):
    client, node = make_app()
    host(client)
    wait_for(lambda: node.name in queue.workers())
    reply = client.post("/api/model", json={"worker": node.name, "model": "model-b"})
    assert reply.json() == {"ok": True, "applied": True}
    state = client.get("/api/state").json()
    assert state["model"] == "model-b"
    assert state["workers"][node.name]["model"] == "model-b"
    assert state["events"][0]["text"] == f"{node.name} switched to model-b"

    missing = client.post("/api/model", json={"worker": node.name, "model": "model-z"})
    assert missing.status_code == 400
    assert config.MODEL == "model-b"


def test_host_switches_another_laptops_model_through_redis(make_app, queue):
    client, node = make_app(run_worker=False)
    host(client)
    queue.heartbeat("other-pc", '{"model": "model-a", "models": ["model-a", "model-b"]}')
    queue.heartbeat("old-pc", "gemma4:e4b")

    reply = client.post("/api/model", json={"worker": "other-pc", "model": "model-b"})
    assert reply.json() == {"ok": True, "applied": False}
    assert queue.take_commands("other-pc") == [
        {"cmd": "set_model", "model": "model-b", "by": node.name}
    ]

    def problem(worker, model):
        return client.post("/api/model", json={"worker": worker, "model": model}).json()["error"]

    assert problem("other-pc", "model-z") == "model-z is not installed on other-pc."
    assert problem("gone-pc", "model-a") == "gone-pc is not connected."
    assert "older worker" in problem("old-pc", "model-a")


def test_a_member_cannot_switch_another_laptops_model(make_app, queue):
    client, _ = make_app(run_worker=False)
    client.post(
        "/api/connect",
        json={
            "role": "member", "password": PASSWORD, "name": "SomeHost",
            "address": "localhost", "port": PORT, "follow": False,
        },
    )
    queue.heartbeat("other-pc", '{"model": "model-a", "models": ["model-a", "model-b"]}')
    reply = client.post("/api/model", json={"worker": "other-pc", "model": "model-b"})
    assert reply.status_code == 403


# --- other kinds of job, retry, history, memory ----------------------------


def kinds_model(prompt, schema=None, on_chunk=None, images=None, context=None):
    if schema is not None:
        return FINDINGS
    if "Several files were each examined" in prompt:
        return "COMBINED"
    if "careful programmer" in prompt:
        return "CODE"
    return "ANSWER"


def start(client, **body):
    reply = client.post("/api/reviews", json=body)
    assert reply.status_code == 200, reply.text
    return reply.json()["job"]


def test_a_prompt_job_returns_its_answer(make_app):
    client, node = make_app()
    node.generate = kinds_model
    host(client)
    job_id = start(client, kind="prompt", question="What is a queue?")
    job = finished_job(client)
    assert (job["id"], job["kind"], job["status"], job["answer"]) == (job_id, "prompt", "done", "ANSWER")
    assert job["question"] == "What is a queue?"
    assert client.get(f"/api/tasks/{job['answer_task']}").json()["type"] == "prompt"
    report = client.get(f"/api/reviews/{job_id}/report.md").text
    assert "## Answer\n\nANSWER" in report and "- Question: What is a queue?" in report


def test_a_code_job_wraps_the_request(make_app):
    client, node = make_app()
    node.generate = kinds_model
    host(client)
    start(client, kind="code", question="Reverse a string")
    assert finished_job(client)["answer"] == "CODE"


def test_a_question_about_files_is_combined_by_the_host(make_app):
    client, node = make_app()
    node.generate = kinds_model
    host(client)
    start(client, kind="ask", source=SAMPLE_REPO, question="What does this do?")
    job = finished_job(client)
    assert (job["kind"], job["status"], job["answer"]) == ("ask", "done", "COMBINED")
    assert [t["name"] for t in job["tasks"]][-1] == "Combined answer"
    assert job["total"] == 5  # three source files, notes.txt, and the combining step
    notes = client.get(f"/api/reviews/{job['id']}/findings").json()["notes"]
    assert len(notes) == 4 and notes[0]["text"] == "ANSWER"


def test_job_requests_are_validated(make_app):
    client, _ = make_app(run_worker=False)
    host(client)

    def problem(**body):
        reply = client.post("/api/reviews", json=body)
        assert reply.status_code == 400
        return reply.json()["error"]

    assert problem(kind="prompt", question="  ") == "Type what you want to ask."
    assert problem(kind="code") == "Type what you want to ask."
    assert problem(kind="ask", question="Q?") == "Enter a folder or a git URL."
    assert problem(kind="nonsense", source="x") == "Unknown kind of job."
    # A question about files has a sensible default.
    job_id = start(client, kind="ask", source=SAMPLE_REPO)
    wait_for(lambda: client.get("/api/state").json()["job"]["total"] == 4)
    assert client.get("/api/state").json()["job"]["question"] == "Summarise what this file contains."
    client.post(f"/api/reviews/{job_id}/cancel")


def test_failed_tasks_can_be_run_again(make_app):
    client, node = make_app()
    broken = {"on": True}

    def model(prompt, schema=None, on_chunk=None):
        if broken["on"] and "users.py" in prompt:
            raise RuntimeError("boom")
        return FINDINGS

    node.generate = model
    host(client)
    job_id = start(client, source=SAMPLE_REPO)
    job = finished_job(client)
    assert (job["done"], job["failed"], job["status"]) == (2, 1, "done")

    broken["on"] = False
    assert client.post(f"/api/reviews/{job_id}/retry").json() == {"ok": True, "retried": 1}
    job = wait_for(lambda: (j := client.get("/api/state").json()["job"])["done"] == 3 and j)
    assert (job["failed"], job["status"]) == (0, "done")
    assert client.post("/api/reviews/nope/retry").status_code == 404


def test_clearing_history_keeps_a_job_that_is_still_running(make_app):
    client, _ = make_app()
    host(client)
    start(client, source=SAMPLE_REPO)
    finished_job(client)
    client.post("/api/worker", json={"action": "stop"})
    wait_for(lambda: client.get("/api/state").json()["worker"]["state"] == "stopped")
    running = start(client, source=SAMPLE_REPO)
    wait_for(lambda: client.get("/api/state").json()["job"]["total"] == 3)
    assert len(client.get("/api/state").json()["jobs"]) == 2
    assert client.post("/api/history/clear").json() == {"ok": True, "deleted": 1}
    assert [j["id"] for j in client.get("/api/state").json()["jobs"]] == [running]
    client.post(f"/api/reviews/{running}/cancel")


def test_members_cannot_retry_or_clear(make_app):
    client, _ = make_app(run_worker=False)
    client.post(
        "/api/connect",
        json={"role": "member", "password": PASSWORD, "name": "SomeHost",
              "address": "localhost", "port": PORT, "follow": False},
    )
    assert client.post("/api/reviews/x/retry").status_code == 403
    assert client.post("/api/history/clear").status_code == 403
    assert client.post("/api/reviews", json={"kind": "prompt", "question": "hi"}).status_code == 403


def test_the_last_connection_is_remembered_until_the_user_leaves(queue, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "REDIS_URL", config.REDIS_URL)
    monkeypatch.setattr(config, "CLUSTER_HOST", config.CLUSTER_HOST)
    monkeypatch.setattr(llm, "list_models", lambda timeout=5.0: ["model-a"])
    monkeypatch.setattr(llm, "can_see", lambda model, timeout=5.0: False)
    memory = tmp_path / "memory.json"

    first = Node(db=15, announce=False, run_worker=False, memory=memory)
    first.connect_host(PASSWORD, PORT)
    assert memory.exists()
    first.disconnect()  # as when the app is closed; the memory stays

    second = Node(db=15, announce=False, run_worker=False, memory=memory)
    second.restore()
    assert (second.role, second.restore_problem) == ("host", "")
    second.leave()  # the user chose Leave; do not come back next time
    assert not memory.exists()

    third = Node(db=15, announce=False, run_worker=False, memory=memory)
    third.restore()
    assert third.role is None


def test_a_stale_memory_reports_why_it_could_not_reconnect(queue, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "REDIS_URL", config.REDIS_URL)
    monkeypatch.setattr(llm, "list_models", lambda timeout=5.0: ["model-a"])
    monkeypatch.setattr(llm, "can_see", lambda model, timeout=5.0: False)
    memory = tmp_path / "memory.json"
    memory.write_text('{"role": "host", "password": "", "port": 1}', encoding="utf-8")
    node = Node(db=15, announce=False, run_worker=False, memory=memory)
    node.restore()
    assert node.role is None
    assert node.restore_problem.startswith("Could not reconnect to the cluster used last time.")
    memory.write_text("not json", encoding="utf-8")
    node.restore_problem = ""
    node.restore()
    assert (node.role, node.restore_problem) == (None, "")


def test_a_laptop_whose_model_is_missing_shows_why_and_takes_nothing(make_app, monkeypatch):
    client, node = make_app()
    monkeypatch.setattr(config, "MODEL", "model-missing")
    host(client)
    start(client, source=SAMPLE_REPO)
    state = wait_for(
        lambda: (s := client.get("/api/state").json())["worker"]["note"]
        and node.name in s["workers"] and s["job"] and s["job"]["total"] == 3 and s
    )
    assert "model-missing is not installed on this laptop" in state["worker"]["note"]
    assert (state["job"]["failed"], state["job"]["pending"]) == (0, 3)  # nothing burned
    assert state["workers"][node.name]["ready"] is False
    # Choosing an installed model is all it takes.
    client.post("/api/model", json={"worker": node.name, "model": "model-a"})
    job = finished_job(client)
    assert (job["done"], job["failed"]) == (3, 0)
