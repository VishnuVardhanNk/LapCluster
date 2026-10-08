"""The call to the local model."""

from __future__ import annotations

import httpx

from lapclusters import config

# Ollama defaults to a 4096-token context, which is too small for source files.
CONTEXT_TOKENS = 8192


def generate(prompt: str, schema: dict | None = None, timeout: float = 300.0) -> str:
    """Ask the local model. A JSON `schema` constrains the reply to that shape."""
    body = {
        "model": config.MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": CONTEXT_TOKENS},
    }
    if schema is not None:
        body["format"] = schema
    response = httpx.post(f"{config.OLLAMA_URL}/api/generate", json=body, timeout=timeout)
    response.raise_for_status()
    return response.json()["response"]
