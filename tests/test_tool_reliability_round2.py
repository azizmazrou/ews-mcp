"""Regression tests for the six production follow-up bugs (round 2).

Each test fails on pre-fix code and passes after.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# CAL-006 — check_availability diagnostic message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cal006_backend_exception_includes_type_in_response(mock_ews_client):
    """When get_free_busy_info raises, the response must include the
    real exception class name + message in the error field (not just
    generic 'Internal Server Error')."""
    from src.tools.calendar_tools import CheckAvailabilityTool

    class _UpstreamOops(Exception):
        pass

    mock_ews_client.account.primary_smtp_address = "me@example.com"
    mock_ews_client.account.protocol.get_free_busy_info.side_effect = _UpstreamOops(
        "exchange said no"
    )

    tool = CheckAvailabilityTool(mock_ews_client)
    # safe_execute never raises — it returns the dict.
    result = await tool.safe_execute(
        email_addresses=["me@example.com"],
        start_time="2026-04-18T08:00:00+00:00",
        end_time="2026-04-18T18:00:00+00:00",
    )
    assert result["success"] is False
    assert result["error_type"] == "ToolExecutionError"
    err = str(result.get("error", ""))
    # The upstream exception type appears in the message so operators
    # see WHAT broke, not just "Internal Server Error".
    assert "_UpstreamOops" in err or "exchange said no" in err, result


@pytest.mark.asyncio
async def test_cal006_self_only_valid_returns_success(mock_ews_client):
    """Smoke test: valid self-only params must NOT crash the tool."""
    from src.tools.calendar_tools import CheckAvailabilityTool

    mock_ews_client.account.primary_smtp_address = "me@example.com"
    mock_ews_client.account.protocol.get_free_busy_info.return_value = iter([])
    tool = CheckAvailabilityTool(mock_ews_client)
    result = await tool.execute(
        email_addresses=["me@example.com"],
        start_time="2026-04-18T08:00:00+00:00",
        end_time="2026-04-18T18:00:00+00:00",
        interval_minutes=30,
    )
    assert result["success"] is True


# ---------------------------------------------------------------------------
# TSK-004 — get_tasks diagnostic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tsk004_backend_exception_includes_type_in_response(mock_ews_client):
    """When the task folder query raises, operators see the class name."""
    from src.tools.task_tools import GetTasksTool

    class _ExchangeBoom(Exception):
        pass

    def _raiser(*args, **kwargs):
        raise _ExchangeBoom("server unavailable")

    mock_ews_client.account.tasks.all.side_effect = _raiser

    tool = GetTasksTool(mock_ews_client)
    result = await tool.safe_execute()
    assert result["success"] is False
    err = str(result.get("error", ""))
    assert "_ExchangeBoom" in err or "server unavailable" in err, result


# ---------------------------------------------------------------------------
# CON-008 — loose email validator accepts .invalid
# ---------------------------------------------------------------------------


def test_con008_create_contact_accepts_reserved_tlds():
    """RFC 2606 reserved TLDs (.invalid / .test / .example) must not be
    rejected by the contact-create model."""
    from src.models import CreateContactRequest

    # All of these are RFC 2606 reserved — pydantic's EmailStr rejects
    # them. Our loose validator accepts them.
    for email in [
        "user@example.invalid",
        "user@example.test",
        "user@example.example",
        "alice@company.com",  # regular domain still works
    ]:
        req = CreateContactRequest(
            given_name="A", surname="B", email_address=email,
        )
        assert req.email_address == email


def test_con008_loose_validator_rejects_real_garbage():
    """Sanity: the validator still rejects genuinely bad syntax."""
    from src.models import CreateContactRequest
    from pydantic import ValidationError as PydanticValidationError

    for bad in ["not-an-email", "@example.com", "user@", "user@space inside.com"]:
        with pytest.raises(PydanticValidationError):
            CreateContactRequest(
                given_name="A", surname="B", email_address=bad,
            )


# ---------------------------------------------------------------------------
# #4 — delete_email(hard_delete=True) actually hard-deletes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_email_hard_delete_uses_hard_delete_type(mock_ews_client):
    """hard_delete=True must call item.delete with the HARD_DELETE
    delete_type — not move() to trash and not a plain delete() that
    defaults to MoveToDeletedItems."""
    from src.tools.email_tools import DeleteEmailTool

    item = MagicMock()
    with patch(
        "src.tools.email_tools.find_message_for_account", return_value=item
    ):
        tool = DeleteEmailTool(mock_ews_client)
        result = await tool.execute(
            message_id="AAMk-1", hard_delete=True,
        )

    # move() must NOT be called — that would put the item in Trash.
    item.move.assert_not_called()
    # delete() must be called with an explicit HARD_DELETE delete_type.
    assert item.delete.called
    call_kwargs = item.delete.call_args.kwargs
    assert "delete_type" in call_kwargs
    # exchangelib's constant or the legacy string.
    delete_type = call_kwargs["delete_type"]
    assert str(delete_type).lower().replace("_", "").endswith("harddelete"), delete_type
    # Response reports permanent=True and hard_delete=True (both aliases).
    assert result["permanent"] is True
    assert result["hard_delete"] is True


@pytest.mark.asyncio
async def test_delete_email_default_moves_to_trash(mock_ews_client):
    """Without hard_delete/permanent, item.move(trash) is called."""
    from src.tools.email_tools import DeleteEmailTool

    trash = mock_ews_client.account.trash
    item = MagicMock()
    with patch(
        "src.tools.email_tools.find_message_for_account", return_value=item
    ):
        tool = DeleteEmailTool(mock_ews_client)
        result = await tool.execute(message_id="AAMk-1")

    item.move.assert_called_once_with(trash)
    item.delete.assert_not_called()
    assert result["permanent"] is False


@pytest.mark.asyncio
async def test_delete_email_permanent_alias_equivalent_to_hard_delete(mock_ews_client):
    """permanent=True must behave identically to hard_delete=True."""
    from src.tools.email_tools import DeleteEmailTool

    item = MagicMock()
    with patch(
        "src.tools.email_tools.find_message_for_account", return_value=item
    ):
        tool = DeleteEmailTool(mock_ews_client)
        result = await tool.execute(message_id="AAMk-1", permanent=True)

    item.move.assert_not_called()
    item.delete.assert_called_once()
    assert result["permanent"] is True


# ---------------------------------------------------------------------------
# #5 — manage_folder(action=move) accepts destination_folder_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_folder_move_accepts_destination_folder_id(mock_ews_client):
    """destination_folder_id should resolve the target parent without
    requiring the caller to also supply destination=<name>."""
    from src.tools.folder_tools import ManageFolderTool

    folder = MagicMock()
    folder.name = "Archive"
    folder.id = "AAMk-folder"

    target_parent = MagicMock()
    target_parent.id = "AAMk-target"

    def _find(root, fid):
        return folder if fid == "AAMk-folder" else target_parent if fid == "AAMk-target" else None

    tool = ManageFolderTool(mock_ews_client)
    with patch.object(tool, "_find_folder_by_id", side_effect=_find):
        result = await tool.execute(
            action="move",
            folder_id="AAMk-folder",
            destination_folder_id="AAMk-target",
        )
    assert result["success"] is True
    assert folder.parent is target_parent
    folder.save.assert_called_once()


@pytest.mark.asyncio
async def test_manage_folder_move_requires_destination(mock_ews_client):
    """Both destination and destination_folder_id missing -> 400."""
    from src.exceptions import ValidationError
    from src.tools.folder_tools import ManageFolderTool

    tool = ManageFolderTool(mock_ews_client)
    with pytest.raises(ValidationError):
        await tool.execute(action="move", folder_id="AAMk-folder")


# ---------------------------------------------------------------------------
# #6 — download_attachment traversal save_path = 400; file_path always
# ---------------------------------------------------------------------------


def _att_with_content():
    att = MagicMock()
    att.attachment_id = {"id": "AT-1"}
    att.name = "report.pdf"
    att.content = b"PDF bytes"
    att.content_type = "application/pdf"
    return att


@pytest.mark.asyncio
async def test_download_attachment_traversal_path_returns_400(mock_ews_client):
    """Traversal-style save_path must raise ValidationError (-> HTTP 400)."""
    from src.exceptions import ValidationError
    from src.tools.attachment_tools import DownloadAttachmentTool

    att = _att_with_content()
    msg = MagicMock()
    msg.attachments = [att]

    tool = DownloadAttachmentTool(mock_ews_client)
    with patch(
        "src.tools.attachment_tools.find_message_for_account",
        return_value=msg,
    ):
        for bad in ["../../etc/passwd", "/etc/passwd", "..\\..\\etc", "foo/bar.pdf"]:
            with pytest.raises(ValidationError):
                await tool.execute(
                    message_id="AAMk-msg",
                    attachment_id="AT-1",
                    return_as="file_path",
                    save_path=bad,
                )


@pytest.mark.asyncio
async def test_download_attachment_file_path_response_includes_file_path(mock_ews_client, tmp_path, monkeypatch):
    """Every file-mode success response must include file_path."""
    from src.tools.attachment_tools import DownloadAttachmentTool

    monkeypatch.setenv("EWS_DOWNLOAD_DIR", str(tmp_path))
    import importlib
    from src.tools import attachment_tools as at
    importlib.reload(at)

    att = _att_with_content()
    msg = MagicMock()
    msg.attachments = [att]

    tool = at.DownloadAttachmentTool(mock_ews_client)
    with patch(
        "src.tools.attachment_tools.find_message_for_account",
        return_value=msg,
    ):
        result = await tool.execute(
            message_id="AAMk-msg",
            attachment_id="AT-1",
            return_as="file_path",
            save_path="downloaded.pdf",
        )
    assert result["success"] is True
    assert "file_path" in result, result
    assert result["file_path"].endswith("downloaded.pdf")
    # File actually exists and contains the expected bytes.
    from pathlib import Path
    saved = Path(result["file_path"])
    assert saved.is_file()
    assert saved.read_bytes() == b"PDF bytes"


@pytest.mark.asyncio
async def test_download_attachment_traversal_via_openapi_returns_400(mock_ews_client):
    """End-to-end: SSE adapter maps the ValidationError to HTTP 400."""
    from src.tools.attachment_tools import DownloadAttachmentTool
    from src.openapi_adapter import OpenAPIAdapter

    att = _att_with_content()
    msg = MagicMock()
    msg.attachments = [att]

    tool = DownloadAttachmentTool(mock_ews_client)
    with patch(
        "src.tools.attachment_tools.find_message_for_account",
        return_value=msg,
    ):
        adapter = OpenAPIAdapter(server=None, tools={"download_attachment": tool}, settings=None)
        response = await adapter.handle_rest_request(
            "download_attachment",
            json.dumps({
                "message_id": "AAMk-msg",
                "attachment_id": "AT-1",
                "return_as": "file_path",
                "save_path": "../../etc/passwd",
            }).encode(),
        )
    assert response["status"] == 400, response
    assert response["success"] is False
