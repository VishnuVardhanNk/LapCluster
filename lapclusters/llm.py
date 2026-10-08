import httpx

from lapclusters import config


def generate(prompt: str, timeout: float = 300.0) -> str:
    response = httpx.post(
        f"{config.OLLAMA_URL}/api/generate",
        json={
            "model": config.MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"num_ctx": 8192},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()["response"]
