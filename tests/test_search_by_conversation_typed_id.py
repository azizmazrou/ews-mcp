"""Pin: search_by_conversation must pass a typed ConversationId to filter().

Pre-fix every per-folder ``folder.filter(conversation_id=raw_string)`` raised
TypeError when exchangelib built the SOAP restriction (Message.conversation_id
is an EWSElementField with value_cls=ConversationId). The walker caught the
exception per folder and marked all 19 mail folders as "skipped: TypeError",
returning 0 results across the board.

This test fails if a future refactor reverts to passing the raw string.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_filter_receives_typed_conversation_id(mock_ews_client):
    """folder.filter(conversation_id=…) must receive a ConversationId, not str."""
    from src.tools.search_tools import SearchByConversationTool
    from exchangelib.properties import ConversationId

    raw_cid = "AAQkADc3MWUyMGQ0LTU5OGUtNGE2MC1hOTUyLTFhZjc5ZDY1ZWJiOQ="

    # Capture the kwarg value the tool passes to filter().
    captured: dict = {}

    fake_query = MagicMock()
    fake_query.order_by.return_value.__getitem__ = lambda _self, _slc: []

    fake_folder = MagicMock()
    fake_folder.name = "Inbox"
    fake_folder.id = "folder-inbox"
    fake_folder.folder_class = "IPF.Note"

    def _capture_filter(*a, **kw):
        captured["conversation_id"] = kw.get("conversation_id")
        return fake_query

    fake_folder.filter = _capture_filter

    # Wire the tool's folder-walk to return exactly one folder.
    root = MagicMock()
    root.walk.return_value = [fake_folder]
    mock_ews_client.account.msg_folder_root = root
    mock_ews_client.account.root = root
    mock_ews_client.account.inbox = fake_folder
    mock_ews_client.account.sent = fake_folder
    mock_ews_client.account.trash = fake_folder
    mock_ews_client.account.drafts = fake_folder

    tool = SearchByConversationTool(mock_ews_client)
    result = await tool.execute(conversation_id=raw_cid, max_results=10)

    assert result["success"] is not False
    passed = captured.get("conversation_id")
    assert passed is not None, "filter() was never called with conversation_id"
    assert isinstance(passed, ConversationId), (
        f"expected ConversationId, got {type(passed).__name__}"
    )
    assert passed.id == raw_cid
