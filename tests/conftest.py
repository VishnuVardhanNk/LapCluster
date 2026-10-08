import os

import pytest
import redis

from lapclusters.taskqueue import TaskQueue

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
def queue():
    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    client.flushdb()
    task_queue = TaskQueue(client)
    task_queue.ensure_group()
    yield task_queue
    client.flushdb()
    client.close()
