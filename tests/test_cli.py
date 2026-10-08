import threading

import pytest

from lapclusters import cli, config
from lapclusters.cli import submit_and_wait
from lapclusters.worker import process_one


def test_submit_and_wait_returns_result(queue):
    worker = threading.Thread(
        target=process_one,
        args=(queue, "w1", lambda prompt: prompt.upper()),
        kwargs={"block_ms": 3000},
    )
    worker.start()
    info = submit_and_wait(queue, "héllo\nworld", timeout_s=5, poll_s=0.05)
    worker.join()
    assert info["status"] == "done"
    assert info["result"] == "HÉLLO\nWORLD"


def test_submit_and_wait_returns_failed_task(queue):
    def broken(prompt):
        raise RuntimeError("boom")

    worker = threading.Thread(
        target=process_one, args=(queue, "w1", broken), kwargs={"block_ms": 3000}
    )
    worker.start()
    info = submit_and_wait(queue, "hi", timeout_s=5, poll_s=0.05)
    worker.join()
    assert info["status"] == "failed"
    assert info["error"] == "RuntimeError: boom"


def test_submit_and_wait_times_out_without_worker(queue):
    with pytest.raises(TimeoutError, match="Is a worker running"):
        submit_and_wait(queue, "hi", timeout_s=0.3, poll_s=0.05)


def test_main_reports_unreachable_redis(monkeypatch, capsys):
    # Port 1 has nothing listening, so the connection is refused.
    monkeypatch.setattr(config, "REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setattr("sys.argv", ["lapclusters.cli", "hi"])
    assert cli.main() == 1
    assert "Cannot reach Redis" in capsys.readouterr().err
