"""Live SIT fixtures.

All tests in this directory are gated. By default they are skipped so a
plain ``pytest`` run against the unit suite is unaffected.

How to run
----------

Read-only SIT against your real Exchange mailbox::

    EWS_LIVE_SIT=1 pytest tests/integration/

Add the draft round-trip (creates one draft in your Drafts folder and
deletes it in the same test — never sends mail)::

    EWS_LIVE_SIT=1 EWS_LIVE_SIT_WRITE=1 pytest tests/integration/

The AI tests additionally require ``ENABLE_AI=true`` and a reachable
``AI_BASE_URL``; they are skipped otherwise.

The fixtures here read configuration from process env (the same
``Settings`` object the running server uses), so a ``.env`` file or
exported ``EWS_*`` / ``AI_*`` variables are picked up automatically.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import pytest

from src.auth import AuthHandler
from src.config import Settings
from src.ews_client import EWSClient


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


LIVE_SIT_ENABLED = _flag("EWS_LIVE_SIT")
LIVE_SIT_WRITE_ENABLED = _flag("EWS_LIVE_SIT_WRITE")
LIVE_AI_ENABLED = _flag("ENABLE_AI")


SKIP_REASON_LIVE = "Set EWS_LIVE_SIT=1 to run live SIT against a real Exchange mailbox."
SKIP_REASON_WRITE = "Set EWS_LIVE_SIT_WRITE=1 to run mutating SIT (draft create + delete)."
SKIP_REASON_AI = "Set ENABLE_AI=true (and configure AI_BASE_URL) to run AI-backed SIT."


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    if not LIVE_SIT_ENABLED:
        pytest.skip(SKIP_REASON_LIVE)
    return Settings()


@pytest.fixture(scope="session")
def live_auth(live_settings: Settings) -> AuthHandler:
    return AuthHandler(live_settings)


@pytest.fixture(scope="session")
def live_client(live_settings: Settings, live_auth: AuthHandler) -> EWSClient:
    """Real EWSClient bound to the configured mailbox.

    Skips the entire SIT suite if connection cannot be established —
    a flaky Exchange should not turn into a sea of red failures.
    """
    client = EWSClient(live_settings, live_auth)
    try:
        ok = client.test_connection()
    except Exception as exc:
        pytest.skip(f"EWSClient.test_connection() raised: {exc}")
    if not ok:
        pytest.skip("EWSClient.test_connection() returned False")
    return client


async def fetch_top_inbox_message(client: EWSClient) -> Optional[Dict[str, Any]]:
    """Return the most recent inbox message dict, or ``None`` if the
    inbox is empty. Used by tests that need a real message_id."""
    from src.tools.email_tools import SearchEmailsTool

    tool = SearchEmailsTool(client)
    result = await tool.execute(mode="quick", folder="inbox", max_results=1)
    items = result.get("items") or result.get("emails") or []
    return items[0] if items else None
