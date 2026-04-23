"""Regression tests for Bug 6 — response envelope bloat + no fields projection.

Measured production bloat: a 148-byte email body came back as a 13.6 KB
envelope (92:1). Two fixes:

1. List endpoints (search_emails, search_by_conversation,
   semantic_search_emails) default to a list-default field set that
   excludes body; a 200-char ``snippet`` is emitted instead.
2. ``fields=[...]`` param on those tools + ``get_email_details`` returns
   only the named fields. Unknown fields are silently ignored so the
   response schema can grow without breaking old callers.

Invariant preserved: ``get_email_details`` returns the full ``email``
shape when ``fields`` is omitted.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.utils import (
    project_fields,
    ensure_snippet,
    strip_body_by_default,
    LIST_DEFAULT_FIELDS,
    EMAIL_DETAIL_FIELDS,
)


# --- Pure helpers --------------------------------------------------------


def test_project_fields_keeps_only_requested_keys():
    item = {"a": 1, "b": 2, "c": 3}
    assert project_fields(item, ["a", "c"]) == {"a": 1, "c": 3}


def test_project_fields_noop_when_fields_none():
    item = {"a": 1}
    assert project_fields(item, None) == item


def test_project_fields_silently_ignores_unknown_fields():
    item = {"a": 1}
    assert project_fields(item, ["a", "not_there"]) == {"a": 1}


def test_strip_body_by_default_removes_body_when_not_requested():
    item = {"subject": "x", "body": "y", "body_html": "<p>y</p>"}
    strip_body_by_default(item, keep_body=False)
    assert "body" not in item and "body_html" not in item


def test_strip_body_by_default_keeps_body_when_requested():
    item = {"subject": "x", "body": "y"}
    strip_body_by_default(item, keep_body=True)
    assert item["body"] == "y"


def test_ensure_snippet_caps_at_200_chars():
    item = {"body": "x" * 500}
    ensure_snippet(item)
    assert len(item["snippet"]) <= 200


# --- search_emails: snippet-only by default ----------------------------


def _patch_folder_with(tool, emails):
    class _Query:
        def filter(self, *a, **kw):
            return self

        def order_by(self, *a, **kw):
            return self

        def __getitem__(self, _slice):
            return list(emails)

    class _Folder:
        def all(self):
            return _Query()

    async def _resolve(*_args, **_kwargs):
        return _Folder()

    patcher = patch("src.tools.email_tools.resolve_folder_for_account", side_effect=_resolve)
    patcher.start()
    return patcher


def _make_fake_email(subject="test", body="x" * 500):
    m = MagicMock()
    m.subject = subject
    m.text_body = body
    m.id = "AAMk-1"
    m.sender = MagicMock(email_address="alice@example.com")
    m.to_recipients = []
    m.cc_recipients = []
    m.bcc_recipients = []
    m.datetime_received = datetime(2026, 4, 18, 10, 0, 0)
    m.is_read = False
    m.has_attachments = False
    return m


@pytest.mark.asyncio
async def test_search_emails_default_list_has_no_body_field(mock_ews_client):
    from src.tools.email_tools import SearchEmailsTool

    tool = SearchEmailsTool(mock_ews_client)
    patcher = _patch_folder_with(tool, [_make_fake_email()])
    try:
        result = await tool.execute(folder="inbox", max_results=5)
    finally:
        patcher.stop()

    assert result["success"] is True
    items = result["items"]
    assert len(items) == 1
    item = items[0]
    assert "body" not in item
    assert "body_html" not in item
    assert "snippet" in item
    assert len(item["snippet"]) <= 200


@pytest.mark.asyncio
async def test_search_emails_fields_param_restricts_output(mock_ews_client):
    from src.tools.email_tools import SearchEmailsTool

    tool = SearchEmailsTool(mock_ews_client)
    patcher = _patch_folder_with(tool, [_make_fake_email()])
    try:
        result = await tool.execute(
            folder="inbox", max_results=5, fields=["message_id", "subject"]
        )
    finally:
        patcher.stop()

    assert result["items"][0].keys() == {"message_id", "subject"}


@pytest.mark.asyncio
async def test_search_emails_fields_body_opts_in(mock_ews_client):
    from src.tools.email_tools import SearchEmailsTool

    tool = SearchEmailsTool(mock_ews_client)
    patcher = _patch_folder_with(tool, [_make_fake_email(body="hello world")])
    try:
        result = await tool.execute(
            folder="inbox", max_results=5, fields=["message_id", "subject", "body"]
        )
    finally:
        patcher.stop()
    item = result["items"][0]
    assert "body" in item
    assert item["body"] == "hello world"


# --- get_email_details ---------------------------------------------------


@pytest.mark.asyncio
async def test_get_email_details_default_shape_unchanged(mock_ews_client):
    """Invariant: ``get_email_details`` without ``fields`` returns the full
    ``email`` object, not a projected one. Backward-compat for unknown callers."""
    from src.tools.email_tools import GetEmailDetailsTool

    fake = _make_fake_email()
    with patch(
        "src.tools.email_tools.find_message_for_account", return_value=fake
    ):
        tool = GetEmailDetailsTool(mock_ews_client)
        result = await tool.execute(message_id="AAMk-1")

    email = result["email"]
    # Full shape keys present.
    for key in ("message_id", "subject", "from", "to", "cc", "body",
                "body_html", "received_time", "sent_time", "is_read",
                "has_attachments", "importance", "attachments"):
        assert key in email, email.keys()


@pytest.mark.asyncio
async def test_get_email_details_fields_param_projects(mock_ews_client):
    from src.tools.email_tools import GetEmailDetailsTool

    fake = _make_fake_email()
    with patch(
        "src.tools.email_tools.find_message_for_account", return_value=fake
    ):
        tool = GetEmailDetailsTool(mock_ews_client)
        result = await tool.execute(
            message_id="AAMk-1", fields=["message_id", "subject"]
        )

    email = result["email"]
    assert email.keys() == {"message_id", "subject"}


# --- search_by_conversation ---------------------------------------------


@pytest.mark.asyncio
async def test_search_by_conversation_default_no_body(mock_ews_client):
    from src.tools.search_tools import SearchByConversationTool

    fake = _make_fake_email()
    fake.conversation_id = "AAQk-1"
    # folder.filter(...).order_by(...).[:n] chain
    mock_folder = mock_ews_client.account.inbox
    mock_folder.filter.return_value.order_by.return_value.__getitem__ = (
        lambda _self, _slc: [fake]
    )

    tool = SearchByConversationTool(mock_ews_client)
    # Issue 3 default is include_all_folders=True; restrict to inbox so the
    # single mocked folder is actually iterated.
    result = await tool.execute(
        conversation_id="AAQk-1",
        include_all_folders=False,
        search_scope=["inbox"],
    )
    assert result["success"] is True
    for item in result["items"]:
        assert "body" not in item
        assert "snippet" in item
