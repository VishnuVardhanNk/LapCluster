import os
from urllib.parse import urlsplit

import pytest

from lapclusters import config
from lapclusters.taskqueue import TaskQueue, connect


def _test_redis_url() -> str:
    # Same server and password as real work, but database 15, so tests never
    # touch real tasks, which live in database 0.
    explicit = os.environ.get("TEST_REDIS_URL")
    return explicit or urlsplit(config.REDIS_URL)._replace(path="/15").geturl()


TEST_REDIS_URL = _test_redis_url()


@pytest.fixture
def queue():
    client = connect(TEST_REDIS_URL)
    client.flushdb()
    task_queue = TaskQueue(client)
    task_queue.ensure_group()
    yield task_queue
    client.flushdb()
    client.close()
