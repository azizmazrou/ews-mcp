"""OpenAI provider implementation."""

import json
import httpx
from typing import List, Dict, Any
from .base import AIProvider, EmbeddingProvider, Message, CompletionResponse, EmbeddingResponse


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
    """OpenAI embedding provider."""

    def __init__(self, api_key: str, model: str = "text-embedding-3-small", base_url: str = "https://api.openai.com/v1"):
        """Initialize OpenAI embedding provider."""
        super().__init__(api_key, model, base_url)
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    async def embed(self, text: str) -> EmbeddingResponse:
        """Generate embedding for text."""
        url = f"{self.base_url}/embeddings"

        payload = {
            "model": self.model,
            "input": text
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            data = response.json()

        return EmbeddingResponse(
            embedding=data["data"][0]["embedding"],
            model=data["model"],
            usage=data.get("usage")
        )

    async def embed_batch(self, texts: List[str]) -> List[EmbeddingResponse]:
        """Generate embeddings for multiple texts."""
        url = f"{self.base_url}/embeddings"

        payload = {
            "model": self.model,
            "input": texts
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            data = response.json()

        return [
            EmbeddingResponse(
                embedding=item["embedding"],
                model=data["model"],
                usage=data.get("usage")
            )
            for item in data["data"]
        ]
