"""AI Intelligence Layer for EWS MCP Server."""

from .base import AIProvider, EmbeddingProvider
from .provider_factory import get_ai_provider, get_embedding_provider
from .classification_service import EmailClassificationService
from .embedding_service import EmbeddingService

__all__ = [
    "AIProvider",
    "EmbeddingProvider",
    "get_ai_provider",
    "get_embedding_provider",
    "EmailClassificationService",
    "EmbeddingService",
]
