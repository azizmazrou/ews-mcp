"""Tests for email tools."""

import pytest
from datetime import datetime
from unittest.mock import ANY, Mock, MagicMock, patch

from src.tools.email_tools import (
    SendEmailTool,
    ReadEmailsTool,
    SearchEmailsTool,
    GetEmailDetailsTool,
    DeleteEmailTool,
    MoveEmailTool,
    UpdateEmailTool,
    CopyEmailTool,
    resolve_folder_for_account,
)
from src.tools.email_tools_draft import CreateDraftTool, CreateReplyDraftTool, CreateForwardDraftTool


@pytest.mark.asyncio
async def test_send_email_tool(mock_ews_client, sample_email):
    """Test sending email."""
    tool = SendEmailTool(mock_ews_client)

    # Mock message
    with patch('src.tools.email_tools.Message') as mock_message:
        mock_msg = MagicMock()
        mock_msg.id = "test-message-id"
        mock_message.return_value = mock_msg

        result = await tool.execute(**sample_email)

        assert result["success"] is True
        assert "sent successfully" in result["message"].lower()
        mock_msg.send.assert_called_once()


@pytest.mark.asyncio
async def test_send_email_validation_error(mock_ews_client):
    """Test send email with invalid input."""
    tool = SendEmailTool(mock_ews_client)

    with pytest.raises(Exception):
        await tool.execute(
            to=[],  # Empty recipients should fail
            subject="",
            body=""
        )


@pytest.mark.asyncio
async def test_create_draft_tool_saves_draft(mock_ews_client, sample_email):
    """Test creating a draft saves to Drafts instead of sending."""
    tool = CreateDraftTool(mock_ews_client)
    mock_ews_client.get_account = Mock(return_value=mock_ews_client.account)
    mock_ews_client.account.drafts = MagicMock()

    with patch('src.tools.email_tools_draft.Message') as mock_message:
        mock_msg = MagicMock()
        mock_msg.id = "draft-message-id"
        mock_message.return_value = mock_msg

        result = await tool.execute(**sample_email)

        assert result["success"] is True
        assert "draft created successfully" in result["message"].lower()
        assert result["subject"] == sample_email["subject"]
        assert result["recipients"] == sample_email["to"]
        mock_msg.save.assert_called_once()
        mock_msg.send.assert_not_called()


@pytest.mark.asyncio
async def test_create_reply_draft_tool_saves_html_draft(mock_ews_client):
    """Test creating a reply draft saves a Message to Drafts instead of sending."""
    tool = CreateReplyDraftTool(mock_ews_client)
    mock_ews_client.get_account = Mock(return_value=mock_ews_client.account)
    mock_ews_client.account.drafts = MagicMock()
    mock_ews_client.account.primary_smtp_address = "test@example.com"

    original_message = MagicMock()
    original_message.subject = "Original Subject"
    original_message.sender.email_address = "sender@example.com"
    original_message.sender.name = "Sender Name"
    original_message.to_recipients = [MagicMock(email_address="test@example.com", name="Test User")]
    original_message.cc_recipients = []
    original_message.datetime_sent = datetime(2025, 1, 1, 10, 0, 0)
    original_message.body = MagicMock()
    original_message.body.body = "<html><body><p>Original HTML</p></body></html>"
    original_message.attachments = []

    with patch("src.tools.email_tools_draft.find_message_for_account", return_value=original_message):
        with patch("src.tools.email_tools_draft.Message") as mock_message:
            mock_msg = MagicMock()
            mock_msg.id = "reply-draft-id"
            mock_message.return_value = mock_msg

            result = await tool.execute(
                message_id="orig-id",
                body="Reply body"
            )

    assert result["success"] is True
    assert "reply draft created successfully" in result["message"].lower()
    assert result["original_subject"] == "Original Subject"
    assert result["reply_subject"] == "RE: Original Subject"
    assert result["message_id"] == "reply-draft-id"
    mock_msg.save.assert_called_once()
    mock_msg.send.assert_not_called()


@pytest.mark.asyncio
async def test_create_reply_draft_tool_reply_all_uses_all_recipients(mock_ews_client):
    """Test reply-all draft includes sender and original recipients except self."""
    tool = CreateReplyDraftTool(mock_ews_client)
    mock_ews_client.get_account = Mock(return_value=mock_ews_client.account)
    mock_ews_client.account.drafts = MagicMock()
    mock_ews_client.account.primary_smtp_address = "me@example.com"

    def mailbox(name, email):
        recipient = MagicMock()
        recipient.name = name
        recipient.email_address = email
        return recipient

    original_message = MagicMock()
    original_message.subject = "Original Subject"
    original_message.sender.email_address = "sender@example.com"
    original_message.sender.name = "Sender Name"
    original_message.to_recipients = [
        mailbox("Me", "me@example.com"),
        mailbox("Teammate", "team@example.com"),
    ]
    original_message.cc_recipients = [
        mailbox("Other", "other@example.com"),
        mailbox("Teammate", "team@example.com"),
    ]
    original_message.datetime_sent = datetime(2025, 1, 1, 10, 0, 0)
    original_message.body = MagicMock()
    original_message.body.body = "<p>Original HTML</p>"
    original_message.attachments = []

    with patch("src.tools.email_tools_draft.find_message_for_account", return_value=original_message):
        with patch("src.tools.email_tools_draft.Message") as mock_message:
            mock_msg = MagicMock()
            mock_msg.id = "reply-all-draft-id"
            mock_message.return_value = mock_msg

            await tool.execute(
                message_id="orig-id",
                body="Reply all body",
                reply_all=True
            )

    called_kwargs = mock_message.call_args.kwargs
    recipients = [recipient.email_address for recipient in called_kwargs["to_recipients"]]
    assert recipients == ["sender@example.com", "team@example.com", "other@example.com"]


@pytest.mark.asyncio
async def test_create_forward_draft_tool_saves_html_draft(mock_ews_client):
    """Test creating a forward draft saves a Message to Drafts instead of sending."""
    tool = CreateForwardDraftTool(mock_ews_client)
    mock_ews_client.get_account = Mock(return_value=mock_ews_client.account)
    mock_ews_client.account.drafts = MagicMock()

    original_message = MagicMock()
    original_message.subject = "Original Subject"
    original_message.sender.email_address = "sender@example.com"
    original_message.sender.name = "Sender Name"
    original_message.to_recipients = [MagicMock(email_address="me@example.com", name="Me")]
    original_message.cc_recipients = []
    original_message.datetime_sent = datetime(2025, 1, 1, 10, 0, 0)
    original_message.body = MagicMock()
    original_message.body.body = "<html><body><p>Original HTML</p></body></html>"
    original_message.attachments = []

    with patch("src.tools.email_tools_draft.find_message_for_account", return_value=original_message):
        with patch("src.tools.email_tools_draft.Message") as mock_message:
            mock_msg = MagicMock()
            mock_msg.id = "forward-draft-id"
            mock_message.return_value = mock_msg

            result = await tool.execute(
                message_id="orig-id",
                to=["target@example.com"],
                body="Forward body"
            )

    assert result["success"] is True
    assert "forward draft created successfully" in result["message"].lower()
    assert result["original_subject"] == "Original Subject"
    assert result["forward_subject"] == "FW: Original Subject"
    assert result["message_id"] == "forward-draft-id"
    assert result["forwarded_to"] == ["target@example.com"]
    mock_msg.save.assert_called_once()
    mock_msg.send.assert_not_called()


@pytest.mark.asyncio
async def test_create_forward_draft_tool_sets_cc_and_bcc(mock_ews_client):
    """Test forward draft includes CC and BCC recipients."""
    tool = CreateForwardDraftTool(mock_ews_client)
    mock_ews_client.get_account = Mock(return_value=mock_ews_client.account)
    mock_ews_client.account.drafts = MagicMock()

    original_message = MagicMock()
    original_message.subject = "Original Subject"
    original_message.sender.email_address = "sender@example.com"
    original_message.sender.name = "Sender Name"
    original_message.to_recipients = []
    original_message.cc_recipients = []
    original_message.datetime_sent = datetime(2025, 1, 1, 10, 0, 0)
    original_message.body = MagicMock()
    original_message.body.body = "<p>Original HTML</p>"
    original_message.attachments = []

    with patch("src.tools.email_tools_draft.find_message_for_account", return_value=original_message):
        with patch("src.tools.email_tools_draft.Message") as mock_message:
            mock_msg = MagicMock()
            mock_msg.id = "forward-draft-id"
            mock_message.return_value = mock_msg

            await tool.execute(
                message_id="orig-id",
                to=["target@example.com"],
                cc=["cc@example.com"],
                bcc=["bcc@example.com"],
                body="Forward body"
            )

    called_kwargs = mock_message.call_args.kwargs
    assert [recipient.email_address for recipient in called_kwargs["to_recipients"]] == ["target@example.com"]
    assert [recipient.email_address for recipient in mock_msg.cc_recipients] == ["cc@example.com"]
    assert [recipient.email_address for recipient in mock_msg.bcc_recipients] == ["bcc@example.com"]


@pytest.mark.asyncio
async def test_read_emails_tool(mock_ews_client):
    """Test reading emails."""
    from datetime import datetime
    tool = ReadEmailsTool(mock_ews_client)

    # Mock inbox items
    mock_email = MagicMock()
    mock_email.id = "email-1"
    mock_email.subject = "Test Subject"
    mock_email.sender.email_address = "sender@example.com"
    mock_email.datetime_received = datetime(2025, 1, 1, 10, 0, 0)
    mock_email.is_read = False
    mock_email.has_attachments = False
    mock_email.text_body = "Test body"

    mock_ews_client.account.inbox.all.return_value.order_by.return_value = [mock_email]

    result = await tool.execute(folder="inbox", max_results=10)

    assert result["success"] is True
    assert len(result["emails"]) > 0
    assert result["emails"][0]["subject"] == "Test Subject"


@pytest.mark.asyncio
async def test_search_emails_tool(mock_ews_client):
    """Test searching emails."""
    tool = SearchEmailsTool(mock_ews_client)

    # Mock search results
    mock_email = MagicMock()
    mock_email.id = "email-1"
    mock_email.subject = "Important Email"
    mock_email.sender.email_address = "important@example.com"

    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = [mock_email]

    mock_ews_client.account.inbox.all.return_value = mock_query

    result = await tool.execute(
        folder="inbox",
        subject_contains="Important"
    )

    assert result["success"] is True


@pytest.mark.asyncio
async def test_delete_email_tool(mock_ews_client):
    """Test deleting email."""
    tool = DeleteEmailTool(mock_ews_client)

    # Mock email
    mock_email = MagicMock()
    mock_ews_client.account.inbox.get.return_value = mock_email

    result = await tool.execute(message_id="test-id", permanent=False)

    assert result["success"] is True
    mock_email.move.assert_called_once()


@pytest.mark.asyncio
async def test_move_email_tool(mock_ews_client):
    """Test moving email."""
    tool = MoveEmailTool(mock_ews_client)

    mock_email = MagicMock()
    mock_folder = MagicMock()
    mock_folder.name = "Sent"

    with patch("src.tools.email_tools.find_message_for_account", return_value=mock_email):
        with patch("src.tools.email_tools.resolve_folder_for_account", return_value=mock_folder):
            result = await tool.execute(
                message_id="test-id",
                destination_folder="sent"
            )

    assert result["success"] is True
    assert result["destination_folder"] == "Sent"
    mock_email.move.assert_called_once_with(mock_folder)


@pytest.mark.asyncio
async def test_move_email_tool_with_destination_folder_id(mock_ews_client):
    """Test moving email using destination folder ID."""
    tool = MoveEmailTool(mock_ews_client)

    mock_email = MagicMock()
    mock_folder = MagicMock()
    mock_folder.name = "Archive 2026"
    folder_id = "AAMk" + ("x" * 60)

    with patch("src.tools.email_tools.find_message_for_account", return_value=mock_email):
        with patch("src.tools.email_tools.resolve_folder_for_account", return_value=mock_folder) as mock_resolve:
            result = await tool.execute(
                message_id="test-id",
                destination_folder_id=folder_id
            )

    assert result["success"] is True
    assert result["destination_folder"] == "Archive 2026"
    mock_resolve.assert_called_once_with(ANY, folder_id)
    mock_email.move.assert_called_once_with(mock_folder)


@pytest.mark.asyncio
async def test_resolve_folder_for_account_prefers_root_for_custom_paths(mock_ews_client):
    """Top-level custom paths should resolve from mailbox root before inbox fallback."""
    top_level = MagicMock()
    top_level.name = "Anwendungen"
    nested = MagicMock()
    nested.name = "ISA"
    top_level.children = [nested]
    mock_ews_client.account.root.children = [top_level]
    mock_ews_client.account.inbox.children = []

    resolved = await resolve_folder_for_account(mock_ews_client.account, "Anwendungen/ISA")

    assert resolved is nested


@pytest.mark.asyncio
async def test_resolve_folder_for_account_supports_root_relative_paths(mock_ews_client):
    """Root-relative custom paths should start from account.root instead of Inbox."""
    top_level = MagicMock()
    top_level.name = "Budget"
    top_level.children = []
    mock_ews_client.account.root.children = [top_level]

    resolved = await resolve_folder_for_account(mock_ews_client.account, "/Budget")

    assert resolved is top_level


@pytest.mark.asyncio
async def test_move_email_tool_requires_destination(mock_ews_client):
    """Test move_email requires folder name or folder ID."""
    tool = MoveEmailTool(mock_ews_client)

    with pytest.raises(Exception) as exc_info:
        await tool.execute(message_id="test-id")

    assert "either destination_folder or destination_folder_id is required" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_update_email_tool(mock_ews_client):
    """Test updating email properties."""
    tool = UpdateEmailTool(mock_ews_client)

    # Mock email
    mock_email = MagicMock()
    mock_email.flag = MagicMock()
    mock_ews_client.account.inbox.get.return_value = mock_email

    result = await tool.execute(
        message_id="test-id",
        is_read=True,
        categories=["Important", "Work"],
        flag_status="Flagged",
        importance="High"
    )

    assert result["success"] is True
    assert "updated successfully" in result["message"].lower()
    assert result["updates"]["is_read"] is True
    assert result["updates"]["categories"] == ["Important", "Work"]
    mock_email.save.assert_called_once()


@pytest.mark.asyncio
async def test_update_email_not_found(mock_ews_client):
    """Test updating email that doesn't exist."""
    tool = UpdateEmailTool(mock_ews_client)

    # Mock all folders to raise exception (message not found)
    mock_ews_client.account.inbox.get.side_effect = Exception("Not found")
    mock_ews_client.account.sent.get.side_effect = Exception("Not found")
    mock_ews_client.account.drafts.get.side_effect = Exception("Not found")

    with pytest.raises(Exception) as exc_info:
        await tool.execute(message_id="nonexistent-id", is_read=True)

    assert "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_copy_email_tool(mock_ews_client):
    """Test copying email to another folder."""
    tool = CopyEmailTool(mock_ews_client)

    # Mock email
    mock_email = MagicMock()
    mock_email.id = "email-to-copy"
    mock_email.subject = "Important Document"

    # Mock copied email
    mock_copied = MagicMock()
    mock_copied.id = "copied-email-id"
    mock_email.copy.return_value = mock_copied

    mock_dest_folder = MagicMock()
    mock_dest_folder.name = "Archive"
    mock_email.folder = MagicMock(name="Inbox")

    with patch("src.tools.email_tools.find_message_for_account", return_value=mock_email):
        with patch("src.tools.email_tools.resolve_folder_for_account", return_value=mock_dest_folder):
            result = await tool.execute(
                message_id="email-to-copy",
                destination_folder="archive"
            )

    assert result["success"] is True
    assert result["destination_folder"] == "Archive"
    mock_email.copy.assert_called_once_with(to_folder=mock_dest_folder)


@pytest.mark.asyncio
async def test_copy_email_tool_with_destination_folder_id(mock_ews_client):
    """Copy email should reuse the same folder resolver as move_email."""
    tool = CopyEmailTool(mock_ews_client)
    mock_email = MagicMock()
    mock_folder = MagicMock()
    mock_folder.name = "ISA"
    mock_email.copy.return_value = MagicMock(id="copied-id")

    with patch("src.tools.email_tools.find_message_for_account", return_value=mock_email):
        with patch("src.tools.email_tools.resolve_folder_for_account", return_value=mock_folder) as mock_resolve:
            result = await tool.execute(
                message_id="email-to-copy",
                destination_folder_id="AAMk" + ("x" * 60)
            )

    assert result["success"] is True
    mock_resolve.assert_called_once()
    mock_email.copy.assert_called_once_with(to_folder=mock_folder)


@pytest.mark.asyncio
async def test_copy_email_not_found(mock_ews_client):
    """Test copying non-existent email."""
    tool = CopyEmailTool(mock_ews_client)

    # Mock all folders to raise exception
    mock_ews_client.account.inbox.get.side_effect = Exception("Not found")
    mock_ews_client.account.sent.get.side_effect = Exception("Not found")
    mock_ews_client.account.drafts.get.side_effect = Exception("Not found")

    with pytest.raises(Exception):
        await tool.execute(
            message_id="nonexistent-id",
            destination_folder="archive"
        )
