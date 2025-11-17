"""Tests for advanced search tools."""

import pytest
from unittest.mock import MagicMock

from src.tools.search_tools import AdvancedSearchTool


@pytest.mark.asyncio
async def test_advanced_search_tool(mock_ews_client):
    """Test advanced search with multiple filters."""
    from datetime import datetime
    tool = AdvancedSearchTool(mock_ews_client)

    # Mock search results
    mock_email = MagicMock()
    mock_email.id = "email-1"
    mock_email.subject = "Important Meeting"
    mock_email.sender.email_address = "boss@example.com"
    mock_email.to_recipients = [MagicMock(email_address="team@example.com")]
    mock_email.datetime_received = datetime(2025, 1, 15, 10, 0, 0)
    mock_email.is_read = False
    mock_email.has_attachments = True
    mock_email.importance = "High"
    mock_email.categories = ["Work"]
    mock_email.text_body = "Please review the quarterly report"

    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = [mock_email]
    mock_query.__getitem__ = lambda self, key: [mock_email]

    mock_ews_client.account.inbox.filter.return_value = mock_query

    result = await tool.execute(
        search_filter={
            "keywords": "meeting",
            "from_address": "boss@example.com",
            "has_attachments": True,
            "importance": "High"
        },
        search_scope=["inbox"],
        max_results=100,
        sort_by="datetime_received",
        sort_order="descending"
    )

    assert result["success"] is True
    assert len(result["results"]) > 0
    assert result["results"][0]["subject"] == "Important Meeting"


@pytest.mark.asyncio
async def test_advanced_search_with_date_range(mock_ews_client):
    """Test advanced search with date range filter."""
    tool = AdvancedSearchTool(mock_ews_client)

    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = []
    mock_query.__getitem__ = lambda self, key: []

    mock_ews_client.account.inbox.filter.return_value = mock_query

    result = await tool.execute(
        search_filter={
            "subject": "Report",
            "start_date": "2025-01-01T00:00:00+00:00",
            "end_date": "2025-01-31T23:59:59+00:00"
        },
        search_scope=["inbox"],
        max_results=50
    )

    assert result["success"] is True
    assert "results" in result


@pytest.mark.asyncio
async def test_advanced_search_multiple_folders(mock_ews_client):
    """Test searching across multiple folders."""
    from datetime import datetime
    tool = AdvancedSearchTool(mock_ews_client)

    # Mock results from different folders
    mock_inbox_email = MagicMock()
    mock_inbox_email.id = "inbox-1"
    mock_inbox_email.subject = "Inbox Email"
    mock_inbox_email.sender.email_address = "sender@example.com"
    mock_inbox_email.to_recipients = []
    mock_inbox_email.datetime_received = datetime(2025, 1, 15, 10, 0, 0)
    mock_inbox_email.is_read = False
    mock_inbox_email.has_attachments = False
    mock_inbox_email.importance = "Normal"
    mock_inbox_email.categories = []
    mock_inbox_email.text_body = "Body text"

    mock_sent_email = MagicMock()
    mock_sent_email.id = "sent-1"
    mock_sent_email.subject = "Sent Email"
    mock_sent_email.sender.email_address = "me@example.com"
    mock_sent_email.to_recipients = []
    mock_sent_email.datetime_received = datetime(2025, 1, 15, 11, 0, 0)
    mock_sent_email.is_read = True
    mock_sent_email.has_attachments = False
    mock_sent_email.importance = "Normal"
    mock_sent_email.categories = []
    mock_sent_email.text_body = "Body text"

    mock_inbox_query = MagicMock()
    mock_inbox_query.filter.return_value = mock_inbox_query
    mock_inbox_query.order_by.return_value = [mock_inbox_email]
    mock_inbox_query.__getitem__ = lambda self, key: [mock_inbox_email]

    mock_sent_query = MagicMock()
    mock_sent_query.filter.return_value = mock_sent_query
    mock_sent_query.order_by.return_value = [mock_sent_email]
    mock_sent_query.__getitem__ = lambda self, key: [mock_sent_email]

    mock_ews_client.account.inbox.filter.return_value = mock_inbox_query
    mock_ews_client.account.sent.filter.return_value = mock_sent_query

    result = await tool.execute(
        search_filter={"keywords": "email"},
        search_scope=["inbox", "sent"],
        max_results=100
    )

    assert result["success"] is True
    assert len(result["results"]) == 2


@pytest.mark.asyncio
async def test_advanced_search_empty_filter(mock_ews_client):
    """Test advanced search with empty filter."""
    tool = AdvancedSearchTool(mock_ews_client)

    with pytest.raises(Exception) as exc_info:
        await tool.execute(
            search_filter={},
            search_scope=["inbox"]
        )

    assert "search_filter is required" in str(exc_info.value).lower() or "empty" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_advanced_search_invalid_folder(mock_ews_client):
    """Test advanced search with invalid folder."""
    tool = AdvancedSearchTool(mock_ews_client)

    with pytest.raises(Exception) as exc_info:
        await tool.execute(
            search_filter={"subject": "test"},
            search_scope=["nonexistent_folder"]
        )

    assert "no valid folders" in str(exc_info.value).lower()
