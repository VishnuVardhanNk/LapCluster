import httpx
import pytest

from lapclusters import config, llm


def _reply(status: int, body: dict | None = None) -> httpx.Response:
    request = httpx.Request("POST", "http://localhost:11434/api/generate")
    return httpx.Response(status, json=body or {}, request=request)


@pytest.fixture
def replies(monkeypatch):
    """Queue up what the model server will answer; records each request body."""
    script: list = []
    sent: list = []

    def post(url, json, timeout):
        sent.append(json)
        outcome = script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(llm.httpx, "post", post)
    monkeypatch.setattr(llm, "RETRY_DELAY_S", 0)
    return script, sent


def test_returns_the_models_reply_and_passes_the_schema(replies):
    script, sent = replies
    script.append(_reply(200, {"response": "hello"}))
    schema = {"type": "object"}
    assert llm.generate("hi", schema) == "hello"
    assert sent[0]["prompt"] == "hi"
    assert sent[0]["format"] == schema
    assert sent[0]["options"]["num_ctx"] == config.MODEL_CONTEXT
    assert "images" not in sent[0]


def test_passes_pictures_and_a_context_size_when_given(replies):
    script, sent = replies
    script.append(_reply(200, {"response": "a cat"}))
    assert llm.generate("what is this", images=["QUJD"], context=4096) == "a cat"
    assert sent[0]["images"] == ["QUJD"]
    assert sent[0]["options"]["num_ctx"] == 4096


def test_uses_llama_cpp_when_configured(monkeypatch):
    sent = []

    def post(url, json=None, timeout=None, **kwargs):
        sent.append((url, json))
        return httpx.Response(200, json={"content": "hello"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(llm.config, "MODEL_PROVIDER", "llama_cpp")
    monkeypatch.setattr(llm.config, "MODEL", "test-model")
    monkeypatch.setattr(llm.config, "LLAMA_CPP_URL", "http://localhost:8080")
    monkeypatch.setattr(llm.httpx, "post", post)

    assert llm.generate("hi") == "hello"
    assert sent[0][0] == "http://localhost:8080/completion"
    assert sent[0][1]["prompt"] == "hi"
    assert sent[0][1]["model"] == "test-model"


def test_retries_once_when_the_model_server_has_a_hiccup(replies):
    # Ollama answers 500 when the model fails to load, which is often temporary.
    script, sent = replies
    script += [_reply(500), _reply(200, {"response": "second time lucky"})]
    assert llm.generate("hi") == "second time lucky"
    assert len(sent) == 2


def test_retries_once_when_the_model_server_is_briefly_unreachable(replies):
    script, sent = replies
    script += [httpx.ConnectError("refused"), _reply(200, {"response": "ok"})]
    assert llm.generate("hi") == "ok"


def test_gives_up_after_the_second_failure_with_ollamas_own_reason(replies, monkeypatch):
    monkeypatch.setattr(config, "MODEL", "model-a")
    script, sent = replies
    script += [_reply(500), _reply(500, {"error": "CUDA out of memory"})]
    with pytest.raises(llm.LaptopProblem, match=r"Ollama could not run model-a \(CUDA out of memory\)"):
        llm.generate("hi")
    assert len(sent) == 2


def test_a_missing_model_is_named_and_not_retried(replies, monkeypatch):
    # 404 means the model is not installed; asking again will not help.
    monkeypatch.setattr(config, "MODEL", "model-a")
    script, sent = replies
    script.append(_reply(404, {"error": "model 'model-a' not found"}))
    with pytest.raises(llm.LaptopProblem, match="the model model-a is not installed in Ollama"):
        llm.generate("hi")
    assert len(sent) == 1


def test_a_timeout_is_explained_and_not_retried(replies, monkeypatch):
    monkeypatch.setattr(config, "MODEL", "model-a")
    script, sent = replies
    script.append(httpx.ReadTimeout("too slow"))
    with pytest.raises(llm.LaptopProblem, match="model-a did not answer within 30 seconds"):
        llm.generate("hi", timeout=30)
    assert len(sent) == 1


def test_ollama_not_running_is_said_plainly(replies):
    script, sent = replies
    script += [httpx.ConnectError("refused"), httpx.ConnectError("refused")]
    with pytest.raises(llm.LaptopProblem, match="Ollama is not running"):
        llm.generate("hi")
