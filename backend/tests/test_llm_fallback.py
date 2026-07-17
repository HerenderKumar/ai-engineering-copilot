"""
LLM fallback router + OllamaClient tests — no network, no real LLMs.

Router semantics under test:
  * primary success → fallback provider never touched
  * primary failure → fallback result returned (async, stream, and sync)
  * streaming falls back only BEFORE the first token; after a token the
    stream closes with the legacy inline "[System Error: ...]" tail instead
    of restarting (no duplicated partial answers)
  * every provider failed → RuntimeError (non-stream) / inline error (stream)
  * OLLAMA_ENABLED=false removes ollama from the chain

OllamaClient wire format is tested against httpx.MockTransport.
"""

import asyncio
import json

import httpx
import pytest

from app.core.config import settings
from app.services.llm import router as router_mod
from app.services.llm.gemini import gemini_client
from app.services.llm.ollama import OllamaClient, ollama_client
from app.services.llm.router import LLMRouter, ask_llm


class FakeProvider:
    """Yields `chunks`, then raises `error` if set (error with no chunks =
    failure before the first token)."""

    def __init__(self, name, response=None, error=None, chunks=()):
        self.name = name
        self._response = response
        self._error = error
        self._chunks = list(chunks)
        self.calls = 0

    async def generate_response(self, prompt):
        self.calls += 1
        if self._error:
            raise RuntimeError(self._error)
        return self._response

    async def generate_stream(self, prompt):
        self.calls += 1
        for chunk in self._chunks:
            yield chunk
        if self._error:
            raise RuntimeError(self._error)


async def _collect(agen):
    return [chunk async for chunk in agen]


# --- LLMRouter.generate_response -------------------------------------------

def test_response_primary_success_skips_fallback():
    primary = FakeProvider("p1", response="primary answer")
    fallback = FakeProvider("p2", response="fallback answer")
    router = LLMRouter(providers=[primary, fallback])
    assert asyncio.run(router.generate_response("q")) == "primary answer"
    assert fallback.calls == 0


def test_response_falls_back_on_failure():
    primary = FakeProvider("p1", error="boom")
    fallback = FakeProvider("p2", response="fallback answer")
    router = LLMRouter(providers=[primary, fallback])
    assert asyncio.run(router.generate_response("q")) == "fallback answer"


def test_response_all_providers_failed_raises_with_both_errors():
    router = LLMRouter(providers=[FakeProvider("p1", error="e1"),
                                  FakeProvider("p2", error="e2")])
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(router.generate_response("q"))
    assert "p1: e1" in str(exc.value) and "p2: e2" in str(exc.value)


def test_response_real_gemini_keyless_falls_back():
    # conftest sets GEMINI_API_KEY="" → the real client raises "not set",
    # which must route to the fallback.
    fallback = FakeProvider("p2", response="local answer")
    router = LLMRouter(providers=[gemini_client, fallback])
    assert asyncio.run(router.generate_response("q")) == "local answer"


# --- LLMRouter.generate_stream ----------------------------------------------

def test_stream_primary_success_skips_fallback():
    primary = FakeProvider("p1", chunks=["a", "b"])
    fallback = FakeProvider("p2", chunks=["nope"])
    router = LLMRouter(providers=[primary, fallback])
    assert asyncio.run(_collect(router.generate_stream("q"))) == ["a", "b"]
    assert fallback.calls == 0


def test_stream_falls_back_before_first_token():
    primary = FakeProvider("p1", error="down")
    fallback = FakeProvider("p2", chunks=["x", "y"])
    router = LLMRouter(providers=[primary, fallback])
    assert asyncio.run(_collect(router.generate_stream("q"))) == ["x", "y"]


def test_stream_no_fallback_after_first_token():
    primary = FakeProvider("p1", chunks=["partial "], error="mid-stream boom")
    fallback = FakeProvider("p2", chunks=["fresh answer"])
    router = LLMRouter(providers=[primary, fallback])
    out = asyncio.run(_collect(router.generate_stream("q")))
    assert out[0] == "partial "
    assert "System Error" in out[1] and "mid-stream boom" in out[1]
    assert fallback.calls == 0  # switching now would duplicate the answer


def test_stream_all_providers_failed_yields_inline_error():
    router = LLMRouter(providers=[FakeProvider("p1", error="e1"),
                                  FakeProvider("p2", error="e2")])
    out = asyncio.run(_collect(router.generate_stream("q")))
    assert len(out) == 1
    assert "System Error" in out[0]
    assert "p1: e1" in out[0] and "p2: e2" in out[0]


# --- provider chain / settings ----------------------------------------------

def test_default_chain_respects_ollama_enabled(monkeypatch):
    monkeypatch.setattr(settings, "OLLAMA_ENABLED", True)
    assert [p.name for p in LLMRouter()._providers()] == ["gemini", "ollama"]
    monkeypatch.setattr(settings, "OLLAMA_ENABLED", False)
    assert [p.name for p in LLMRouter()._providers()] == ["gemini"]


# --- ask_llm (sync path used by the analysis layer) --------------------------

def test_ask_llm_keyless_gemini_falls_back_to_ollama(monkeypatch):
    monkeypatch.setattr(settings, "OLLAMA_ENABLED", True)
    monkeypatch.setattr(ollama_client, "generate_sync", lambda prompt: "local summary")
    assert ask_llm("summarize") == "local summary"


def test_ask_llm_all_failed_raises(monkeypatch):
    def _fail(prompt):
        raise RuntimeError("no server")
    monkeypatch.setattr(settings, "OLLAMA_ENABLED", True)
    monkeypatch.setattr(ollama_client, "generate_sync", _fail)
    with pytest.raises(RuntimeError, match="All LLM providers failed"):
        ask_llm("summarize")


def test_ask_llm_respects_disabled_ollama(monkeypatch):
    monkeypatch.setattr(settings, "OLLAMA_ENABLED", False)
    names = [name for name, _ in router_mod._sync_providers()]
    assert names == ["gemini"]


# --- OllamaClient wire format (httpx.MockTransport, no server) ---------------

def _chat_response_body(content):
    return {"model": "m", "message": {"role": "assistant", "content": content},
            "done": True}


def test_ollama_generate_response_parses_chat_payload():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_response_body("hello from ollama"))

    client = OllamaClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(client.generate_response("hi")) == "hello from ollama"
    assert seen["url"].endswith("/api/chat")
    assert seen["payload"]["model"] == settings.OLLAMA_MODEL
    assert seen["payload"]["stream"] is False
    assert seen["payload"]["messages"] == [{"role": "user", "content": "hi"}]
    assert seen["payload"]["options"]["num_ctx"] == settings.OLLAMA_NUM_CTX
    assert seen["payload"]["keep_alive"] == settings.OLLAMA_KEEP_ALIVE


def test_ollama_generate_stream_parses_ndjson():
    lines = [
        json.dumps({"message": {"content": "Hel"}, "done": False}),
        json.dumps({"message": {"content": "lo"}, "done": False}),
        json.dumps({"message": {"content": ""}, "done": True}),
    ]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content="\n".join(lines).encode()))
    client = OllamaClient(transport=transport)
    assert asyncio.run(_collect(client.generate_stream("hi"))) == ["Hel", "lo"]


def test_ollama_connection_error_wrapped():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = OllamaClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="Ollama request failed"):
        asyncio.run(client.generate_response("hi"))


def test_ollama_http_error_includes_body():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(404, json={"error": "model 'x' not found"}))
    client = OllamaClient(transport=transport)
    with pytest.raises(RuntimeError, match="not found"):
        asyncio.run(client.generate_response("hi"))


def test_ollama_sync_one_shot():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_chat_response_body("sync ok")))
    client = OllamaClient(sync_transport=transport)
    assert client.generate_sync("hi") == "sync ok"
