"""Tests for attachment tools."""

import pytest
from unittest.mock import MagicMock
import base64

from src.tools.attachment_tools import (
    ListAttachmentsTool,
    DownloadAttachmentTool
)


@pytest.mark.asyncio
async def test_list_attachments_tool(mock_ews_client):
    """Test listing email attachments."""
    tool = ListAttachmentsTool(mock_ews_client)

    # Mock message with attachments
    mock_message = MagicMock()
    mock_attachment1 = MagicMock()
    mock_attachment1.attachment_id = {"id": "att1"}
    mock_attachment1.name = "document.pdf"
    mock_attachment1.size = 1024
    mock_attachment1.content_type = "application/pdf"
    mock_attachment1.is_inline = False
    mock_attachment1.content_id = None

    mock_attachment2 = MagicMock()
    mock_attachment2.attachment_id = {"id": "att2"}
    mock_attachment2.name = "image.png"
    mock_attachment2.size = 2048
    mock_attachment2.content_type = "image/png"
    mock_attachment2.is_inline = True
    mock_attachment2.content_id = "image001"

    mock_message.attachments = [mock_attachment1, mock_attachment2]
    mock_ews_client.account.inbox.get.return_value = mock_message

    result = await tool.execute(
        message_id="test-id",
        include_inline=True
    )

    assert result["success"] is True
    assert result["count"] == 2
    assert len(result["attachments"]) == 2
    assert result["attachments"][0]["name"] == "document.pdf"
    assert result["attachments"][1]["is_inline"] is True


@pytest.mark.asyncio
async def test_list_attachments_exclude_inline(mock_ews_client):
    """Test listing attachments excluding inline."""
    tool = ListAttachmentsTool(mock_ews_client)

    # Mock message with mixed attachments
    mock_message = MagicMock()
    mock_attachment1 = MagicMock()
    mock_attachment1.attachment_id = {"id": "att1"}
    mock_attachment1.name = "document.pdf"
    mock_attachment1.is_inline = False

    mock_attachment2 = MagicMock()
    mock_attachment2.attachment_id = {"id": "att2"}
    mock_attachment2.name = "image.png"
    mock_attachment2.is_inline = True

    mock_message.attachments = [mock_attachment1, mock_attachment2]
    mock_ews_client.account.inbox.get.return_value = mock_message

    result = await tool.execute(
        message_id="test-id",
        include_inline=False
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["attachments"][0]["name"] == "document.pdf"


@pytest.mark.asyncio
async def test_download_attachment_as_base64(mock_ews_client):
    """Test downloading attachment as base64."""
    tool = DownloadAttachmentTool(mock_ews_client)

    # Mock message and attachment
    mock_message = MagicMock()
    mock_attachment = MagicMock()
    mock_attachment.attachment_id = {"id": "att123"}
    mock_attachment.name = "test.txt"
    mock_attachment.content = b"Hello, World!"
    mock_attachment.content_type = "text/plain"

    mock_message.attachments = [mock_attachment]
    mock_ews_client.account.inbox.get.return_value = mock_message

    result = await tool.execute(
        message_id="test-id",
        attachment_id="att123",
        return_as="base64"
    )

    assert result["success"] is True
    assert result["name"] == "test.txt"
    assert result["size"] == 13
    assert "content_base64" in result
    assert result["content_base64"] == base64.b64encode(b"Hello, World!").decode('utf-8')


@pytest.mark.asyncio
async def test_download_attachment_as_file(mock_ews_client, tmp_path):
    """Test downloading attachment to file."""
    tool = DownloadAttachmentTool(mock_ews_client)

    # Mock message and attachment
    mock_message = MagicMock()
    mock_attachment = MagicMock()
    mock_attachment.attachment_id = {"id": "att456"}
    mock_attachment.name = "document.pdf"
    mock_attachment.content = b"PDF content here"
    mock_attachment.content_type = "application/pdf"

    mock_message.attachments = [mock_attachment]
    mock_ews_client.account.inbox.get.return_value = mock_message

    # Use temporary path
    save_path = tmp_path / "downloaded.pdf"

    result = await tool.execute(
        message_id="test-id",
        attachment_id="att456",
        return_as="file_path",
        save_path=str(save_path)
    )

    assert result["success"] is True
    assert result["name"] == "document.pdf"
    assert "file_path" in result
    assert save_path.read_bytes() == b"PDF content here"


@pytest.mark.asyncio
async def test_download_attachment_not_found(mock_ews_client):
    """Test downloading non-existent attachment."""
    tool = DownloadAttachmentTool(mock_ews_client)

    # Mock message with no matching attachment
    mock_message = MagicMock()
    mock_attachment = MagicMock()
    mock_attachment.attachment_id = {"id": "different-id"}
    mock_message.attachments = [mock_attachment]
    mock_ews_client.account.inbox.get.return_value = mock_message

    with pytest.raises(Exception) as exc_info:
        await tool.execute(
            message_id="test-id",
            attachment_id="nonexistent-id",
            return_as="base64"
        )

    assert "not found" in str(exc_info.value).lower()
