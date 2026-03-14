"""Tests for contact tools."""

import pytest
from unittest.mock import MagicMock

from src.tools.contact_tools import (
    CreateContactTool,
    UpdateContactTool,
    DeleteContactTool
)
from src.tools.contact_intelligence_tools import FindPersonTool


@pytest.mark.asyncio
async def test_find_person_gal(mock_ews_client):
    """Test finding person via GAL (replaces resolve_names)."""
    tool = FindPersonTool(mock_ews_client)

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
        query="john",
        source="gal"
    )

    assert result["success"] is True


@pytest.mark.asyncio
async def test_find_person_no_results(mock_ews_client):
    """Test finding person with no matches."""
    tool = FindPersonTool(mock_ews_client)

    mock_ews_client.account.protocol.resolve_names.return_value = []

    result = await tool.execute(query="nonexistent", source="gal")

    assert result["success"] is True
