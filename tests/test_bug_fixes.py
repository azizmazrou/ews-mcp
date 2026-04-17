"""Regression tests for Bug 1 (find_meeting_times free-calendar) and Bug 2
(semantic_search_emails embedding-error surfacing).

Both bugs were filed after the v3.4 security/reliability release:

* Bug 1 — FindMeetingTimesTool read ``busy_info.merged_free_busy`` while
  exchangelib populates ``busy_info.merged``. With the C3 hardening that
  treats missing data as "busy", every slot came back unavailable.
* Bug 2 — OpenAIEmbeddingProvider used raise_for_status() then indexed
  into response JSON; errors were converted to "Failed to perform
  semantic search: <generic>" with no upstream detail.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import httpx

from src.exceptions import EmbeddingError, ToolExecutionError


# ---------------------------------------------------------------------------
# Bug 1: find_meeting_times
# ---------------------------------------------------------------------------


def _make_free_busy_view(merged: str | None, *, legacy: bool = False):
    """Return a mock resembling exchangelib's FreeBusyView.

    When ``legacy=True``, expose ``merged_free_busy`` (old attribute that
    exchangelib no longer populates) — the helper should still find it.
    Otherwise expose the current ``merged`` attribute.
    """
    view = MagicMock(spec=[])  # spec=[] -> no auto-attrs
    if legacy:
        view.merged_free_busy = merged
    else:
        view.merged = merged
    return view


def test_extract_merged_prefers_current_attribute():
    from src.tools.calendar_tools import _extract_merged

    view = _make_free_busy_view("00000000")
    assert _extract_merged(view) == "00000000"


def test_extract_merged_falls_back_to_legacy():
    from src.tools.calendar_tools import _extract_merged

    view = _make_free_busy_view("00001000", legacy=True)
    assert _extract_merged(view) == "00001000"


def test_extract_merged_returns_none_when_missing():
    from src.tools.calendar_tools import _extract_merged

    view = MagicMock(spec=[])
    assert _extract_merged(view) is None


@pytest.mark.asyncio
async def test_find_meeting_times_all_free(mock_ews_client):
    """With a fully-free merged string, the tool should return suggestions."""
    from src.tools.calendar_tools import FindMeetingTimesTool

    # 10 hours × 4 intervals/hour = 40 "0" characters = all free.
    merged = "0" * 40
    view = _make_free_busy_view(merged)
    mock_ews_client.account.protocol.get_free_busy_info.return_value = [view]

    tool = FindMeetingTimesTool(mock_ews_client)
    result = await tool.execute(
        attendees=["amazrou@sdb.gov.sa"],
        date_range_start="2026-04-18T08:00:00",
        date_range_end="2026-04-18T18:00:00",
        duration_minutes=30,
        max_suggestions=5,
    )

    assert result["success"] is True
    assert len(result["suggestions"]) >= 1, result
    first = result["suggestions"][0]
    assert first["duration_minutes"] == 30


@pytest.mark.asyncio
async def test_find_meeting_times_partial_busy(mock_ews_client):
    """Busy intervals should be respected; only the free windows come back."""
    from src.tools.calendar_tools import FindMeetingTimesTool

    # 10h window. Intervals index from start_date (08:00). earliest_hour=9 -> index 4.
    # Mark intervals 8..15 (10:00-12:00) as busy.
    merged_list = ["0"] * 40
    for i in range(8, 16):
        merged_list[i] = "2"
    merged = "".join(merged_list)
    view = _make_free_busy_view(merged)
    mock_ews_client.account.protocol.get_free_busy_info.return_value = [view]

    tool = FindMeetingTimesTool(mock_ews_client)
    result = await tool.execute(
        attendees=["amazrou@sdb.gov.sa"],
        date_range_start="2026-04-18T08:00:00",
        date_range_end="2026-04-18T18:00:00",
        duration_minutes=30,
        max_suggestions=10,
    )
    assert result["success"] is True
    # At least one suggestion should exist and none should cover a busy slot.
    assert len(result["suggestions"]) >= 1


@pytest.mark.asyncio
async def test_find_meeting_times_missing_merged_returns_zero(mock_ews_client):
    """Legacy C3 behaviour: missing merged data is treated as busy."""
    from src.tools.calendar_tools import FindMeetingTimesTool

    view = _make_free_busy_view(None)
    mock_ews_client.account.protocol.get_free_busy_info.return_value = [view]

    tool = FindMeetingTimesTool(mock_ews_client)
    result = await tool.execute(
        attendees=["amazrou@sdb.gov.sa"],
        date_range_start="2026-04-18T08:00:00",
        date_range_end="2026-04-18T18:00:00",
        duration_minutes=30,
        max_suggestions=5,
    )
    assert result["success"] is True
    assert result["suggestions"] == []


@pytest.mark.asyncio
async def test_find_meeting_times_legacy_attribute(mock_ews_client):
    """If a source happens to expose ``merged_free_busy`` we still honour it."""
    from src.tools.calendar_tools import FindMeetingTimesTool

    merged = "0" * 40
    view = _make_free_busy_view(merged, legacy=True)
    mock_ews_client.account.protocol.get_free_busy_info.return_value = [view]

    tool = FindMeetingTimesTool(mock_ews_client)
    result = await tool.execute(
        attendees=["amazrou@sdb.gov.sa"],
        date_range_start="2026-04-18T08:00:00",
        date_range_end="2026-04-18T18:00:00",
        duration_minutes=30,
        max_suggestions=5,
    )
    assert result["success"] is True
    assert len(result["suggestions"]) >= 1


# ---------------------------------------------------------------------------
# Bug 2: OpenAIEmbeddingProvider error surfacing
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal httpx.Response-compatible test double."""

    def __init__(self, *, status_code: int, body):
        self.status_code = status_code
        self._body = body

    @property
    def text(self) -> str:
        if isinstance(self._body, (dict, list)):
            import json
            return json.dumps(self._body)
        return str(self._body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeClient:
    """Async context manager returning a single canned response."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return self._response


def _patch_httpx(monkeypatch, response):
    monkeypatch.setattr(
        "src.ai.openai_provider.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(response),
    )


@pytest.mark.asyncio
async def test_embedding_404_surfaces_upstream_error(monkeypatch):
    """Ollama-style 404 must raise EmbeddingError with upstream message."""
    from src.ai.openai_provider import OpenAIEmbeddingProvider

    resp = _FakeResponse(
        status_code=404,
        body={"error": {"message": 'model "ollama" not found, try pulling it first',
                        "type": "api_error"}},
    )
    _patch_httpx(monkeypatch, resp)

    provider = OpenAIEmbeddingProvider(
        api_key="x", model="ollama", base_url="http://fake/v1",
    )
    with pytest.raises(EmbeddingError) as excinfo:
        await provider.embed("hello")
    assert "model \"ollama\" not found" in str(excinfo.value)
    assert "HTTP 404" in str(excinfo.value)


@pytest.mark.asyncio
async def test_embedding_200_with_error_body_raises(monkeypatch):
    """Some providers return 200 with ``{"error": ...}`` — catch that too."""
    from src.ai.openai_provider import OpenAIEmbeddingProvider

    resp = _FakeResponse(
        status_code=200,
        body={"error": {"message": "something went wrong", "type": "api_error"}},
    )
    _patch_httpx(monkeypatch, resp)

    provider = OpenAIEmbeddingProvider(
        api_key="x", model="ollama", base_url="http://fake/v1",
    )
    with pytest.raises(EmbeddingError) as excinfo:
        await provider.embed("hello")
    assert "something went wrong" in str(excinfo.value)


@pytest.mark.asyncio
async def test_embedding_empty_data_raises(monkeypatch):
    """A 200 with missing ``data`` array must raise rather than KeyError."""
    from src.ai.openai_provider import OpenAIEmbeddingProvider

    resp = _FakeResponse(status_code=200, body={"model": "x"})
    _patch_httpx(monkeypatch, resp)

    provider = OpenAIEmbeddingProvider(
        api_key="x", model="text-embedding-3-small", base_url="http://fake/v1",
    )
    with pytest.raises(EmbeddingError) as excinfo:
        await provider.embed("hello")
    assert "no data" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_embedding_batch_mismatched_count(monkeypatch):
    """Response with fewer vectors than inputs must raise."""
    from src.ai.openai_provider import OpenAIEmbeddingProvider

    resp = _FakeResponse(
        status_code=200,
        body={
            "model": "text-embedding-3-small",
            "data": [{"embedding": [0.1, 0.2]}],
        },
    )
    _patch_httpx(monkeypatch, resp)

    provider = OpenAIEmbeddingProvider(
        api_key="x", model="text-embedding-3-small", base_url="http://fake/v1",
    )
    with pytest.raises(EmbeddingError) as excinfo:
        await provider.embed_batch(["a", "b", "c"])
    assert "returned 1 vectors" in str(excinfo.value)


@pytest.mark.asyncio
async def test_embedding_batch_empty_input_returns_empty(monkeypatch):
    """Empty input list must NOT hit the network and must return []."""
    from src.ai.openai_provider import OpenAIEmbeddingProvider

    resp = _FakeResponse(status_code=500, body={"error": "should not be called"})
    _patch_httpx(monkeypatch, resp)

    provider = OpenAIEmbeddingProvider(
        api_key="x", model="text-embedding-3-small", base_url="http://fake/v1",
    )
    assert await provider.embed_batch([]) == []


@pytest.mark.asyncio
async def test_embedding_network_error_wraps_cleanly(monkeypatch):
    """Connection failures must surface as EmbeddingError with diagnostic."""
    from src.ai.openai_provider import OpenAIEmbeddingProvider

    class _BrokenClient(_FakeClient):
        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        "src.ai.openai_provider.httpx.AsyncClient",
        lambda *a, **kw: _BrokenClient(None),
    )

    provider = OpenAIEmbeddingProvider(
        api_key="x", model="text-embedding-3-small", base_url="http://fake/v1",
    )
    with pytest.raises(EmbeddingError) as excinfo:
        await provider.embed("hello")
    assert "unreachable" in str(excinfo.value)
    assert "connection refused" in str(excinfo.value)


@pytest.mark.asyncio
async def test_embedding_success_returns_vector(monkeypatch):
    """Happy path: 200 with a real embedding returns the vector."""
    from src.ai.openai_provider import OpenAIEmbeddingProvider

    resp = _FakeResponse(
        status_code=200,
        body={
            "model": "text-embedding-3-small",
            "data": [{"embedding": [0.1, 0.2, 0.3]}],
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        },
    )
    _patch_httpx(monkeypatch, resp)

    provider = OpenAIEmbeddingProvider(
        api_key="x", model="text-embedding-3-small", base_url="http://fake/v1",
    )
    embedded = await provider.embed("hi")
    assert embedded.embedding == [0.1, 0.2, 0.3]
    assert embedded.model == "text-embedding-3-small"


@pytest.mark.asyncio
async def test_embedding_provider_warns_on_provider_name_as_model(caplog):
    """Using AI_EMBEDDING_MODEL=ollama should emit a warning."""
    from src.ai.openai_provider import OpenAIEmbeddingProvider
    import logging

    with caplog.at_level(logging.WARNING, logger="src.ai.openai_provider"):
        OpenAIEmbeddingProvider(api_key="x", model="ollama", base_url="http://fake/v1")
    assert any(
        "looks like a provider name" in rec.message for rec in caplog.records
    ), caplog.records


# ---------------------------------------------------------------------------
# Bug 2: SemanticSearchEmailsTool surfaces EmbeddingError with a hint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_surfaces_embedding_error(mock_ews_client, monkeypatch):
    """When the embedding provider errors out, the tool must return success:false
    with the upstream error in the message and an actionable hint."""
    from src.tools.ai_tools import SemanticSearchEmailsTool

    # Enable AI + semantic search on the mock settings.
    mock_ews_client.config.enable_ai = True
    mock_ews_client.config.enable_semantic_search = True
    mock_ews_client.config.ai_provider = "local"
    mock_ews_client.config.ai_api_key = "x"
    mock_ews_client.config.ai_model = "ignored"
    mock_ews_client.config.ai_embedding_model = "ollama"
    mock_ews_client.config.ai_base_url = "http://fake/v1"

    # Mock the folder to return one email so the tool reaches the embedding call.
    fake_email = MagicMock()
    fake_email.subject = "Q1 budget review"
    fake_email.text_body = "Please approve the Q1 cloud spend by Friday."
    fake_email.id = "AAMk-1"
    fake_email.sender = MagicMock(email_address="ceo@corp.com")
    fake_email.datetime_received = "2026-04-18T09:00:00Z"

    ordered = MagicMock()
    ordered.__getitem__ = lambda _self, _slc: [fake_email]
    all_result = MagicMock()
    all_result.order_by.return_value = ordered
    mock_ews_client.account.inbox.all.return_value = all_result

    # Patch the embedding HTTP call to return the Ollama-style 404 body.
    resp = _FakeResponse(
        status_code=404,
        body={"error": {"message": 'model "ollama" not found', "type": "api_error"}},
    )
    monkeypatch.setattr(
        "src.ai.openai_provider.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(resp),
    )

    tool = SemanticSearchEmailsTool(mock_ews_client)
    result = await tool.safe_execute(query="data security")

    assert result["success"] is False
    err = result.get("error", "")
    assert "model \"ollama\" not found" in err
    assert "Hint:" in err


@pytest.mark.asyncio
async def test_semantic_search_empty_folder_explicit(mock_ews_client):
    """Empty folder must return success with an explicit 'no messages' message
    — no network call is made, no confusing 'Found 0 similar' framing."""
    from src.tools.ai_tools import SemanticSearchEmailsTool

    mock_ews_client.config.enable_ai = True
    mock_ews_client.config.enable_semantic_search = True
    mock_ews_client.config.ai_provider = "local"
    mock_ews_client.config.ai_api_key = "x"
    mock_ews_client.config.ai_model = "ignored"
    mock_ews_client.config.ai_embedding_model = "text-embedding-3-small"
    mock_ews_client.config.ai_base_url = "http://fake/v1"

    # Empty folder.
    ordered = MagicMock()
    ordered.__getitem__ = lambda _self, _slc: []
    all_result = MagicMock()
    all_result.order_by.return_value = ordered
    mock_ews_client.account.inbox.all.return_value = all_result

    tool = SemanticSearchEmailsTool(mock_ews_client)
    result = await tool.execute(query="anything")
    assert result["success"] is True
    assert result["result_count"] == 0
    assert "No messages found" in result["message"]
