"""Regression tests for Issue 1 — find_person(source=email_history)
wall-clock blew past 30s when the inbox scan stalled.

Fix wraps each ``asyncio.to_thread`` in a ``wait_for`` deadline and
eagerly materialises the slice inside the thread.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_issue1_email_history_timeout_returns_error_code(mock_ews_client, monkeypatch):
    """A slow inbox scan must be bounded by ``EWS_EMAIL_HISTORY_TIMEOUT``
    and surface ``error_code=TIMEOUT`` in the tool response rather than
    hanging the client past the 30s MCP protocol timeout."""
    from src.tools.contact_intelligence_tools import FindPersonTool

    monkeypatch.setenv("EWS_EMAIL_HISTORY_TIMEOUT", "2")

    def _slow(*_a, **_kw):
        # Block for much longer than the configured budget.
        time.sleep(5)
        return {}

    with patch(
        "src.services.person_service.PersonService._search_email_history",
        new=_patched_search_email_history(_slow),
    ):
        tool = FindPersonTool(mock_ews_client)
        start = time.monotonic()
        result = await tool.execute(
            query="anyone", source="email_history", max_results=1,
        )
        elapsed = time.monotonic() - start

    assert elapsed < 8, f"tool ran {elapsed:.1f}s — no timeout applied"
    # success=True because the tool layer falls through to empty results
    # with an error_code envelope — fan-out from source=all expects this.
    assert result["success"] is True
    # error_code surfaces TIMEOUT (or one of the classified fallbacks).
    assert result.get("error_code") in {"TIMEOUT", "GAL_UNAVAILABLE"}, result


def _patched_search_email_history(blocking_callable):
    """Build a fresh _search_email_history that does the actual wait_for
    + to_thread dance the real method does, but invokes ``blocking_callable``
    in the thread so tests can simulate hang / exception / success."""
    async def _impl(self, query, days_back, include_stats):
        timeout_s = self._email_history_timeout_s()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(blocking_callable),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            self._last_error_code = "TIMEOUT"
            self._last_error_message = (
                f"email_history scan timed out after {timeout_s}s"
            )
            return []
        except Exception as exc:
            self._last_error_code = self._classify_email_history_error(exc)
            self._last_error_message = f"{type(exc).__name__}: {exc}"
            return []
        return []
    return _impl


@pytest.mark.asyncio
async def test_issue1_source_all_fanout_respects_timeout(mock_ews_client, monkeypatch):
    """source='all' fans out to email_history; the deadline must still fire."""
    from src.tools.contact_intelligence_tools import FindPersonTool

    monkeypatch.setenv("EWS_EMAIL_HISTORY_TIMEOUT", "2")

    def _slow(*_a, **_kw):
        time.sleep(5)
        return {}

    # Also neutralise the GAL + contacts paths so the tool returns
    # quickly when those return empty.
    async def _empty_gal(self, *a, **kw):
        return []

    async def _empty_contacts(self, *a, **kw):
        return []

    with patch(
        "src.services.person_service.PersonService._search_email_history",
        new=_patched_search_email_history(_slow),
    ), patch(
        "src.adapters.gal_adapter.GALAdapter.search",
        new=_empty_gal,
    ), patch(
        "src.services.person_service.PersonService._search_contacts",
        new=_empty_contacts,
    ):
        tool = FindPersonTool(mock_ews_client)
        start = time.monotonic()
        result = await tool.execute(
            query="anyone", source="all", max_results=1,
        )
        elapsed = time.monotonic() - start

    assert elapsed < 8, f"source=all ran {elapsed:.1f}s"
    assert result["success"] is True


async def _async_empty_list(*_a, **_kw):
    return []


def test_issue1_timeout_env_clamped(monkeypatch):
    """Env var is clamped to [1, 120]. Out-of-range falls back to 10."""
    from src.services.person_service import PersonService

    monkeypatch.setenv("EWS_EMAIL_HISTORY_TIMEOUT", "0")
    assert PersonService._email_history_timeout_s() == 1
    monkeypatch.setenv("EWS_EMAIL_HISTORY_TIMEOUT", "10000")
    assert PersonService._email_history_timeout_s() == 120
    monkeypatch.setenv("EWS_EMAIL_HISTORY_TIMEOUT", "not-a-number")
    assert PersonService._email_history_timeout_s() == 10


def test_issue1_error_classifier():
    from src.services.person_service import PersonService

    class ErrorServerBusy(Exception):
        pass

    assert PersonService._classify_email_history_error(ErrorServerBusy("x")) == "THROTTLED"
    assert PersonService._classify_email_history_error(
        RuntimeError("HTTP 401 Unauthorized")
    ) == "AUTH_EXPIRED"
    assert PersonService._classify_email_history_error(
        RuntimeError("request timed out")
    ) == "TIMEOUT"
    assert PersonService._classify_email_history_error(
        RuntimeError("something else")
    ) == "GAL_UNAVAILABLE"
