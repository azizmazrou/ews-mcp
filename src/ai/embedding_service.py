"""Embedding service for semantic search."""

import json
import logging
import os
import tempfile
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from .base import EmbeddingProvider


class EmbeddingService:
    """Service for managing embeddings and semantic search."""

    def __init__(self, provider: EmbeddingProvider, cache_dir: Optional[str] = None):
        """Initialize embedding service.

        Args:
            provider: Embedding provider to use
            cache_dir: Optional directory to cache embeddings
        """
        self.provider = provider
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.logger = logging.getLogger(__name__)

        # In-memory cache
        self.embedding_cache: Dict[str, List[float]] = {}

        # Load cache from disk if available
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._load_cache()

    def _load_cache(self):
        """Load embeddings cache from disk."""
        cache_file = self.cache_dir / "embeddings.json"
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    self.embedding_cache = json.load(f)
                self.logger.info(f"Loaded {len(self.embedding_cache)} cached embeddings")
            except Exception as e:
                self.logger.warning(f"Failed to load embeddings cache: {e}")

    def _save_cache(self):
        """Save embeddings cache to disk atomically.

        Writes to a temp file in the same directory and renames over the
        target so a crash mid-write cannot corrupt the cache.
        """
        if not self.cache_dir:
            return

        cache_file = self.cache_dir / "embeddings.json"
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix="embeddings-", suffix=".json.tmp", dir=self.cache_dir
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self.embedding_cache, f)
                os.replace(tmp_path, cache_file)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            self.logger.warning(f"Failed to save embeddings cache: {e}")

    def _get_cache_key(self, text: str) -> str:
        """Generate cache key for text."""
        import hashlib
        return hashlib.sha256(text.encode()).hexdigest()

    async def embed_text(self, text: str, use_cache: bool = True) -> List[float]:
        """Generate embedding for text.

        Args:
            text: Text to embed
            use_cache: Whether to use cached embeddings

        Returns:
            Embedding vector
        """
        if use_cache:
            cache_key = self._get_cache_key(text)
            if cache_key in self.embedding_cache:
                return self.embedding_cache[cache_key]

        # Generate embedding
        response = await self.provider.embed(text)
        embedding = response.embedding

        # Cache it
        if use_cache:
            cache_key = self._get_cache_key(text)
            self.embedding_cache[cache_key] = embedding
            self._save_cache()

        return embedding

    async def embed_batch(self, texts: List[str], use_cache: bool = True) -> List[List[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: Texts to embed
            use_cache: Whether to use cached embeddings

        Returns:
            List of embedding vectors
        """
        if not use_cache:
            responses = await self.provider.embed_batch(texts)
            return [r.embedding for r in responses]

        # Check cache
        embeddings: List[Tuple[int, List[float]]] = []
        texts_to_embed: List[str] = []
        indices_to_embed: List[int] = []

        for i, text in enumerate(texts):
            cache_key = self._get_cache_key(text)
            if cache_key in self.embedding_cache:
                embeddings.append((i, self.embedding_cache[cache_key]))
            else:
                texts_to_embed.append(text)
                indices_to_embed.append(i)

        # Embed uncached texts. texts_to_embed[pos] corresponds positionally
        # to responses[pos] and to the original texts[indices_to_embed[pos]].
        if texts_to_embed:
            responses = await self.provider.embed_batch(texts_to_embed)
            for pos, (original_idx, response) in enumerate(zip(indices_to_embed, responses)):
                embedding = response.embedding
                embeddings.append((original_idx, embedding))
                cache_key = self._get_cache_key(texts_to_embed[pos])
                self.embedding_cache[cache_key] = embedding

            # One disk write for the whole batch (was N writes / call).
            self._save_cache()

        # Sort by original index
        embeddings.sort(key=lambda x: x[0])
        return [e[1] for e in embeddings]

    async def search_similar(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        text_key: str = "text",
        top_k: int = 10,
        threshold: float = 0.0
    ) -> List[Tuple[Dict[str, Any], float]]:
        """Search for similar documents using semantic similarity.

        Args:
            query: Search query
            documents: List of documents to search
            text_key: Key in document dict containing text
            top_k: Number of results to return
            threshold: Minimum similarity threshold

        Returns:
            List of (document, similarity_score) tuples, sorted by score
        """
        # Generate query embedding
        query_embedding = await self.embed_text(query)

        # Generate document embeddings
        doc_texts = [doc[text_key] for doc in documents]
        doc_embeddings = await self.embed_batch(doc_texts)

        # Calculate similarities
        results = []
        for doc, doc_embedding in zip(documents, doc_embeddings):
            similarity = self.provider.cosine_similarity(query_embedding, doc_embedding)
            if similarity >= threshold:
                results.append((doc, similarity))

        # Sort by similarity (descending) and return top_k
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    async def warmup(
        self,
        texts: List[str],
        *,
        batch_size: int = 32,
        max_items: int = 1000,
        progress_every: int = 100,
    ) -> Dict[str, int]:
        """Pre-embed ``texts`` into the cache in small batches.

        Intended for startup so cold semantic_search calls don't spend 45+
        seconds embedding on demand. Returns counts for observability:

        * ``requested``: input count (after max_items truncation)
        * ``cache_hits``: already in cache, no network work done
        * ``embedded``: new embeddings persisted during this call
        * ``errors``: batches that raised EmbeddingError (these are logged
          and skipped so a partial outage doesn't abort the warmup)
        """
        if not texts:
            return {"requested": 0, "cache_hits": 0, "embedded": 0, "errors": 0}

        # De-duplicate while preserving order: same subject/body in two
        # folders shouldn't pay for two embeddings.
        dedup_seen: set = set()
        unique_texts: List[str] = []
        for text in texts:
            if not text or not isinstance(text, str):
                continue
            key = self._get_cache_key(text)
            if key in dedup_seen:
                continue
            dedup_seen.add(key)
            unique_texts.append(text)

        unique_texts = unique_texts[:max_items]
        total = len(unique_texts)
        hits = 0
        misses: List[str] = []
        for text in unique_texts:
            if self._get_cache_key(text) in self.embedding_cache:
                hits += 1
            else:
                misses.append(text)

        embedded = 0
        errors = 0
        last_log = 0
        for start in range(0, len(misses), batch_size):
            batch = misses[start : start + batch_size]
            try:
                responses = await self.provider.embed_batch(batch)
            except Exception as exc:  # EmbeddingError or transport failure
                errors += 1
                self.logger.warning(
                    "warmup: batch %d-%d failed (%s: %s); continuing",
                    start, start + len(batch), type(exc).__name__, exc,
                )
                continue
            for text, response in zip(batch, responses):
                self.embedding_cache[self._get_cache_key(text)] = response.embedding
                embedded += 1
            if embedded - last_log >= progress_every:
                last_log = embedded
                self.logger.info(
                    "warmup: embedded %d/%d items (%.0f%%)",
                    embedded, total, (embedded / max(total, 1)) * 100.0,
                )

        if embedded:
            # One atomic write after the whole warmup — O(N) disk writes
            # are still cheaper than per-miss writes at steady state.
            self._save_cache()

        self.logger.info(
            "warmup complete: embedded=%d cache_hits=%d errors=%d total=%d",
            embedded, hits, errors, total,
        )
        return {
            "requested": total,
            "cache_hits": hits,
            "embedded": embedded,
            "errors": errors,
        }

    async def find_duplicates(
        self,
        documents: List[Dict[str, Any]],
        text_key: str = "text",
        threshold: float = 0.95
    ) -> List[Tuple[int, int, float]]:
        """Find duplicate or near-duplicate documents.

        Args:
            documents: List of documents to check
            text_key: Key in document dict containing text
            threshold: Similarity threshold for duplicates

        Returns:
            List of (index1, index2, similarity) tuples
        """
        # Generate embeddings for all documents
        doc_texts = [doc[text_key] for doc in documents]
        embeddings = await self.embed_batch(doc_texts)

        # Find pairs above threshold
        duplicates = []
        for i in range(len(embeddings)):
            for j in range(i + 1, len(embeddings)):
                similarity = self.provider.cosine_similarity(embeddings[i], embeddings[j])
                if similarity >= threshold:
                    duplicates.append((i, j, similarity))

        return duplicates

    def clear_cache(self):
        """Clear embedding cache."""
        self.embedding_cache.clear()
        if self.cache_dir:
            cache_file = self.cache_dir / "embeddings.json"
            if cache_file.exists():
                cache_file.unlink()
        self.logger.info("Embedding cache cleared")
