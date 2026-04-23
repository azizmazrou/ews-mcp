"""Regression tests for Bug 2 — full_text mode returned 500 instead of 400.

Original symptom (production logs):

  2026-04-18 15:45:26 - src.utils - ERROR - query is required for full_text mode
  ... response: HTTP 500

Client had sent ``mode="full_text"`` with ``search_query="Hishmat"``.
Tool expected ``query``; mismatch raised ToolExecutionError which the
adapter bucketed as 500. Two fixes:

1. Accept ``search_query`` as a backward-compat alias for ``query``.
2. Missing query raises ValidationError -> HTTP 400, not 500.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def tool(mock_ews_client):
    from src.tools.email_tools import SearchEmailsTool
    return SearchEmailsTool(mock_ews_client)


@pytest.mark.asyncio
async def test_full_text_accepts_search_query_alias(tool, mock_ews_client):
    """Calling full_text with ``search_query='X'`` must run the search."""
    # Make the folder iteration return nothing so we don't need extensive mocks
    # — we just want to prove the tool got past the query validation.
    fake_folder = mock_ews_client.account.inbox
    fake_filter = fake_folder.filter.return_value
    fake_filter.order_by.return_value = []

    result = await tool.execute(
        mode="full_text",
        search_query="Hishmat",
        search_scope=["inbox"],
    )
    # With no emails the tool returns success + 0 items.
    assert result["success"] is True
    assert "items" in result, result


@pytest.mark.asyncio
async def test_full_text_missing_query_raises_validation_error(tool):
    """Missing query -> ValidationError (mapped to HTTP 400)."""
    from src.exceptions import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        await tool.execute(mode="full_text", search_scope=["inbox"])
    assert "query" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_full_text_missing_query_returns_400_via_openapi(mock_ews_client):
    """Via the SSE/HTTP adapter, missing query comes back as HTTP 400."""
    from src.tools.email_tools import SearchEmailsTool
    from src.openapi_adapter import OpenAPIAdapter

    tool = SearchEmailsTool(mock_ews_client)
    adapter = OpenAPIAdapter(server=None, tools={"search_emails": tool}, settings=None)
    payload = json.dumps({"mode": "full_text", "search_scope": ["inbox"]}).encode()
    response = await adapter.handle_rest_request("search_emails", payload)

    assert response["status"] == 400, response
    assert response["success"] is False
    # Upstream message should mention the missing parameter so operators can act.
    assert "query" in str(response).lower()


@pytest.mark.asyncio
async def test_full_text_prefers_query_over_search_query(tool, mock_ews_client):
    """If both are supplied, ``query`` wins. Consistent with _search_quick."""
    # Mock folder.filter().order_by() -> iter of zero emails for side-effect
    fake_folder = mock_ews_client.account.inbox
    fake_folder.filter.return_value.order_by.return_value = []

    result = await tool.execute(
        mode="full_text",
        query="preferred",
        search_query="legacy",
        search_scope=["inbox"],
    )
    assert result["success"] is True
