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


def test_task_name_is_stored_with_its_status(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"}, name="src/app.py")
    assert queue.get(task_id)["name"] == "src/app.py"


def test_get_many_returns_each_task_by_id(queue):
    first = queue.add_task("job1", {"prompt": "a"}, name="a")
    second = queue.add_task("job1", {"prompt": "b"}, name="b")
    infos = queue.get_many([first, second])
    assert infos[first]["name"] == "a"
    assert infos[second]["name"] == "b"
    assert queue.get_many([]) == {}


def test_heartbeat_lists_live_workers(queue):
    assert queue.workers() == {}
    queue.heartbeat("laptop-1", "gemma4:e4b")
    queue.heartbeat("laptop-2", "gemma4:e2b")
    assert queue.workers() == {"laptop-1": "gemma4:e4b", "laptop-2": "gemma4:e2b"}


def test_task_abandoned_by_a_dead_worker_is_taken_over(queue):
    queue.reclaim_idle_ms = 0
    task_id = queue.add_task("job1", {"prompt": "hi"})
    queue.claim("dead-worker", block_ms=100)
    task = queue.claim("worker-b", block_ms=100)
    assert task.task_id == task_id
    assert queue.get(task_id)["worker"] == "worker-b"


def test_task_held_by_a_live_worker_is_left_alone(queue):
    queue.reclaim_idle_ms = 0
    queue.add_task("job1", {"prompt": "hi"})
    queue.heartbeat("slow-worker")
    queue.claim("slow-worker", block_ms=100)
    assert queue.claim("worker-b", block_ms=100) is None


def test_restarted_worker_picks_up_its_own_unfinished_task(queue):
    queue.reclaim_idle_ms = 0
    task_id = queue.add_task("job1", {"prompt": "hi"})
    queue.heartbeat("worker-a")
    queue.claim("worker-a", block_ms=100)
    again = queue.claim("worker-a", block_ms=100)
    assert again.task_id == task_id


def test_finished_task_left_unacknowledged_is_not_run_again(queue):
    queue.reclaim_idle_ms = 0
    task_id = queue.add_task("job1", {"prompt": "hi"})
    queue.claim("dead-worker", block_ms=100)
    queue.client.hset(f"task:{task_id}", mapping={"status": "done", "result": "answer"})
    assert queue.claim("worker-b", block_ms=100) is None
    assert queue.get(task_id)["result"] == "answer"
    assert queue.client.xpending(queue.stream, queue.group)["pending"] == 0


def test_task_abandoned_three_times_is_marked_failed(queue):
    queue.reclaim_idle_ms = 0
    task_id = queue.add_task("job1", {"prompt": "hi"})
    queue.claim("dead-1", block_ms=100)
    queue.claim("dead-2", block_ms=100)
    queue.claim("dead-3", block_ms=100)
    assert queue.claim("worker-b", block_ms=100) is None
    info = queue.get(task_id)
    assert info["status"] == "failed"
    assert info["error"] == "abandoned by 3 workers in a row"
