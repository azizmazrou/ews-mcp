"""Regression tests for the six production follow-up bugs.

Each test fails against pre-fix code and passes after the fix. Bugs are
labelled B1–B6 matching the bug-report order.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# B1 — get_email_details without message_id -> 400, not 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b1_get_email_details_missing_message_id_raises_validation_error(mock_ews_client):
    from src.exceptions import ValidationError
    from src.tools.email_tools import GetEmailDetailsTool

    tool = GetEmailDetailsTool(mock_ews_client)
    with pytest.raises(ValidationError) as excinfo:
        await tool.execute()
    assert "message_id" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_b1_get_email_details_empty_message_id_raises_validation_error(mock_ews_client):
    from src.exceptions import ValidationError
    from src.tools.email_tools import GetEmailDetailsTool

    tool = GetEmailDetailsTool(mock_ews_client)
    with pytest.raises(ValidationError):
        await tool.execute(message_id="   ")


@pytest.mark.asyncio
async def test_b1_get_email_details_missing_message_id_is_400_via_openapi(mock_ews_client):
    """The SSE adapter must surface this as HTTP 400, not 500."""
    from src.tools.email_tools import GetEmailDetailsTool
    from src.openapi_adapter import OpenAPIAdapter

    tool = GetEmailDetailsTool(mock_ews_client)
    adapter = OpenAPIAdapter(server=None, tools={"get_email_details": tool}, settings=None)
    response = await adapter.handle_rest_request("get_email_details", json.dumps({}).encode())
    assert response["status"] == 400, response
    assert response["success"] is False


# ---------------------------------------------------------------------------
# B2 — check_availability bad-ISO -> 400 + clearer diagnostic on genuine errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b2_check_availability_bad_iso_returns_400(mock_ews_client):
    """Previously ``parse_display_datetime`` raised ValueError which was
    re-wrapped as ToolExecutionError (500). Now ValueError is caught and
    re-raised as ValidationError (400)."""
    from src.exceptions import ValidationError
    from src.tools.calendar_tools import CheckAvailabilityTool

    tool = CheckAvailabilityTool(mock_ews_client)
    with pytest.raises(ValidationError) as excinfo:
        await tool.execute(
            email_addresses=["me@example.com"],
            start_time="08:00",  # not a valid ISO datetime
            end_time="2026-04-18T18:00:00+00:00",
        )
    assert "iso" in str(excinfo.value).lower() or "datetime" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_b2_check_availability_missing_times_returns_400(mock_ews_client):
    from src.exceptions import ValidationError
    from src.tools.calendar_tools import CheckAvailabilityTool

    tool = CheckAvailabilityTool(mock_ews_client)
    with pytest.raises(ValidationError):
        await tool.execute(email_addresses=["me@example.com"])


@pytest.mark.asyncio
async def test_b2_check_availability_tolerates_null_primary_mailbox(mock_ews_client):
    """Accounts with primary_smtp_address=None must not crash the tool
    when include_self defaults to True."""
    from src.tools.calendar_tools import CheckAvailabilityTool

    # primary_smtp_address is None.
    mock_ews_client.account.primary_smtp_address = None
    # get_free_busy_info returns an empty iterable — exchangelib's type,
    # not important for this assertion; we just want the function to
    # complete without AttributeError.
    mock_ews_client.account.protocol.get_free_busy_info.return_value = iter([])

    tool = CheckAvailabilityTool(mock_ews_client)
    result = await tool.execute(
        email_addresses=["user@example.com"],
        start_time="2026-04-18T08:00:00+00:00",
        end_time="2026-04-18T18:00:00+00:00",
    )
    assert result["success"] is True


# ---------------------------------------------------------------------------
# B3 — get_tasks: malformed item doesn't sink the whole list; error logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b3_get_tasks_skips_malformed_items(mock_ews_client, caplog):
    """One broken task must not turn into an HTTP 500 for the whole list."""
    import logging

    from src.tools.task_tools import GetTasksTool

    good = MagicMock()
    good.subject = "Write spec"
    good.id = "AAMk-good"
    good.status = "NotStarted"
    good.percent_complete = 0
    good.is_complete = False
    good.due_date = None
    good.importance = "Normal"

    bad = MagicMock()
    bad.subject = "Broken"
    bad.id = "AAMk-bad"

    # Trigger AttributeError when the formatter touches due_date.isoformat
    # via an unexpected object type.
    class _Weird:
        # No isoformat, and str() raises too.
        def __str__(self):  # pragma: no cover - forced to raise
            raise RuntimeError("cannot stringify due_date")

    bad.due_date = _Weird()
    bad.status = "NotStarted"
    bad.percent_complete = 0
    bad.is_complete = False
    bad.importance = "Normal"

    # Tasks folder.all().filter().order_by() -> iterable[: max_results]
    chain = MagicMock()
    chain.filter.return_value = chain
    chain.order_by.return_value = chain
    chain.__getitem__ = lambda _self, _slc: [good, bad]
    mock_ews_client.account.tasks.all.return_value = chain

    tool = GetTasksTool(mock_ews_client)
    with caplog.at_level(logging.WARNING, logger="GetTasksTool"):
        result = await tool.execute(max_results=10)

    assert result["success"] is True, result
    # Good task survived; bad one was skipped and counted.
    assert result["count"] == 1
    assert result["skipped"] == 1
    assert result["tasks"][0]["item_id"] == "AAMk-good"
    # And the skip was logged at WARNING for diagnosis.
    assert any("malformed task" in rec.message.lower() for rec in caplog.records)


@pytest.mark.asyncio
async def test_b3_get_tasks_missing_folder_returns_tool_error(mock_ews_client):
    """Mailboxes without a Tasks folder get a clear ToolExecutionError, not 500."""
    from src.exceptions import ToolExecutionError
    from src.tools.task_tools import GetTasksTool

    mock_ews_client.account.tasks = None
    tool = GetTasksTool(mock_ews_client)
    with pytest.raises(ToolExecutionError) as excinfo:
        await tool.execute()
    assert "tasks folder" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# B4 — manage_folder delete: accept hard_delete alias; missing folder = 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b4_manage_folder_delete_accepts_hard_delete_alias(mock_ews_client):
    """hard_delete=True must behave the same as permanent=True."""
    from src.tools.folder_tools import ManageFolderTool

    folder = MagicMock()
    folder.name = "Archive"
    folder.id = "AAMkFolder"
    tool = ManageFolderTool(mock_ews_client)
    with patch.object(tool, "_find_folder_by_id", return_value=folder):
        result = await tool.execute(
            action="delete", folder_id="AAMkFolder", hard_delete=True,
        )
    assert result["success"] is True
    assert result["permanent"] is True
    folder.delete.assert_called_once()
    folder.soft_delete.assert_not_called()


@pytest.mark.asyncio
async def test_b4_manage_folder_delete_soft_default(mock_ews_client):
    """Without hard_delete/permanent, soft_delete is called."""
    from src.tools.folder_tools import ManageFolderTool

    folder = MagicMock()
    folder.name = "Projects"
    folder.id = "AAMkFolder"
    tool = ManageFolderTool(mock_ews_client)
    with patch.object(tool, "_find_folder_by_id", return_value=folder):
        result = await tool.execute(action="delete", folder_id="AAMkFolder")
    assert result["success"] is True
    assert result["permanent"] is False
    folder.soft_delete.assert_called_once()
    folder.delete.assert_not_called()


@pytest.mark.asyncio
async def test_b4_manage_folder_delete_missing_folder_returns_400(mock_ews_client):
    """Not-found folder returns ValidationError (→ HTTP 400), not 500."""
    from src.exceptions import ValidationError
    from src.tools.folder_tools import ManageFolderTool

    tool = ManageFolderTool(mock_ews_client)
    with patch.object(tool, "_find_folder_by_id", return_value=None):
        with pytest.raises(ValidationError):
            await tool.execute(action="delete", folder_id="unknown")


@pytest.mark.asyncio
async def test_b4_manage_folder_delete_requires_id_or_name(mock_ews_client):
    from src.exceptions import ValidationError
    from src.tools.folder_tools import ManageFolderTool

    tool = ManageFolderTool(mock_ews_client)
    with pytest.raises(ValidationError):
        await tool.execute(action="delete")


# ---------------------------------------------------------------------------
# B5 — Fwd: prefix normalisation (create_forward_draft/ReplyDraft)
# ---------------------------------------------------------------------------


def test_b5_add_forward_prefix_normalises_fwd_variants():
    from src.tools.email_tools import add_forward_prefix

    assert add_forward_prefix("hello") == "FW: hello"
    # All existing variants normalise to FW:
    assert add_forward_prefix("FW: hello") == "FW: hello"
    assert add_forward_prefix("Fwd: hello") == "FW: hello"
    assert add_forward_prefix("FWD: hello") == "FW: hello"
    assert add_forward_prefix("fwd: hello") == "FW: hello"
    assert add_forward_prefix("Forward: hello") == "FW: hello"
    # Never stacks.
    assert add_forward_prefix("FW: FW: hello") == "FW: FW: hello"  # inner is part of body
    # Mixed whitespace/casing.
    assert add_forward_prefix("  Fwd:   hello world") == "FW: hello world"


def test_b5_add_reply_prefix_normalises_re_variants():
    from src.tools.email_tools import add_reply_prefix

    assert add_reply_prefix("hello") == "RE: hello"
    assert add_reply_prefix("RE: hello") == "RE: hello"
    assert add_reply_prefix("Re: hello") == "RE: hello"
    assert add_reply_prefix("Reply: hello") == "RE: hello"


def test_b5_empty_input_returns_bare_prefix():
    from src.tools.email_tools import add_forward_prefix, add_reply_prefix

    assert add_forward_prefix("") == "FW:"
    assert add_forward_prefix(None) == "FW:"
    assert add_reply_prefix("") == "RE:"
    assert add_reply_prefix(None) == "RE:"


# ---------------------------------------------------------------------------
# B6a — create_contact accepts full_name alias
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b6a_create_contact_accepts_full_name_alias(mock_ews_client):
    """full_name='Alice Bob' -> given_name='Alice', surname='Bob'."""
    from src.tools.contact_tools import CreateContactTool
    from src.models import CreateContactRequest

    captured_request = {}

    # Patch validate_input to capture what kwargs reached the model.
    tool = CreateContactTool(mock_ews_client)
    original = tool.validate_input

    def _capture(model, **kwargs):
        captured_request.update(kwargs)
        return original(model, **kwargs)

    mock_ews_client.account.bulk_create = MagicMock()

    with patch.object(tool, "validate_input", side_effect=_capture):
        # Patch the Contact class so the actual save path is a no-op —
        # we only care that the validated request has given_name/surname
        # populated from full_name before model validation runs.
        import src.tools.contact_tools as ct
        with patch.object(ct, "Contact", MagicMock()):
            try:
                await tool.execute(
                    full_name="Alice Bob",
                    email_address="alice@example.com",
                )
            except Exception:
                # Downstream save path is mocked; we care about the
                # kwargs that reached validate_input.
                pass
    assert captured_request.get("given_name") == "Alice"
    assert captured_request.get("surname") == "Bob"


def test_b6a_create_contact_schema_advertises_full_name():
    from src.tools.contact_tools import CreateContactTool

    tool = CreateContactTool(MagicMock())
    schema = tool.get_schema()
    props = schema["inputSchema"]["properties"]
    assert "full_name" in props
    assert "deprecated" in props["full_name"]["description"].lower()


# ---------------------------------------------------------------------------
# B6b — download_attachment accepts attachment_name as fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b6b_download_attachment_resolves_by_name(mock_ews_client):
    """attachment_name='report.pdf' should find the attachment by name."""
    from src.tools.attachment_tools import DownloadAttachmentTool

    att = MagicMock()
    att.attachment_id = {"id": "AT-1"}
    att.name = "report.pdf"
    att.content = b"PDF bytes"
    att.content_type = "application/pdf"

    msg = MagicMock()
    msg.attachments = [att]

    tool = DownloadAttachmentTool(mock_ews_client)
    with patch(
        "src.tools.attachment_tools.find_message_for_account",
        return_value=msg,
    ):
        result = await tool.execute(
            message_id="AAMk-msg",
            attachment_name="Report.PDF",  # case-insensitive match
        )
    assert result["success"] is True
    assert result["name"] == "report.pdf"


@pytest.mark.asyncio
async def test_b6b_download_attachment_ambiguous_name_raises_validation(mock_ews_client):
    """When two attachments share a name, the caller gets a 400-mapped
    error asking for attachment_id."""
    from src.exceptions import ValidationError
    from src.tools.attachment_tools import DownloadAttachmentTool

    def _att(id_):
        a = MagicMock()
        a.attachment_id = {"id": id_}
        a.name = "duplicate.pdf"
        a.content = b"x"
        return a

    msg = MagicMock()
    msg.attachments = [_att("A"), _att("B")]

    tool = DownloadAttachmentTool(mock_ews_client)
    with patch(
        "src.tools.attachment_tools.find_message_for_account",
        return_value=msg,
    ):
        with pytest.raises(ValidationError):
            await tool.execute(
                message_id="AAMk-msg",
                attachment_name="duplicate.pdf",
            )


@pytest.mark.asyncio
async def test_b6b_download_attachment_requires_id_or_name(mock_ews_client):
    from src.exceptions import ValidationError
    from src.tools.attachment_tools import DownloadAttachmentTool

    tool = DownloadAttachmentTool(mock_ews_client)
    with pytest.raises(ValidationError):
        await tool.execute(message_id="AAMk-msg")


def test_b6b_download_attachment_schema_advertises_attachment_name():
    from src.tools.attachment_tools import DownloadAttachmentTool

    tool = DownloadAttachmentTool(MagicMock())
    schema = tool.get_schema()
    props = schema["inputSchema"]["properties"]
    assert "attachment_name" in props
    # message_id is required; attachment_{id,name} checked at runtime.
    assert schema["inputSchema"]["required"] == ["message_id"]
