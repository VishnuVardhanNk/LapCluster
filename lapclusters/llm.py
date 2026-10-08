"""The call to the local model."""

from __future__ import annotations

import json
import time
from typing import Callable

import httpx

from lapclusters import config

RETRY_DELAY_S = 3.0


class LaptopProblem(Exception):
    """This laptop could not run the model for a task. The task itself may be
    fine, so it should go to another laptop rather than be marked as failed."""


class ModelError(Exception):
    """The model server answered, but with an error of its own."""


def _is_temporary(exc: Exception) -> bool:
    # A 5xx usually means the model failed to load this once (for example the
    # GPU was busy). A timeout is not retried: it already waited the full limit.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError) and not isinstance(exc, httpx.TimeoutException)


def _server_message(response: httpx.Response) -> str:
    try:
        if not response.is_stream_consumed:
            response.read()
        return str(response.json().get("error", "")).strip()
    except (httpx.HTTPError, ValueError, AttributeError):
        return ""


def _explain(exc: Exception, timeout: float) -> str:
    """Say what went wrong in terms of what the laptop's owner can do about it."""
    if isinstance(exc, httpx.HTTPStatusError):
        detail = _server_message(exc.response)
        if exc.response.status_code == 404:
            return f"the model {config.MODEL} is not installed in Ollama"
        return f"Ollama could not run {config.MODEL}" + (f" ({detail})" if detail else "")
    if isinstance(exc, httpx.TimeoutException):
        return f"{config.MODEL} did not answer within {timeout:g} seconds"
    if isinstance(exc, ModelError):
        return f"Ollama could not run {config.MODEL} ({exc})"
    return "Ollama is not running"


def generate(
    prompt: str,
    schema: dict | None = None,
    timeout: float | None = None,
    on_chunk: Callable[[str | None], None] | None = None,
    images: list[str] | None = None,
    context: int | None = None,
) -> str:
    """Ask the local model.

    A JSON `schema` constrains the reply to that shape. `images` are
    base64-encoded pictures for a model that can see. `context` is how many
    tokens the model may read and write for this request.

    With `on_chunk`, the reply is streamed: each piece of text is passed to it
    as it is written. It is called with None when a failed attempt is being
    retried, meaning "discard what you have so far".

    Raises LaptopProblem when this laptop cannot run the model.
    """
    if timeout is None:
        timeout = config.MODEL_TIMEOUT_S
    body = {
        "model": config.MODEL,
        "prompt": prompt,
        "stream": on_chunk is not None,
        "options": {"num_ctx": int(context or config.MODEL_CONTEXT)},
    }
    if schema is not None:
        body["format"] = schema
    if images:
        body["images"] = images
    url = f"{config.OLLAMA_URL}/api/generate"
    for attempt in (1, 2):
        try:
            if on_chunk is None:
                response = httpx.post(url, json=body, timeout=timeout)
                response.raise_for_status()
                return response.json()["response"]
            return _stream(url, body, timeout, on_chunk)
        except (httpx.HTTPError, ModelError) as exc:
            if attempt == 2 or not _is_temporary(exc):
                raise LaptopProblem(_explain(exc, timeout)) from exc
            if on_chunk is not None:
                on_chunk(None)
            time.sleep(RETRY_DELAY_S)
    raise AssertionError("unreachable")


def _stream(url: str, body: dict, timeout: float, on_chunk: Callable[[str | None], None]) -> str:
    pieces: list[str] = []
    with httpx.stream("POST", url, json=body, timeout=timeout) as response:
        if response.status_code >= 400:
            response.read()  # so the error text can be shown
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


def can_see(model: str, timeout: float = 5.0) -> bool:
    """Whether a model accepts images. A model imported without its image
    projector, or one that is text-only, does not."""
    response = httpx.post(f"{config.OLLAMA_URL}/api/show", json={"model": model}, timeout=timeout)
    response.raise_for_status()
    return "vision" in response.json().get("capabilities", [])
