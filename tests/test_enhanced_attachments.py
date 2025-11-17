"""Tests for enhanced attachment tools."""

import pytest
from unittest.mock import Mock, MagicMock, patch
import base64
from pathlib import Path

from src.tools.attachment_tools import (
    AddAttachmentTool,
    DeleteAttachmentTool
)
from src.exceptions import ToolExecutionError


@pytest.mark.asyncio
async def test_add_attachment_from_file(mock_ews_client):
    """Test adding attachment from file path."""
    tool = AddAttachmentTool(mock_ews_client)

    # Mock message
    mock_message = MagicMock()
    mock_message.id = "message-id"
    mock_message.attachments = []
    mock_ews_client.account.drafts.get.return_value = mock_message

    # Create a temporary test file
    test_content = b"Test file content"
    test_file = Path("/tmp/test_attachment.txt")

    with patch('builtins.open', create=True) as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = test_content

        with patch('src.tools.attachment_tools.Path') as mock_path_class:
            mock_path = MagicMock()
            mock_path.name = "test_attachment.txt"
            mock_path_class.return_value = mock_path

            with patch('src.tools.attachment_tools.FileAttachment') as mock_file_attachment:
                mock_attachment = MagicMock()
                mock_attachment.attachment_id = "attachment-123"
                mock_file_attachment.return_value = mock_attachment

                result = await tool.execute(
                    message_id="message-id",
                    file_path="/tmp/test_attachment.txt",
                    content_type="text/plain",
                    is_inline=False
                )

    assert result["success"] is True
    assert "added successfully" in result["message"]
    assert result["attachment_name"] == "test_attachment.txt"
    assert result["message_id"] == "message-id"
    mock_message.save.assert_called_once()


@pytest.mark.asyncio
async def test_add_attachment_from_base64(mock_ews_client):
    """Test adding attachment from base64 content."""
    tool = AddAttachmentTool(mock_ews_client)

    # Mock message
    mock_message = MagicMock()
    mock_message.id = "message-id"
    mock_message.attachments = []
    mock_ews_client.account.drafts.get.return_value = mock_message

    # Create base64 content
    test_content = b"Test file content"
    b64_content = base64.b64encode(test_content).decode('utf-8')

    with patch('src.tools.attachment_tools.FileAttachment') as mock_file_attachment:
        mock_attachment = MagicMock()
        mock_attachment.attachment_id = "attachment-123"
        mock_file_attachment.return_value = mock_attachment

        result = await tool.execute(
            message_id="message-id",
            file_content=b64_content,
            file_name="document.pdf",
            content_type="application/pdf"
        )

    assert result["success"] is True
    assert "added successfully" in result["message"]
    assert result["attachment_name"] == "document.pdf"


@pytest.mark.asyncio
async def test_add_attachment_inline(mock_ews_client):
    """Test adding inline attachment."""
    tool = AddAttachmentTool(mock_ews_client)

    # Mock message
    mock_message = MagicMock()
    mock_message.id = "message-id"
    mock_message.attachments = []
    mock_ews_client.account.drafts.get.return_value = mock_message

    test_content = b"Image content"
    b64_content = base64.b64encode(test_content).decode('utf-8')

    with patch('src.tools.attachment_tools.FileAttachment') as mock_file_attachment:
        mock_attachment = MagicMock()
        mock_file_attachment.return_value = mock_attachment

        result = await tool.execute(
            message_id="message-id",
            file_content=b64_content,
            file_name="logo.png",
            content_type="image/png",
            is_inline=True,
            content_id="logo123"
        )

    assert result["success"] is True
    assert result["is_inline"] is True
    assert result["content_id"] == "logo123"


@pytest.mark.asyncio
async def test_add_attachment_missing_params(mock_ews_client):
    """Test adding attachment without required parameters."""
    tool = AddAttachmentTool(mock_ews_client)

    with pytest.raises(ToolExecutionError) as exc_info:
        await tool.execute(message_id="message-id")

    assert "file_path or file_content" in str(exc_info.value)


@pytest.mark.asyncio
async def test_add_attachment_file_not_found(mock_ews_client):
    """Test adding attachment from non-existent file."""
    tool = AddAttachmentTool(mock_ews_client)

    # Mock message
    mock_message = MagicMock()
    mock_ews_client.account.drafts.get.return_value = mock_message

    with patch('builtins.open', side_effect=FileNotFoundError("File not found")):
        with pytest.raises(ToolExecutionError) as exc_info:
            await tool.execute(
                message_id="message-id",
                file_path="/nonexistent/file.txt"
            )

        assert "File not found" in str(exc_info.value) or "Failed to add attachment" in str(exc_info.value)


@pytest.mark.asyncio
async def test_add_attachment_message_not_found(mock_ews_client):
    """Test adding attachment to non-existent message."""
    tool = AddAttachmentTool(mock_ews_client)

    # Mock all folders to raise exception
    mock_ews_client.account.drafts.get.side_effect = Exception("Not found")
    mock_ews_client.account.inbox.get.side_effect = Exception("Not found")
    mock_ews_client.account.sent.get.side_effect = Exception("Not found")

    with pytest.raises(ToolExecutionError) as exc_info:
        test_content = b"content"
        b64_content = base64.b64encode(test_content).decode('utf-8')

        await tool.execute(
            message_id="nonexistent-id",
            file_content=b64_content,
            file_name="test.txt"
        )

    assert "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_delete_attachment_by_id(mock_ews_client):
    """Test deleting attachment by attachment ID."""
    tool = DeleteAttachmentTool(mock_ews_client)

    # Mock message with attachments
    mock_attachment = MagicMock()
    mock_attachment.attachment_id = "attachment-to-delete"
    mock_attachment.name = "document.pdf"

    mock_message = MagicMock()
    mock_message.id = "message-id"
    mock_message.attachments = [mock_attachment]

    mock_ews_client.account.inbox.get.return_value = mock_message

    result = await tool.execute(
        message_id="message-id",
        attachment_id="attachment-to-delete"
    )

    assert result["success"] is True
    assert "deleted successfully" in result["message"]
    assert result["attachment_id"] == "attachment-to-delete"
    assert result["attachment_name"] == "document.pdf"
    mock_attachment.detach.assert_called_once()


@pytest.mark.asyncio
async def test_delete_attachment_by_name(mock_ews_client):
    """Test deleting attachment by name."""
    tool = DeleteAttachmentTool(mock_ews_client)

    # Mock message with attachments
    mock_attachment = MagicMock()
    mock_attachment.attachment_id = "attachment-123"
    mock_attachment.name = "report.pdf"

    mock_message = MagicMock()
    mock_message.id = "message-id"
    mock_message.attachments = [mock_attachment]

    mock_ews_client.account.inbox.get.return_value = mock_message

    result = await tool.execute(
        message_id="message-id",
        attachment_name="report.pdf"
    )

    assert result["success"] is True
    assert result["attachment_name"] == "report.pdf"
    mock_attachment.detach.assert_called_once()


@pytest.mark.asyncio
async def test_delete_attachment_not_found(mock_ews_client):
    """Test deleting non-existent attachment."""
    tool = DeleteAttachmentTool(mock_ews_client)

    # Mock message with different attachment
    mock_attachment = MagicMock()
    mock_attachment.attachment_id = "other-attachment"
    mock_attachment.name = "other.pdf"

    mock_message = MagicMock()
    mock_message.attachments = [mock_attachment]

    mock_ews_client.account.inbox.get.return_value = mock_message

    with pytest.raises(ToolExecutionError) as exc_info:
        await tool.execute(
            message_id="message-id",
            attachment_id="nonexistent-attachment"
        )

    assert "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_delete_attachment_missing_identifier(mock_ews_client):
    """Test deleting attachment without ID or name."""
    tool = DeleteAttachmentTool(mock_ews_client)

    with pytest.raises(ToolExecutionError) as exc_info:
        await tool.execute(message_id="message-id")

    assert "attachment_id or attachment_name" in str(exc_info.value)


@pytest.mark.asyncio
async def test_delete_attachment_message_not_found(mock_ews_client):
    """Test deleting attachment from non-existent message."""
    tool = DeleteAttachmentTool(mock_ews_client)

    # Mock all folders to raise exception
    mock_ews_client.account.inbox.get.side_effect = Exception("Not found")
    mock_ews_client.account.sent.get.side_effect = Exception("Not found")
    mock_ews_client.account.drafts.get.side_effect = Exception("Not found")

    with pytest.raises(ToolExecutionError) as exc_info:
        await tool.execute(
            message_id="nonexistent-id",
            attachment_id="attachment-id"
        )

    assert "not found" in str(exc_info.value).lower()
