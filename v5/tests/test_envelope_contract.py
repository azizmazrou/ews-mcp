"""Envelope contract: every list-shaped tool ships EXACTLY the canon
``{items, count, total_available, next_offset}`` page envelope.

v3 grew five overlapping list shapes (results/total/total_results/
total_count/emails) and clients silently learned the wrong keys. This
contract makes shape drift a test failure instead of a client bug.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from conftest import FIXED_OFFSET_TZ, make_settings

from ewsmcp.audit import AuditLog
from ewsmcp.ids import get_aliaser
from ewsmcp.tools import build_registry
from ewsmcp.tools.base import Context, dispatch

TZ = ZoneInfo(FIXED_OFFSET_TZ)
ENVELOPE_KEYS = {"ok", "items", "count", "total_available", "next_offset"}
LIST_TOOLS = ["search_messages", "list_folders", "list_events", "find_people"]


class _Query(list):
    def filter(self, *a, **k):
        return self

    def only(self, *a):
        return self

    def order_by(self, *a):
        return self

    def count(self):
        return len(self)


class _FakeGateway:
    def __init__(self, account):
        self.account = account

    async def call(self, fn):
        return fn(self.account)

    def resolve_folder(self, account, ref, aliaser):
        return account.inbox


def _msg(raw_id):
    return SimpleNamespace(
        id=raw_id, subject="Subj",
        sender=SimpleNamespace(name="A", email_address="a@corp.example"),
        datetime_received=datetime(2026, 6, 10, 9, 0, tzinfo=TZ),
        is_read=True, has_attachments=False, text_body="Body.",
        message_id=f"<{raw_id}@corp.example>",
        conversation_id=SimpleNamespace(id="CONV-1"),
        to_recipients=[], cc_recipients=[], attachments=[],
        importance="Normal", body=None,
    )


def _account():
    account = MagicMock(name="account")
    inbox = _Query([_msg("RAW-1"), _msg("RAW-2")])
    account.inbox = inbox
    account.sent = _Query()
    folder = SimpleNamespace(id="FLD-1", name="Inbox", total_count=2,
                             unread_count=1, children=[])
    account.msg_folder_root = SimpleNamespace(id="FLD-ROOT", name="root",
                                              total_count=0, unread_count=0,
                                              children=[folder])
    event = SimpleNamespace(id="EV-1", subject="Standup",
                            start=datetime(2026, 6, 13, 9, 0, tzinfo=TZ),
                            end=datetime(2026, 6, 13, 9, 30, tzinfo=TZ))
    account.calendar.view.return_value = [event]
    mailbox = SimpleNamespace(name="Contact", email_address="contact@corp.example")
    account.protocol.resolve_names.return_value = [(mailbox, None)]
    account.contacts.all.return_value = []
    return account


def _ctx(tmp_path) -> Context:
    ctx = Context(
        settings=make_settings(),
        gateway=_FakeGateway(_account()),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "alias")),
        audit=AuditLog(str(tmp_path / "audit")),
    )
    build_registry(ctx)
    return ctx


_ARGS = {
    "search_messages": {},
    "list_folders": {},
    "list_events": {},
    "find_people": {"query": "contact"},
}


@pytest.mark.parametrize("name", LIST_TOOLS)
def test_list_tool_ships_canonical_envelope(tmp_path, name):
    ctx = _ctx(tmp_path)
    result = asyncio.run(dispatch(ctx, ctx.registry[name], dict(_ARGS[name])))
    assert result["ok"] is True, result
    assert ENVELOPE_KEYS.issubset(result.keys()), (
        f"{name} is missing envelope keys: {ENVELOPE_KEYS - set(result)}"
    )
    assert isinstance(result["items"], list)
    assert result["count"] == len(result["items"])
    # No sibling spellings — the exact v3 drift this contract kills.
    for legacy in ("results", "total", "total_results", "total_count",
                   "emails", "events", "folders", "people"):
        assert legacy not in result, f"{name} ships legacy key {legacy!r}"


@pytest.mark.parametrize("name", LIST_TOOLS)
def test_registry_declares_list_tools_read_class(tmp_path, name):
    ctx = _ctx(tmp_path)
    assert ctx.registry[name].side_effect_class == "read"
