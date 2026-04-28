"""Regression tests for Bug 1 — search_emails silently dropped query/sender.

Before: calling ``search_emails`` with ``query="foo"`` or ``sender="x"``
(both advertised in the tool's schema) fell through the filter-build
whitelist. Log said ``No filters or date range provided. Limiting to
last 30 days.`` and 9 identical requests returned the same default
inbox slice.

After:
* The free-text ``query`` param applies subject-OR-body substring filter.
* ``sender``/``recipient`` aliases for ``from_address``/``to_address``
  now take effect.
* Unknown params raise ValidationError (HTTP 400), not silent drop.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tool(mock_ews_client):
    """Return a fresh SearchEmailsTool bound to the mock client."""
    from src.tools.email_tools import SearchEmailsTool
    return SearchEmailsTool(mock_ews_client)


def _patch_folder_returning(tool, emails):
    """Make the resolve_folder_for_account call produce a fake folder whose
    ``.all().filter(...).order_by(...)`` chain just records filters and
    returns ``emails``."""
    recorded: list = []

    class _Query:
        def filter(self, *args, **kwargs):
            recorded.append(("filter", args, kwargs))
            return self

        def order_by(self, *_args, **_kwargs):
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
    return recorded, patcher


@pytest.mark.asyncio
async def test_quick_mode_applies_query_filter(tool):
    """Bug 1 regression: ``query='invoice'`` must produce a filter call."""
    recorded, patcher = _patch_folder_returning(tool, [])
    try:
        result = await tool.execute(folder="inbox", query="invoice", max_results=5)
    finally:
        patcher.stop()

    assert result["success"] is True
    # The caller's query should show up as an OR filter on subject/body,
    # NOT get silently replaced by the "last 30 days" default.
    assert any(
        "invoice" in str(args) + str(kwargs)
        for (_op, args, kwargs) in recorded
    ), recorded


@pytest.mark.asyncio
async def test_quick_mode_applies_sender_alias(tool):
    """``sender='alice@...'`` should produce the same filter as ``from_address``."""
    recorded, patcher = _patch_folder_returning(tool, [])
    try:
        await tool.execute(folder="inbox", sender="alice@example.com", max_results=5)
    finally:
        patcher.stop()

    assert any(
        kwargs.get("sender") == "alice@example.com"
        for (_op, _args, kwargs) in recorded
    ), recorded


@pytest.mark.asyncio
async def test_quick_mode_applies_recipient_alias(tool):
    """``recipient='bob@...'`` should apply the to_recipients filter."""
    recorded, patcher = _patch_folder_returning(tool, [])
    try:
        await tool.execute(folder="inbox", recipient="bob@example.com", max_results=5)
    finally:
        patcher.stop()

    assert any(
        "bob@example.com" in str(kwargs)
        for (_op, _args, kwargs) in recorded
    ), recorded


@pytest.mark.asyncio
async def test_quick_mode_applies_is_flagged_filter(tool):
    """``is_flagged=True`` should filter on the Outlook follow-up flag."""
    recorded, patcher = _patch_folder_returning(tool, [])
    try:
        await tool.execute(folder="inbox", is_flagged=True, max_results=5)
    finally:
        patcher.stop()

    assert any(
        kwargs.get("flag_status_value") == 2
        for (_op, _args, kwargs) in recorded
    ), recorded


@pytest.mark.asyncio
async def test_quick_mode_query_suppresses_30day_default(tool, caplog):
    """With a filter supplied, the auto "last 30 days" log must NOT appear."""
    import logging
    recorded, patcher = _patch_folder_returning(tool, [])
    try:
        with caplog.at_level(logging.INFO, logger="SearchEmailsTool"):
            await tool.execute(folder="inbox", query="report", max_results=5)
    finally:
        patcher.stop()
    assert not any(
        "auto-limiting to last" in rec.message for rec in caplog.records
    ), [r.message for r in caplog.records]


@pytest.mark.asyncio
async def test_quick_mode_no_filter_still_applies_30day_default(tool, caplog):
    """Invariant: callers with no filters still get the "last 30 days" safety net."""
    import logging
    recorded, patcher = _patch_folder_returning(tool, [])
    try:
        with caplog.at_level(logging.INFO, logger="SearchEmailsTool"):
            await tool.execute(folder="inbox", max_results=5)
    finally:
        patcher.stop()
    assert any(
        "auto-limiting to last" in rec.message for rec in caplog.records
    ), [r.message for r in caplog.records]


@pytest.mark.asyncio
async def test_unknown_param_rejected_with_suggestion(tool):
    """Bug 1 b-side: unknown params must raise a ValidationError, not be
    silently ignored. The error should suggest the closest known name."""
    from src.exceptions import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        await tool.execute(folder="inbox", bogus_param="x")
    msg = str(excinfo.value).lower()
    assert "unknown param" in msg
    assert "bogus_param" in msg


@pytest.mark.asyncio
async def test_unknown_param_close_match_suggestion(tool):
    """Typo close to a known name should include a 'did you mean' hint."""
    from src.exceptions import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        await tool.execute(folder="inbox", subject_contain="x")  # typo
    assert "subject_contains" in str(excinfo.value)


@pytest.mark.asyncio
async def test_unknown_param_surfaces_as_400_via_openapi_adapter(mock_ews_client):
    """Via the SSE/HTTP path, unknown params must come back as HTTP 400."""
    import json
    from src.tools.email_tools import SearchEmailsTool
    from src.openapi_adapter import OpenAPIAdapter

    tool = SearchEmailsTool(mock_ews_client)
    adapter = OpenAPIAdapter(server=None, tools={"search_emails": tool}, settings=None)
    payload = json.dumps({"folder": "inbox", "bogus_param": "x"}).encode()
    response = await adapter.handle_rest_request("search_emails", payload)

    assert response["status"] == 400, response
    assert response["success"] is False, response
