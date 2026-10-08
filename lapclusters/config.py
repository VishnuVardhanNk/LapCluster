"""Settings read from environment variables or a local .env file."""

import os
import socket

from dotenv import load_dotenv

load_dotenv()


def _setting(name: str, default: str) -> str:
    # An empty value in .env counts as "not set".
    return os.environ.get(name) or default


REDIS_URL = _setting("REDIS_URL", "redis://localhost:6379/0")
MODEL_PROVIDER = _setting("MODEL_PROVIDER", "ollama").lower()
if MODEL_PROVIDER not in {"ollama", "llama_cpp"}:
    raise SystemExit("MODEL_PROVIDER in .env must be 'ollama' or 'llama_cpp'.")
OLLAMA_URL = _setting("OLLAMA_URL", "http://localhost:11434")
LLAMA_CPP_URL = _setting("LLAMA_CPP_URL", "http://localhost:9931")
# Manual override for llama.cpp vision capability: "true", "false", or "" (ask the server).
LLAMA_CPP_VISION = _setting("LLAMA_CPP_VISION", "")
MODEL = _setting("MODEL", "gemma4:e4b")
WORKER_NAME = _setting("WORKER_NAME", socket.gethostname())
# Only needed when REDIS_URL uses the host name "auto" and several hosts answer.
CLUSTER_HOST = _setting("CLUSTER_HOST", "")
# Tokens the model may read and write per request. 32768 fits Gemma 4 E4B fully
# on an 8 GB graphics card such as an RTX 4060 (measured: about 5.1 GB used).
# The host's value is sent with every task, so the whole cluster uses the same.
MODEL_CONTEXT = int(_setting("MODEL_CONTEXT", "32768"))
# Largest piece of one file sent in a single task, in characters. Longer files
# are split into overlapping parts. It must fit inside MODEL_CONTEXT together
# with the instructions, the line numbers and the reply.
PART_CHARS = int(_setting("PART_CHARS", "48000"))
# Seconds to wait for one model reply. Laptops without a GPU can be very slow.
try:
    MODEL_TIMEOUT_S = float(_setting("MODEL_TIMEOUT_S", "600"))
except ValueError:
    raise SystemExit("MODEL_TIMEOUT_S in .env must be a number of seconds, such as 600.")
