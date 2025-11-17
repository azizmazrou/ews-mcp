"""Base classes for AI providers."""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class Message:
    """Message for chat completion."""
    role: str  # "system", "user", "assistant"
    content: str


@dataclass
class CompletionResponse:
    """Response from AI completion."""
    content: str
    model: str
    usage: Optional[Dict[str, int]] = None
    finish_reason: Optional[str] = None


@dataclass
class EmbeddingResponse:
    """Response from embedding generation."""
    embedding: List[float]
    model: str
    usage: Optional[Dict[str, int]] = None


class AIProvider(ABC):
    """Base class for AI providers."""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None, **kwargs):
        """Initialize provider.

        Args:
            api_key: API key for the provider
            model: Model name to use
            base_url: Optional base URL for API (for local models)
            **kwargs: Additional provider-specific configuration
        """
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.config = kwargs

    @abstractmethod
    async def complete(
        self,
        messages: List[Message],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs
    ) -> CompletionResponse:
        """Generate completion from messages.

        Args:
            messages: List of conversation messages
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Maximum tokens to generate
            **kwargs: Additional provider-specific parameters

        Returns:
            CompletionResponse with generated content
        """
        pass

    @abstractmethod
    async def complete_with_json(
        self,
        messages: List[Message],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs
    ) -> Dict[str, Any]:
        """Generate JSON completion from messages.

        Args:
            messages: List of conversation messages
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Maximum tokens to generate
            **kwargs: Additional provider-specific parameters

        Returns:
            Parsed JSON object
        """
        pass


class EmbeddingProvider(ABC):
    """Base class for embedding providers."""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        """Initialize embedding provider.

        Args:
            api_key: API key for the provider
            model: Embedding model name
            base_url: Optional base URL for API
        """
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    @abstractmethod
    async def embed(self, text: str) -> EmbeddingResponse:
        """Generate embedding for text.

        Args:
            text: Text to embed

        Returns:
            EmbeddingResponse with vector
        """
        pass

    @abstractmethod
    async def embed_batch(self, texts: List[str]) -> List[EmbeddingResponse]:
        """Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of EmbeddingResponse objects
        """
        pass

    @staticmethod
    def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
        """Calculate cosine similarity between two vectors.

        Args:
            vec1: First vector
            vec2: Second vector

        Returns:
            Similarity score (0.0-1.0)
        """
        import math

        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        magnitude1 = math.sqrt(sum(a * a for a in vec1))
        magnitude2 = math.sqrt(sum(b * b for b in vec2))

        if magnitude1 == 0 or magnitude2 == 0:
            return 0.0

        return dot_product / (magnitude1 * magnitude2)
