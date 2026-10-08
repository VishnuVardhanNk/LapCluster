"""The call to the local model."""

import httpx

from lapclusters import config

# Ollama defaults to a 4096-token context, which is too small for source files.
CONTEXT_TOKENS = 8192


def generate(prompt: str, timeout: float = 300.0) -> str:
    response = httpx.post(
        f"{config.OLLAMA_URL}/api/generate",
        json={
            "model": config.MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"num_ctx": CONTEXT_TOKENS},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()["response"]
