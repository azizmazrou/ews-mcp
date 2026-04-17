"""OpenAI provider implementation."""

import json
import logging
import httpx
from typing import List, Dict, Any, Optional
from .base import AIProvider, EmbeddingProvider, Message, CompletionResponse, EmbeddingResponse
from ..exceptions import EmbeddingError

_LOG = logging.getLogger(__name__)


# Subset of well-known strings that look like provider names, not embedding
# model names. Used only to flag likely misconfiguration (e.g.
# AI_EMBEDDING_MODEL=ollama).
_LIKELY_PROVIDER_NAMES = {
    "ollama", "openai", "anthropic", "cohere", "voyage", "local",
}


def _extract_upstream_error(response: "httpx.Response") -> str:
    """Pull a human-readable error out of an embeddings response.

    Handles both OpenAI's ``{"error": {"message": "..."}}`` shape and
    Ollama's equivalent, plus a raw text fallback. Never raises; returns
    a short string suitable for a ``ToolExecutionError`` message.
    """
    body_text = ""
    try:
        body_text = response.text or ""
    except Exception:  # pragma: no cover - defensive
        body_text = ""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("type")
            if msg:
                return str(msg)
        if isinstance(err, str) and err:
            return err
    # Trim raw body for log legibility.
    snippet = body_text.strip()
    if len(snippet) > 300:
        snippet = snippet[:297] + "..."
    return snippet or f"HTTP {response.status_code}"


class OpenAIProvider(AIProvider):
    """OpenAI API provider for chat completions."""

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1", **kwargs):
        """Initialize OpenAI provider."""
        super().__init__(api_key, model, base_url, **kwargs)
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    async def complete(
        self,
        messages: List[Message],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs
    ) -> CompletionResponse:
        """Generate completion using OpenAI API."""
        url = f"{self.base_url}/chat/completions"

        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            data = response.json()

        choice = data["choices"][0]
        return CompletionResponse(
            content=choice["message"]["content"],
            model=data["model"],
            usage=data.get("usage"),
            finish_reason=choice.get("finish_reason")
        )

    async def complete_with_json(
        self,
        messages: List[Message],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs
    ) -> Dict[str, Any]:
        """Generate JSON completion using OpenAI API."""
        # Add response_format for JSON mode
        kwargs["response_format"] = {"type": "json_object"}

        # Ensure system message mentions JSON
        if messages and messages[0].role == "system":
            if "json" not in messages[0].content.lower():
                messages[0].content += "\n\nRespond with valid JSON only."
        else:
            messages.insert(0, Message(role="system", content="Respond with valid JSON only."))

        response = await self.complete(messages, temperature, max_tokens, **kwargs)
        return json.loads(response.content)


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible embedding provider (OpenAI, Ollama-OpenAI, etc).

    Error handling
    --------------
    All non-2xx responses and 2xx responses with an ``{"error": ...}`` body
    are converted to :class:`src.exceptions.EmbeddingError` with the
    upstream message preserved. This is the single place where embedding
    failures get a clear diagnostic — higher layers (``EmbeddingService``,
    ``SemanticSearchEmailsTool``) let the exception propagate so the
    operator sees the real error, not an empty result set.
    """

    # OpenAI allows ~8192 tokens per request; we also cap the input list
    # length so a single call can't pin the process on batches of 10k+
    # emails. Callers can iterate if they need more.
    _MAX_BATCH_INPUTS = 256

    def __init__(self, api_key: str, model: str = "text-embedding-3-small", base_url: str = "https://api.openai.com/v1"):
        """Initialize OpenAI embedding provider."""
        if model and model.strip().lower() in _LIKELY_PROVIDER_NAMES:
            _LOG.warning(
                "AI_EMBEDDING_MODEL=%r looks like a provider name, not a model "
                "name. Typical values: 'text-embedding-3-small', "
                "'nomic-embed-text', 'bge-m3'. The request will be sent "
                "as-is; if the upstream returns 'model not found', update "
                "AI_EMBEDDING_MODEL.",
                model,
            )
        super().__init__(api_key, model, base_url)
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _post_embeddings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST to ``/embeddings`` and return parsed JSON or raise EmbeddingError."""
        url = f"{self.base_url.rstrip('/')}/embeddings"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, headers=self.headers, json=payload)
        except httpx.RequestError as exc:
            raise EmbeddingError(
                f"Embedding endpoint unreachable at {url}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        # Non-2xx: pull the upstream error out of the body and surface it.
        if response.status_code >= 400:
            message = _extract_upstream_error(response)
            raise EmbeddingError(
                f"Embedding provider returned HTTP {response.status_code} "
                f"for model {payload.get('model')!r}: {message}"
            )

        # Some OpenAI-compat servers (e.g. certain Ollama versions) reply
        # 200 with an ``{"error": ...}`` body. Catch that here too.
        try:
            data = response.json()
        except Exception as exc:
            raise EmbeddingError(
                f"Embedding provider returned non-JSON body for model "
                f"{payload.get('model')!r}: {type(exc).__name__}"
            ) from exc

        if isinstance(data, dict) and data.get("error"):
            message = _extract_upstream_error(response)
            raise EmbeddingError(
                f"Embedding provider returned error for model "
                f"{payload.get('model')!r}: {message}"
            )

        if not isinstance(data, dict) or not isinstance(data.get("data"), list) or not data["data"]:
            raise EmbeddingError(
                f"Embedding provider returned no data for model "
                f"{payload.get('model')!r}. Body: {str(data)[:200]}"
            )

        return data

    async def embed(self, text: str) -> EmbeddingResponse:
        """Generate embedding for text."""
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingError("embed() requires a non-empty string")
        data = await self._post_embeddings({"model": self.model, "input": text})
        first = data["data"][0]
        if not isinstance(first, dict) or "embedding" not in first:
            raise EmbeddingError(
                f"Embedding provider response missing 'embedding' field: "
                f"{str(first)[:200]}"
            )
        return EmbeddingResponse(
            embedding=first["embedding"],
            model=data.get("model", self.model),
            usage=data.get("usage"),
        )

    async def embed_batch(self, texts: List[str]) -> List[EmbeddingResponse]:
        """Generate embeddings for multiple texts."""
        if not isinstance(texts, list):
            raise EmbeddingError("embed_batch() requires a list")
        # Empty input: return [] without making a request. Provider semantics
        # differ on how they handle empty input arrays, so don't rely on it.
        if not texts:
            return []
        if len(texts) > self._MAX_BATCH_INPUTS:
            raise EmbeddingError(
                f"embed_batch called with {len(texts)} inputs; max is "
                f"{self._MAX_BATCH_INPUTS}. Iterate in smaller batches."
            )
        data = await self._post_embeddings({"model": self.model, "input": texts})
        items = data["data"]
        if len(items) != len(texts):
            raise EmbeddingError(
                f"Embedding provider returned {len(items)} vectors for "
                f"{len(texts)} inputs. Response: {str(data)[:200]}"
            )
        return [
            EmbeddingResponse(
                embedding=item["embedding"],
                model=data.get("model", self.model),
                usage=data.get("usage"),
            )
            for item in items
        ]

    async def health_check(self) -> None:
        """Small probe to confirm the endpoint + model work.

        Callers can invoke this once at startup (or the first time a
        semantic tool is used) to convert "0 hits at query time" into a
        visible startup error. Raises :class:`EmbeddingError` on failure.
        """
        await self.embed("probe")
