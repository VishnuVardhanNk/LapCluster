def test_idle_claim_outlasts_default_socket_timeout(queue):
    # redis-py times out socket reads after 5 seconds by default, which made an
    # idle worker crash while waiting for its first task.
    assert queue.claim("worker-a", block_ms=5500) is None


def test_add_task_registers_task_under_its_job(queue):
    first = queue.add_task("job1", {"prompt": "a"})
    second = queue.add_task("job1", {"prompt": "b"})
    queue.add_task("job2", {"prompt": "c"})
    assert sorted(queue.job_tasks("job1")) == sorted([first, second])


def test_new_task_is_pending(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"})
    info = queue.get(task_id)
    assert info["status"] == "pending"
    assert info["job_id"] == "job1"


def test_claim_returns_task_and_marks_running(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"})
    task = queue.claim("worker-a", block_ms=100)
    assert task.task_id == task_id
    assert task.job_id == "job1"
    assert task.payload == {"prompt": "hi"}
    info = queue.get(task_id)
    assert info["status"] == "running"
    assert info["worker"] == "worker-a"


def test_claim_returns_none_when_empty(queue):
    assert queue.claim("worker-a", block_ms=100) is None


def test_complete_stores_result_and_acknowledges(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"})
    task = queue.claim("worker-a", block_ms=100)
    queue.complete(task, "hello")
    info = queue.get(task_id)
    assert info["status"] == "done"
    assert info["result"] == "hello"
    assert queue.client.xpending(queue.stream, queue.group)["pending"] == 0


def test_fail_stores_error_and_acknowledges(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"})
    task = queue.claim("worker-a", block_ms=100)
    queue.fail(task, "model crashed")
    info = queue.get(task_id)
    assert info["status"] == "failed"
    assert info["error"] == "model crashed"
    assert queue.client.xpending(queue.stream, queue.group)["pending"] == 0


def test_payload_keeps_unicode_and_newlines(queue):
    queue.add_task("job1", {"prompt": "héllo\nwörld"})
    task = queue.claim("worker-a", block_ms=100)
    assert task.payload["prompt"] == "héllo\nwörld"


def test_ensure_group_twice_is_safe(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"})
    queue.ensure_group()
    assert queue.get(task_id)["status"] == "pending"
    assert queue.claim("worker-a", block_ms=100).task_id == task_id


def test_task_goes_to_only_one_worker(queue):
    queue.add_task("job1", {"prompt": "hi"})
    first = queue.claim("worker-a", block_ms=100)
    second = queue.claim("worker-b", block_ms=100)
    assert first is not None
    assert second is None
