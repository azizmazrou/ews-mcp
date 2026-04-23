"""Regression tests for Issue 3 — search_by_conversation missed
archive and subfolder messages because the prior implementation
only iterated the well-known folders.

Fix walks ``account.msg_folder_root`` recursively by default and
surfaces ``searched_folders`` + ``skipped_folders`` for the caller.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from typing import Any, List

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_msg(msg_id: str, subject: str, conv_id: str):
    m = MagicMock()
    m.id = msg_id
    m.subject = subject
    m.conversation_id = conv_id
    m.sender = MagicMock(email_address="user@example.com")
    m.to_recipients = []
    m.text_body = "body"
    m.datetime_received = datetime(2026, 4, 18, 10, 0, 0)
    m.is_read = False
    m.has_attachments = False
    m.importance = "Normal"
    return m


class _FolderQuery:
    """Terminal query object supporting ``.order_by(...)[:N]``."""

    def __init__(self, items):
        self._items = items

    def order_by(self, *a, **kw):
        return self

    def __getitem__(self, sl):
        start = sl.start or 0
        stop = sl.stop if sl.stop is not None else len(self._items)
        return list(self._items[start:stop])


class _FakeFolder:
    def __init__(self, name: str, messages, *, folder_class: str = "IPF.Note",
                 raise_on_filter: BaseException | None = None):
        self.name = name
        self.folder_class = folder_class
        self.id = f"folder-{name}"
        self._messages = messages
        self._raise_on_filter = raise_on_filter

    def filter(self, *a, **kw):
        if self._raise_on_filter is not None:
            raise self._raise_on_filter
        conv_id = kw.get("conversation_id")
        items = [m for m in self._messages if m.conversation_id == conv_id]
        return _FolderQuery(items)


def _install_tree(mock_ews_client, folders, *,
                  inbox=None, sent=None, trash=None):
    """Attach ``folders`` to ``account.msg_folder_root.walk()``.

    Also wires standard attributes so include_all_folders=False paths work.
    """
    account = mock_ews_client.account
    root = MagicMock()
    root.walk.return_value = folders
    account.msg_folder_root = root
    # account.root is a separate fallback path in the tool.
    account.root = root
    if inbox is not None:
        account.inbox = inbox
    if sent is not None:
        account.sent = sent
    if trash is not None:
        account.trash = trash


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue3_walks_all_mail_folders_by_default(mock_ews_client):
    """include_all_folders defaults to True — archive + subfolders are found."""
    from src.tools.search_tools import SearchByConversationTool

    conv = "CONV-1"
    inbox_msgs = [_fake_msg("m1", "s1", conv)]
    archive_msgs = [_fake_msg("m2", "s2", conv)]
    project_sub = [_fake_msg("m3", "s3", conv)]

    inbox = _FakeFolder("Inbox", inbox_msgs)
    sent = _FakeFolder("Sent Items", [])
    archive = _FakeFolder("Archive", archive_msgs)
    project = _FakeFolder("Project/Client-X", project_sub)
    calendar = _FakeFolder("Calendar", [], folder_class="IPF.Appointment")

    _install_tree(
        mock_ews_client,
        folders=[inbox, sent, archive, project, calendar],
        inbox=inbox, sent=sent,
    )

    tool = SearchByConversationTool(mock_ews_client)
    result = await tool.execute(conversation_id=conv, max_results=100)

    assert result["success"] is True
    assert result["count"] == 3, result  # m1 + m2 + m3, calendar skipped

    ids = {item["message_id"] for item in result["items"]}
    assert ids == {"m1", "m2", "m3"}

    # Non-mail folder class (IPF.Appointment) must not appear in the walk.
    assert "Calendar" not in result["searched_folders"]
    # All three mail folders we iterated are named.
    assert set(result["searched_folders"]) == {
        "Inbox", "Sent Items", "Archive", "Project/Client-X",
    }


@pytest.mark.asyncio
async def test_issue3_dedup_across_folders(mock_ews_client):
    """If the same message surfaces in two folders (e.g. inbox + label),
    it only appears once in the response."""
    from src.tools.search_tools import SearchByConversationTool

    conv = "CONV-2"
    shared = _fake_msg("dup-id", "shared", conv)

    f1 = _FakeFolder("Inbox", [shared])
    f2 = _FakeFolder("Labeled/X", [shared])

    _install_tree(mock_ews_client, folders=[f1, f2], inbox=f1)
    tool = SearchByConversationTool(mock_ews_client)
    result = await tool.execute(conversation_id=conv, max_results=100)

    assert result["count"] == 1
    assert result["items"][0]["message_id"] == "dup-id"


@pytest.mark.asyncio
async def test_issue3_restricted_scope_respects_search_scope(mock_ews_client):
    """include_all_folders=False + search_scope=['inbox'] skips archive."""
    from src.tools.search_tools import SearchByConversationTool

    conv = "CONV-3"
    inbox_msgs = [_fake_msg("m1", "s1", conv)]
    archive_msgs = [_fake_msg("m-arch", "archived", conv)]

    inbox = _FakeFolder("Inbox", inbox_msgs)
    archive = _FakeFolder("Archive", archive_msgs)
    # archive listed in the tree walk — but we're restricting scope.
    _install_tree(
        mock_ews_client,
        folders=[inbox, archive],
        inbox=inbox,
    )

    tool = SearchByConversationTool(mock_ews_client)
    result = await tool.execute(
        conversation_id=conv,
        include_all_folders=False,
        search_scope=["inbox"],
        max_results=100,
    )

    ids = {item["message_id"] for item in result["items"]}
    assert ids == {"m1"}, ids
    assert result["searched_folders"] == ["Inbox"]


@pytest.mark.asyncio
async def test_issue3_skipped_folders_carry_error_type(mock_ews_client):
    """A folder whose .filter() raises is reported in skipped_folders."""
    from src.tools.search_tools import SearchByConversationTool

    class _AccessDenied(Exception):
        pass
    _AccessDenied.__name__ = "ErrorAccessDenied"

    conv = "CONV-4"
    inbox = _FakeFolder("Inbox", [_fake_msg("m1", "s1", conv)])
    forbidden = _FakeFolder(
        "Shared/Locked", [],
        raise_on_filter=_AccessDenied("access denied"),
    )

    _install_tree(
        mock_ews_client,
        folders=[inbox, forbidden],
        inbox=inbox,
    )

    tool = SearchByConversationTool(mock_ews_client)
    result = await tool.execute(conversation_id=conv, max_results=50)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["items"][0]["message_id"] == "m1"
    skipped = result["skipped_folders"]
    assert any(
        entry["folder"] == "Shared/Locked"
        and entry["reason"] == "permission_denied"
        and entry["error_type"] == "ErrorAccessDenied"
        for entry in skipped
    ), skipped


@pytest.mark.asyncio
async def test_issue3_response_drops_legacy_keys(mock_ews_client):
    """Response envelope: items + count + conversation_id + searched_folders.
    No ``results``, ``total``, or ``total_results``."""
    from src.tools.search_tools import SearchByConversationTool

    conv = "CONV-5"
    inbox = _FakeFolder("Inbox", [_fake_msg("m1", "s1", conv)])
    _install_tree(mock_ews_client, folders=[inbox], inbox=inbox)
    tool = SearchByConversationTool(mock_ews_client)
    result = await tool.execute(conversation_id=conv, max_results=10)

    assert "items" in result
    assert "count" in result
    assert "conversation_id" in result
    assert "searched_folders" in result
    assert "skipped_folders" in result
    for legacy in ("results", "total", "total_results"):
        assert legacy not in result, f"{legacy} still present: {list(result)}"


@pytest.mark.asyncio
async def test_issue3_resolves_conversation_id_from_message_id(mock_ews_client):
    """Caller may pass message_id instead of conversation_id."""
    from src.tools.search_tools import SearchByConversationTool

    conv = "CONV-6"
    seed = _fake_msg("seed-id", "seed", conv)
    followup = _fake_msg("reply-id", "Re: seed", conv)
    inbox = _FakeFolder("Inbox", [seed, followup])
    _install_tree(mock_ews_client, folders=[inbox], inbox=inbox)

    with patch(
        "src.tools.search_tools.find_message_across_folders",
        return_value=seed,
    ):
        tool = SearchByConversationTool(mock_ews_client)
        result = await tool.execute(message_id="seed-id", max_results=10)

    assert result["conversation_id"] == conv
    ids = {item["message_id"] for item in result["items"]}
    assert ids == {"seed-id", "reply-id"}


@pytest.mark.asyncio
async def test_issue3_missing_ids_raises_validation_error(mock_ews_client):
    """Neither conversation_id nor message_id => ValidationError."""
    from src.exceptions import ValidationError
    from src.tools.search_tools import SearchByConversationTool

    _install_tree(mock_ews_client, folders=[])
    tool = SearchByConversationTool(mock_ews_client)
    with pytest.raises(ValidationError):
        await tool.execute(max_results=10)
