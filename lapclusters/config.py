"""Settings read from environment variables or a local .env file."""

import os
import socket

from dotenv import load_dotenv

load_dotenv()


def _setting(name: str, default: str) -> str:
    # An empty value in .env counts as "not set".
    return os.environ.get(name) or default


REDIS_URL = _setting("REDIS_URL", "redis://localhost:6379/0")
OLLAMA_URL = _setting("OLLAMA_URL", "http://localhost:11434")
MODEL = _setting("MODEL", "gemma4:e4b")
WORKER_NAME = _setting("WORKER_NAME", socket.gethostname())
