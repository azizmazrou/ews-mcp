"""End-to-end workflow SIT.

The three workflow tests below verify that the high-level paths work
against a real Exchange mailbox. They are env-gated by ``EWS_LIVE_SIT``
(see ``tests/integration/conftest.py``) so a normal ``pytest`` run still
skips them.

These cover read-mostly workflows. The narrower tool-by-tool SIT lives
in ``test_live_sit.py``; this module exists for "does the whole shape
work" smoke tests.
"""

from __future__ import annotations

import pytest

from .conftest import LIVE_SIT_ENABLED, LIVE_SIT_WRITE_ENABLED, SKIP_REASON_LIVE, SKIP_REASON_WRITE, fetch_top_inbox_message

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not LIVE_SIT_ENABLED, reason=SKIP_REASON_LIVE),
]


@pytest.mark.asyncio
async def test_full_email_workflow(live_client):
    """Search → get details → list folders. Read-only roundtrip."""
    from src.tools.email_tools import GetEmailDetailsTool
    from src.tools.folder_tools import ListFoldersTool

    top = await fetch_top_inbox_message(live_client)
    if top is None:
        pytest.skip("Inbox is empty.")

    msg_id = top.get("message_id") or top.get("id")
    assert msg_id, "search_emails returned an item without a message_id"

    details = await GetEmailDetailsTool(live_client).execute(message_id=msg_id)
    assert details.get("success") is not False
    assert details.get("subject") is not None or details.get("message_id") == msg_id

    folders = await ListFoldersTool(live_client).execute(folder="inbox", max_depth=1)
    assert folders.get("success") is not False
    assert "folders" in folders or "items" in folders


@pytest.mark.asyncio
async def test_full_calendar_workflow(live_client):
    """Get calendar → check availability → find meeting times. Read-only."""
    from datetime import datetime, timedelta, timezone

    from src.tools.calendar_tools import (
        CheckAvailabilityTool,
        FindMeetingTimesTool,
        GetCalendarTool,
    )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = now + timedelta(days=1)

    cal = await GetCalendarTool(live_client).execute(
        start_time=now.isoformat(),
        end_time=end.isoformat(),
    )
    assert cal.get("success") is not False

    avail = await CheckAvailabilityTool(live_client).execute(
        attendees=[live_client.config.ews_email],
        start_time=now.isoformat(),
        end_time=(now + timedelta(hours=1)).isoformat(),
    )
    assert avail.get("success") is not False

    suggestions = await FindMeetingTimesTool(live_client).execute(
        attendees=[live_client.config.ews_email],
        duration_minutes=30,
        start_date=(now + timedelta(days=1)).date().isoformat(),
        end_date=(now + timedelta(days=2)).date().isoformat(),
    )
    assert suggestions.get("success") is not False


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_SIT_WRITE_ENABLED, reason=SKIP_REASON_WRITE)
async def test_full_contact_workflow(live_client):
    """GAL find_person on the user's own address — read-only.

    Gated by ``EWS_LIVE_SIT_WRITE`` only because GAL traffic against a
    corporate directory can be sensitive to log; the call itself never
    mutates anything.
    """
    from src.tools.contact_intelligence_tools import FindPersonTool

    me = live_client.config.ews_email
    result = await FindPersonTool(live_client).execute(query=me.split("@")[0])
    assert result.get("success") is not False
    items = result.get("items") or result.get("matches") or result.get("people") or []
    assert isinstance(items, list)
