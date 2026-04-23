"""Regression tests for Issue 5 — add get_emails_bulk so clients can
batch-fetch N messages in a single GetItem round-trip instead of N
sequential get_email_details calls.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from typing import Any, List

import pytest


def _fake_msg(i: int, *, subject: str | None = None):
    m = MagicMock()
    m.id = f"AAMk-{i}"
    m.subject = subject if subject is not None else f"subject-{i}"
    m.text_body = f"body {i}"
    m.sender = MagicMock(email_address=f"user{i}@example.com")
    m.to_recipients = []
    m.cc_recipients = []
    m.bcc_recipients = []
    m.datetime_received = datetime(2026, 4, 18, 10, i % 60)
    m.is_read = False
    m.has_attachments = False
    m.importance = "Normal"
    m.categories = []
    m.body = None
    m.conversation_id = f"conv-{i}"
    return m


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_issue5_get_schema_builds_without_nameerror(mock_ews_client):
    """Regression: the schema used ``_MAX_MESSAGES_HARD_CAP`` as a bare
    name inside a method, which crashed ``register_tools`` at server
    startup with NameError. Every tool's ``get_schema`` must return a
    plain dict without raising — that's how the MCP server discovers
    them on boot."""
    from src.tools.email_tools import GetEmailsBulkTool

    tool = GetEmailsBulkTool(mock_ews_client)
    schema = tool.get_schema()
    assert schema["name"] == "get_emails_bulk"
    props = schema["inputSchema"]["properties"]
    assert props["max_messages"]["maximum"] == 100


@pytest.mark.asyncio
async def test_issue5_batch_fetch_returns_all_requested(mock_ews_client):
    """10 ids -> 1 account.fetch() call -> 10 items back."""
    from src.tools.email_tools import GetEmailsBulkTool

    ids = [f"AAMk-{i}" for i in range(10)]
    returned = [_fake_msg(i) for i in range(10)]
    mock_ews_client.account.fetch = MagicMock(return_value=iter(returned))

    tool = GetEmailsBulkTool(mock_ews_client)
    result = await tool.execute(message_ids=ids)

    assert result["success"] is True
    assert result["count"] == 10
    assert result["requested"] == 10
    assert result["errors"] == []
    assert mock_ews_client.account.fetch.call_count == 1, \
        "batch tool should issue exactly one fetch call"


@pytest.mark.asyncio
async def test_issue5_response_envelope_shape(mock_ews_client):
    """Response exposes items/count/requested/errors; no ``results`` key."""
    from src.tools.email_tools import GetEmailsBulkTool

    ids = ["AAMk-1"]
    mock_ews_client.account.fetch = MagicMock(return_value=iter([_fake_msg(1)]))
    tool = GetEmailsBulkTool(mock_ews_client)
    result = await tool.execute(message_ids=ids)

    for key in ("items", "count", "requested", "errors"):
        assert key in result, (key, list(result))
    for legacy in ("results", "total", "total_results"):
        assert legacy not in result


@pytest.mark.asyncio
async def test_issue5_per_id_error_classification(mock_ews_client):
    """exchangelib returns BaseException-typed entries for per-id failures.
    NOT_FOUND when class name contains 'ErrorItemNotFound'; FETCH_ERROR otherwise."""
    from src.tools.email_tools import GetEmailsBulkTool

    class _NotFound(Exception):
        pass
    _NotFound.__name__ = "ErrorItemNotFound"

    class _Other(Exception):
        pass

    ids = ["good", "missing", "broken"]
    mixed = [
        _fake_msg(1),
        _NotFound("no such item"),
        _Other("corrupted"),
    ]
    mock_ews_client.account.fetch = MagicMock(return_value=iter(mixed))

    tool = GetEmailsBulkTool(mock_ews_client)
    result = await tool.execute(message_ids=ids)

    assert result["count"] == 1
    errors = {e["message_id"]: e["error_code"] for e in result["errors"]}
    assert errors["missing"] == "NOT_FOUND"
    assert errors["broken"] == "FETCH_ERROR"


@pytest.mark.asyncio
async def test_issue5_dedup_preserves_order(mock_ews_client):
    """Duplicate ids collapsed; fetch sees each unique id exactly once,
    in input order."""
    from src.tools.email_tools import GetEmailsBulkTool

    ids = ["a", "b", "a", "c", "b"]
    seen_batches: List[List[str]] = []

    def _fetch(messages):
        batch_ids = [m.id for m in messages]
        seen_batches.append(batch_ids)
        return iter([_fake_msg(i) for i, _ in enumerate(batch_ids)])

    mock_ews_client.account.fetch = MagicMock(side_effect=_fetch)

    tool = GetEmailsBulkTool(mock_ews_client)
    result = await tool.execute(message_ids=ids)

    assert result["requested"] == 3
    assert seen_batches == [["a", "b", "c"]]


@pytest.mark.asyncio
async def test_issue5_empty_list_raises_validation_error(mock_ews_client):
    """message_ids=[] must be rejected early (before any EWS call)."""
    from src.exceptions import ValidationError
    from src.tools.email_tools import GetEmailsBulkTool

    mock_ews_client.account.fetch = MagicMock()
    tool = GetEmailsBulkTool(mock_ews_client)
    with pytest.raises(ValidationError):
        await tool.execute(message_ids=[])
    mock_ews_client.account.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_issue5_oversize_batch_rejected(mock_ews_client):
    """>max_messages ids => ValidationError, no fetch attempt."""
    from src.exceptions import ValidationError
    from src.tools.email_tools import GetEmailsBulkTool

    ids = [f"AAMk-{i}" for i in range(60)]
    mock_ews_client.account.fetch = MagicMock()
    tool = GetEmailsBulkTool(mock_ews_client)
    with pytest.raises(ValidationError):
        await tool.execute(message_ids=ids, max_messages=50)
    mock_ews_client.account.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_issue5_max_messages_clamped_to_hard_cap(mock_ews_client):
    """max_messages above the hard cap (100) is silently clamped rather
    than crashing."""
    from src.tools.email_tools import GetEmailsBulkTool

    ids = [f"AAMk-{i}" for i in range(50)]
    mock_ews_client.account.fetch = MagicMock(
        return_value=iter([_fake_msg(i) for i in range(50)])
    )

    tool = GetEmailsBulkTool(mock_ews_client)
    # 500 -> clamped to 100, list of 50 fits.
    result = await tool.execute(message_ids=ids, max_messages=500)
    assert result["count"] == 50


@pytest.mark.asyncio
async def test_issue5_non_string_id_rejected(mock_ews_client):
    """Non-string entries rejected with ValidationError."""
    from src.exceptions import ValidationError
    from src.tools.email_tools import GetEmailsBulkTool

    mock_ews_client.account.fetch = MagicMock()
    tool = GetEmailsBulkTool(mock_ews_client)
    with pytest.raises(ValidationError):
        await tool.execute(message_ids=["a", 123, "b"])


@pytest.mark.asyncio
async def test_issue5_batch_fetch_error_bubbles_as_tool_execution_error(
    mock_ews_client,
):
    """A whole-batch failure (network/auth) surfaces as ToolExecutionError."""
    from src.exceptions import ToolExecutionError
    from src.tools.email_tools import GetEmailsBulkTool

    mock_ews_client.account.fetch = MagicMock(
        side_effect=RuntimeError("network blew up")
    )
    tool = GetEmailsBulkTool(mock_ews_client)
    with pytest.raises(ToolExecutionError):
        await tool.execute(message_ids=["AAMk-1"])
