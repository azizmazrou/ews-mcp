"""Factory for creating AI providers."""

from typing import Optional
from ..config import Settings
from .base import AIProvider, EmbeddingProvider
from .openai_provider import OpenAIProvider, OpenAIEmbeddingProvider
from .anthropic_provider import AnthropicProvider


def get_ai_provider(settings: Settings) -> Optional[AIProvider]:
    """Create AI provider based on settings.

    Args:
        settings: Application settings

    Returns:
        AIProvider instance or None if AI is disabled
    """
    if not settings.enable_ai:
        return None

    if settings.ai_provider == "openai":
        return OpenAIProvider(
            api_key=settings.ai_api_key,
            model=settings.ai_model,
            base_url=settings.ai_base_url or "https://api.openai.com/v1"
        )
    elif settings.ai_provider == "anthropic":
        return AnthropicProvider(
            api_key=settings.ai_api_key,
            model=settings.ai_model,
            base_url=settings.ai_base_url or "https://api.anthropic.com/v1"
        )
    elif settings.ai_provider == "local":
        # For local models, use OpenAI-compatible API
        if not settings.ai_base_url:
            raise ValueError("ai_base_url required for local provider")
        return OpenAIProvider(
            api_key=settings.ai_api_key or "local",
            model=settings.ai_model,
            base_url=settings.ai_base_url
        )
    else:
        raise ValueError(f"Unknown AI provider: {settings.ai_provider}")


def get_embedding_provider(settings: Settings) -> Optional[EmbeddingProvider]:
    """Create embedding provider based on settings.

    Args:
        settings: Application settings

    Returns:
        EmbeddingProvider instance or None if semantic search is disabled
    """
    if not settings.enable_ai or not settings.enable_semantic_search:
        return None

    if settings.ai_provider == "openai":
        return OpenAIEmbeddingProvider(
            api_key=settings.ai_api_key,
            model=settings.ai_embedding_model or "text-embedding-3-small",
            base_url=settings.ai_base_url or "https://api.openai.com/v1"
        )
    elif settings.ai_provider == "local":
        # For local models, use OpenAI-compatible embedding API
        if not settings.ai_base_url:
            raise ValueError("ai_base_url required for local provider")
        if not settings.ai_embedding_model:
            raise ValueError("ai_embedding_model required for local provider")
        return OpenAIEmbeddingProvider(
            api_key=settings.ai_api_key or "local",
            model=settings.ai_embedding_model,
            base_url=settings.ai_base_url
        )
    else:
        # Anthropic doesn't have embedding API, would need alternative
        raise ValueError(f"Embedding not supported for provider: {settings.ai_provider}")
