"""Regression tests for Bug 4 — semantic search pays a 45-76s tax on cold cache.

Before: each semantic_search_emails call embedded N new emails on demand.
The cache grew only 1 entry per call for queries that shared most text,
because the query embedding was cached but the document embeddings were
regenerated every time.

After:
* EmbeddingService.warmup(texts, batch_size, max_items) pre-fills the
  cache in batches, dedupes inputs, and is resilient to partial failure.
* Server startup kicks warmup off as a background task when
  ENABLE_SEMANTIC_SEARCH + ENABLE_EMBEDDING_WARMUP.
* SemanticSearchEmailsTool caps on-demand embedding at
  _PER_CALL_EMBED_CAP (50) per call so very large folders stay
  responsive.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeResp:
    def __init__(self, vec):
        self.embedding = vec
        self.model = "test"
        self.usage = None


class _FakeProvider:
    """Minimal EmbeddingProvider — records calls so tests can assert."""

    def __init__(self, *, fail_every: int = 0):
        self.calls: list[list[str]] = []
        self.single_calls: list[str] = []
        self.fail_every = fail_every

    async def embed(self, text):
        self.single_calls.append(text)
        return _FakeResp([0.1])

    async def embed_batch(self, texts):
        self.calls.append(list(texts))
        if self.fail_every and len(self.calls) % self.fail_every == 0:
            from src.exceptions import EmbeddingError
            raise EmbeddingError("simulated batch failure")
        return [_FakeResp([float(i)]) for i, _ in enumerate(texts)]


@pytest.mark.asyncio
async def test_warmup_populates_cache(tmp_path):
    from src.ai.embedding_service import EmbeddingService

    provider = _FakeProvider()
    svc = EmbeddingService(provider, cache_dir=str(tmp_path))
    texts = [f"subject-{i} body text here" for i in range(20)]
    stats = await svc.warmup(texts, batch_size=5, max_items=100)
    assert stats["requested"] == 20
    assert stats["embedded"] == 20
    assert stats["cache_hits"] == 0
    assert stats["errors"] == 0
    # Cache is fully populated.
    assert len(svc.embedding_cache) == 20
    # Batches were actually N/5 = 4 batches.
    assert len(provider.calls) == 4


@pytest.mark.asyncio
async def test_warmup_dedupes_inputs(tmp_path):
    from src.ai.embedding_service import EmbeddingService

    provider = _FakeProvider()
    svc = EmbeddingService(provider, cache_dir=str(tmp_path))
    # Same text 10 times + 5 unique = 6 unique total.
    texts = ["repeat"] * 10 + [f"unique-{i}" for i in range(5)]
    stats = await svc.warmup(texts, batch_size=3)
    assert stats["embedded"] == 6
    assert len(svc.embedding_cache) == 6


@pytest.mark.asyncio
async def test_warmup_reports_cache_hits_on_second_run(tmp_path):
    from src.ai.embedding_service import EmbeddingService

    provider = _FakeProvider()
    svc = EmbeddingService(provider, cache_dir=str(tmp_path))
    texts = [f"item-{i}" for i in range(10)]
    await svc.warmup(texts, batch_size=4)
    stats = await svc.warmup(texts, batch_size=4)
    assert stats["cache_hits"] == 10
    assert stats["embedded"] == 0


@pytest.mark.asyncio
async def test_warmup_continues_after_batch_failure(tmp_path):
    from src.ai.embedding_service import EmbeddingService

    # fail_every=2: second batch fails but the first and third succeed.
    provider = _FakeProvider(fail_every=2)
    svc = EmbeddingService(provider, cache_dir=str(tmp_path))
    texts = [f"item-{i}" for i in range(9)]
    stats = await svc.warmup(texts, batch_size=3)
    assert stats["errors"] == 1
    # 3 batches - 1 failure = 2 successful batches × 3 items = 6 embeddings.
    assert stats["embedded"] == 6


@pytest.mark.asyncio
async def test_warmup_honours_max_items(tmp_path):
    from src.ai.embedding_service import EmbeddingService

    provider = _FakeProvider()
    svc = EmbeddingService(provider, cache_dir=str(tmp_path))
    texts = [f"item-{i}" for i in range(50)]
    stats = await svc.warmup(texts, batch_size=10, max_items=15)
    assert stats["requested"] == 15
    assert stats["embedded"] == 15


@pytest.mark.asyncio
async def test_semantic_search_respects_per_call_cap(mock_ews_client):
    """With 200 candidate emails, only _PER_CALL_EMBED_CAP are passed to
    the embedding provider."""
    from src.tools.ai_tools import SemanticSearchEmailsTool

    mock_ews_client.config.enable_ai = True
    mock_ews_client.config.enable_semantic_search = True
    mock_ews_client.config.ai_provider = "local"
    mock_ews_client.config.ai_api_key = "x"
    mock_ews_client.config.ai_model = "ignored"
    mock_ews_client.config.ai_embedding_model = "text-embedding-3-small"
    mock_ews_client.config.ai_base_url = "http://fake/v1"

    # Build 200 fake emails.
    def _fake(i):
        m = MagicMock()
        m.subject = f"Subject {i}"
        m.text_body = "body text"
        m.id = f"AAMk-{i}"
        m.sender = MagicMock(email_address=f"user{i}@example.com")
        m.to_recipients = []
        m.datetime_received = "2026-04-18T10:00:00"
        m.is_read = False
        m.has_attachments = False
        return m

    fakes = [_fake(i) for i in range(200)]
    ordered = MagicMock()
    ordered.__getitem__ = lambda _self, _slc: fakes
    mock_ews_client.account.inbox.all.return_value.order_by.return_value = ordered

    captured: list[list[str]] = []

    class _StubService:
        def __init__(self, *_a, **_kw):
            pass

        async def search_similar(self, *, query, documents, text_key, top_k, threshold):
            captured.append([d[text_key] for d in documents])
            return []

    tool = SemanticSearchEmailsTool(mock_ews_client)
    with patch("src.tools.ai_tools.EmbeddingService", _StubService), \
         patch("src.tools.ai_tools.get_embedding_provider", return_value=object()):
        result = await tool.execute(query="test", folder="inbox", exclude_automated=False)

    assert result["success"] is True
    assert captured, "search_similar never invoked"
    # Tool may pull up to 3x cap as raw candidates, but after the cap
    # applied, documents passed to search_similar must be <= cap.
    assert len(captured[0]) <= tool._PER_CALL_EMBED_CAP, (
        f"passed {len(captured[0])} to search_similar; cap is "
        f"{tool._PER_CALL_EMBED_CAP}"
    )
    # Response should flag the partial sampling.
    meta = result.get("meta") or {}
    assert meta.get("sampled_partial") is True
