"""Pin the `.only(...)` projection on semantic_search_emails.

Cold semantic_search used to take 90+s on a 200-message inbox window
because exchangelib lazy-loaded every property (full text body, MIME
headers, attachments metadata) for every candidate just to embed
subject + first 500 chars of body. With `.only(...)` we narrow GetItem
to ~7 fields and the same window finishes in single-digit seconds.

This test pins the call shape so future refactors don't accidentally
drop the projection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_semantic_search_uses_only_projection(mock_ews_client):
    """folder.all() must be followed by .only(...) before .order_by(...)."""
    from src.tools.ai_tools import SemanticSearchEmailsTool

    mock_ews_client.config.enable_ai = True
    mock_ews_client.config.enable_semantic_search = True
    mock_ews_client.config.ai_provider = "local"
    mock_ews_client.config.ai_api_key = "x"
    mock_ews_client.config.ai_model = "ignored"
    mock_ews_client.config.ai_embedding_model = "nomic-embed-text"
    mock_ews_client.config.ai_base_url = "http://fake/v1"

    fake = MagicMock()
    fake.subject = "subject"
    fake.text_body = "body"
    fake.id = "AAMk-1"
    fake.sender = MagicMock(email_address="x@example.com")
    fake.datetime_received = "2026-04-18T10:00:00"
    fake.is_read = False
    fake.has_attachments = False

    ordered = MagicMock()
    ordered.__getitem__ = lambda _self, _slc: [fake]

    only_qs = MagicMock()
    only_qs.order_by.return_value = ordered

    all_qs = MagicMock()
    all_qs.only.return_value = only_qs
    mock_ews_client.account.inbox.all.return_value = all_qs

    class _Service:
        def __init__(self, *_a, **_kw):
            pass

        async def search_similar(self, *, query, documents, text_key, top_k, threshold):
            return [(documents[0], 0.8)]

    tool = SemanticSearchEmailsTool(mock_ews_client)
    with patch("src.tools.ai_tools.EmbeddingService", _Service), \
         patch("src.tools.ai_tools.get_embedding_provider", return_value=object()):
        result = await tool.execute(query="anything", exclude_automated=False)

    assert result["success"] is not False
    # Pin that .only(...) was called exactly once with the slim field set.
    all_qs.only.assert_called_once()
    fields = set(all_qs.only.call_args.args)
    expected = {"id", "subject", "sender", "datetime_received", "text_body"}
    assert expected.issubset(fields), (
        f"semantic_search must project at least {expected}; got {fields}"
    )
    # And that .order_by came after .only, not after .all directly.
    only_qs.order_by.assert_called_once()
    all_qs.order_by.assert_not_called()
