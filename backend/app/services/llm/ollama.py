"""
Local-LLM provider — Ollama (`ollama serve`, default http://localhost:11434).

Implements the same two-method interface as GeminiClient (the pluggable-LLM
contract described in gemini.py), plus a synchronous one-shot for the
analysis layer. The router (services/llm/router.py) uses it as the fallback
provider, so chat keeps working with no/invalid GEMINI_API_KEY or no
internet — any machine running `ollama serve` with a pulled model.

No new dependencies: httpx is already a first-class requirement (used by the
google-genai SDK too). Construction is side-effect free; HTTP clients are
created lazily like GeminiClient's.
"""

import json
import logging
from typing import AsyncGenerator, Optional

import httpx

from app.core.config import settings
from app.core.logging import log_event

logger = logging.getLogger(__name__)


class OllamaClient:
    name = "ollama"

    def __init__(self,
                 transport: Optional[httpx.AsyncBaseTransport] = None,
                 sync_transport: Optional[httpx.BaseTransport] = None):
        # Transports are injectable so tests can use httpx.MockTransport.
        self._transport = transport
        self._sync_transport = sync_transport
        self._client: Optional[httpx.AsyncClient] = None
        self._sync_client: Optional[httpx.Client] = None

    # Config is read per-call so .env overrides and tests apply without
    # rebuilding the singleton.
    @property
    def base_url(self) -> str:
        return settings.OLLAMA_BASE_URL.rstrip("/")

    @property
    def model_name(self) -> str:
        return settings.OLLAMA_MODEL

    def _timeout(self) -> httpx.Timeout:
        # Fast connect failure (the router needs a quick fallback verdict when
        # no server is running); generous read (local models cold-load weights
        # on first call and stream slowly).
        return httpx.Timeout(settings.OLLAMA_TIMEOUT_S, connect=5.0)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport,
                                             timeout=self._timeout())
            log_event(logger, "llm.ollama_client_ready",
                      base_url=self.base_url, model=self.model_name)
        return self._client

    def _get_sync_client(self) -> httpx.Client:
        if self._sync_client is None:
            self._sync_client = httpx.Client(transport=self._sync_transport,
                                             timeout=self._timeout())
        return self._sync_client

    def _payload(self, prompt: str, stream: bool) -> dict:
        return {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
            # RAG prompts overflow Ollama's 4096-token default context
            # (top_k=10 × 1500-char chunks) — raise it, and keep the model
            # warm between queries so follow-ups skip the weight reload.
            "keep_alive": settings.OLLAMA_KEEP_ALIVE,
            "options": {"num_ctx": settings.OLLAMA_NUM_CTX},
        }

    def _chat_url(self) -> str:
        return f"{self.base_url}/api/chat"

    def _describe(self, error: Exception) -> str:
        detail = str(error)
        if isinstance(error, httpx.HTTPStatusError) and error.response is not None:
            # The useful part ("model 'x' not found, try pulling it") is in the body.
            detail = f"{error.response.status_code}: {error.response.text[:300]}"
        return (f"Ollama request failed ({self.base_url}, model "
                f"'{self.model_name}'): {detail}")

    @staticmethod
    def _extract(data: dict) -> str:
        if data.get("error"):
            raise RuntimeError(f"Ollama error: {data['error']}")
        return (data.get("message") or {}).get("content") or ""

    async def generate_response(self, prompt: str) -> str:
        """Standard (non-streaming) generation — POST {base}/api/chat."""
        try:
            resp = await self._get_client().post(self._chat_url(),
                                                 json=self._payload(prompt, False))
            resp.raise_for_status()
            return self._extract(resp.json())
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(self._describe(e)) from e

    async def generate_stream(self, prompt: str) -> AsyncGenerator[str, None]:
        """Token streaming — NDJSON lines from POST {base}/api/chat."""
        try:
            async with self._get_client().stream(
                    "POST", self._chat_url(),
                    json=self._payload(prompt, True)) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Ollama request failed ({self.base_url}, model "
                        f"'{self.model_name}'): {resp.status_code}: {body[:300]}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    text = self._extract(data)
                    if text:
                        yield text
                    if data.get("done"):
                        break
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(self._describe(e)) from e

    def generate_sync(self, prompt: str) -> str:
        """SYNCHRONOUS one-shot for non-async callers (analysis layer)."""
        try:
            resp = self._get_sync_client().post(self._chat_url(),
                                                json=self._payload(prompt, False))
            resp.raise_for_status()
            return self._extract(resp.json())
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(self._describe(e)) from e


# Singleton — safe, construction is side-effect free.
ollama_client = OllamaClient()
