"""Regression tests for Bug 7 — semantic search returned duplicates.

4 coverage queries produced 40 hits but only 23 unique message_ids — the
same thread was counted per-message against the similarity threshold.

Fix: SemanticSearchEmailsTool dedupes by message_id before returning,
keeping the highest-scoring hit per id. ``duplicate_count`` on each
item tracks the collapsed copies.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def _enable_ai(mock_ews_client):
    mock_ews_client.config.enable_ai = True
    mock_ews_client.config.enable_semantic_search = True
    mock_ews_client.config.ai_provider = "local"
    mock_ews_client.config.ai_api_key = "x"
    mock_ews_client.config.ai_model = "ignored"
    mock_ews_client.config.ai_embedding_model = "text-embedding-3-small"
    mock_ews_client.config.ai_base_url = "http://fake/v1"
    return mock_ews_client


def _fake(id_):
    m = MagicMock()
    m.subject = f"subject-{id_}"
    m.text_body = "body text"
    m.id = id_
    m.sender = MagicMock(email_address=f"u{id_}@x.com")
    m.to_recipients = []
    m.datetime_received = "2026-04-18T10:00:00"
    m.is_read = False
    m.has_attachments = False
    return m


@pytest.mark.asyncio
async def test_semantic_search_dedupes_by_message_id(_enable_ai):
    """Same id returned multiple times collapses to one hit; winner keeps
    the highest similarity."""
    from src.tools.ai_tools import SemanticSearchEmailsTool

    fakes = [_fake("A"), _fake("B"), _fake("C")]
    ordered = MagicMock()
    ordered.__getitem__ = lambda _self, _slc: fakes
    _enable_ai.account.inbox.all.return_value.only.return_value.order_by.return_value = ordered

    class _Service:
        def __init__(self, *_a, **_kw):
            pass

        async def search_similar(self, *, query, documents, text_key, top_k, threshold):
            # Return each document twice with different scores — one high,
            # one lower. The dedupe must keep the higher-scoring copy.
            out = []
            for i, doc in enumerate(documents):
                out.append((doc, 0.9 - i * 0.1))
                out.append((doc, 0.5 - i * 0.1))
            return out

    tool = SemanticSearchEmailsTool(_enable_ai)
    with patch("src.tools.ai_tools.EmbeddingService", _Service), \
         patch("src.tools.ai_tools.get_embedding_provider", return_value=object()):
        result = await tool.execute(query="anything", exclude_automated=False)

    ids = [item["message_id"] for item in result["items"]]
    assert len(ids) == len(set(ids)), ids
    assert sorted(ids) == ["A", "B", "C"]

    # All items should have duplicate_count == 1 (they were returned twice
    # each, so exactly one collapse).
    for item in result["items"]:
        assert item.get("duplicate_count") == 1, item


@pytest.mark.asyncio
async def test_semantic_search_dedupe_keeps_highest_score(_enable_ai):
    from src.tools.ai_tools import SemanticSearchEmailsTool

    fake = _fake("X")
    ordered = MagicMock()
    ordered.__getitem__ = lambda _self, _slc: [fake]
    _enable_ai.account.inbox.all.return_value.only.return_value.order_by.return_value = ordered

    class _Service:
        def __init__(self, *_a, **_kw):
            pass

        async def search_similar(self, *, query, documents, text_key, top_k, threshold):
            # Provider may return the same doc multiple times with wildly
            # different similarity scores. Dedupe must keep the highest.
            doc = documents[0]
            return [(doc, 0.3), (doc, 0.95), (doc, 0.6)]

    tool = SemanticSearchEmailsTool(_enable_ai)
    with patch("src.tools.ai_tools.EmbeddingService", _Service), \
         patch("src.tools.ai_tools.get_embedding_provider", return_value=object()):
        result = await tool.execute(query="anything", exclude_automated=False)

    assert result["count"] == 1
    item = result["items"][0]
    assert item["similarity_score"] == 0.95
    # Two collapsed copies -> duplicate_count 2.
    assert item["duplicate_count"] == 2
