"""Regression tests for round-3 follow-ups (CAL-006 JSON, TSK-004 Decimal,
EDS-006 delete API, ATT-010 response field, warmup retry, naive datetime
stamping)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# CAL-006 — CalendarEventDetails serialises cleanly
# ---------------------------------------------------------------------------


def test_calendar_event_details_serialises():
    """CalendarEventDetails must pass through safe_json_dumps without
    TypeError. Previously the plain json.dumps in main.py crashed."""
    from exchangelib.properties import CalendarEventDetails
    from src.utils import safe_json_dumps, make_json_serializable

    details = CalendarEventDetails(
        id="AAMk-event",
        subject="Budget review",
        location="Room A",
        is_meeting=True,
        is_recurring=False,
        is_exception=False,
        is_reminder_set=True,
        is_private=False,
    )

    payload = {"event": details}
    serialised = safe_json_dumps(payload)
    parsed = json.loads(serialised)
    assert parsed["event"]["subject"] == "Budget review"
    assert parsed["event"]["location"] == "Room A"
    assert parsed["event"]["is_meeting"] is True

    # make_json_serializable handles it directly too.
    as_dict = make_json_serializable(details)
    assert as_dict["subject"] == "Budget review"


def test_calendar_event_details_in_nested_list():
    """Lists of events inside availability results must serialise."""
    from exchangelib.properties import CalendarEventDetails
    from src.utils import safe_json_dumps

    events = [
        CalendarEventDetails(id=None, subject="A", location=None,
                             is_meeting=False, is_recurring=False,
                             is_exception=False, is_reminder_set=False,
                             is_private=False),
        CalendarEventDetails(id="AAMk-2", subject="B", location="X",
                             is_meeting=True, is_recurring=False,
                             is_exception=False, is_reminder_set=False,
                             is_private=False),
    ]
    out = safe_json_dumps({"events": events})
    parsed = json.loads(out)
    assert [e["subject"] for e in parsed["events"]] == ["A", "B"]


# ---------------------------------------------------------------------------
# TSK-004 — Decimal serialises cleanly
# ---------------------------------------------------------------------------


def test_decimal_serialises_as_float():
    from src.utils import safe_json_dumps, make_json_serializable

    assert make_json_serializable(Decimal("0.25")) == 0.25
    parsed = json.loads(safe_json_dumps({"percent_complete": Decimal("0.75")}))
    assert parsed["percent_complete"] == 0.75


def test_decimal_in_tasks_response_survives_round_trip():
    from src.utils import safe_json_dumps

    task = {
        "item_id": "AAMk-1",
        "subject": "Write tests",
        "percent_complete": Decimal("25.5"),
        "status": "InProgress",
    }
    out = safe_json_dumps({"tasks": [task], "count": 1})
    parsed = json.loads(out)
    assert parsed["tasks"][0]["percent_complete"] == 25.5


# ---------------------------------------------------------------------------
# Naive datetime serialisation stamps configured TZ
# ---------------------------------------------------------------------------


def test_naive_datetime_stamped_with_configured_tz(monkeypatch):
    """Naive datetimes emitted by exchangelib get a tz stamp so the ISO
    string isn't ambiguous."""
    from src.utils import make_json_serializable

    monkeypatch.setenv("TIMEZONE", "Asia/Riyadh")
    naive = datetime(2026, 4, 19, 10, 0, 0)
    out = make_json_serializable(naive)
    # Asia/Riyadh is UTC+3 year-round.
    assert out.endswith("+03:00"), out
    assert out.startswith("2026-04-19T10:00:00")


def test_aware_datetime_passes_through_unchanged():
    """An already-aware datetime is serialised as-is."""
    from datetime import timezone, timedelta
    from src.utils import make_json_serializable

    aware = datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    out = make_json_serializable(aware)
    assert out == "2026-04-19T10:00:00+05:00"


# ---------------------------------------------------------------------------
# EDS-006 — delete_email hard_delete uses correct exchangelib API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eds006_hard_delete_calls_bare_delete(mock_ews_client):
    """``Item.delete()`` in exchangelib 5.x already does HARD_DELETE
    internally and accepts no kwargs. Passing ``disposal_type=`` or
    ``delete_type=`` raises TypeError — the live NAS surfaced this as a
    500 on every delete_email(permanent=True). The fix is to call the
    bare method."""
    from src.tools.email_tools import DeleteEmailTool

    item = MagicMock()
    with patch(
        "src.tools.email_tools.find_message_for_account", return_value=item
    ):
        tool = DeleteEmailTool(mock_ews_client)
        await tool.execute(message_id="AAMk-1", hard_delete=True)

    # Bare call — no disposal_type / delete_type, no positional args.
    item.delete.assert_called_once_with()


def test_eds006_real_exchangelib_delete_signature_has_no_disposal_type():
    """Regression guard against the original sandbox-vs-prod divergence:
    ``MagicMock().delete(disposal_type=X)`` silently records the kwarg, so
    a unit test using a plain mock would have green-lit the bug. Pin the
    real exchangelib signature so we'd notice if it ever grows back."""
    import inspect
    from exchangelib.items import Item
    sig = inspect.signature(Item.delete)
    params = set(sig.parameters)
    assert "disposal_type" not in params, (
        "exchangelib.Item.delete() now accepts disposal_type — adjust the "
        "production code in src/tools/email_tools.py and revisit this guard."
    )
    assert "delete_type" not in params


# ---------------------------------------------------------------------------
# ATT-010 — file_path always present in download_attachment response
# ---------------------------------------------------------------------------


def _att():
    att = MagicMock()
    att.attachment_id = {"id": "AT-1"}
    att.name = "report.pdf"
    att.content = b"PDF bytes"
    att.content_type = "application/pdf"
    return att


@pytest.mark.asyncio
async def test_att010_base64_response_has_file_path_key(mock_ews_client):
    """Even in base64 mode, file_path is present (as null) so callers
    don't have to branch on return_as."""
    from src.tools.attachment_tools import DownloadAttachmentTool

    msg = MagicMock()
    msg.attachments = [_att()]

    tool = DownloadAttachmentTool(mock_ews_client)
    with patch(
        "src.tools.attachment_tools.find_message_for_account",
        return_value=msg,
    ):
        result = await tool.execute(
            message_id="AAMk-msg", attachment_id="AT-1"
        )
    assert result["success"] is True
    # file_path is present (null because no file was written).
    assert "file_path" in result
    assert result["file_path"] is None
    # saved_path alias present too.
    assert "saved_path" in result


@pytest.mark.asyncio
async def test_att010_file_mode_response_has_both_keys(mock_ews_client, tmp_path, monkeypatch):
    """File-path mode: both file_path and saved_path point at the resolved
    location."""
    from src.tools.attachment_tools import DownloadAttachmentTool

    monkeypatch.setenv("EWS_DOWNLOAD_DIR", str(tmp_path))
    import importlib
    from src.tools import attachment_tools as at
    importlib.reload(at)

    msg = MagicMock()
    msg.attachments = [_att()]

    tool = at.DownloadAttachmentTool(mock_ews_client)
    with patch(
        "src.tools.attachment_tools.find_message_for_account",
        return_value=msg,
    ):
        result = await tool.execute(
            message_id="AAMk-msg",
            attachment_id="AT-1",
            return_as="file_path",
            save_path="report.pdf",
        )
    assert result["success"] is True
    assert result["file_path"]
    assert result["saved_path"]
    assert result["file_path"] == result["saved_path"]


# ---------------------------------------------------------------------------
# Warmup retry-with-backoff
# ---------------------------------------------------------------------------


def test_is_transient_error_detects_connection_issues():
    from src.main import _is_transient_error

    class RemoteDisconnected(Exception):
        pass

    assert _is_transient_error([("inbox", RemoteDisconnected("x"))]) is True
    # Message-only match (wrapped exchangelib errors).
    assert _is_transient_error([("inbox", RuntimeError("Connection aborted"))]) is True
    assert _is_transient_error([("inbox", RuntimeError("timed out"))]) is True


def test_is_transient_error_skips_non_retryable():
    from src.main import _is_transient_error

    class SchemaError(Exception):
        pass

    assert _is_transient_error([("inbox", SchemaError("attribute missing"))]) is False
    assert _is_transient_error([("inbox", ValueError("bad input"))]) is False
    assert _is_transient_error([]) is False
