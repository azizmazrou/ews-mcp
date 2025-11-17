"""Tests for contact tools."""

import pytest
from unittest.mock import MagicMock

from src.tools.contact_tools import (
    CreateContactTool,
    SearchContactsTool,
    GetContactsTool,
    UpdateContactTool,
    DeleteContactTool,
    ResolveNamesTool
)


@pytest.mark.asyncio
async def test_resolve_names_tool(mock_ews_client):
    """Test resolving names to contacts."""
    tool = ResolveNamesTool(mock_ews_client)

    # Mock resolution result
    mock_resolution = MagicMock()
    mock_mailbox = MagicMock()
    mock_mailbox.name = "John Doe"
    mock_mailbox.email_address = "john.doe@example.com"
    mock_mailbox.routing_type = "SMTP"
    mock_mailbox.mailbox_type = "Mailbox"
    mock_resolution.mailbox = mock_mailbox

    mock_ews_client.account.protocol.resolve_names.return_value = [mock_resolution]

    result = await tool.execute(
        name_query="john",
        return_full_info=False
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "John Doe"
    assert result["results"][0]["email"] == "john.doe@example.com"
    mock_ews_client.account.protocol.resolve_names.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_names_with_full_info(mock_ews_client):
    """Test resolving names with full contact information."""
    tool = ResolveNamesTool(mock_ews_client)

    # Mock resolution with contact details
    mock_resolution = MagicMock()
    mock_mailbox = MagicMock()
    mock_mailbox.name = "Jane Smith"
    mock_mailbox.email_address = "jane.smith@example.com"
    mock_resolution.mailbox = mock_mailbox

    mock_contact = MagicMock()
    mock_contact.given_name = "Jane"
    mock_contact.surname = "Smith"
    mock_contact.company_name = "Acme Corp"
    mock_contact.job_title = "Manager"
    mock_resolution.contact = mock_contact

    mock_ews_client.account.protocol.resolve_names.return_value = [mock_resolution]

    result = await tool.execute(
        name_query="jane",
        return_full_info=True
    )

    assert result["success"] is True
    assert len(result["results"]) == 1
    assert "contact_details" in result["results"][0]
    assert result["results"][0]["contact_details"]["given_name"] == "Jane"


@pytest.mark.asyncio
async def test_resolve_names_no_results(mock_ews_client):
    """Test resolving names with no matches."""
    tool = ResolveNamesTool(mock_ews_client)

    mock_ews_client.account.protocol.resolve_names.return_value = []

    result = await tool.execute(name_query="nonexistent")

    assert result["success"] is True
    assert result["count"] == 0
    assert len(result["results"]) == 0
