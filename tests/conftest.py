import os

import pytest

from lapclusters.taskqueue import TaskQueue, connect

# Database 15 keeps test data away from real tasks, which live in database 0.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
def queue():
    client = connect(TEST_REDIS_URL)
    client.flushdb()
    task_queue = TaskQueue(client)
    task_queue.ensure_group()
    yield task_queue
    client.flushdb()
    client.close()
