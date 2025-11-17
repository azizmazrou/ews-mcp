"""Tests for advanced search tools."""

import pytest
from unittest.mock import Mock, MagicMock
from datetime import datetime

from src.tools.search_tools import (
    SearchByConversationTool,
    FullTextSearchTool
)
from src.exceptions import ToolExecutionError


@pytest.mark.asyncio
async def test_search_by_conversation_with_conversation_id(mock_ews_client):
    """Test searching by conversation ID."""
    tool = SearchByConversationTool(mock_ews_client)

    # Mock emails in conversation
    mock_email1 = MagicMock()
    mock_email1.id = "email-1"
    mock_email1.subject = "Project Discussion"
    mock_email1.sender.email_address = "alice@example.com"
    mock_email1.datetime_received = datetime(2025, 1, 1, 10, 0)
    mock_email1.conversation_id = "conversation-123"

    mock_email2 = MagicMock()
    mock_email2.id = "email-2"
    mock_email2.subject = "RE: Project Discussion"
    mock_email2.sender.email_address = "bob@example.com"
    mock_email2.datetime_received = datetime(2025, 1, 1, 11, 0)
    mock_email2.conversation_id = "conversation-123"

    # Mock filter and order_by chain
    mock_query = MagicMock()
    mock_query.filter.return_value.order_by.return_value = [mock_email1, mock_email2]
    mock_ews_client.account.inbox.all.return_value = mock_query

    result = await tool.execute(
        conversation_id="conversation-123",
        folder="inbox"
    )

    assert result["success"] is True
    assert result["conversation_id"] == "conversation-123"
    assert result["thread_count"] == 2
    assert len(result["emails"]) == 2
    assert result["emails"][0]["subject"] == "Project Discussion"


@pytest.mark.asyncio
async def test_search_by_conversation_with_message_id(mock_ews_client):
    """Test searching by message ID to get conversation."""
    tool = SearchByConversationTool(mock_ews_client)

    # Mock original message
    mock_original = MagicMock()
    mock_original.id = "email-1"
    mock_original.conversation_id = "conversation-456"

    # Mock conversation emails
    mock_email1 = MagicMock()
    mock_email1.id = "email-1"
    mock_email1.subject = "Meeting Request"
    mock_email1.sender.email_address = "carol@example.com"
    mock_email1.datetime_received = datetime(2025, 1, 2, 9, 0)
    mock_email1.conversation_id = "conversation-456"

    mock_email2 = MagicMock()
    mock_email2.id = "email-2"
    mock_email2.subject = "RE: Meeting Request"
    mock_email2.sender.email_address = "dave@example.com"
    mock_email2.datetime_received = datetime(2025, 1, 2, 10, 0)
    mock_email2.conversation_id = "conversation-456"

    # Mock get and filter/order_by
    mock_ews_client.account.inbox.get.return_value = mock_original

    mock_query = MagicMock()
    mock_query.filter.return_value.order_by.return_value = [mock_email1, mock_email2]
    mock_ews_client.account.inbox.all.return_value = mock_query

    result = await tool.execute(
        message_id="email-1",
        folder="inbox"
    )

    assert result["success"] is True
    assert result["conversation_id"] == "conversation-456"
    assert result["thread_count"] == 2


@pytest.mark.asyncio
async def test_search_by_conversation_no_results(mock_ews_client):
    """Test searching conversation with no results."""
    tool = SearchByConversationTool(mock_ews_client)

    # Mock empty results
    mock_query = MagicMock()
    mock_query.filter.return_value.order_by.return_value = []
    mock_ews_client.account.inbox.all.return_value = mock_query

    result = await tool.execute(conversation_id="nonexistent-conversation")

    assert result["success"] is True
    assert result["thread_count"] == 0
    assert len(result["emails"]) == 0


@pytest.mark.asyncio
async def test_search_by_conversation_missing_ids(mock_ews_client):
    """Test searching conversation without IDs."""
    tool = SearchByConversationTool(mock_ews_client)

    with pytest.raises(ToolExecutionError) as exc_info:
        await tool.execute(folder="inbox")

    assert "conversation_id or message_id" in str(exc_info.value)


@pytest.mark.asyncio
async def test_full_text_search_subject_and_body(mock_ews_client):
    """Test full-text search in subject and body."""
    tool = FullTextSearchTool(mock_ews_client)

    # Mock search results
    mock_email1 = MagicMock()
    mock_email1.id = "email-1"
    mock_email1.subject = "Important Project Update"
    mock_email1.sender.email_address = "manager@example.com"
    mock_email1.datetime_received = datetime(2025, 1, 3, 14, 0)
    mock_email1.text_body = "The project deadline has been extended."

    mock_email2 = MagicMock()
    mock_email2.id = "email-2"
    mock_email2.subject = "Budget Report"
    mock_email2.sender.email_address = "finance@example.com"
    mock_email2.datetime_received = datetime(2025, 1, 3, 15, 0)
    mock_email2.text_body = "The project budget is approved."

    # Mock filter and order_by chain
    mock_query = MagicMock()
    mock_query.filter.return_value.order_by.return_value = [mock_email1, mock_email2]
    mock_ews_client.account.inbox.all.return_value = mock_query

    result = await tool.execute(
        query="project",
        folder="inbox",
        search_in=["subject", "body"]
    )

    assert result["success"] is True
    assert result["query"] == "project"
    assert result["result_count"] == 2
    assert len(result["emails"]) == 2


@pytest.mark.asyncio
async def test_full_text_search_subject_only(mock_ews_client):
    """Test full-text search in subject only."""
    tool = FullTextSearchTool(mock_ews_client)

    # Mock search results
    mock_email = MagicMock()
    mock_email.id = "email-1"
    mock_email.subject = "Invoice #12345"
    mock_email.sender.email_address = "billing@example.com"
    mock_email.datetime_received = datetime(2025, 1, 4, 10, 0)
    mock_email.text_body = "Please find attached the invoice."

    mock_query = MagicMock()
    mock_query.filter.return_value.order_by.return_value = [mock_email]
    mock_ews_client.account.inbox.all.return_value = mock_query

    result = await tool.execute(
        query="Invoice",
        folder="inbox",
        search_in=["subject"]
    )

    assert result["success"] is True
    assert result["search_in"] == ["subject"]


@pytest.mark.asyncio
async def test_full_text_search_with_max_results(mock_ews_client):
    """Test full-text search with result limit."""
    tool = FullTextSearchTool(mock_ews_client)

    # Mock 5 search results
    mock_emails = []
    for i in range(5):
        mock_email = MagicMock()
        mock_email.id = f"email-{i}"
        mock_email.subject = f"Result {i}"
        mock_email.sender.email_address = f"user{i}@example.com"
        mock_email.datetime_received = datetime(2025, 1, 5, 10 + i, 0)
        mock_emails.append(mock_email)

    mock_query = MagicMock()
    mock_query.filter.return_value.order_by.return_value = mock_emails
    mock_ews_client.account.inbox.all.return_value = mock_query

    result = await tool.execute(
        query="test",
        folder="inbox",
        max_results=3
    )

    assert result["success"] is True
    assert len(result["emails"]) == 3  # Should be limited to 3
    assert result["result_count"] == 3


@pytest.mark.asyncio
async def test_full_text_search_case_sensitive(mock_ews_client):
    """Test case-sensitive full-text search."""
    tool = FullTextSearchTool(mock_ews_client)

    # Mock case-sensitive results
    mock_email = MagicMock()
    mock_email.id = "email-1"
    mock_email.subject = "URGENT: Action Required"
    mock_email.sender.email_address = "alerts@example.com"
    mock_email.datetime_received = datetime(2025, 1, 6, 9, 0)
    mock_email.text_body = "URGENT message"

    mock_query = MagicMock()
    mock_query.filter.return_value.order_by.return_value = [mock_email]
    mock_ews_client.account.inbox.all.return_value = mock_query

    result = await tool.execute(
        query="URGENT",
        folder="inbox",
        case_sensitive=True
    )

    assert result["success"] is True
    assert result["case_sensitive"] is True


@pytest.mark.asyncio
async def test_full_text_search_exact_phrase(mock_ews_client):
    """Test exact phrase search."""
    tool = FullTextSearchTool(mock_ews_client)

    # Mock exact phrase results
    mock_email = MagicMock()
    mock_email.id = "email-1"
    mock_email.subject = "Out of office"
    mock_email.sender.email_address = "colleague@example.com"
    mock_email.datetime_received = datetime(2025, 1, 7, 8, 0)
    mock_email.text_body = "I am out of office this week."

    mock_query = MagicMock()
    mock_query.filter.return_value.order_by.return_value = [mock_email]
    mock_ews_client.account.inbox.all.return_value = mock_query

    result = await tool.execute(
        query="out of office",
        folder="inbox",
        exact_phrase=True
    )

    assert result["success"] is True
    assert result["exact_phrase"] is True


@pytest.mark.asyncio
async def test_full_text_search_no_results(mock_ews_client):
    """Test full-text search with no results."""
    tool = FullTextSearchTool(mock_ews_client)

    # Mock empty results
    mock_query = MagicMock()
    mock_query.filter.return_value.order_by.return_value = []
    mock_ews_client.account.inbox.all.return_value = mock_query

    result = await tool.execute(
        query="nonexistent_term_xyz123",
        folder="inbox"
    )

    assert result["success"] is True
    assert result["result_count"] == 0
    assert len(result["emails"]) == 0


@pytest.mark.asyncio
async def test_full_text_search_missing_query(mock_ews_client):
    """Test full-text search without query."""
    tool = FullTextSearchTool(mock_ews_client)

    with pytest.raises(ToolExecutionError) as exc_info:
        await tool.execute(folder="inbox")

    assert "query is required" in str(exc_info.value)
