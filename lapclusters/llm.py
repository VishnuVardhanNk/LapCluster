"""The call to the local model."""

from __future__ import annotations

import json
import time
from typing import Callable

import httpx

from lapclusters import config

# Ollama defaults to a 4096-token context, which is too small for source files.
CONTEXT_TOKENS = 8192
RETRY_DELAY_S = 3.0


class ModelError(Exception):
    """The model server answered, but with an error of its own."""


def _is_temporary(exc: Exception) -> bool:
    # A 5xx usually means the model failed to load this once (for example the
    # GPU was busy). A timeout is not retried: it already waited the full limit.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError) and not isinstance(exc, httpx.TimeoutException)


def generate(
    prompt: str,
    schema: dict | None = None,
    timeout: float | None = None,
    on_chunk: Callable[[str | None], None] | None = None,
) -> str:
    """Ask the local model. A JSON `schema` constrains the reply to that shape.

    With `on_chunk`, the reply is streamed: each piece of text is passed to it
    as it is written. It is called with None when a failed attempt is being
    retried, meaning "discard what you have so far".
    """
    if timeout is None:
        timeout = config.MODEL_TIMEOUT_S
    body = {
        "model": config.MODEL,
        "prompt": prompt,
        "stream": on_chunk is not None,
        "options": {"num_ctx": CONTEXT_TOKENS},
    }
    if schema is not None:
        body["format"] = schema
    url = f"{config.OLLAMA_URL}/api/generate"
    for attempt in (1, 2):
        try:
            if on_chunk is None:
                response = httpx.post(url, json=body, timeout=timeout)
                response.raise_for_status()
                return response.json()["response"]
            return _stream(url, body, timeout, on_chunk)
        except httpx.HTTPError as exc:
            if attempt == 2 or not _is_temporary(exc):
                raise
            if on_chunk is not None:
                on_chunk(None)
            time.sleep(RETRY_DELAY_S)
    raise AssertionError("unreachable")


def _stream(url: str, body: dict, timeout: float, on_chunk: Callable[[str | None], None]) -> str:
    pieces: list[str] = []
    with httpx.stream("POST", url, json=body, timeout=timeout) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.strip():
                continue
            data = json.loads(line)
            if data.get("error"):
                raise ModelError(str(data["error"]))
            piece = data.get("response") or ""
            if piece:
                pieces.append(piece)
                on_chunk(piece)
            if data.get("done"):
                break
    return "".join(pieces)


def list_models(timeout: float = 5.0) -> list[str]:
    """Names of the models installed on this laptop's Ollama."""
    response = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=timeout)
    response.raise_for_status()
    return sorted(model["name"] for model in response.json().get("models", []))
