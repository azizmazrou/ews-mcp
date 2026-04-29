"""Pin that ``add_attachment(is_inline=True, content_id=...)`` actually
passes ``content_id`` THROUGH to the ``FileAttachment(...)`` constructor.

History (CHANGELOG, 2026-04-26): the schema didn't accept ``content_id``,
so callers couldn't reference inline images via ``cid:<id>``. The fix
added the parameter and plumbing.

The existing test in ``test_enhanced_attachments.py`` only asserts that
``result["content_id"]`` echoes back — it does NOT verify that the kwarg
reaches ``FileAttachment(...)``. A future refactor that drops the kwarg
on the floor (but still echoes it in the response) would slip past.

This test patches ``FileAttachment`` and inspects the actual constructor
kwargs.
"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from src.tools.attachment_tools import AddAttachmentTool


@pytest.fixture
def draft_message():
    msg = MagicMock(name="draft")
    msg.attachments = []
    return msg


@pytest.mark.asyncio
async def test_add_attachment_inline_passes_content_id_to_file_attachment(
    mock_ews_client, draft_message
):
    """The exact regression hazard: a future refactor could record the
    content_id in the response dict but never pass it to FileAttachment,
    silently breaking inline images. Pin the constructor kwargs."""
    payload = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()

    captured_kwargs: dict = {}

    def _record(**kwargs):
        captured_kwargs.update(kwargs)
        att = MagicMock()
        att.content_id = kwargs.get("content_id")
        att.is_inline = kwargs.get("is_inline")
        att.name = kwargs.get("name")
        return att

    # Find the message: stub the in-Drafts lookup so we don't need an
    # actual mailbox traversal.
    mock_ews_client.account.drafts.filter.return_value = [draft_message]
    mock_ews_client.account.drafts.children = []

    with patch("src.tools.attachment_tools.FileAttachment", side_effect=_record):
        tool = AddAttachmentTool(mock_ews_client)
        await tool.execute(
            message_id="AAMk-draft",
            file_name="logo.png",
            file_content=payload,
            content_type="image/png",
            is_inline=True,
            content_id="logo123",
        )

    assert captured_kwargs.get("content_id") == "logo123", (
        "FileAttachment did NOT receive content_id — inline images break. "
        "See CHANGELOG add_attachment is_inline fix."
    )
    assert captured_kwargs.get("is_inline") is True
    assert captured_kwargs.get("name") == "logo.png"
    assert captured_kwargs.get("content") == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_add_attachment_non_inline_does_not_force_content_id(
    mock_ews_client, draft_message
):
    """Non-inline attachments should NOT carry a content_id (it's optional
    and only meaningful for cid: references in HTML)."""
    payload = base64.b64encode(b"PDF data").decode()

    captured_kwargs: dict = {}

    def _record(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    mock_ews_client.account.drafts.filter.return_value = [draft_message]
    mock_ews_client.account.drafts.children = []

    with patch("src.tools.attachment_tools.FileAttachment", side_effect=_record):
        tool = AddAttachmentTool(mock_ews_client)
        await tool.execute(
            message_id="AAMk-draft",
            file_name="report.pdf",
            file_content=payload,
            content_type="application/pdf",
            is_inline=False,
        )

    # content_id should not be set when caller doesn't provide it.
    assert "content_id" not in captured_kwargs or captured_kwargs.get("content_id") in (None, "")
    assert captured_kwargs.get("is_inline") is False
