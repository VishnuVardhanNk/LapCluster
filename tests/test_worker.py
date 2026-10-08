from lapclusters.worker import process_one


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
    assert "ollama unreachable" in info["error"]


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
