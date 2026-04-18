"""Regression tests for Bug 3 — GAL fuzzy-match Mailbox discard.

The adapter previously only handled result shapes ``(mailbox, contact_info)``
and objects with a ``.mailbox`` attribute. The fuzzy-match / GAL-scan
paths return raw ``exchangelib.properties.Mailbox`` objects, which fell
through to a "Unknown result format" warning and were silently discarded.
After the fix, Mailbox objects parse directly.

Also covers:
* ``ErrorNameResolutionNoResults`` is treated as "no match", not a warning.
* Negative-lookup cache: repeated misses return in <50ms.
* Strategies 1 and 2 run in parallel (not serial).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest


def _reset_negative_cache():
    from src.adapters import gal_adapter as ga
    ga._NEGATIVE_CACHE.clear()


def test_parse_resolve_result_handles_mailbox_object():
    """Bug 3 core: raw Mailbox must become a Person, not a WARNING log line."""
    from exchangelib.properties import Mailbox
    from src.adapters.gal_adapter import GALAdapter

    adapter = GALAdapter(ews_client=MagicMock())
    mailbox = Mailbox(email_address="test@example.com", name="Test User")
    person = adapter._parse_resolve_result(mailbox, return_full_data=False)

    assert person is not None
    assert person.primary_email == "test@example.com"
    assert person.name == "Test User"


def test_parse_resolve_result_skips_mailbox_without_email():
    """Defensive: Mailbox with no email_address returns None, not raises."""
    from exchangelib.properties import Mailbox
    from src.adapters.gal_adapter import GALAdapter

    adapter = GALAdapter(ews_client=MagicMock())
    mailbox = Mailbox(name="Nameless")  # no email
    assert adapter._parse_resolve_result(mailbox, return_full_data=False) is None


def test_parse_resolve_result_handles_no_results_error():
    """Exchange "no name resolution" error must not log a warning."""
    from exchangelib.errors import ErrorNameResolutionNoResults
    from src.adapters.gal_adapter import GALAdapter

    adapter = GALAdapter(ews_client=MagicMock())
    err = ErrorNameResolutionNoResults("no match")
    assert adapter._parse_resolve_result(err, return_full_data=False) is None


def test_parse_resolve_result_handles_tuple_still():
    """Regression guard: the old (mailbox, contact) shape still works."""
    from exchangelib.properties import Mailbox
    from src.adapters.gal_adapter import GALAdapter

    adapter = GALAdapter(ews_client=MagicMock())
    mailbox = Mailbox(email_address="tuple@example.com", name="Tuple User")
    person = adapter._parse_resolve_result((mailbox, None), return_full_data=False)
    assert person is not None
    assert person.primary_email == "tuple@example.com"


@pytest.mark.asyncio
async def test_gal_negative_cache_returns_fast(mock_ews_client):
    """Second call for a missing name inside the TTL must be near-instant."""
    from src.adapters.gal_adapter import GALAdapter

    _reset_negative_cache()
    mock_ews_client.account.primary_smtp_address = "me@example.com"

    adapter = GALAdapter(ews_client=mock_ews_client)

    async def _empty(*_args, **_kwargs):
        await asyncio.sleep(0.05)  # simulate some latency
        return []

    # Patch every strategy method to simulate a full miss.
    with patch.object(adapter, "_search_exact", side_effect=_empty), \
         patch.object(adapter, "_search_partial", side_effect=_empty), \
         patch.object(adapter, "_search_domain", side_effect=_empty), \
         patch.object(adapter, "_search_fuzzy", side_effect=_empty):

        t0 = time.monotonic()
        first = await adapter.search("NoSuchPersonAlpha")
        first_duration = time.monotonic() - t0

        t1 = time.monotonic()
        second = await adapter.search("NoSuchPersonAlpha")
        second_duration = time.monotonic() - t1

    assert first == second == []
    # Second call should be dominated by the cache lookup, not the
    # (mocked) strategy walk.
    assert second_duration < 0.05, (
        f"negative cache didn't short-circuit: {second_duration=:.3f}s "
        f"first={first_duration:.3f}s"
    )


@pytest.mark.asyncio
async def test_gal_search_runs_strategies_1_and_2_in_parallel(mock_ews_client):
    """Strategy 1 + 2 must launch concurrently so a 5s strategy doesn't
    serialise into 10s with a second 5s strategy."""
    from src.adapters.gal_adapter import GALAdapter

    _reset_negative_cache()
    mock_ews_client.account.primary_smtp_address = "me@example.com"
    adapter = GALAdapter(ews_client=mock_ews_client)

    started: list[float] = []

    async def _slow(*_args, **_kwargs):
        started.append(time.monotonic())
        await asyncio.sleep(0.1)
        return []

    with patch.object(adapter, "_search_exact", side_effect=_slow), \
         patch.object(adapter, "_search_partial", side_effect=_slow), \
         patch.object(adapter, "_search_domain", side_effect=_slow), \
         patch.object(adapter, "_search_fuzzy", side_effect=_slow):

        t0 = time.monotonic()
        await adapter.search("never-mind")
        total = time.monotonic() - t0

    # With parallel 1+2 (0.1s each, concurrent) then strategy 4 (0.1s),
    # total should be well under the serial ceiling of 3 * 0.1 = 0.3s.
    assert total < 0.28, f"strategies not parallel enough: total={total:.3f}s"
    # And the two fast strategies should start within a few ms of each other.
    assert len(started) >= 2
    assert abs(started[1] - started[0]) < 0.02, started
