"""Tests for attachment tools."""

import pytest
from unittest.mock import MagicMock, patch
import base64

from src.tools.attachment_tools import (
    ListAttachmentsTool,
    DownloadAttachmentTool,
    GetEmailMimeTool,
    AttachEmailToDraftTool,
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

    # The tool jails writes to EWS_DOWNLOAD_DIR (defaults to ./downloads).
    # Point the jail at the pytest tmp_path and verify we still get the
    # correct filename and content at the returned absolute path.
    import os
    from pathlib import Path
    os.environ["EWS_DOWNLOAD_DIR"] = str(tmp_path)
    # Re-import so the module picks up the new env var.
    import importlib
    from src.tools import attachment_tools as _at
    importlib.reload(_at)

    tool = _at.DownloadAttachmentTool(mock_ews_client)

    result = await tool.execute(
        message_id="test-id",
        attachment_id="att456",
        return_as="file_path",
        save_path="downloaded.pdf"
    )

    assert result["success"] is True
    assert result["name"] == "document.pdf"
    assert "file_path" in result
    saved = Path(result["file_path"])
    assert saved.name == "downloaded.pdf"
    # Resolved path must live inside the jail (no traversal).
    assert str(saved).startswith(str(tmp_path))
    assert saved.read_bytes() == b"PDF content here"


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


@pytest.mark.asyncio
async def test_get_email_mime_tool(mock_ews_client):
    """Test exporting MIME content for an email."""
    tool = GetEmailMimeTool(mock_ews_client)
    mock_message = MagicMock()
    mock_message.subject = "Subject"
    mock_message.mime_content = b"From: test@example.com\r\n\r\nHello"

    with patch("src.tools.attachment_tools.find_message_for_account", return_value=mock_message):
        result = await tool.execute(message_id="msg-1")

    assert result["success"] is True
    assert result["subject"] == "Subject"
    assert result["mime_content_base64"]


@pytest.mark.asyncio
async def test_attach_email_to_draft_tool(mock_ews_client):
    """Test embedding an existing email into a saved draft."""
    tool = AttachEmailToDraftTool(mock_ews_client)
    mock_draft = MagicMock()
    mock_source = MagicMock()
    mock_source.subject = "Original message"
    mock_source.body = "Body text"
    mock_source.to_recipients = []
    mock_source.cc_recipients = []
    mock_source.bcc_recipients = []
    mock_source.mime_content = b"From: test@example.com\r\n\r\nHello"

    with patch("src.tools.attachment_tools.find_message_for_account", side_effect=[mock_draft, mock_source]):
        with patch("src.tools.attachment_tools.build_embedded_message", return_value=MagicMock()):
            result = await tool.execute(draft_id="draft-1", source_message_id="msg-1")

    assert result["success"] is True
    assert result["attachment_name"] == "Original message.eml"
    mock_draft.attach.assert_called_once()
