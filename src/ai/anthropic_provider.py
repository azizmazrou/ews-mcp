"""Anthropic provider implementation."""

import json
import httpx
from typing import List, Dict, Any
from .base import AIProvider, Message, CompletionResponse


class AnthropicProvider(AIProvider):
    """Anthropic API provider for chat completions."""

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.anthropic.com/v1", **kwargs):
        """Initialize Anthropic provider."""
        super().__init__(api_key, model, base_url, **kwargs)
        self.headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }

    async def complete(
        self,
        messages: List[Message],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs
    ) -> CompletionResponse:
        """Generate completion using Anthropic API."""
        url = f"{self.base_url}/messages"

        # Convert messages to Anthropic format
        system_message = None
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                system_message = msg.content
            else:
                api_messages.append({
                    "role": msg.role,
                    "content": msg.content
                })

        payload = {
            "model": self.model,
            "messages": api_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs
        }

        if system_message:
            payload["system"] = system_message

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            data = response.json()

        return CompletionResponse(
            content=data["content"][0]["text"],
            model=data["model"],
            usage=data.get("usage"),
            finish_reason=data.get("stop_reason")
        )

    async def complete_with_json(
        self,
        messages: List[Message],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs
    ) -> Dict[str, Any]:
        """Generate JSON completion using Anthropic API."""
        # Ensure system message mentions JSON
        if messages and messages[0].role == "system":
            if "json" not in messages[0].content.lower():
                messages[0].content += "\n\nRespond with valid JSON only. Do not include any text outside the JSON object."
        else:
            messages.insert(0, Message(
                role="system",
                content="Respond with valid JSON only. Do not include any text outside the JSON object."
            ))

        response = await self.complete(messages, temperature, max_tokens, **kwargs)

        # Extract JSON from response (Claude sometimes adds text around it)
        content = response.content.strip()

        # Try to find JSON in the response
        start_idx = content.find('{')
        end_idx = content.rfind('}')

        if start_idx >= 0 and end_idx >= 0:
            json_str = content[start_idx:end_idx + 1]
            return json.loads(json_str)
        else:
            # Fallback: try to parse entire content
            return json.loads(content)
