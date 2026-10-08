"""The call to the local model."""

from __future__ import annotations

import time

import httpx

from lapclusters import config

# Ollama defaults to a 4096-token context, which is too small for source files.
CONTEXT_TOKENS = 8192
RETRY_DELAY_S = 3.0


def _is_temporary(exc: Exception) -> bool:
    # A 5xx usually means the model failed to load this once (for example the
    # GPU was busy). A timeout is not retried: it already waited the full limit.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError) and not isinstance(exc, httpx.TimeoutException)


def generate(prompt: str, schema: dict | None = None, timeout: float | None = None) -> str:
    """Ask the local model. A JSON `schema` constrains the reply to that shape."""
    if timeout is None:
        timeout = config.MODEL_TIMEOUT_S
    body = {
        "model": config.MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": CONTEXT_TOKENS},
    }
    if schema is not None:
        body["format"] = schema
    for attempt in (1, 2):
        try:
            response = httpx.post(f"{config.OLLAMA_URL}/api/generate", json=body, timeout=timeout)
            response.raise_for_status()
            return response.json()["response"]
        except httpx.HTTPError as exc:
            if attempt == 2 or not _is_temporary(exc):
                raise
            time.sleep(RETRY_DELAY_S)
    raise AssertionError("unreachable")
