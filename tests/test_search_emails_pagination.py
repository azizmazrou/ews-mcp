"""Regression tests for Issue 2 — search_emails pagination + .only() +
narrow exception handling + dropped ``results`` key.

Each test fails against pre-fix code and passes after.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_email(i: int):
    m = MagicMock()
    m.subject = f"subject-{i}"
    m.text_body = "body"
    m.id = f"AAMk-{i}"
    m.sender = MagicMock(email_address=f"user{i}@example.com")
    m.to_recipients = []
    m.cc_recipients = []
    m.bcc_recipients = []
    m.datetime_received = datetime(2026, 4, 18, 10, 0, 0)
    m.is_read = False
    m.has_attachments = False
    m.importance = "Normal"
    m.categories = []
    return m


class _SpyQuery:
    """Stand-in for an exchangelib QuerySet that records slice access
    + captures the ``.only(*fields)`` call for assertion."""

    def __init__(self, size: int, *, total_count: int | None = None,
                 raise_at_offset: int | None = None, raise_exc: BaseException | None = None):
        self.size = size
        self.total_count = total_count if total_count is not None else size
        self.raise_at_offset = raise_at_offset
        self.raise_exc = raise_exc
        self.only_calls: list[tuple[str, ...]] = []
        self.slice_calls: list[tuple[int, int]] = []

    def filter(self, *a, **kw):
        return self

    def order_by(self, *a, **kw):
        return self

    def only(self, *fields):
        self.only_calls.append(fields)
        return self

    def count(self):
        return self.total_count

    def __getitem__(self, sl):
        self.slice_calls.append((sl.start or 0, sl.stop or 0))
        if self.raise_at_offset is not None and (sl.start or 0) >= self.raise_at_offset:
            raise self.raise_exc or RuntimeError("mid-iteration failure")
        start, stop = sl.start or 0, sl.stop or self.size
        stop = min(stop, self.size)
        if start >= self.size:
            return []
        return [_fake_email(i) for i in range(start, stop)]


class _FakeFolder:
    name = "inbox"

    def __init__(self, query: _SpyQuery):
        self._query = query

    def filter(self, *a, **kw):
        return self._query

    def all(self):
        return self._query


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _patch_advanced_search(mock_ews_client, query: _SpyQuery):
    """Point account.inbox / account.sent etc. at the same spy query."""
    folder = _FakeFolder(query)
    mock_ews_client.account.inbox = folder
    mock_ews_client.account.sent = folder
    mock_ews_client.account.drafts = folder
    mock_ews_client.account.trash = folder
    mock_ews_client.account.junk = folder


@pytest.mark.asyncio
async def test_issue2_advanced_returns_requested_count(mock_ews_client):
    """Paginated fetch must collect up to max_results from a sized iterable."""
    from src.tools.email_tools import SearchEmailsTool

    q = _SpyQuery(size=75, total_count=75)
    _patch_advanced_search(mock_ews_client, q)

    tool = SearchEmailsTool(mock_ews_client)
    result = await tool.execute(
        mode="advanced", search_scope=["inbox"], max_results=50,
        subject_contains="budget",
    )

    assert result["count"] == 50
    assert result["total_available"] == 75
    # Explicit paging: chunks of 50 (max_results itself), so at least one
    # slice request up to 50.
    assert q.slice_calls, q.slice_calls


@pytest.mark.asyncio
async def test_issue2_response_exposes_next_offset(mock_ews_client):
    """When total_available > count, the tool advertises next_offset."""
    from src.tools.email_tools import SearchEmailsTool

    q = _SpyQuery(size=200, total_count=200)
    _patch_advanced_search(mock_ews_client, q)

    tool = SearchEmailsTool(mock_ews_client)
    result = await tool.execute(
        mode="advanced", search_scope=["inbox"], max_results=50,
        subject_contains="anything",
    )
    assert result["total_available"] == 200
    assert result.get("next_offset") == 50


@pytest.mark.asyncio
async def test_issue2_response_drops_legacy_results_key(mock_ews_client):
    """Issue 4: ``results`` is gone; only ``items`` remains."""
    from src.tools.email_tools import SearchEmailsTool

    q = _SpyQuery(size=3, total_count=3)
    _patch_advanced_search(mock_ews_client, q)
    tool = SearchEmailsTool(mock_ews_client)
    result = await tool.execute(
        mode="advanced", search_scope=["inbox"], max_results=50,
        subject_contains="x",
    )
    assert "items" in result
    assert "results" not in result, list(result.keys())
    # Issue 4 also drops ``total`` and ``total_results``.
    assert "total" not in result
    assert "total_results" not in result


@pytest.mark.asyncio
async def test_issue2_fields_projection_calls_only(mock_ews_client):
    """fields=[...] narrows the EWS payload via ``.only(*db_fields)``."""
    from src.tools.email_tools import SearchEmailsTool

    q = _SpyQuery(size=5, total_count=5)
    _patch_advanced_search(mock_ews_client, q)
    tool = SearchEmailsTool(mock_ews_client)
    await tool.execute(
        mode="advanced", search_scope=["inbox"], max_results=10,
        subject_contains="x",
        fields=["message_id", "subject"],
    )
    assert q.only_calls, "query.only(...) was never called"
    called_with = set(q.only_calls[-1])
    # subject -> subject. message_id -> id. Both required.
    assert "id" in called_with
    assert "subject" in called_with


@pytest.mark.asyncio
async def test_issue2_mid_iteration_exception_surfaces_as_error_code(mock_ews_client):
    """A mid-stream exchangelib failure must leave an error_code in the
    response meta — prior code silently swallowed + continued."""
    from src.tools.email_tools import SearchEmailsTool

    class _ThrottledError(Exception):
        pass
    _ThrottledError.__name__ = "ErrorServerBusy"  # classify as THROTTLED

    q = _SpyQuery(size=500, total_count=500, raise_at_offset=100, raise_exc=_ThrottledError("busy"))
    _patch_advanced_search(mock_ews_client, q)
    tool = SearchEmailsTool(mock_ews_client)
    result = await tool.execute(
        mode="advanced", search_scope=["inbox"], max_results=250,
        subject_contains="x",
    )
    # Still a "success" from the caller's view — we don't fail the
    # overall call — but meta.per_folder_errors names the throttling.
    assert result["success"] is True
    meta = result.get("meta") or {}
    per_folder = meta.get("per_folder_errors") or []
    assert per_folder, result
    assert per_folder[0]["error_code"] == "THROTTLED"


@pytest.mark.asyncio
async def test_issue2_quick_mode_items_count_total_available(mock_ews_client):
    """Quick mode also returns items + count + total_available."""
    from src.tools.email_tools import SearchEmailsTool

    q = _SpyQuery(size=10, total_count=42)
    _patch_advanced_search(mock_ews_client, q)

    async def _resolve(*_args, **_kwargs):
        return _FakeFolder(q)

    with patch("src.tools.email_tools.resolve_folder_for_account", side_effect=_resolve):
        tool = SearchEmailsTool(mock_ews_client)
        result = await tool.execute(
            folder="inbox", query="report", max_results=10,
        )
    assert "items" in result
    assert "results" not in result
    assert result["count"] == 10
    assert result["total_available"] == 42
    assert result.get("next_offset") == 10
