"""Regression tests for Bug 5 — response schema inconsistency.

Observed:
* semantic_search_emails items used key ``id``
* search_emails items used key ``message_id``
* search_by_conversation items used key ``id``
* get_email_details wrapped under ``email`` (different from lists)

Required:
* Every list item exposes ``message_id`` (the canonical key).
* ``id`` kept as a deprecated alias for one release, with a
  ``meta.deprecations`` note in the response.
* List responses gain ``items`` + ``count`` + ``total`` alongside the
  legacy keys (``emails`` / ``results``).
* ``get_email_details`` default shape unchanged (non-negotiable).
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest


def _fake_email(id_="AAMk-1", subject="hello"):
    m = MagicMock()
    m.subject = subject
    m.text_body = "body text"
    m.id = id_
    m.sender = MagicMock(email_address="alice@example.com")
    m.to_recipients = []
    m.cc_recipients = []
    m.bcc_recipients = []
    m.datetime_received = datetime(2026, 4, 18, 10, 0, 0)
    m.is_read = False
    m.has_attachments = False
    m.importance = "Normal"
    m.conversation_id = "AAQk-1"
    m.attachments = []
    return m


def _patch_folder_with(tool, emails):
    class _Query:
        def filter(self, *a, **kw):
            return self

        def order_by(self, *a, **kw):
            return self

        def __getitem__(self, _slice):
            return list(emails)

    class _Folder:
        def all(self):
            return _Query()

    async def _resolve(*_args, **_kwargs):
        return _Folder()

    patcher = patch(
        "src.tools.email_tools.resolve_folder_for_account", side_effect=_resolve
    )
    patcher.start()
    return patcher


@pytest.mark.asyncio
async def test_search_emails_items_have_message_id(mock_ews_client):
    """Every item must carry a ``message_id`` key (canonical)."""
    from src.tools.email_tools import SearchEmailsTool

    tool = SearchEmailsTool(mock_ews_client)
    patcher = _patch_folder_with(tool, [_fake_email()])
    try:
        result = await tool.execute(folder="inbox", max_results=5)
    finally:
        patcher.stop()

    assert result["success"] is True
    for item in result["items"]:
        assert "message_id" in item
        assert item["message_id"] == "AAMk-1"


@pytest.mark.asyncio
async def test_search_emails_response_carries_items_count_total(mock_ews_client):
    """Envelope exposes canonical ``items`` + ``count`` + ``total``."""
    from src.tools.email_tools import SearchEmailsTool

    tool = SearchEmailsTool(mock_ews_client)
    patcher = _patch_folder_with(tool, [_fake_email(), _fake_email(id_="AAMk-2")])
    try:
        result = await tool.execute(folder="inbox", max_results=5)
    finally:
        patcher.stop()

    assert "items" in result
    assert result["count"] == 2
    assert result["total"] == 2
    # Legacy keys preserved.
    assert "emails" in result
    assert result["total_count"] == 2


@pytest.mark.asyncio
async def test_search_by_conversation_items_have_message_id(mock_ews_client):
    from src.tools.search_tools import SearchByConversationTool

    fake = _fake_email()
    mock_folder = mock_ews_client.account.inbox
    mock_folder.filter.return_value.order_by.return_value.__getitem__ = (
        lambda _self, _slc: [fake]
    )

    tool = SearchByConversationTool(mock_ews_client)
    result = await tool.execute(conversation_id="AAQk-1")
    assert result["success"] is True
    for item in result["items"]:
        assert "message_id" in item
        # Keep ``id`` as alias for one release.
        assert item.get("id") == item.get("message_id")


@pytest.mark.asyncio
async def test_get_email_details_default_backward_compat(mock_ews_client):
    """Invariant: default ``get_email_details`` still wraps under ``email``."""
    from src.tools.email_tools import GetEmailDetailsTool

    with patch(
        "src.tools.email_tools.find_message_for_account",
        return_value=_fake_email(),
    ):
        tool = GetEmailDetailsTool(mock_ews_client)
        result = await tool.execute(message_id="AAMk-1")

    assert "email" in result
    # message_id is inside the email object (canonical).
    assert result["email"]["message_id"] == "AAMk-1"


# --- Semantic search: id kept as alias for message_id -------------------


@pytest.mark.asyncio
async def test_semantic_search_emits_message_id_and_id_alias(mock_ews_client):
    """Semantic search items must carry ``message_id`` (new canonical) and
    ``id`` (deprecated alias, for one release). meta.deprecations must be
    emitted in the response envelope."""
    from src.tools.ai_tools import SemanticSearchEmailsTool

    mock_ews_client.config.enable_ai = True
    mock_ews_client.config.enable_semantic_search = True
    mock_ews_client.config.ai_provider = "local"
    mock_ews_client.config.ai_api_key = "x"
    mock_ews_client.config.ai_model = "ignored"
    mock_ews_client.config.ai_embedding_model = "text-embedding-3-small"
    mock_ews_client.config.ai_base_url = "http://fake/v1"

    fake = _fake_email()
    ordered = MagicMock()
    ordered.__getitem__ = lambda _self, _slc: [fake]
    mock_ews_client.account.inbox.all.return_value.order_by.return_value = ordered

    class _StubService:
        def __init__(self, *_a, **_kw):
            pass

        async def search_similar(self, *args, **kwargs):
            # Return the fake doc as the only match.
            doc = {
                "id": "AAMk-1",
                "subject": "hello",
                "from": "alice@example.com",
                "datetime_received": "2026-04-18T10:00:00",
                "text": "hello body",
                "text_body": "hello body",
            }
            return [(doc, 0.9)]

    tool = SemanticSearchEmailsTool(mock_ews_client)
    with patch("src.tools.ai_tools.EmbeddingService", _StubService), \
         patch(
             "src.tools.ai_tools.get_embedding_provider",
             return_value=object(),
         ):
        result = await tool.execute(query="anything")

    assert result["success"] is True
    assert result["items"]
    item = result["items"][0]
    assert item["message_id"] == "AAMk-1"
    # Alias preserved.
    assert item.get("id") == "AAMk-1"
    # Deprecation note surfaced for discovery.
    meta = result.get("meta") or {}
    deps = meta.get("deprecations") or []
    assert any("id is deprecated" in d for d in deps), deps
