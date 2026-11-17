"""Writes tool pack tests — driven through dispatch() so every dispatcher
gate (kill-switch, tier, two-phase confirm, alias resolution) is exercised
through the full gate chain.

exchangelib classes that handlers *construct* (Message, CalendarItem) are
monkeypatched with capture stubs because the real classes refuse non-Account
accounts at __init__; everything fetched from the fake account is a plain
mock. Constants (SEND_TO_NONE, …) are the REAL exchangelib 5.0.3 objects so
drift would fail here first.
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from exchangelib import (
    BASIC,
    DELEGATE,
    UTC,
    Account,
    CalendarItem,
    Configuration,
    Contact,
    Credentials,
    EWSDate,
    EWSDateTime,
    EWSTimeZone,
    FileAttachment,
    HTMLBody,
    Message,
    OofSettings,
    Task,
)
from exchangelib.attachments import AttachmentId
from exchangelib.errors import ErrorItemNotFound, ErrorServerBusy
from exchangelib.items import (
    SEND_TO_ALL_AND_SAVE_COPY,
    SEND_TO_CHANGED_AND_SAVE_COPY,
    SEND_TO_NONE,
    MeetingCancellation,
    MeetingRequest,
)
from exchangelib.protocol import CachingProtocol
from exchangelib.recurrence import (
    AbsoluteMonthlyPattern,
    DailyPattern,
    EndDatePattern,
    NoEndPattern,
    NumberedPattern,
    WeeklyPattern,
)
from exchangelib.util import xml_to_str
from exchangelib.version import EXCHANGE_2016, Version

from conftest import FIXED_OFFSET_TZ, TIME_MARGIN, make_settings
from ewsmcp.audit import AuditLog
from ewsmcp.confirm import reset_consumed_tokens
from ewsmcp.dates import parse_when
from ewsmcp.htmlsafe import MAX_INSPECT_HTML_CHARS
from ewsmcp.ids import get_aliaser
from ewsmcp.tools import writes
from ewsmcp.tools.base import Context, dispatch, mint_token, reset_send_rate_window

SPEC = {s.name: s for s in writes.TOOLS}
DRAFTS_ID = "FOLDER-DRAFTS"
LOCAL_TZ = ZoneInfo(FIXED_OFFSET_TZ)


@pytest.fixture(autouse=True)
def _fresh_idempotency():
    writes.reset_idempotency_store()
    writes.reset_draft_locks()
    yield
    writes.reset_idempotency_store()
    writes.reset_draft_locks()


class FakeGateway:
    """async call → fn(account), executed inline."""

    def __init__(self, account):
        self.account = account
        self.folders: Dict[str, Any] = {}

    async def call(self, fn):
        return fn(self.account)

    def resolve_folder(self, account, ref, aliaser):
        return self.folders[ref]


def make_account():
    account = SimpleNamespace()
    account.drafts = SimpleNamespace(id=DRAFTS_ID, name="Drafts")
    account.calendar = SimpleNamespace(id="FOLDER-CAL")
    account._by_id = {}
    account.fetch_projections = []
    def fetch(pairs, only_fields=None):
        account.fetch_projections.append(only_fields)
        return [account._by_id[i] for i, _ in pairs]
    account.fetch = fetch
    return account


def make_ctx(tmp_path, account, **overrides) -> Context:
    overrides.setdefault("ews_tz", FIXED_OFFSET_TZ)
    return Context(
        settings=make_settings(**overrides),
        gateway=FakeGateway(account),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "aliases")),
        audit=AuditLog(str(tmp_path / "audit")),
    )


def call(ctx: Context, name: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return asyncio.run(dispatch(ctx, SPEC[name], dict(kwargs)))


def full_ctx(tmp_path, account, **overrides) -> Context:
    return make_ctx(tmp_path, account, send_enabled=True,
                    ews_capability_tier="full", **overrides)


class FakeDraftMessage:
    """Stand-in for exchangelib.Message in mode=new (real one rejects fake accounts)."""

    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.id = None
        self.changekey = None
        self.save_calls = []
        FakeDraftMessage.last = self

    def save(self, **kwargs):
        self.save_calls.append(kwargs)
        self.id = "RAW-NEW-DRAFT"
        self.changekey = "CK-NEW"
        return self

    def attach(self, attachment):
        if attachment.name == "fails.txt":
            raise RuntimeError("ErrorMessageSizeExceeded")
        self.attachments = getattr(self, "attachments", []) + [attachment]
        self.changekey = "CK-AFTER-ATTACH"


class FakeCalendarItem:
    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.id = None
        self.save_calls = []
        FakeCalendarItem.last = self

    def save(self, **kwargs):
        self.save_calls.append(kwargs)
        self.id = "RAW-NEW-EVENT"
        return self


def make_sendable_draft():
    draft = MagicMock()
    draft.parent_folder_id = SimpleNamespace(id=DRAFTS_ID)
    draft.message_id = "<imid-1@corp.example>"
    draft.send.return_value = None
    return draft


# --- pack surface -------------------------------------------------------------


def test_pack_surface_classes_and_confirm_declarations():
    assert len(writes.TOOLS) == 13
    classes = {s.name: s.side_effect_class for s in writes.TOOLS}
    assert classes == {
        "create_draft": "write", "update_draft": "write", "delete_draft": "write",
        "add_draft_attachment": "write",
        "update_messages": "write", "move_messages": "write",
        "create_event": "write", "update_event": "write",
        "send_draft": "send", "respond_to_event": "send", "set_oof": "send",
        "cancel_event": "destructive", "delete_messages": "destructive",
    }
    for name in ("create_draft", "update_draft", "delete_draft", "add_draft_attachment",
                 "update_messages", "move_messages"):
        assert SPEC[name].confirm is False
    for name in ("respond_to_event", "cancel_event", "set_oof"):
        assert SPEC[name].confirm is True
    # send_draft: confirm-gated with a CONTENT-BOUND preview hook; the gate
    # is skipped only for an idempotent replay of an already-sent draft.
    assert SPEC["send_draft"].confirm({"draft_id": "d1"}) is True
    assert SPEC["send_draft"].preview is not None
    assert SPEC["cancel_event"].preview is not None
    assert SPEC["create_event"].confirm({"send_invitations": True}) is True
    assert SPEC["create_event"].confirm({}) is False
    assert SPEC["update_event"].confirm({"notify_attendees": True}) is True
    assert SPEC["delete_messages"].confirm({"disposition": "permanent"}) is True
    assert SPEC["delete_messages"].confirm({"disposition": "trash"}) is False


# --- create_draft ---------------------------------------------------------------


def test_create_draft_new_saves_into_drafts(tmp_path, monkeypatch):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    res = call(ctx, "create_draft",
               {"to": ["a@x.com"], "subject": "Hi", "body": "Hello\n\nWorld"})
    assert res["ok"] is True
    assert res["draft_id"].startswith("d")
    assert res["folder"] == "f:drafts"
    assert "NOT sent" in res["note"]
    msg = FakeDraftMessage.last
    assert msg.kwargs["folder"] is account.drafts
    assert msg.kwargs["to_recipients"] == ["a@x.com"]
    assert "<p>Hello</p>" in str(msg.kwargs["body"])
    assert msg.save_calls == [{}]  # SAVE_ONLY — no send kwargs
    assert ctx.aliaser.resolve(res["draft_id"]) == "RAW-NEW-DRAFT"
    assert res["preview"]["to"] == ["a@x.com"]
    assert res["preview"]["body_snippet"].startswith("Hello")


def test_create_draft_reply_calls_create_reply_and_saves_to_drafts(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    original = MagicMock()
    original.subject = "Budget"
    reply_item = MagicMock()
    reply_item.to_recipients = [SimpleNamespace(email_address="boss@corp.example")]
    reply_item.cc_recipients = None
    reply_item.save.return_value = SimpleNamespace(id="RAW-REPLY-DRAFT", changekey="CK2")
    original.create_reply.return_value = reply_item
    account._by_id["RAW-ORIG"] = original
    alias = ctx.aliaser.alias_for("RAW-ORIG", "m")

    res = call(ctx, "create_draft", {"mode": "reply", "reply_to": alias, "body": "Thanks"})
    assert res["ok"] is True
    args, _kwargs = original.create_reply.call_args
    assert args[0] == "Re: Budget"
    assert "<p>Thanks</p>" in str(args[1])
    reply_item.save.assert_called_once_with(account.drafts)
    assert ctx.aliaser.resolve(res["draft_id"]) == "RAW-REPLY-DRAFT"
    assert res["preview"]["to"] == ["boss@corp.example"]
    assert res["preview"]["subject"] == "Re: Budget"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_create_draft_attaches_files(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path, make_account())
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    res = call(ctx, "create_draft", {
        "to": ["a@x.com"], "body": "Hi",
        "attachments": [{"filename": "/mnt/data/list.pdf", "content_base64": _b64(b"%PDF")}],
    })
    assert res["ok"] is True
    assert res["attachments"] == [{"name": "list.pdf", "size": 4}]
    assert "attachments_failed" not in res
    attached = FakeDraftMessage.last.attachments[0]
    assert attached.content == b"%PDF"
    assert attached.content_type == "application/pdf"


def test_create_draft_keeps_the_draft_when_an_attachment_fails(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path, make_account())
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    res = call(ctx, "create_draft", {
        "to": ["a@x.com"], "body": "Hi",
        "attachments": [{"filename": "ok.txt", "content_base64": _b64(b"a")},
                        {"filename": "fails.txt", "content_base64": _b64(b"b")}],
    })
    assert res["ok"] is True and res["draft_id"].startswith("d")
    assert res["attachments"] == [{"name": "ok.txt", "size": 1}]
    assert res["attachments_failed"][0]["name"] == "fails.txt"
    assert "ErrorMessageSizeExceeded" in res["attachments_failed"][0]["error"]


@pytest.mark.parametrize("attachment, message", [
    ({"filename": "a.txt", "content_base64": "not base64!"}, "not valid base64"),
    ({"filename": "a.txt", "content_base64": "{{file:F-123456}}"}, "not valid base64"),
    ({"filename": "a.txt", "content_base64": ""}, "empty"),
    ({"filename": "  ", "content_base64": "YQ=="}, "needs a filename"),
])
def test_create_draft_rejects_bad_attachment_before_saving(tmp_path, monkeypatch,
                                                           attachment, message):
    ctx = make_ctx(tmp_path, make_account())
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    FakeDraftMessage.last = None
    res = call(ctx, "create_draft", {"to": ["a@x.com"], "body": "Hi",
                                     "attachments": [attachment]})
    assert res["ok"] is False and message in res["error"]["message"]
    assert FakeDraftMessage.last is None


def test_create_draft_rejects_too_much_data(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path, make_account(), ews_max_attachment_mb=1)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    FakeDraftMessage.last = None
    chunk = b"x" * (600 * 1024)
    res = call(ctx, "create_draft", {
        "to": ["a@x.com"], "body": "Hi",
        "attachments": [{"filename": "a.bin", "content_base64": _b64(chunk)},
                        {"filename": "b.bin", "content_base64": _b64(chunk)}],
    })
    assert res["ok"] is False and "exceed" in res["error"]["message"]
    assert FakeDraftMessage.last is None


def test_create_draft_reply_attaches_to_the_fetched_draft(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    original = MagicMock()
    original.create_reply.return_value.save.return_value = SimpleNamespace(
        id="RAW-REPLY-DRAFT", changekey="CK1")
    saved_draft = MagicMock()
    account._by_id.update({"RAW-ORIG": original, "RAW-REPLY-DRAFT": saved_draft})
    alias = ctx.aliaser.alias_for("RAW-ORIG", "m")
    res = call(ctx, "create_draft", {
        "mode": "reply", "reply_to": alias, "body": "Thanks",
        "attachments": [{"filename": "a.txt", "content_base64": _b64(b"a")}],
    })
    assert res["ok"] is True
    assert saved_draft.attach.call_count == 1
    assert saved_draft.attach.call_args.args[0].name == "a.txt"


def test_add_draft_attachment(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    draft = make_sendable_draft()
    draft.changekey = "CK2"
    draft.attachments = [SimpleNamespace(name="old.pdf", size=9)]
    account._by_id["RAW-D"] = draft
    alias = ctx.aliaser.alias_for("RAW-D", "d")
    res = call(ctx, "add_draft_attachment", {
        "draft_id": alias, "filename": "a.txt", "content_base64": _b64(b"abc"),
        "content_type": "text/plain",
    })
    assert res["ok"] is True
    assert res["added"] == {"name": "a.txt", "size": 3}
    assert res["attachments"] == [{"name": "old.pdf", "size": 9}]  # the fake's attach() is a no-op
    attached = draft.attach.call_args.args[0]
    assert (attached.content, attached.content_type) == (b"abc", "text/plain")


def test_add_draft_attachment_refuses_a_non_draft(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    item = make_sendable_draft()
    item.parent_folder_id = SimpleNamespace(id="SOME-OTHER-FOLDER")
    account._by_id["RAW-M"] = item
    res = call(ctx, "add_draft_attachment", {
        "draft_id": ctx.aliaser.alias_for("RAW-M", "m"),
        "filename": "a.txt", "content_base64": _b64(b"a"),
    })
    assert res["ok"] is False and "not a draft" in res["error"]["message"]
    item.attach.assert_not_called()


def test_send_draft_preview_lists_attachments_and_token_binds_them(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    draft = make_sendable_draft()
    draft.attachments = [SimpleNamespace(name="list.xlsx", size=11)]
    account._by_id["RAW-D"] = draft
    alias = ctx.aliaser.alias_for("RAW-D", "d")
    p1 = call(ctx, "send_draft", {"draft_id": alias})
    assert p1["preview"]["attachment_count"] == 1
    assert p1["preview"]["attachments"] == [{"name": "list.xlsx", "size": 11}]
    # Same count, other file: the preview no longer holds.
    draft.attachments = [SimpleNamespace(name="other.xlsx", size=11)]
    p2 = call(ctx, "send_draft", {"draft_id": alias, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is False
    draft.send.assert_not_called()


def test_create_draft_validation(tmp_path):
    ctx = make_ctx(tmp_path, make_account())
    res = call(ctx, "create_draft", {"mode": "reply", "body": "x"})
    assert res["ok"] is False and res["error"]["code"] == "validation"
    res = call(ctx, "create_draft", {"mode": "forward", "reply_to": "RAW-Z", "body": "x"})
    assert res["error"]["code"] == "validation"  # forward without `to`


# --- update_draft / delete_draft -----------------------------------------------


def test_update_draft_saves_with_recipient_field_names(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    draft = MagicMock()
    draft.parent_folder_id = SimpleNamespace(id=DRAFTS_ID)
    draft.cc_recipients = None
    account._by_id["RAW-D1"] = draft
    res = call(ctx, "update_draft",
               {"draft_id": "RAW-D1", "to": ["a@b.c"], "subject": "S2"})
    assert res["ok"] is True
    assert draft.to_recipients == ["a@b.c"]
    assert draft.subject == "S2"
    draft.save.assert_called_once_with(update_fields=["to_recipients", "subject"])
    assert res["preview"]["to"] == ["a@b.c"]
    assert res["preview"]["subject"] == "S2"


def test_update_draft_refuses_items_outside_drafts(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    stranger = MagicMock()
    stranger.parent_folder_id = SimpleNamespace(id="FOLDER-INBOX")
    account._by_id["RAW-M8"] = stranger
    res = call(ctx, "update_draft", {"draft_id": "RAW-M8", "subject": "Rewritten"})
    assert res["ok"] is False
    assert res["error"]["code"] == "validation"
    assert "not a draft" in res["error"]["message"]
    stranger.save.assert_not_called()


def test_delete_draft_refuses_items_outside_drafts(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    stranger = MagicMock()
    stranger.parent_folder_id = SimpleNamespace(id="FOLDER-INBOX")
    account._by_id["RAW-M9"] = stranger
    res = call(ctx, "delete_draft", {"draft_id": "RAW-M9"})
    assert res["error"]["code"] == "validation"
    assert "draft" in res["error"]["message"]
    stranger.move_to_trash.assert_not_called()

    real = MagicMock()
    real.parent_folder_id = SimpleNamespace(id=DRAFTS_ID)
    account._by_id["RAW-D2"] = real
    res = call(ctx, "delete_draft", {"draft_id": "RAW-D2"})
    assert res["ok"] is True and res["deleted"] is True
    real.move_to_trash.assert_called_once_with()


# --- update_messages / move_messages -------------------------------------------


def test_update_messages_isolates_per_item_failures(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    good1, bad, good2 = MagicMock(), MagicMock(), MagicMock()
    bad.save.side_effect = RuntimeError("boom")
    account._by_id.update({"RAW-1": good1, "RAW-2": bad, "RAW-3": good2})
    res = call(ctx, "update_messages",
               {"ids": ["RAW-1", "RAW-2", "RAW-3"], "set_read": True})
    assert res["ok"] is True
    assert res["updated"] == 2
    assert len(res["failed"]) == 1 and "boom" in res["failed"][0]["error"]
    assert good1.is_read is True
    good1.save.assert_called_once_with(update_fields=["is_read"])
    good2.save.assert_called_once_with(update_fields=["is_read"])


def test_update_messages_rejects_set_flag(tmp_path):
    ctx = make_ctx(tmp_path, make_account())
    res = call(ctx, "update_messages", {"ids": ["RAW-1"], "set_flag": "flagged"})
    assert res["error"]["code"] == "validation"
    assert "set_flag" in res["error"]["message"]


def test_move_messages_rebinds_alias_to_new_raw_id(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)

    class MovableItem:
        def __init__(self, raw):
            self.id = raw
            self.moved_to = None

        def move(self, folder):  # 5.0.3 contract: returns None, id updated in place
            self.moved_to = folder
            self.id = "NEW-" + self.id

    item1, item2 = MovableItem("RAW-A"), MovableItem("RAW-B")
    account._by_id.update({"RAW-A": item1, "RAW-B": item2})
    target = SimpleNamespace(id="FOLDER-ARCHIVE")
    ctx.gateway.folders["f:archive"] = target
    a1 = ctx.aliaser.alias_for("RAW-A", "m")
    a2 = ctx.aliaser.alias_for("RAW-B", "m")

    res = call(ctx, "move_messages", {"ids": [a1, a2], "to_folder": "f:archive"})
    assert res["ok"] is True
    assert res["moved"] == 2 and res["failed"] == []
    assert [it["id"] for it in res["items"]] == [a1, a2]  # SAME aliases survive
    assert item1.moved_to is target
    assert ctx.aliaser.resolve(a1) == "NEW-RAW-A"
    assert ctx.aliaser.resolve(a2) == "NEW-RAW-B"


# --- send_draft: two-phase, idempotency, kill-switch, tier ----------------------


def test_send_draft_two_phase_through_dispatcher(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    draft = make_sendable_draft()
    account._by_id["RAW-D5"] = draft

    p1 = call(ctx, "send_draft", {"draft_id": "RAW-D5"})
    assert p1["requires_confirmation"] is True and p1["confirm_token"]
    draft.send.assert_not_called()  # phase 1 executes NOTHING

    p2 = call(ctx, "send_draft",
              {"draft_id": "RAW-D5", "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is True and p2["sent"] is True
    assert p2["internet_message_id"] == "<imid-1@corp.example>"
    draft.send.assert_called_once_with(save_copy=True)


def test_send_draft_idempotency_replay_and_key_reuse(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    draft = make_sendable_draft()
    account._by_id["RAW-D5"] = draft
    kwargs = {"draft_id": "RAW-D5", "idempotency_key": "K1"}

    token = call(ctx, "send_draft", kwargs)["confirm_token"]
    sent = call(ctx, "send_draft", {**kwargs, "confirm_token": token})
    assert sent["sent"] is True and "replayed" not in sent

    replay = call(ctx, "send_draft", {**kwargs, "confirm_token": token})
    assert replay["replayed"] is True and replay["sent"] is True
    assert draft.send.call_count == 1  # no second send

    other = make_sendable_draft()
    account._by_id["RAW-D6"] = other
    kwargs2 = {"draft_id": "RAW-D6", "idempotency_key": "K1"}
    token2 = call(ctx, "send_draft", kwargs2)["confirm_token"]
    res = call(ctx, "send_draft", {**kwargs2, "confirm_token": token2})
    assert res["error"]["code"] == "validation"  # same key, different draft
    other.send.assert_not_called()


def test_send_draft_kill_switch_refuses_at_phase_one(tmp_path):
    ctx = make_ctx(tmp_path, make_account(),
                   send_enabled=False, ews_capability_tier="full")
    res = call(ctx, "send_draft", {"draft_id": "RAW-D5"})
    assert res["ok"] is False and res["error"]["code"] == "kill_switch"
    assert "confirm_token" not in res  # not even a preview token is minted


def test_send_draft_tier_blocked_on_draft_tier(tmp_path):
    ctx = make_ctx(tmp_path, make_account(),
                   send_enabled=True, ews_capability_tier="draft")
    res = call(ctx, "send_draft", {"draft_id": "RAW-D5"})
    assert res["error"]["code"] == "tier_blocked"


# --- send_draft: content binding (the v3.5 port; TOCTOU + resolved guards) ------


def make_content_draft(subject="Q3 numbers", to=("board@corp.example",),
                       body="please review before Sunday"):
    draft = MagicMock()
    draft.parent_folder_id = SimpleNamespace(id=DRAFTS_ID)
    draft.message_id = "<imid-2@corp.example>"
    draft.subject = subject
    draft.to_recipients = [SimpleNamespace(email_address=a) for a in to]
    draft.cc_recipients = []
    draft.bcc_recipients = []
    draft.text_body = body
    draft.attachments = []
    draft.send.return_value = None
    return draft


def test_send_draft_phase1_previews_the_drafts_real_content(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    account._by_id["RAW-D7"] = make_content_draft(
        to=("board@corp.example", "cfo@external.example"))
    p1 = call(ctx, "send_draft", {"draft_id": "RAW-D7"})
    assert p1["requires_confirmation"] is True
    assert p1["preview"]["subject"] == "Q3 numbers"
    assert p1["preview"]["to"] == ["board@corp.example", "cfo@external.example"]
    assert p1["preview"]["body_snippet"].startswith("please review")
    assert p1["preview"]["attachment_count"] == 0
    assert any("cfo@external.example" in w for w in p1["warnings"])


def test_send_draft_toctou_edit_invalidates_token(tmp_path):
    """update_draft between preview and confirm must kill the token —
    the exact redirect attack the old draft_id-only hash allowed."""
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    draft = make_content_draft()
    account._by_id["RAW-D8"] = draft
    token = call(ctx, "send_draft", {"draft_id": "RAW-D8"})["confirm_token"]
    draft.to_recipients = [SimpleNamespace(email_address="attacker@evil.example")]
    p2 = call(ctx, "send_draft", {"draft_id": "RAW-D8", "confirm_token": token})
    assert p2["error"]["code"] == "confirm_invalid"
    assert "stale" in p2["error"]["message"]
    draft.send.assert_not_called()


def test_send_draft_body_edit_also_invalidates_token(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    draft = make_content_draft()
    account._by_id["RAW-D9"] = draft
    token = call(ctx, "send_draft", {"draft_id": "RAW-D9"})["confirm_token"]
    draft.text_body = "wire the funds to the new account"
    p2 = call(ctx, "send_draft", {"draft_id": "RAW-D9", "confirm_token": token})
    assert p2["error"]["code"] == "confirm_invalid"
    draft.send.assert_not_called()


def test_send_draft_guard_blocks_denylisted_draft_recipient(tmp_path):
    """kwargs carry no recipients — the guard must fire on the DRAFT's."""
    account = make_account()
    ctx = full_ctx(tmp_path, account,
                   ews_recipient_denylist="*@competitor.example")
    account._by_id["RAW-DA"] = make_content_draft(to=("ceo@competitor.example",))
    p1 = call(ctx, "send_draft", {"draft_id": "RAW-DA"})
    assert p1["error"]["code"] == "recipient_blocked"
    assert "confirm_token" not in p1


def test_send_draft_allowlist_enforced_on_resolved_recipients(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account,
                   ews_recipient_allowlist="*@corp.example")
    account._by_id["RAW-DB"] = make_content_draft(to=("out@other.example",))
    p1 = call(ctx, "send_draft", {"draft_id": "RAW-DB"})
    assert p1["error"]["code"] == "recipient_blocked"


def test_send_draft_token_single_use_without_idempotency_key(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    draft = make_content_draft()
    account._by_id["RAW-DC"] = draft
    token = call(ctx, "send_draft", {"draft_id": "RAW-DC"})["confirm_token"]
    first = call(ctx, "send_draft", {"draft_id": "RAW-DC", "confirm_token": token})
    assert first["sent"] is True
    again = call(ctx, "send_draft", {"draft_id": "RAW-DC", "confirm_token": token})
    assert again["error"]["code"] == "confirm_invalid"
    assert "consumed" in again["error"]["message"]
    assert draft.send.call_count == 1


def test_idempotency_store_ttl_and_cap(monkeypatch):
    writes.reset_idempotency_store()
    writes._idempotency_put("old", {"draft_id": "d", "result": {}, "ts": 0.0})
    assert writes._idempotency_get("old") is None  # expired entries pruned
    monkeypatch.setattr(writes, "_IDEMPOTENCY_MAX", 2)
    now = __import__("time").time()
    writes._idempotency_put("k1", {"draft_id": "a", "result": {}, "ts": now - 30})
    writes._idempotency_put("k2", {"draft_id": "b", "result": {}, "ts": now - 20})
    writes._idempotency_put("k3", {"draft_id": "c", "result": {}, "ts": now - 10})
    assert writes._idempotency_get("k1") is None  # oldest evicted at the cap
    assert writes._idempotency_get("k2") is not None
    assert writes._idempotency_get("k3") is not None


def test_create_draft_recipients_are_guarded(tmp_path, monkeypatch):
    """Write-class argument-borne recipients hit the guard (old dead code)."""
    account = make_account()
    ctx = make_ctx(tmp_path, account,
                   ews_recipient_denylist="*@competitor.example")
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    res = call(ctx, "create_draft",
               {"to": ["ceo@competitor.example"], "body": "hi"})
    assert res["error"]["code"] == "recipient_blocked"


# --- create_event / update_event ------------------------------------------------


def test_create_event_default_saves_without_invites_no_confirm(tmp_path, monkeypatch):
    account = make_account()
    ctx = make_ctx(tmp_path, account)  # draft tier suffices for write class
    monkeypatch.setattr(writes, "CalendarItem", FakeCalendarItem)
    res = call(ctx, "create_event",
               {"subject": "Sync", "start": "2026-07-01", "end": "2026-07-01T01:00"})
    assert res["ok"] is True
    assert "requires_confirmation" not in res  # confirm only fires on invites
    assert res["event_id"].startswith("e")
    assert res["invitations_sent"] is False
    ev = FakeCalendarItem.last
    assert ev.kwargs["folder"] is account.calendar
    assert ev.save_calls == [{"send_meeting_invitations": SEND_TO_NONE}]
    # date-only → midnight EWS_TZ; naive → EWS_TZ attached (tz-aware both ways)
    assert ev.kwargs["start"] == datetime(2026, 7, 1, tzinfo=LOCAL_TZ)
    assert ev.kwargs["end"] == datetime(2026, 7, 1, 1, 0, tzinfo=LOCAL_TZ)
    assert ctx.aliaser.resolve(res["event_id"]) == "RAW-NEW-EVENT"


def test_create_event_with_invites_blocked_by_kill_switch_at_phase_one(
        tmp_path, monkeypatch):
    account = make_account()
    ctx = make_ctx(tmp_path, account, ews_email="organizer@example.com")
    monkeypatch.setattr(writes, "CalendarItem", FakeCalendarItem)
    FakeCalendarItem.last = None
    kwargs = {"subject": "Board", "start": "2026-07-01T10:00",
              "end": "2026-07-01T11:00", "attendees": ["guest@example.org"],
              "send_invitations": True}
    p1 = call(ctx, "create_event", kwargs)
    assert p1["ok"] is False
    assert p1["error"]["code"] == "kill_switch"
    assert p1["error"]["hint"] == (
        "Run without notifying attendees, or have the operator flip SEND_ENABLED."
    )
    assert "confirm_token" not in p1
    assert FakeCalendarItem.last is None  # nothing was built or saved

    silent = call(ctx, "create_event", {**kwargs, "send_invitations": False})
    assert silent["ok"] is True and silent["invitations_sent"] is False
    assert FakeCalendarItem.last.save_calls == [{"send_meeting_invitations": SEND_TO_NONE}]


def test_create_event_with_invites_sends_when_enabled(tmp_path, monkeypatch):
    account = make_account()
    ctx = make_ctx(tmp_path, account, send_enabled=True)
    monkeypatch.setattr(writes, "CalendarItem", FakeCalendarItem)
    kwargs = {"subject": "Board", "start": "2026-07-01T10:00",
              "end": "2026-07-01T11:00", "attendees": ["x@corp.example"],
              "send_invitations": True}
    p1 = call(ctx, "create_event", kwargs)
    p2 = call(ctx, "create_event", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is True and p2["invitations_sent"] is True
    ev = FakeCalendarItem.last
    assert ev.save_calls == [{"send_meeting_invitations": SEND_TO_ALL_AND_SAVE_COPY}]
    assert ev.kwargs["start"].tzinfo is not None


def test_update_event_silent_edit_uses_send_to_none(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    ev = MagicMock()
    account._by_id["RAW-E2"] = ev
    res = call(ctx, "update_event", {"event_id": "RAW-E2", "subject": "New title"})
    assert res["ok"] is True and res["updated_fields"] == ["subject"]
    assert ev.subject == "New title"
    ev.save.assert_called_once_with(update_fields=["subject"],
                                    send_meeting_invitations=SEND_TO_NONE)


def test_create_event_sets_a_reminder(tmp_path, monkeypatch):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "CalendarItem", FakeCalendarItem)
    res = call(ctx, "create_event", {"subject": "Sync", "start": "2026-07-01", "end": "2026-07-01T01:00",
                                     "reminder_minutes": 15})
    ev = FakeCalendarItem.last
    assert ev.reminder_is_set is True and ev.reminder_minutes_before_start == 15
    assert res["reminder_minutes"] == 15

    res = call(ctx, "create_event", {"subject": "Sync", "start": "2026-07-01", "end": "2026-07-01T01:00"})
    assert not hasattr(FakeCalendarItem.last, "reminder_is_set")
    assert "reminder_minutes" not in res


@pytest.mark.parametrize("minutes", [-1, 1.5, "15", True])
def test_create_event_rejects_a_bad_reminder(tmp_path, monkeypatch, minutes):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "CalendarItem", FakeCalendarItem)
    FakeCalendarItem.last = None
    res = call(ctx, "create_event", {"subject": "Sync", "start": "2026-07-01", "end": "2026-07-01T01:00",
                                     "reminder_minutes": minutes})
    assert res["ok"] is False and res["error"]["code"] == "validation"
    assert FakeCalendarItem.last is None


def test_update_event_sets_and_removes_a_reminder(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    ev = MagicMock()
    account._by_id["RAW-E3"] = ev
    res = call(ctx, "update_event", {"event_id": "RAW-E3", "reminder_minutes": 30})
    assert res["updated_fields"] == ["reminder_is_set", "reminder_minutes_before_start"]
    assert ev.reminder_is_set is True and ev.reminder_minutes_before_start == 30

    ev = MagicMock()
    account._by_id["RAW-E3"] = ev
    res = call(ctx, "update_event", {"event_id": "RAW-E3", "reminder_minutes": -1})
    assert res["updated_fields"] == ["reminder_is_set"]
    assert ev.reminder_is_set is False


def _series(tmp_path, monkeypatch, recurrence, **extra):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "CalendarItem", FakeCalendarItem)
    FakeCalendarItem.last = None
    kwargs = {"subject": "Sync", "start": "2026-11-12T13:45", "end": "2026-11-12T14:30",
              "recurrence": recurrence, **extra}
    return call(ctx, "create_event", kwargs)


def test_create_event_weekly_series_without_end(tmp_path, monkeypatch):
    res = _series(tmp_path, monkeypatch, {"frequency": "weekly"})
    assert res["ok"] is True and res["recurring"] is True
    rule = FakeCalendarItem.last.recurrence
    assert isinstance(rule.pattern, WeeklyPattern)
    assert rule.pattern.interval == 1
    assert rule.pattern.weekdays == [4]  # 2026-11-12 is a Thursday, ISO 4
    assert isinstance(rule.boundary, NoEndPattern)
    assert rule.boundary.start == date(2026, 11, 12)


def test_create_event_series_boundaries_and_patterns(tmp_path, monkeypatch):
    _series(tmp_path, monkeypatch, {"frequency": "weekly", "interval": 2,
                                    "weekdays": ["thursday", "monday"], "until": "2027-06-30"})
    rule = FakeCalendarItem.last.recurrence
    assert rule.pattern.interval == 2 and rule.pattern.weekdays == [1, 4]
    assert isinstance(rule.boundary, EndDatePattern) and rule.boundary.end == date(2027, 6, 30)

    _series(tmp_path, monkeypatch, {"frequency": "daily", "count": 5})
    rule = FakeCalendarItem.last.recurrence
    assert isinstance(rule.pattern, DailyPattern)
    assert isinstance(rule.boundary, NumberedPattern) and rule.boundary.number == 5

    _series(tmp_path, monkeypatch, {"frequency": "monthly"})
    rule = FakeCalendarItem.last.recurrence
    assert isinstance(rule.pattern, AbsoluteMonthlyPattern)
    assert rule.pattern.day_of_month == 12


def test_create_event_series_uses_the_named_zone_not_a_fixed_offset(tmp_path, monkeypatch):
    # A fixed offset would move the occurrences across a DST change.
    _series(tmp_path, monkeypatch, {"frequency": "weekly"},
            start="2026-11-12T10:00+00:00", end="2026-11-12T11:00+00:00")
    ev = FakeCalendarItem.last
    assert ev.kwargs["start"].tzinfo == LOCAL_TZ
    assert ev.kwargs["start"] == datetime(2026, 11, 12, 13, 0, tzinfo=LOCAL_TZ)
    assert ev.kwargs["end"].tzinfo == LOCAL_TZ


def test_create_event_series_first_date_comes_from_the_named_zone(tmp_path, monkeypatch):
    # 22:00 UTC is already the next day in UTC+3.
    _series(tmp_path, monkeypatch, {"frequency": "weekly"},
            start="2026-11-11T22:00+00:00", end="2026-11-11T23:00+00:00")
    rule = FakeCalendarItem.last.recurrence
    assert rule.boundary.start == date(2026, 11, 12)
    assert rule.pattern.weekdays == [4]


@pytest.mark.parametrize("recurrence, message", [
    ({"frequency": "hourly"}, "frequency"),
    ({"frequency": "weekly", "weekdays": ["monday"]}, "not in recurrence.weekdays"),
    ({"frequency": "weekly", "weekdays": ["someday"]}, "unknown day"),
    ({"frequency": "daily", "weekdays": ["thursday"]}, "requires frequency"),
    ({"frequency": "weekly", "interval": 0}, "interval"),
    ({"frequency": "weekly", "until": "2027-01-01", "count": 3}, "not both"),
    ({"frequency": "weekly", "until": "soon"}, "cannot parse"),
    ({"frequency": "weekly", "until": "2026-01-01"}, "before the first"),
    ({"frequency": "weekly", "count": 0}, "at least 1"),
])
def test_create_event_rejects_bad_recurrence(tmp_path, monkeypatch, recurrence, message):
    res = _series(tmp_path, monkeypatch, recurrence)
    assert res["ok"] is False and res["error"]["code"] == "validation"
    assert message in res["error"]["message"]
    assert FakeCalendarItem.last is None  # nothing was built or saved


def test_create_event_with_attendees_reports_pending_invitations(tmp_path, monkeypatch):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "CalendarItem", FakeCalendarItem)
    res = call(ctx, "create_event", {"subject": "Sync", "start": "2026-07-01",
                                     "end": "2026-07-01T01:00", "attendees": ["a@corp.example"]})
    assert res["invitations_sent"] is False and res["invitations_pending"] is True
    assert "Outlook" in res["note"]
    res = call(ctx, "create_event", {"subject": "Sync", "start": "2026-07-01",
                                     "end": "2026-07-01T01:00"})
    assert "invitations_pending" not in res and "note" not in res


def _event(account, kind, start=None):
    ev = MagicMock()
    ev.type = kind
    ev.start = start or datetime(2026, 11, 12, 13, 45, tzinfo=LOCAL_TZ)
    account._by_id["RAW-E3"] = ev
    return ev


def test_update_event_occurrence_id_changes_that_occurrence(tmp_path):
    account = make_account()
    ev = _event(account, "Occurrence")
    ctx = make_ctx(tmp_path, account)
    res = call(ctx, "update_event", {"event_id": "RAW-E3", "subject": "New"})
    assert res["ok"] is True
    ev.recurring_master.assert_not_called()
    assert ev.subject == "New"
    ev.save.assert_called_once_with(update_fields=["subject"],
                                    send_meeting_invitations=SEND_TO_NONE)


def test_update_event_scope_series_changes_the_master(tmp_path):
    account = make_account()
    ev = _event(account, "Occurrence")
    master = ev.recurring_master.return_value
    master.start = datetime(2026, 11, 12, 13, 45, tzinfo=LOCAL_TZ)
    ctx = make_ctx(tmp_path, account)
    res = call(ctx, "update_event", {"event_id": "RAW-E3", "subject": "New", "scope": "series",
                                     "start": "2026-11-12T14:00", "end": "2026-11-12T14:45"})
    assert res["ok"] is True
    master.refresh.assert_called_once_with()
    assert master.subject == "New"
    assert master.start == datetime(2026, 11, 12, 14, 0, tzinfo=LOCAL_TZ)
    ev.save.assert_not_called()
    master.save.assert_called_once_with(update_fields=["subject", "start", "end"],
                                        send_meeting_invitations=SEND_TO_NONE)


def test_update_event_scope_series_rejects_another_date(tmp_path):
    account = make_account()
    ev = _event(account, "RecurringMaster")
    ctx = make_ctx(tmp_path, account)
    res = call(ctx, "update_event", {"event_id": "RAW-E3", "scope": "series",
                                     "start": "2026-12-03T14:00"})
    assert res["ok"] is False and res["error"]["code"] == "validation"
    assert "2026-11-12" in res["error"]["message"]
    ev.save.assert_not_called()


def test_update_event_scope_series_rejects_a_single_event(tmp_path):
    account = make_account()
    ev = _event(account, "Single")
    ctx = make_ctx(tmp_path, account)
    res = call(ctx, "update_event", {"event_id": "RAW-E3", "subject": "New", "scope": "series"})
    assert res["ok"] is False and res["error"]["code"] == "validation"
    ev.save.assert_not_called()


def test_update_event_master_id_needs_an_explicit_scope(tmp_path):
    # Without this a "small" edit of the id create_event returned rewrites every occurrence.
    account = make_account()
    ev = _event(account, "RecurringMaster")
    ctx = make_ctx(tmp_path, account)
    res = call(ctx, "update_event", {"event_id": "RAW-E3", "subject": "New"})
    assert res["ok"] is False and res["error"]["code"] == "validation"
    ev.save.assert_not_called()


def test_update_event_notify_blocked_by_kill_switch(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account, ews_email="organizer@example.com")
    ev = MagicMock()
    account._by_id["RAW-E2"] = ev
    kwargs = {"event_id": "RAW-E2", "subject": "Moved", "notify_attendees": True}
    p1 = call(ctx, "update_event", kwargs)
    assert p1["ok"] is False
    assert p1["error"]["code"] == "kill_switch"
    assert p1["error"]["hint"] == (
        "Run without notifying attendees, or have the operator flip SEND_ENABLED."
    )
    assert "confirm_token" not in p1
    assert account.fetch_projections == []
    ev.save.assert_not_called()

    silent = call(ctx, "update_event", {**kwargs, "notify_attendees": False})
    assert silent["ok"] is True and silent["attendees_notified"] is False
    ev.save.assert_called_once_with(
        update_fields=["subject"], send_meeting_invitations=SEND_TO_NONE
    )


# --- respond_to_event / cancel_event --------------------------------------------


def test_respond_to_event_accept_two_phase(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    ev = MagicMock()
    account._by_id["RAW-E1"] = ev
    kwargs = {"event_id": "RAW-E1", "response": "accept", "message": "see you"}
    p1 = call(ctx, "respond_to_event", kwargs)
    assert p1["requires_confirmation"] is True
    ev.accept.assert_not_called()
    p2 = call(ctx, "respond_to_event", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["responded"] == "accept"
    ev.accept.assert_called_once_with(body="see you")


def test_cancel_event_two_phase(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    ev = SimpleNamespace(
        id="RAW-E3",
        changekey="CK-E3",
        item_class="IPM.Appointment",
        subject="Design review",
        start=datetime(2026, 11, 20, 9, 0, tzinfo=LOCAL_TZ),
        end=datetime(2026, 11, 20, 10, 0, tzinfo=LOCAL_TZ),
        type="Single",
        is_meeting=True,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="exec@corp.example"),
        required_attendees=[
            SimpleNamespace(mailbox=SimpleNamespace(email_address="peer@corp.example"))
        ],
        optional_attendees=None,
        resources=None,
        cancel=MagicMock(name="cancel"),
        move_to_trash=MagicMock(name="move_to_trash"),
    )
    account._by_id["RAW-E3"] = ev
    kwargs = {"event_id": "RAW-E3", "message": "postponed"}
    p1 = call(ctx, "cancel_event", kwargs)
    assert p1["requires_confirmation"] is True
    assert p1["preview"]["action"] == "cancel"
    assert p1["preview"]["attendee_count"] == 1
    assert p1["preview"]["attendees"] == ["peer@corp.example"]
    assert p1["preview"]["type"] == "Single"
    assert account.fetch_projections == [writes._EVENT_CANCEL_FIELDS]
    ev.cancel.assert_not_called()
    p2 = call(ctx, "cancel_event", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["cancelled"] is True
    assert account.fetch_projections == [
        writes._EVENT_CANCEL_FIELDS,
        writes._EVENT_CANCEL_FIELDS,
    ]
    ev.cancel.assert_called_once_with(new_body="postponed")

    # Someone else's meeting on the same path must be refused before minting a token.
    foreign = SimpleNamespace(
        id="RAW-E3-FOREIGN",
        changekey="CK-E3F",
        item_class="IPM.Appointment",
        subject="External sync",
        start=datetime(2026, 11, 20, 11, 0, tzinfo=LOCAL_TZ),
        end=datetime(2026, 11, 20, 12, 0, tzinfo=LOCAL_TZ),
        type="Single",
        is_meeting=True,
        my_response_type="Unknown",
        organizer=SimpleNamespace(email_address="chair@example.com"),
        required_attendees=[
            SimpleNamespace(mailbox=SimpleNamespace(email_address="exec@corp.example"))
        ],
        optional_attendees=None,
        resources=None,
        cancel=MagicMock(name="cancel"),
        move_to_trash=MagicMock(name="move_to_trash"),
    )
    account._by_id["RAW-E3-FOREIGN"] = foreign
    refused = call(ctx, "cancel_event", {"event_id": "RAW-E3-FOREIGN"})
    assert refused["ok"] is False
    assert refused["error"]["code"] == "validation"
    assert "respond_to_event" in refused["error"]["message"]
    assert "confirm_token" not in refused
    foreign.cancel.assert_not_called()
    foreign.move_to_trash.assert_not_called()


# --- delete_messages / set_oof ----------------------------------------------------


def test_delete_messages_trash_executes_without_confirm(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    msg = MagicMock()
    account._by_id["RAW-T1"] = msg
    res = call(ctx, "delete_messages", {"ids": ["RAW-T1"]})
    assert res["ok"] is True and res["deleted"] == 1
    assert "requires_confirmation" not in res
    msg.move_to_trash.assert_called_once_with()
    msg.delete.assert_not_called()


def test_delete_messages_permanent_requires_confirm_token(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    msg = MagicMock()
    account._by_id["RAW-P1"] = msg
    kwargs = {"ids": ["RAW-P1"], "disposition": "permanent"}
    p1 = call(ctx, "delete_messages", kwargs)
    assert p1["requires_confirmation"] is True
    msg.delete.assert_not_called()
    p2 = call(ctx, "delete_messages", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["deleted"] == 1 and p2["disposition"] == "permanent"
    msg.delete.assert_called_once_with()  # 5.0.3 hard delete takes no kwargs
    msg.move_to_trash.assert_not_called()


def test_set_oof_phase_one_preview_then_plain_string_replies(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    kwargs = {"state": "enabled", "internal_reply": "I am away"}
    p1 = call(ctx, "set_oof", kwargs)
    assert p1["requires_confirmation"] is True
    assert p1["preview"]["state"] == "enabled"
    assert not hasattr(account, "oof_settings")  # nothing applied at phase 1

    p2 = call(ctx, "set_oof", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is True and p2["state"] == "enabled"
    oof = account.oof_settings
    assert isinstance(oof, OofSettings)
    assert oof.state == OofSettings.ENABLED
    assert oof.internal_reply == "I am away"  # plain str — no OofReply wrapper
    assert oof.external_reply == "I am away"  # defaulted; clean() needs both


# --- write-side fixes: reply-all, scheduled OOF, path attachments, series, confirm ---


def test_create_draft_reply_all_removes_own_dedupes_and_adds_caller_recipients(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)  # ews_email="exec@corp.example"
    original = MagicMock()
    original.subject = "Quarterly plan"
    reply_item = MagicMock()
    reply_item.to_recipients = [
        SimpleNamespace(email_address="EXEC@corp.example"),
        SimpleNamespace(email_address="sender@corp.example"),
        SimpleNamespace(email_address="peer@corp.example"),
    ]
    reply_item.cc_recipients = [
        SimpleNamespace(email_address="Exec@Corp.Example"),
        SimpleNamespace(email_address="PEER@corp.example"),
        SimpleNamespace(email_address="observer@corp.example"),
    ]
    reply_item.bcc_recipients = None
    reply_item.save.return_value = SimpleNamespace(id="RAW-RA-1", changekey="CK-RA")
    original.create_reply_all.return_value = reply_item
    account._by_id["RAW-ORIG-RA"] = original
    alias = ctx.aliaser.alias_for("RAW-ORIG-RA", "m")

    res = call(ctx, "create_draft", {
        "mode": "reply_all",
        "reply_to": alias,
        "body": "Noted.",
        "to": ["Sender@corp.example", "added_to@corp.example"],
        "cc": ["added_to@corp.example", "OBSERVER@corp.example", "added_cc@corp.example"],
        "bcc": ["added_cc@corp.example", "audit@corp.example", "AUDIT@corp.example"],
    })
    assert res["ok"] is True
    assert writes._email_list(reply_item.to_recipients) == [
        "sender@corp.example", "peer@corp.example", "added_to@corp.example",
    ]
    assert writes._email_list(reply_item.cc_recipients) == [
        "observer@corp.example", "added_cc@corp.example",
    ]
    assert writes._email_list(reply_item.bcc_recipients) == ["audit@corp.example"]
    assert res["preview"]["to"] == [
        "sender@corp.example", "peer@corp.example", "added_to@corp.example",
    ]
    assert res["preview"]["cc"] == [
        "observer@corp.example", "added_cc@corp.example",
    ]


def test_create_draft_reply_all_note_to_self_never_leaves_to_empty(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    original = MagicMock()
    original.subject = "Self reminder"
    reply_item = MagicMock()
    reply_item.to_recipients = [
        SimpleNamespace(email_address="EXEC@corp.example"),
        SimpleNamespace(email_address="exec@corp.example"),
    ]
    reply_item.cc_recipients = [SimpleNamespace(email_address="exec@corp.example")]
    reply_item.bcc_recipients = None
    reply_item.save.return_value = SimpleNamespace(id="RAW-RA-SELF", changekey="CK-S")
    original.create_reply_all.return_value = reply_item
    account._by_id["RAW-SELF"] = original
    alias = ctx.aliaser.alias_for("RAW-SELF", "m")

    res = call(ctx, "create_draft", {
        "mode": "reply_all", "reply_to": alias, "body": "Follow up.",
    })
    assert res["ok"] is True
    assert writes._email_list(reply_item.to_recipients) == ["EXEC@corp.example"]
    assert reply_item.cc_recipients is None
    assert res["preview"]["to"] == ["EXEC@corp.example"]
    assert res["preview"]["cc"] == []


def test_set_oof_scheduled_serialises_utc_window_and_returns_configured_zone(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    kwargs = {
        "state": "scheduled",
        "internal_reply": "Out of office",
        "start": "2099-05-01T12:00",
        "end": "2099-05-02T12:00",
    }
    p1 = call(ctx, "set_oof", kwargs)
    assert p1["requires_confirmation"] is True
    assert not hasattr(account, "oof_settings")

    p2 = call(ctx, "set_oof", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is True
    assert p2["state"] == "scheduled"
    assert p2["start"] == "2099-05-01T12:00+03:00"
    assert p2["end"] == "2099-05-02T12:00+03:00"

    oof = account.oof_settings
    assert isinstance(oof, OofSettings)
    assert isinstance(oof.start, EWSDateTime)
    assert isinstance(oof.end, EWSDateTime)
    assert oof.start.tzinfo == UTC
    assert oof.end.tzinfo == UTC
    xml = xml_to_str(oof.to_xml(version=Version(build=EXCHANGE_2016)))
    assert "2099-05-01T09:00:00Z" in xml
    assert "2099-05-02T09:00:00Z" in xml
    assert "+03:00" not in xml


@pytest.mark.parametrize("bad_kwargs, expected_field", [
    ({"state": "scheduled", "internal_reply": "Away",
      "end": "2099-05-02T12:00"}, "start"),
    ({"state": "scheduled", "internal_reply": "Away",
      "start": "2099-05-01T12:00"}, "end"),
    ({"state": "scheduled", "internal_reply": "Away",
      "start": "2099-05-03T12:00", "end": "2099-05-02T12:00"}, "start"),
    ({"state": "scheduled", "internal_reply": "Away",
      "start": "2020-01-01T09:00", "end": "2020-01-02T09:00"}, "end"),
])
def test_set_oof_scheduled_validation_fires_with_no_exchange_call(
    tmp_path, bad_kwargs, expected_field,
):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    calls = []
    orig_call = ctx.gateway.call

    async def counting_call(fn):
        calls.append(True)
        return await orig_call(fn)

    ctx.gateway.call = counting_call
    p1 = call(ctx, "set_oof", bad_kwargs)
    p2 = call(ctx, "set_oof", {**bad_kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is False
    assert p2["error"]["code"] == "validation"
    assert expected_field in p2["error"]["message"]
    assert calls == []
    assert not hasattr(account, "oof_settings")


def test_path_attachments_off_by_default(tmp_path, monkeypatch):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    FakeDraftMessage.last = None
    res = call(ctx, "create_draft", {
        "to": ["peer@corp.example"],
        "body": "Hi",
        "attachments": [{"path": "report.pdf"}],
    })
    assert res["ok"] is False
    assert res["error"]["code"] == "validation"
    assert "EWS_ATTACHMENT_DIR" in res["error"]["message"]
    assert FakeDraftMessage.last is None

    draft = make_sendable_draft()
    account._by_id["RAW-D-OFF"] = draft
    alias = ctx.aliaser.alias_for("RAW-D-OFF", "d")
    res_add = call(ctx, "add_draft_attachment", {
        "draft_id": alias, "path": "report.pdf",
    })
    assert res_add["ok"] is False
    assert res_add["error"]["code"] == "validation"
    assert "EWS_ATTACHMENT_DIR" in res_add["error"]["message"]
    draft.attach.assert_not_called()


def test_path_attachments_inside_directory_work_and_hide_dir_path(tmp_path, monkeypatch):
    att_dir = tmp_path / "staging_uploads"
    (att_dir / "nested").mkdir(parents=True)
    (att_dir / "report.pdf").write_bytes(b"%PDF-1.4")
    (att_dir / "nested" / "notes.txt").write_bytes(b"hello")

    account = make_account()
    ctx = make_ctx(tmp_path, account, ews_attachment_dir=str(att_dir))
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    FakeDraftMessage.last = None

    res = call(ctx, "create_draft", {
        "to": ["peer@corp.example"],
        "body": "See attached.",
        "attachments": [
            {"path": "report.pdf"},
            {"path": "nested/notes.txt", "filename": "renamed.txt"},
        ],
    })
    assert res["ok"] is True
    assert res["attachments"] == [
        {"name": "report.pdf", "size": 8},
        {"name": "renamed.txt", "size": 5},
    ]
    assert str(att_dir) not in json.dumps(res)
    assert "staging_uploads" not in json.dumps(res)

    draft = make_sendable_draft()
    draft.attachments = []
    account._by_id["RAW-D-PATH"] = draft
    alias = ctx.aliaser.alias_for("RAW-D-PATH", "d")
    res_add = call(ctx, "add_draft_attachment", {
        "draft_id": alias, "path": "nested/notes.txt",
    })
    assert res_add["ok"] is True
    assert res_add["added"] == {"name": "notes.txt", "size": 5}
    assert str(att_dir) not in json.dumps(res_add)
    assert "staging_uploads" not in json.dumps(res_add)


def test_path_attachments_reject_invalid_cases_without_leaving_draft(tmp_path, monkeypatch):
    att_dir = tmp_path / "staging_uploads"
    (att_dir / "sub").mkdir(parents=True)
    (att_dir / "valid.txt").write_bytes(b"ok")
    (att_dir / "empty.txt").write_bytes(b"")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")

    account = make_account()
    ctx = make_ctx(tmp_path, account, ews_attachment_dir=str(att_dir))
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)

    bad_items = [
        {"path": "../outside.txt"},
        {"path": "sub/../../outside.txt"},
        {"path": str(outside.resolve())},
        {"path": "sub"},
        {"path": "missing.txt"},
        {"path": "empty.txt"},
        {"filename": "both.txt", "content_base64": _b64(b"x"), "path": "valid.txt"},
        {"filename": "neither.txt"},
    ]
    for bad in bad_items:
        FakeDraftMessage.last = None
        res = call(ctx, "create_draft", {
            "to": ["peer@corp.example"],
            "body": "Hi",
            "attachments": [bad],
        })
        assert res["ok"] is False, bad
        assert res["error"]["code"] == "validation", bad
        assert FakeDraftMessage.last is None, bad
        assert str(att_dir) not in json.dumps(res), bad
        assert "staging_uploads" not in json.dumps(res), bad


def test_path_attachments_reject_symlink_leaving_directory(tmp_path, monkeypatch):
    att_dir = tmp_path / "staging_uploads"
    att_dir.mkdir()
    outside = tmp_path / "outside_secret.txt"
    outside.write_bytes(b"top-secret")
    escape_link = att_dir / "escape.txt"
    try:
        escape_link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable on this platform: {exc}")

    ctx = make_ctx(tmp_path, make_account(), ews_attachment_dir=str(att_dir))
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    FakeDraftMessage.last = None
    res = call(ctx, "create_draft", {
        "to": ["peer@corp.example"],
        "body": "Hi",
        "attachments": [{"path": "escape.txt"}],
    })
    assert res["ok"] is False
    assert res["error"]["code"] == "validation"
    assert FakeDraftMessage.last is None
    assert str(att_dir) not in json.dumps(res)


def test_attachment_limits_count_base64_and_path_together(tmp_path, monkeypatch):
    att_dir = tmp_path / "staging_uploads"
    att_dir.mkdir()
    (att_dir / "small.bin").write_bytes(b"z")
    (att_dir / "half.bin").write_bytes(b"p" * (600 * 1024))

    ctx = make_ctx(
        tmp_path, make_account(),
        ews_attachment_dir=str(att_dir),
        ews_max_attachment_mb=1,
    )
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)

    # 11 base64 + 10 path = 21 > MAX_ATTACHMENTS (20)
    FakeDraftMessage.last = None
    many = (
        [{"filename": f"b{i}.txt", "content_base64": _b64(b"a")} for i in range(11)]
        + [{"path": "small.bin"} for _ in range(10)]
    )
    res_count = call(ctx, "create_draft", {
        "to": ["peer@corp.example"], "body": "Hi", "attachments": many,
    })
    assert res_count["ok"] is False and res_count["error"]["code"] == "validation"
    assert FakeDraftMessage.last is None

    # 600 KiB base64 + 600 KiB path > 1 MiB cap
    FakeDraftMessage.last = None
    res_size = call(ctx, "create_draft", {
        "to": ["peer@corp.example"],
        "body": "Hi",
        "attachments": [
            {"filename": "half_b64.bin", "content_base64": _b64(b"q" * (600 * 1024))},
            {"path": "half.bin"},
        ],
    })
    assert res_size["ok"] is False and res_size["error"]["code"] == "validation"
    assert "exceed" in res_size["error"]["message"]
    assert FakeDraftMessage.last is None


def test_update_event_scope_series_all_day_does_not_raise(tmp_path):
    account = make_account()
    ev = _event(account, "Occurrence", start=EWSDate(2026, 11, 12))
    master = ev.recurring_master.return_value
    master.start = EWSDate(2026, 11, 12)
    ctx = make_ctx(tmp_path, account)

    res = call(ctx, "update_event", {
        "event_id": "RAW-E3", "subject": "All-day series", "scope": "series",
    })
    assert res["ok"] is True
    assert master.subject == "All-day series"
    master.save.assert_called_once_with(
        update_fields=["subject"],
        send_meeting_invitations=SEND_TO_NONE,
    )

    wrong_day = call(ctx, "update_event", {
        "event_id": "RAW-E3", "scope": "series", "start": "2026-12-01",
    })
    assert wrong_day["ok"] is False
    assert wrong_day["error"]["code"] == "validation"
    assert "2026-11-12" in wrong_day["error"]["message"]


def test_send_draft_swapped_attachment_same_name_and_size_invalidates_token(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    draft = make_content_draft()
    draft.attachments = [
        FileAttachment(
            attachment_id=AttachmentId(id="ATT-ID-ORIGINAL"),
            name="statement.pdf",
            size=256,
        ),
    ]
    account._by_id["RAW-D-SWAP"] = draft
    alias = ctx.aliaser.alias_for("RAW-D-SWAP", "d")

    p1 = call(ctx, "send_draft", {"draft_id": alias})
    assert p1["requires_confirmation"] is True
    assert p1["preview"]["attachments"] == [{"name": "statement.pdf", "size": 256}]
    assert "ATT-ID-ORIGINAL" not in json.dumps(p1["preview"])

    draft.attachments = [
        FileAttachment(
            attachment_id=AttachmentId(id="ATT-ID-SWAPPED"),
            name="statement.pdf",
            size=256,
        ),
    ]
    p2 = call(ctx, "send_draft", {
        "draft_id": alias,
        "confirm_token": p1["confirm_token"],
    })
    assert p2["ok"] is False
    assert p2["error"]["code"] == "confirm_invalid"
    assert "stale" in p2["error"]["message"]
    draft.send.assert_not_called()


# --- review fixes: TOCTOU + per-draft lock, offsets, flag_complete, projections ---


def test_send_draft_verifies_and_sends_same_fetched_object(tmp_path):
    """_send_draft must fetch ONCE, re-verify the hash on that object, and call
    .send(save_copy=True) on that very instance."""
    account = SimpleNamespace(drafts=SimpleNamespace(id=DRAFTS_ID))
    preview_p1 = make_content_draft(subject="Approve", body="clean body")
    preview_p2 = make_content_draft(subject="Approve", body="clean body")
    handler_obj = make_content_draft(subject="Approve", body="clean body")
    objects = [preview_p1, preview_p2, handler_obj]

    def fetch_seq(pairs, only_fields=None):
        return [objects.pop(0) for _ in pairs]

    account.fetch = fetch_seq
    ctx = full_ctx(tmp_path, account)
    p1 = call(ctx, "send_draft", {"draft_id": "RAW-SAME-OBJ"})
    p2 = call(ctx, "send_draft", {
        "draft_id": "RAW-SAME-OBJ",
        "confirm_token": p1["confirm_token"],
    })
    assert p2["ok"] is True and p2["sent"] is True
    assert objects == []
    preview_p1.send.assert_not_called()
    preview_p2.send.assert_not_called()
    handler_obj.send.assert_called_once_with(save_copy=True)

    # Now mutate ONLY the object returned to _send_draft (simulating a change
    # between _confirm_gate's preview fetch and _send_draft's fetch).
    reset_consumed_tokens()
    p1_b = make_content_draft(subject="Approve", body="clean body")
    p2_b = make_content_draft(subject="Approve", body="clean body")
    tampered_handler_obj = make_content_draft(
        subject="Approve",
        to=("board@corp.example", "extra@corp.example"),
        body="clean body",
    )
    objects.extend([p1_b, p2_b, tampered_handler_obj])
    tok_b = call(ctx, "send_draft", {"draft_id": "RAW-SAME-OBJ"})["confirm_token"]
    refused = call(ctx, "send_draft", {
        "draft_id": "RAW-SAME-OBJ",
        "confirm_token": tok_b,
    })
    assert refused["ok"] is False
    assert refused["error"]["code"] == "confirm_invalid"
    assert "the draft changed after the preview" in refused["error"]["message"]
    tampered_handler_obj.send.assert_not_called()


def test_send_draft_concurrent_attachment_or_edit_never_sends_unpreviewed_content(
    tmp_path,
):
    att_dir = tmp_path / "att"
    att_dir.mkdir()
    (att_dir / "payroll.xlsx").write_bytes(b"confidential-table")

    class _DraftItem:
        def __init__(self) -> None:
            self.id = "RAW-DRAFT"
            self.changekey = "CK1"
            self.subject = "Schedule?"
            self.to_recipients = [SimpleNamespace(email_address="colleague@corp.example")]
            self.cc_recipients = []
            self.bcc_recipients = []
            self.text_body = "Initial note"
            self.body = "x"
            self.attachments = []
            self.parent_folder_id = SimpleNamespace(id=DRAFTS_ID)
            self.sent_as = None

        def save(self, update_fields=None) -> None:
            self.changekey = "CK2"

        def attach(self, att: Any) -> None:
            self.attachments.append(
                SimpleNamespace(
                    name=att.name,
                    size=len(att.content),
                    attachment_id=SimpleNamespace(id="ATT-NEW"),
                )
            )
            self.changekey = "CK3"

        def send(self, save_copy: bool = True) -> None:
            self.sent_as = {
                "to": [getattr(r, "email_address", r) for r in self.to_recipients],
                "bcc": [getattr(r, "email_address", r) for r in self.bcc_recipients],
                "body": str(self.body),
                "attachments": [a.name for a in self.attachments],
            }

    class _QueueGateway:
        def __init__(self, account: Any) -> None:
            self.account = account
            self._fifo = asyncio.Lock()

        async def call(self, fn: Any) -> Any:
            async with self._fifo:
                await asyncio.sleep(0.01)
                return fn(self.account)

    async def run_interleaved(label: str, extra_tool: str, extra_args: Dict[str, Any]):
        reset_consumed_tokens()
        draft = _DraftItem()
        account = SimpleNamespace(drafts=SimpleNamespace(id=DRAFTS_ID))
        account.fetch = lambda pairs, only_fields=None: [draft for _ in pairs]
        ctx = Context(
            settings=make_settings(
                ews_tz=FIXED_OFFSET_TZ,
                send_enabled=True,
                ews_capability_tier="full",
                ews_recipient_allowlist="*@corp.example",
                ews_attachment_dir=str(att_dir),
            ),
            gateway=_QueueGateway(account),
            manager=None,
            aliaser=get_aliaser(str(tmp_path / f"al-{label}")),
            audit=AuditLog(str(tmp_path / f"au-{label}")),
        )
        p1 = await dispatch(ctx, SPEC["send_draft"], {"draft_id": "RAW-DRAFT"})
        assert p1["requires_confirmation"] is True
        send_task = asyncio.create_task(dispatch(
            ctx,
            SPEC["send_draft"],
            {"draft_id": "RAW-DRAFT", "confirm_token": p1["confirm_token"]},
        ))
        await asyncio.sleep(0)  # phase-2 preview fetch is now queued first in FIFO
        edit_task = asyncio.create_task(dispatch(
            ctx,
            SPEC[extra_tool],
            {"draft_id": "RAW-DRAFT", **extra_args},
        ))
        r_send, r_edit = await asyncio.gather(send_task, edit_task)
        assert r_edit["ok"] is True
        assert r_send["ok"] is False
        assert r_send["error"]["code"] == "confirm_invalid"
        assert "the draft changed after the preview" in r_send["error"]["message"]
        assert draft.sent_as is None

    asyncio.run(run_interleaved(
        "attach",
        "add_draft_attachment",
        {"path": "payroll.xlsx"},
    ))
    asyncio.run(run_interleaved(
        "bcc-body",
        "update_draft",
        {"bcc": ["auditor@corp.example"], "body": "replaced text"},
    ))
    asyncio.run(run_interleaved(
        "body-only",
        "update_draft",
        {"body": "subtle replacement body"},
    ))


def test_per_draft_lock_serializes_mutations_with_send_and_stays_bounded(tmp_path):
    events: list[str] = []
    send_inside = asyncio.Event()
    release_send = asyncio.Event()

    draft_main = make_content_draft(subject="Main", body="ok")
    draft_other = make_content_draft(subject="Other", body="ok")
    by_id = {"RAW-LOCK-1": draft_main, "RAW-LOCK-2": draft_other}

    class _GatedGateway:
        def __init__(self, account: Any) -> None:
            self.account = account
            self.in_send_handler = False

        async def call(self, fn: Any) -> Any:
            if self.in_send_handler:
                events.append("send:enter")
                send_inside.set()
                await release_send.wait()
                out = fn(self.account)
                events.append("send:exit")
                return out
            out = fn(self.account)
            return out

    account = SimpleNamespace(drafts=SimpleNamespace(id=DRAFTS_ID))
    account.fetch = lambda pairs, only_fields=None: [by_id[i] for i, _ in pairs]
    gw = _GatedGateway(account)
    ctx = Context(
        settings=make_settings(
            ews_tz=FIXED_OFFSET_TZ,
            send_enabled=True,
            ews_capability_tier="full",
        ),
        gateway=gw,
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "al-lock")),
        audit=AuditLog(str(tmp_path / "au-lock")),
    )

    async def scenario() -> None:
        p1 = await dispatch(ctx, SPEC["send_draft"], {"draft_id": "RAW-LOCK-1"})
        token = p1["confirm_token"]

        # Wrap _send_draft's gateway call only: once phase-2 preview runs, flip flag.
        orig_preview = SPEC["send_draft"].preview
        assert orig_preview is not None

        async def preview_then_arm(c: Any, kw: Dict[str, Any]) -> Dict[str, Any]:
            res = await orig_preview(c, kw)
            gw.in_send_handler = True
            return res

        SPEC["send_draft"].preview = preview_then_arm
        try:
            send_task = asyncio.create_task(dispatch(
                ctx, SPEC["send_draft"], {"draft_id": "RAW-LOCK-1", "confirm_token": token}
            ))
            await send_inside.wait()
            gw.in_send_handler = False

            # Same-draft update/attach/delete must block until send_draft finishes.
            async def tracked(tag: str, tool_name: str, kw: Dict[str, Any]):
                res = await dispatch(ctx, SPEC[tool_name], kw)
                events.append(f"{tag}:done")
                return res

            upd_same = asyncio.create_task(tracked(
                "upd_same", "update_draft", {"draft_id": "RAW-LOCK-1", "subject": "After"}
            ))
            att_same = asyncio.create_task(tracked(
                "att_same",
                "add_draft_attachment",
                {"draft_id": "RAW-LOCK-1", "filename": "f.txt", "content_base64": _b64(b"x")},
            ))
            del_same = asyncio.create_task(tracked(
                "del_same", "delete_draft", {"draft_id": "RAW-LOCK-1"}
            ))
            await asyncio.sleep(0.02)
            # None of the same-draft mutations finished while send holds the lock.
            assert events == ["send:enter"]

            # A different draft id is NOT blocked by RAW-LOCK-1's lock.
            other_res = await dispatch(
                ctx, SPEC["update_draft"], {"draft_id": "RAW-LOCK-2", "subject": "Independent"}
            )
            assert other_res["ok"] is True

            release_send.set()
            r_send, r_upd, r_att, r_del = await asyncio.gather(
                send_task, upd_same, att_same, del_same
            )
            assert r_send["ok"] is True and r_send["sent"] is True
            assert r_upd["ok"] is True
            assert r_att["ok"] is True
            assert r_del["ok"] is True
            assert events[:2] == ["send:enter", "send:exit"]
        finally:
            SPEC["send_draft"].preview = orig_preview

        # Exercise 100 distinct draft ids (including raising work) and prove the
        # lock map self-prunes to 0 entries rather than growing without bound.
        for idx in range(100):
            key = f"RAW-BOUND-{idx}"
            if idx % 2 == 0:
                d = MagicMock()
                d.parent_folder_id = SimpleNamespace(id=DRAFTS_ID)
                by_id[key] = d
                res_d = await dispatch(ctx, SPEC["delete_draft"], {"draft_id": key})
                assert res_d["ok"] is True
            else:
                by_id[key] = RuntimeError("missing")
                res_e = await dispatch(ctx, SPEC["delete_draft"], {"draft_id": key})
                assert res_e["ok"] is False
        assert len(writes._DRAFT_LOCKS) == 0

    asyncio.run(scenario())


def test_parse_when_converts_aware_offsets_to_configured_zoneinfo():
    for raw, expected_hour in (
        ("2026-11-20T09:00:00+03:00", 9),
        ("2026-11-20T07:00:00+01:00", 9),
        ("2026-11-20T01:00:00-05:00", 9),
        ("2026-11-20T06:00:00+00:00", 9),
        ("2026-11-20T06:00:00Z", 9),
    ):
        dt_local = parse_when(raw, "start", FIXED_OFFSET_TZ)
        assert isinstance(dt_local.tzinfo, ZoneInfo)
        assert dt_local.tzinfo == LOCAL_TZ
        assert dt_local == datetime(2026, 11, 20, expected_hour, 0, tzinfo=LOCAL_TZ)

        dt_utc = parse_when(raw, "start", "UTC")
        assert isinstance(dt_utc.tzinfo, ZoneInfo)
        assert dt_utc.tzinfo == ZoneInfo("UTC")
        assert dt_utc == datetime(2026, 11, 20, 6, 0, tzinfo=ZoneInfo("UTC"))


def test_set_oof_create_event_and_update_event_accept_explicit_utc_offsets_with_real_exchangelib(
    tmp_path, monkeypatch,
):
    ver = Version(build=EXCHANGE_2016)

    # 1. set_oof with explicit non-zero UTC offsets (+03:00 and +01:00) using
    #    real EWSDateTime.from_datetime and real OofSettings XML serialization.
    oof_account = make_account()
    oof_ctx = full_ctx(tmp_path / "oof", oof_account)
    oof_args = {
        "state": "scheduled",
        "internal_reply": "Away on leave",
        "start": "2099-05-01T12:00:00+03:00",
        "end": "2099-05-02T10:00:00+01:00",
    }
    p1 = call(oof_ctx, "set_oof", oof_args)
    assert p1["requires_confirmation"] is True
    p2 = call(oof_ctx, "set_oof", {**oof_args, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is True
    assert p2["start"] == "2099-05-01T12:00+03:00"
    assert p2["end"] == "2099-05-02T12:00+03:00"
    assert oof_ctx._circuit_failures == 0
    oof_xml = xml_to_str(oof_account.oof_settings.to_xml(version=ver))
    assert "2099-05-01T09:00:00Z" in oof_xml
    assert "2099-05-02T09:00:00Z" in oof_xml

    # 2 & 3. Non-recurring create_event and update_event with explicit non-zero
    #        UTC offsets against a REAL offline Account and REAL CalendarItem
    #        (exercising CalendarItem.clean + CalendarItem.to_xml).
    CachingProtocol.clear_cache()
    try:
        cfg = Configuration(
            service_endpoint="https://ews.example.invalid/EWS/Exchange.asmx",
            credentials=Credentials("svc", "secret"),
            auth_type=BASIC,
            version=ver,
        )
        real_account = Account(
            primary_smtp_address="exec@corp.example",
            config=cfg,
            autodiscover=False,
            access_type=DELEGATE,
            default_timezone=EWSTimeZone(FIXED_OFFSET_TZ),
        )
        saved_items: list[tuple[CalendarItem, str, Dict[str, Any]]] = []

        def offline_cal_save(self: CalendarItem, **kwargs: Any) -> CalendarItem:
            self.clean(version=ver)
            xml_payload = xml_to_str(self.to_xml(version=ver))
            if not self.id:
                self.id = "RAW-REAL-CAL-CREATED"
            saved_items.append((self, xml_payload, kwargs))
            return self

        existing = CalendarItem(
            account=real_account,
            id="RAW-REAL-CAL-EXISTING",
            changekey="CK-CAL-1",
            subject="Initial slot",
            start=EWSDateTime(2026, 11, 20, 8, 0, tzinfo=EWSTimeZone(FIXED_OFFSET_TZ)),
            end=EWSDateTime(2026, 11, 20, 9, 0, tzinfo=EWSTimeZone(FIXED_OFFSET_TZ)),
        )
        monkeypatch.setattr(Account, "calendar", property(lambda _self: None))
        monkeypatch.setattr(
            Account,
            "fetch",
            lambda _self, ids, only_fields=None, **kw: [existing for _ in ids],
        )
        monkeypatch.setattr(CalendarItem, "save", offline_cal_save)

        cal_ctx = full_ctx(tmp_path / "cal", real_account)
        created = call(cal_ctx, "create_event", {
            "subject": "Offset create",
            "start": "2026-11-20T09:00:00+03:00",
            "end": "2026-11-20T08:00:00+01:00",
        })
        assert created["ok"] is True
        assert len(saved_items) == 1
        created_item, created_xml, _ = saved_items[0]
        assert created_item.start == datetime(2026, 11, 20, 9, 0, tzinfo=LOCAL_TZ)
        assert created_item.end == datetime(2026, 11, 20, 10, 0, tzinfo=LOCAL_TZ)
        assert "Offset create" in created_xml

        updated = call(cal_ctx, "update_event", {
            "event_id": "RAW-REAL-CAL-EXISTING",
            "start": "2026-11-20T11:00:00+03:00",
            "end": "2026-11-20T10:00:00+01:00",
        })
        assert updated["ok"] is True
        assert updated["updated_fields"] == ["start", "end"]
        assert len(saved_items) == 2
        assert existing.start == datetime(2026, 11, 20, 11, 0, tzinfo=LOCAL_TZ)
        assert existing.end == datetime(2026, 11, 20, 12, 0, tzinfo=LOCAL_TZ)
        assert cal_ctx._circuit_failures == 0
    finally:
        CachingProtocol.clear_cache()


def test_update_messages_flag_complete_false_preserves_open_followup_flag(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)

    open_flag = MagicMock()
    open_flag.flag_status = 2
    unflagged = MagicMock()
    unflagged.flag_status = None
    completed = MagicMock()
    completed.flag_status = 1

    account._by_id.update({
        "RAW-OPEN": open_flag,
        "RAW-NONE": unflagged,
        "RAW-DONE": completed,
    })

    res = call(ctx, "update_messages", {
        "ids": ["RAW-OPEN", "RAW-NONE", "RAW-DONE"],
        "flag_complete": False,
    })
    assert res == {"ok": True, "updated": 1, "failed": []}
    assert open_flag.flag_status == 2
    open_flag.save.assert_not_called()
    assert unflagged.flag_status is None
    unflagged.save.assert_not_called()
    assert completed.flag_status is None
    completed.save.assert_called_once_with(update_fields=["flag_status"])

    # Combining set_read=True with flag_complete=False on an open-flagged item
    # saves only is_read and leaves flag_status == 2 intact.
    res_combo = call(ctx, "update_messages", {
        "ids": ["RAW-OPEN"],
        "set_read": True,
        "flag_complete": False,
    })
    assert res_combo == {"ok": True, "updated": 1, "failed": []}
    assert open_flag.flag_status == 2
    open_flag.save.assert_called_once_with(update_fields=["is_read"])


def test_draft_fetches_use_explicit_projections_excluding_mime_content(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)

    # 1. create_draft reply-with-attachment path (fetches reply_to + saved draft)
    original = MagicMock()
    original.subject = "Plan"
    reply_item = MagicMock()
    reply_item.to_recipients = [SimpleNamespace(email_address="peer@corp.example")]
    reply_item.cc_recipients = None
    reply_item.save.return_value = SimpleNamespace(id="RAW-PROJ-REPLY", changekey="CK1")
    original.create_reply.return_value = reply_item

    draft = make_content_draft(subject="Re: Plan", to=("peer@corp.example",), body="Thanks")
    draft.changekey = "CK2"
    account._by_id.update({"RAW-PROJ-ORIG": original, "RAW-PROJ-REPLY": draft})

    res_reply = call(ctx, "create_draft", {
        "mode": "reply",
        "reply_to": "RAW-PROJ-ORIG",
        "body": "Thanks",
        "attachments": [{"filename": "note.txt", "content_base64": _b64(b"hi")}],
    })
    assert res_reply["ok"] is True

    # 2. add_draft_attachment
    res_add = call(ctx, "add_draft_attachment", {
        "draft_id": "RAW-PROJ-REPLY",
        "filename": "second.txt",
        "content_base64": _b64(b"abc"),
    })
    assert res_add["ok"] is True

    # 3. update_draft
    res_upd = call(ctx, "update_draft", {
        "draft_id": "RAW-PROJ-REPLY",
        "subject": "Re: Plan v2",
    })
    assert res_upd["ok"] is True

    # 4. send_draft (phase 1 preview + phase 2 gate preview + phase 2 handler send)
    p1 = call(ctx, "send_draft", {"draft_id": "RAW-PROJ-REPLY"})
    assert p1["requires_confirmation"] is True
    p2 = call(ctx, "send_draft", {
        "draft_id": "RAW-PROJ-REPLY",
        "confirm_token": p1["confirm_token"],
    })
    assert p2["ok"] is True and p2["sent"] is True

    # 5. delete_draft
    to_delete = make_content_draft()
    account._by_id["RAW-PROJ-DEL"] = to_delete
    res_del = call(ctx, "delete_draft", {"draft_id": "RAW-PROJ-DEL"})
    assert res_del["ok"] is True

    assert account.fetch_projections == [
        writes._REPLY_SOURCE_FIELDS,
        writes._DRAFT_ATTACH_FIELDS,
        writes._DRAFT_ATTACH_FIELDS,
        writes._DRAFT_UPDATE_FIELDS,
        writes._DRAFT_PREVIEW_FIELDS,
        writes._DRAFT_PREVIEW_FIELDS,
        writes._DRAFT_SEND_FIELDS,
        writes._DRAFT_DELETE_FIELDS,
    ]
    for projection in account.fetch_projections:
        assert isinstance(projection, list) and projection
        assert "mime_content" not in projection


# --- HTML draft bodies & send_draft link preview --------------------------------


def test_create_draft_html_clean_stores_rebuilt_body_without_sanitised(tmp_path, monkeypatch):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    FakeDraftMessage.last = None

    markup = '<div dir="rtl" style="text-align: right"><p>مرحباً <b>بكم</b></p></div>'
    res = call(ctx, "create_draft", {
        "to": ["team@example.com"],
        "subject": "Notice",
        "body": markup,
        "body_format": "html",
    })
    assert res["ok"] is True
    assert res["body_format"] == "html"
    assert "sanitised" not in res
    assert res["preview"]["body_snippet"] == "مرحباً بكم"
    saved_body = FakeDraftMessage.last.kwargs["body"]
    assert isinstance(saved_body, HTMLBody)
    assert str(saved_body) == f"<html><body>{markup}</body></html>"


def test_create_draft_html_dirty_reports_sanitised_and_plain_text_unchanged(
    tmp_path, monkeypatch
):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)

    dirty = (
        "<script>alert(1)</script>"
        '<img src="https://example.com/pixel.gif">'
        '<p onclick="steal()">Review '
        '<a href="javascript:alert(1)">bad</a> and '
        '<a href="https://example.com/doc">good</a></p>'
    )
    res = call(ctx, "create_draft", {
        "to": ["team@example.com"],
        "subject": "Review",
        "body": dirty,
        "body_format": "html",
    })
    assert res["ok"] is True
    assert res["body_format"] == "html"
    assert res["sanitised"] == {
        "removed_elements": ["img", "script"],
        "removed_attributes": 1,
        "removed_links": 1,
    }
    stored = str(FakeDraftMessage.last.kwargs["body"])
    assert stored == (
        "<html><body><p>Review <a>bad</a> and "
        '<a href="https://example.com/doc">good</a></p></body></html>'
    )
    assert "<script" not in res["preview"]["body_snippet"]

    # Plain text default still escapes tags literally and omits sanitised.
    plain_res = call(ctx, "create_draft", {
        "to": ["team@example.com"],
        "subject": "Plain",
        "body": "<b>literal</b> & plain",
    })
    assert plain_res["ok"] is True
    assert plain_res["body_format"] == "text"
    assert "sanitised" not in plain_res
    assert "&lt;b&gt;literal&lt;/b&gt; &amp; plain" in str(
        FakeDraftMessage.last.kwargs["body"]
    )


@pytest.mark.parametrize("bad_kwargs", [
    {"to": ["team@example.com"], "body": "<p>" + ("x" * 200_000) + "</p>", "body_format": "html"},
    {"to": ["team@example.com"], "body": "<script>alert(1)</script>", "body_format": "html"},
    {"to": ["team@example.com"], "body": '<img src="https://example.com/a.png">', "body_format": "html"},
    {"to": ["team@example.com"], "body": "<p>&nbsp;&#8203;</p>", "body_format": "html"},
    {"to": ["team@example.com"], "body": "hello", "body_format": "markdown"},
])
def test_create_draft_html_refusals_save_nothing(tmp_path, monkeypatch, bad_kwargs):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    FakeDraftMessage.last = None

    res = call(ctx, "create_draft", bad_kwargs)
    assert res["ok"] is False
    assert res["error"]["code"] == "validation"
    assert FakeDraftMessage.last is None


def test_create_draft_reply_reply_all_and_forward_support_html(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)

    original = MagicMock()
    original.subject = "Schedule"
    reply_item = MagicMock()
    reply_item.to_recipients = [SimpleNamespace(email_address="peer@corp.example")]
    reply_item.cc_recipients = None
    reply_item.bcc_recipients = None
    reply_item.save.return_value = SimpleNamespace(id="RAW-HTML-REP", changekey="CK-H1")
    original.create_reply.return_value = reply_item
    original.create_reply_all.return_value = reply_item
    original.create_forward.return_value = reply_item
    account._by_id["RAW-ORIG-HTML"] = original
    orig_alias = ctx.aliaser.alias_for("RAW-ORIG-HTML", "m")

    html_in = '<div dir="rtl"><p>تمت المراجعة</p><script>x()</script></div>'
    expected_body = '<html><body><div dir="rtl"><p>تمت المراجعة</p></div></body></html>'

    r_rep = call(ctx, "create_draft", {
        "mode": "reply",
        "reply_to": orig_alias,
        "body": html_in,
        "body_format": "html",
    })
    assert r_rep["ok"] is True and r_rep["body_format"] == "html"
    assert r_rep["sanitised"]["removed_elements"] == ["script"]
    assert str(original.create_reply.call_args.args[1]) == expected_body

    r_all = call(ctx, "create_draft", {
        "mode": "reply_all",
        "reply_to": orig_alias,
        "body": "<p>Approved</p>",
        "body_format": "html",
    })
    assert r_all["ok"] is True and r_all["body_format"] == "html"
    assert "sanitised" not in r_all
    assert str(original.create_reply_all.call_args.args[1]) == (
        "<html><body><p>Approved</p></body></html>"
    )

    r_fwd = call(ctx, "create_draft", {
        "mode": "forward",
        "reply_to": orig_alias,
        "to": ["delegate@corp.example"],
        "body": "<ul><li>Point one</li></ul>",
        "body_format": "html",
    })
    assert r_fwd["ok"] is True and r_fwd["body_format"] == "html"
    assert "sanitised" not in r_fwd
    assert str(original.create_forward.call_args.args[1]) == (
        "<html><body><ul><li>Point one</li></ul></body></html>"
    )


def test_update_draft_html_clean_dirty_and_refusals(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    draft = MagicMock()
    draft.parent_folder_id = SimpleNamespace(id=DRAFTS_ID)
    draft.to_recipients = [SimpleNamespace(email_address="peer@corp.example")]
    draft.cc_recipients = None
    draft.subject = "Draft subject"
    account._by_id["RAW-UPD-HTML"] = draft

    clean_res = call(ctx, "update_draft", {
        "draft_id": "RAW-UPD-HTML",
        "body": "<p>Updated <strong>body</strong></p>",
        "body_format": "html",
    })
    assert clean_res["ok"] is True
    assert clean_res["body_format"] == "html"
    assert "sanitised" not in clean_res
    assert isinstance(draft.body, HTMLBody)
    assert str(draft.body) == "<html><body><p>Updated <strong>body</strong></p></body></html>"
    assert clean_res["preview"]["body_snippet"] == "Updated body"

    dirty_res = call(ctx, "update_draft", {
        "draft_id": "RAW-UPD-HTML",
        "body": '<p onload="bad()">Text <a href="file:///etc/hosts">link</a></p>',
        "body_format": "html",
    })
    assert dirty_res["ok"] is True
    assert dirty_res["sanitised"] == {
        "removed_elements": [],
        "removed_attributes": 1,
        "removed_links": 1,
    }
    assert str(draft.body) == "<html><body><p>Text <a>link</a></p></body></html>"

    # Refusals happen before touching Exchange or saving.
    save_calls_before = draft.save.call_count
    fetch_count_before = len(account.fetch_projections)
    for bad in (
        {"draft_id": "RAW-UPD-HTML", "body": "x" * 200_001, "body_format": "html"},
        {"draft_id": "RAW-UPD-HTML", "body": "<style>p{}</style>", "body_format": "html"},
        {"draft_id": "RAW-UPD-HTML", "subject": "No body", "body_format": "html"},
        {"draft_id": "RAW-UPD-HTML", "body": "hi", "body_format": "rtf"},
    ):
        refused = call(ctx, "update_draft", bad)
        assert refused["ok"] is False
        assert refused["error"]["code"] == "validation"
    assert draft.save.call_count == save_calls_before
    assert len(account.fetch_projections) == fetch_count_before


def test_send_draft_preview_lists_links_caps_them_and_warns_on_mismatch(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)

    benign = make_content_draft(subject="Links", to=("peer@corp.example",), body="Portal")
    benign.body = HTMLBody(
        '<html><body><p><a href="https://portal.example.com/home">Portal</a></p></body></html>'
    )
    account._by_id["RAW-LINKS-OK"] = benign
    p1_ok = call(ctx, "send_draft", {"draft_id": "RAW-LINKS-OK"})
    assert p1_ok["requires_confirmation"] is True
    assert p1_ok["preview"]["links"] == [
        {"text": "Portal", "href": "https://portal.example.com/home"}
    ]
    assert "links_total" not in p1_ok["preview"]
    assert "warnings" not in p1_ok["preview"]

    many_anchors = "".join(
        f'<a href="https://example.com/p/{i}">Page {i}</a> ' for i in range(22)
    )
    deceptive = (
        '<a href="https://example.com/p/0">Page 0</a> '
        '<a href="https://other.example.com/signin">https://portal.example.com/signin</a>'
    )
    crowded = make_content_draft(subject="Many", to=("peer@corp.example",), body="Pages")
    crowded.body = HTMLBody(f"<html><body><p>{many_anchors}{deceptive}</p></body></html>")
    account._by_id["RAW-LINKS-CAP"] = crowded

    p1_cap = call(ctx, "send_draft", {"draft_id": "RAW-LINKS-CAP"})
    assert p1_cap["requires_confirmation"] is True
    assert len(p1_cap["preview"]["links"]) == 20
    assert p1_cap["preview"]["links_total"] == 23
    assert any(
        "portal.example.com" in w and "other.example.com" in w
        for w in p1_cap["preview"]["warnings"]
    )


def test_send_draft_refuses_when_only_html_target_changes_after_preview(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    draft = make_content_draft(
        subject="Invoice link",
        to=("peer@corp.example",),
        body="Open portal",
    )
    draft.body = HTMLBody(
        '<html><body><p><a href="https://portal.example.com/pay">Open portal</a></p></body></html>'
    )
    account._by_id["RAW-HTML-TOCTOU"] = draft

    p1 = call(ctx, "send_draft", {"draft_id": "RAW-HTML-TOCTOU"})
    assert p1["requires_confirmation"] is True

    # Visible text_body, recipients, subject, and attachments stay identical;
    # only the href inside the stored HTML body changes.
    draft.body = HTMLBody(
        '<html><body><p><a href="https://other.example.com/pay">Open portal</a></p></body></html>'
    )
    p2 = call(ctx, "send_draft", {
        "draft_id": "RAW-HTML-TOCTOU",
        "confirm_token": p1["confirm_token"],
    })
    assert p2["ok"] is False
    assert p2["error"]["code"] == "confirm_invalid"
    draft.send.assert_not_called()


def test_send_draft_preview_always_exposes_21st_link_host_and_warns_on_deceptive_text(
    tmp_path, monkeypatch
):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    filler = "".join(
        f'<li><a href="https://intranet.example.com/doc/{i}">Document {i}</a></li>'
        for i in range(20)
    )
    cases = [
        (
            "plain_deceptive",
            '<a href="https://evil.example/login">portal.example.com</a>',
            True,
        ),
        (
            "two_word_deceptive",
            '<a href="https://evil.example/login">Go to portal.example.com</a>',
            True,
        ),
        (
            "quoted_deceptive",
            '<a href="https://evil.example/login">\u201cportal.example.com\u201d</a>',
            True,
        ),
        (
            "neutral_text",
            '<a href="https://evil.example/login">Sign in to the staff portal</a>',
            False,
        ),
    ]
    for label, tail_link, should_warn in cases:
        FakeDraftMessage.last = None
        body = f"<p>Please review:</p><ul>{filler}</ul><p>{tail_link}</p>"
        created = call(ctx, "create_draft", {
            "to": ["peer@corp.example"],
            "subject": "Docs",
            "body": body,
            "body_format": "html",
        })
        assert created["ok"] is True, label
        stored = str(FakeDraftMessage.last.kwargs["body"])
        rid = f"RAW-21ST-{label}"
        draft = make_content_draft(subject="Docs", to=("peer@corp.example",), body="Docs")
        draft.body = HTMLBody(stored)
        account._by_id[rid] = draft

        p1 = call(ctx, "send_draft", {"draft_id": rid})
        assert p1["ok"] is True and p1["requires_confirmation"] is True
        prev = p1["preview"]
        assert len(prev["links"]) == 20
        assert prev["links_total"] == 21
        assert prev["link_hosts"] == [
            {"host": "evil.example", "count": 1},
            {"host": "intranet.example.com", "count": 20},
        ]
        assert "evil.example" in json.dumps(p1, ensure_ascii=False)
        assert bool(prev.get("warnings")) is should_warn, (label, prev)


@pytest.mark.parametrize(
    ("label", "doc", "expect_embedded"),
    [
        (
            "control",
            "<html><body><p>new text</p><div>"
            '<a href="https://evil.example/login">https://portal.example.com/login</a>'
            "</div></body></html>",
            False,
        ),
        (
            "with_bogus_marked_section",
            "<html><body><p>new text</p><div>"
            '<a href="https://evil.example/login">https://portal.example.com/login</a><![x]>'
            "</div></body></html>",
            False,
        ),
        (
            "with_endif_marked_section",
            "<html><body><![ endif]><p>new text</p><div>"
            '<a href="https://evil.example/login">https://portal.example.com/login</a>'
            "</div></body></html>",
            False,
        ),
        (
            "link_inside_stray_head",
            "<html><body><p>new text</p><head>"
            '<a href="https://evil.example/login">https://portal.example.com/login</a>'
            "</head></body></html>",
            False,
        ),
        (
            "link_inside_noscript",
            "<html><body><p>new text</p><noscript>"
            '<a href="https://evil.example/login">https://portal.example.com/login</a>'
            "</noscript></body></html>",
            False,
        ),
        (
            "link_after_abrupt_comment",
            "<html><body><p>new text</p><!-->"
            '<a href="https://evil.example/login">https://portal.example.com/login</a><!-- -->'
            "</body></html>",
            False,
        ),
        (
            "link_after_title_comment",
            "<html><body><p>new text</p><title><!--</title>-->"
            '<a href="https://evil.example/login">https://portal.example.com/login</a>'
            "</body></html>",
            False,
        ),
        (
            "link_after_script_end_attr",
            "<html><body><p>new text</p><script></script x>"
            '<a href="https://evil.example/login">https://portal.example.com/login</a>'
            "</body></html>",
            False,
        ),
        (
            "link_in_cdata_gt",
            "<html><body><p>new text</p><![CDATA[>"
            '<a href="https://evil.example/login">https://portal.example.com/login</a>]]>'
            "</body></html>",
            False,
        ),
        (
            "area_href",
            '<html><body><p>new text</p><map name="m">'
            '<area shape="rect" coords="0,0,900,900" href="https://evil.example/login">'
            "</map></body></html>",
            True,
        ),
    ],
)
def test_send_draft_preview_fails_closed_on_unsanitised_quoted_html(
    tmp_path, label, doc, expect_embedded
):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    rid = f"RAW-QUOTED-{label}"
    draft = make_content_draft(subject="Fwd", to=("peer@corp.example",), body="new text")
    draft.body = HTMLBody(doc)
    account._by_id[rid] = draft

    p1 = call(ctx, "send_draft", {"draft_id": rid})
    assert p1["ok"] is True and p1["requires_confirmation"] is True
    assert ctx._circuit_failures == 0
    prev = p1["preview"]
    assert prev.get("warnings"), (label, prev)
    if not expect_embedded:
        assert any("link mismatch" in w for w in prev["warnings"]), (label, prev)
    expected_host = {"host": "evil.example", "count": 1}
    if expect_embedded:
        expected_host["embedded"] = True
    expected_hosts = [expected_host]
    if "https://portal.example.com" in doc:
        expected_hosts.append({"host": "portal.example.com", "count": 1, "unparsed": True})
    assert prev["link_hosts"] == expected_hosts


def test_send_draft_preview_handles_fullwidth_slash_unparseable_url_and_oversize_html(
    tmp_path, monkeypatch
):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    FakeDraftMessage.last = None

    # Fullwidth slash in visible link text never crashes preview.
    res = call(ctx, "create_draft", {
        "to": ["peer@corp.example"],
        "subject": "Slash",
        "body": '<p>See <a href="https://example.com/a">https://example.com\uff0fa</a></p>',
        "body_format": "html",
    })
    assert res["ok"] is True
    d_fw = make_content_draft(subject="Slash", to=("peer@corp.example",), body="See")
    d_fw.body = HTMLBody(str(FakeDraftMessage.last.kwargs["body"]))
    account._by_id["RAW-FW"] = d_fw
    p1_fw = call(ctx, "send_draft", {"draft_id": "RAW-FW"})
    assert p1_fw["ok"] is True and p1_fw["requires_confirmation"] is True
    assert ctx._circuit_failures == 0

    # Unparseable stored URL is listed as written with a warning, never raising.
    d_bad = make_content_draft(subject="Bad URL", to=("peer@corp.example",), body="Broken")
    d_bad.body = HTMLBody('<html><body><p><a href="https://[">Broken</a></p></body></html>')
    account._by_id["RAW-BAD-URL"] = d_bad
    p1_bad = call(ctx, "send_draft", {"draft_id": "RAW-BAD-URL"})
    assert p1_bad["ok"] is True
    assert p1_bad["preview"]["links"] == [{"text": "Broken", "href": "https://["}]
    assert p1_bad["preview"]["warnings"]

    # Oversize stored HTML sets links_unverified=True and warns.
    d_huge = make_content_draft(subject="Huge", to=("peer@corp.example",), body="Huge")
    d_huge.body = HTMLBody("<html><body>" + ("x" * (MAX_INSPECT_HTML_CHARS + 5)) + "</body></html>")
    account._by_id["RAW-HUGE-HTML"] = d_huge
    p1_huge = call(ctx, "send_draft", {"draft_id": "RAW-HUGE-HTML"})
    assert p1_huge["ok"] is True
    assert p1_huge["preview"]["links_unverified"] is True
    assert p1_huge["preview"]["warnings"]


def test_malformed_html_and_bad_links_never_trip_circuit_breaker(tmp_path, monkeypatch):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)

    # Marked sections and refused link targets keep visible text and save cleanly.
    for body, expect_removed_links in (
        ("<p>hello</p><![x]>", 0),
        ("<p>hello</p><![CDAT[x]]>", 0),
        ('<p><a href="https://[">x</a> hello</p>', 1),
        ('<p><a href="https://example.com\uff0fx">x</a> hello</p>', 1),
        ('<p><a href="https://[example.com]/">x</a> hello</p>', 1),
    ):
        FakeDraftMessage.last = None
        res = call(ctx, "create_draft", {
            "to": ["peer@corp.example"],
            "subject": "x",
            "body": body,
            "body_format": "html",
        })
        assert res["ok"] is True, (body, res)
        assert ctx._circuit_failures == 0
        if expect_removed_links:
            assert res["sanitised"]["removed_links"] == expect_removed_links

    # Repeated validation failures (and even a forced internal sanitiser error)
    # return validation errors and never increment _circuit_failures.
    for _ in range(6):
        bad = call(ctx, "create_draft", {
            "to": ["peer@corp.example"],
            "subject": "x",
            "body": "<![x]><script>alert(1)</script>",
            "body_format": "html",
        })
        assert bad["ok"] is False
        assert bad["error"]["code"] == "validation"
        assert ctx._circuit_failures == 0

    def boom_sanitise(_raw: str):
        raise AssertionError("synthetic parser crash")

    monkeypatch.setattr(writes, "sanitise_html", boom_sanitise)
    for _ in range(5):
        crashed = call(ctx, "create_draft", {
            "to": ["peer@corp.example"],
            "subject": "x",
            "body": "<p>hello</p>",
            "body_format": "html",
        })
        assert crashed["ok"] is False
        assert crashed["error"]["code"] == "validation"
        assert ctx._circuit_failures == 0

    assert ctx.counters.get("err.upstream_error", 0) == 0
    after = call(ctx, "create_draft", {
        "to": ["peer@corp.example"],
        "subject": "x",
        "body": "ordinary plain text",
    })
    assert after["ok"] is True


def test_update_draft_without_body_omits_body_format_and_non_html_args_rejected(
    tmp_path, monkeypatch
):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    draft = MagicMock()
    draft.parent_folder_id = SimpleNamespace(id=DRAFTS_ID)
    draft.to_recipients = [SimpleNamespace(email_address="peer@corp.example")]
    draft.cc_recipients = None
    draft.subject = "Initial"
    account._by_id["RAW-NO-BODY"] = draft

    res_subj = call(ctx, "update_draft", {
        "draft_id": "RAW-NO-BODY",
        "subject": "Only subject changed",
    })
    assert res_subj["ok"] is True
    assert "body_format" not in res_subj
    assert "sanitised" not in res_subj

    res_with_body = call(ctx, "update_draft", {
        "draft_id": "RAW-NO-BODY",
        "body": "<b>plain</b>",
    })
    assert res_with_body["ok"] is True
    assert res_with_body["body_format"] == "text"
    assert "&lt;b&gt;plain&lt;/b&gt;" in str(draft.body)

    for bad_fmt in ("HTML", " html", "html ", "text/html", None, True, 1, "", "htm"):
        FakeDraftMessage.last = None
        r_bad = call(ctx, "create_draft", {
            "to": ["peer@corp.example"],
            "subject": "x",
            "body": "<p><b>b</b></p>",
            "body_format": bad_fmt,
        })
        assert r_bad["ok"] is False
        assert r_bad["error"]["code"] == "validation"
        assert FakeDraftMessage.last is None


def test_confirm_token_rejects_every_single_detail_stored_html_mutation(tmp_path):
    base = (
        '<html><body><div dir="rtl"><p style="text-align: right">Open the '
        '<a href="https://portal.example.com/pay" title="Portal">portal</a> &amp; reply.</p>'
        '<table border="1"><tr><td colspan="2">x</td></tr></table></div></body></html>'
    )
    mutations = {
        "href host": base.replace("portal.example.com", "portal.example.org"),
        "href one char": base.replace("/pay", "/paz"),
        "href scheme": base.replace("https://", "http://"),
        "href case": base.replace("portal.example.com", "Portal.example.com"),
        "href trailing slash": base.replace('/pay"', '/pay/"'),
        "href fragment": base.replace('/pay"', '/pay#x"'),
        "title value": base.replace('title="Portal"', 'title="Porta1"'),
        "title removed": base.replace(' title="Portal"', ""),
        "dir value": base.replace('dir="rtl"', 'dir="ltr"'),
        "style value": base.replace("text-align: right", "text-align: left"),
        "style spacing": base.replace("text-align: right", "text-align:right"),
        "border value": base.replace('border="1"', 'border="0"'),
        "colspan value": base.replace('colspan="2"', 'colspan="3"'),
        "attribute added": base.replace("<td ", '<td width="1" '),
        "attribute order": base.replace(
            'href="https://portal.example.com/pay" title="Portal"',
            'title="Portal" href="https://portal.example.com/pay"',
        ),
        "quote style": base.replace('dir="rtl"', "dir='rtl'"),
        "tag case": base.replace("<div", "<DIV"),
        "space in tag": base.replace("<p style", "<p  style"),
        "entity form": base.replace("&amp;", "&#38;"),
        "zero width char": base.replace("portal</a>", "portal\u200b</a>"),
        "trailing space": base + " ",
        "comment added": base.replace("<div", "<!--x--><div"),
        "second link added": base.replace("</p>", '<a href="https://evil.example/">.</a></p>'),
        "handler added": base.replace("<p ", '<p onclick="x()" '),
        "nfc_nfd": base.replace("reply", "re\u0301ply"),
    }
    account = make_account()
    ctx = full_ctx(tmp_path, account)

    for idx, (name, mutated) in enumerate(mutations.items()):
        assert mutated != base, name
        reset_consumed_tokens()
        reset_send_rate_window()
        rid = f"RAW-MUT-{idx}"
        draft = make_content_draft(
            subject="S",
            to=("peer@corp.example",),
            body="Open the portal & reply. x",
        )
        draft.body = HTMLBody(base)
        account._by_id[rid] = draft

        p1 = call(ctx, "send_draft", {"draft_id": rid})
        draft.body = HTMLBody(mutated)
        p2 = call(ctx, "send_draft", {
            "draft_id": rid,
            "confirm_token": p1["confirm_token"],
        })
        assert p2["ok"] is False, name
        assert p2["error"]["code"] == "confirm_invalid", name
        draft.send.assert_not_called()


def test_sanitise_html_and_inspect_draft_links_run_off_event_loop(tmp_path, monkeypatch):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)
    seen_threads: dict[str, threading.Thread] = {}
    real_sanitise = writes.sanitise_html
    real_inspect = writes.inspect_draft_links

    def spy_sanitise(raw: str):
        seen_threads.setdefault("sanitise", threading.current_thread())
        return real_sanitise(raw)

    def spy_inspect(raw: str):
        seen_threads["inspect"] = threading.current_thread()
        return real_inspect(raw)

    monkeypatch.setattr(writes, "sanitise_html", spy_sanitise)
    monkeypatch.setattr(writes, "inspect_draft_links", spy_inspect)

    async def exercise() -> threading.Thread:
        loop_thread = threading.current_thread()
        r_create = await dispatch(ctx, SPEC["create_draft"], {
            "to": ["peer@corp.example"],
            "subject": "Thread check",
            "body": '<p><a href="https://example.com">Link</a></p>',
            "body_format": "html",
        })
        assert r_create["ok"] is True
        draft = make_content_draft(subject="Thread check", to=("peer@corp.example",), body="Link")
        draft.body = HTMLBody(str(FakeDraftMessage.last.kwargs["body"]))
        account._by_id["RAW-THR"] = draft
        p1 = await dispatch(ctx, SPEC["send_draft"], {"draft_id": "RAW-THR"})
        assert p1["ok"] is True
        return loop_thread

    loop_thread = asyncio.run(exercise())
    assert "sanitise" in seen_threads and seen_threads["sanitise"] is not loop_thread
    assert "inspect" in seen_threads and seen_threads["inspect"] is not loop_thread


def test_event_loop_ticker_never_stalls_during_200k_combining_mark_preview(
    tmp_path, monkeypatch
):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)

    # Exact 200,000-character combining-mark HTML body (tested both raw on stored
    # draft and through create_draft -> send_draft).
    prefix = '<p><a href="https://x.example/">a'
    suffix = "</a></p>"
    room = 200_000 - len(prefix) - len(suffix)
    half = room // 2
    combining_200k = prefix + ("\u0301" * half) + ("\u0316" * (room - half)) + suffix
    assert len(combining_200k) == 200_000

    draft_raw = make_content_draft(
        subject="Combining", to=("peer@corp.example",), body="a"
    )
    draft_raw.body = HTMLBody(combining_200k)
    account._by_id["RAW-COMB-200K"] = draft_raw

    async def measure_preview_stall(draft_id: str) -> tuple[dict, list[float]]:
        gaps: list[float] = []
        done = asyncio.Event()

        async def ticker() -> None:
            last = time.perf_counter()
            while not done.is_set():
                await asyncio.sleep(0.02)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)  # ensure ticker is actively polling before work starts
        res = await dispatch(ctx, SPEC["send_draft"], {"draft_id": draft_id})
        done.set()
        await task
        return res, gaps

    res_raw, gaps_raw = asyncio.run(measure_preview_stall("RAW-COMB-200K"))
    assert res_raw["ok"] is True and res_raw["requires_confirmation"] is True
    assert gaps_raw and max(gaps_raw) < 0.5 * TIME_MARGIN, f"event loop stalled {max(gaps_raw):.2f}s"

    FakeDraftMessage.last = None
    created = call(ctx, "create_draft", {
        "to": ["peer@corp.example"],
        "subject": "Combining authored",
        "body": combining_200k,
        "body_format": "html",
    })
    assert created["ok"] is True
    draft_san = make_content_draft(
        subject="Combining authored", to=("peer@corp.example",), body="a"
    )
    draft_san.body = HTMLBody(str(FakeDraftMessage.last.kwargs["body"]))
    account._by_id["RAW-COMB-SAN"] = draft_san

    res_san, gaps_san = asyncio.run(measure_preview_stall("RAW-COMB-SAN"))
    assert res_san["ok"] is True and res_san["requires_confirmation"] is True
    assert gaps_san and max(gaps_san) < 0.5 * TIME_MARGIN, f"event loop stalled {max(gaps_san):.2f}s"


def test_send_draft_preview_surfaces_backtick_nosemi_and_defeats_empty_anchor_flood(
    tmp_path, monkeypatch
):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    monkeypatch.setattr(writes, "Message", FakeDraftMessage)

    # 1. Quoted reply hiding a URL behind backtick + foreign CDATA or &#64 without ';'.
    for idx, quoted_html in enumerate((
        "<html><body><p>See below.</p><svg><p><![CDATA[><a data=`1 "
        'href="https://evil.example/login">https://good.example/login</a>]]></p></body></html>',
        '<html><body><p><a href="https://portal.example.com&#64evil.example/login">'
        "Sign in</a></p></body></html>",
    )):
        rid = f"RAW-R5-Q{idx}"
        d_q = make_content_draft(subject="Re: access", to=("peer@corp.example",), body="See below")
        d_q.body = HTMLBody(quoted_html)
        account._by_id[rid] = d_q
        p1_q = call(ctx, "send_draft", {"draft_id": rid})
        assert p1_q["ok"] is True
        hosts_q = {h["host"] for h in p1_q["preview"]["link_hosts"]}
        assert "evil.example" in hosts_q

    # 2. Model-authored flood: 55 empty anchors + 1 deceptive link at the tail.
    empties = "".join(f'<a href="https://a{i:03d}.flood.example/"></a>' for i in range(55))
    flood_body = (
        "<p>Hello</p>"
        + empties
        + '<p><a href="https://zzz-evil.example/login">https://good.example/login</a></p>'
    )
    FakeDraftMessage.last = None
    r_flood = call(ctx, "create_draft", {
        "to": ["peer@corp.example"],
        "subject": "Flood check",
        "body": flood_body,
        "body_format": "html",
    })
    assert r_flood["ok"] is True
    assert r_flood["sanitised"]["removed_links"] == 55

    d_f = make_content_draft(subject="Flood check", to=("peer@corp.example",), body="Hello")
    d_f.body = HTMLBody(str(FakeDraftMessage.last.kwargs["body"]))
    account._by_id["RAW-R5-FLOOD"] = d_f
    p1_f = call(ctx, "send_draft", {"draft_id": "RAW-R5-FLOOD"})
    prev_f = p1_f["preview"]
    assert prev_f["links"] == [
        {"text": "https://good.example/login", "href": "https://zzz-evil.example/login"}
    ]
    assert prev_f["link_hosts"][0] == {"host": "zzz-evil.example", "count": 1}
    assert any(
        "link mismatch" in w and "zzz-evil.example" in w for w in prev_f["warnings"]
    )


# --- draft attachment removal, .eml message attachment, appointment deletion ---


class _StatefulDraft:
    """Draft stand-in with real FileAttachment attach/detach state transitions."""

    def __init__(
        self,
        raw_id: str = "RAW-ST-DRAFT",
        *,
        folder_id: str = DRAFTS_ID,
        subject: str = "Initial subject",
        to: tuple[str, ...] = ("peer@example.com",),
        body_text: str = "Please see attached.",
    ) -> None:
        self.id = raw_id
        self.changekey = "CK-INIT"
        self.parent_folder_id = SimpleNamespace(id=folder_id)
        self.subject = subject
        self.to_recipients = [SimpleNamespace(email_address=a) for a in to]
        self.cc_recipients: list[Any] = []
        self.bcc_recipients: list[Any] = []
        self.text_body = body_text
        self.body = HTMLBody(f"<html><body><p>{body_text}</p></body></html>")
        self.message_id = f"<{raw_id}@example.com>"
        self.attachments: list[Any] = []
        self.attach_calls: list[Any] = []
        self.detach_calls: list[list[Any]] = []
        self.save_calls: list[list[str]] = []
        self.send_calls: list[bool] = []
        self._seq = 0

    def attach(self, att: Any) -> None:
        self._seq += 1
        att.attachment_id = AttachmentId(id=f"RAW-ATT-{self.id}-{self._seq}")
        att.size = len(att.content)
        self.attach_calls.append(att)
        self.attachments.append(att)
        self.changekey = f"CK-ATT-{self._seq}"

    def detach(self, atts: Any) -> None:
        items = list(atts)
        self.detach_calls.append(items)
        for att in items:
            self.attachments.remove(att)
        self._seq += 1
        self.changekey = f"CK-DET-{self._seq}"

    def save(self, update_fields: Any = None) -> None:
        self.save_calls.append(list(update_fields or []))
        self._seq += 1
        self.changekey = f"CK-SAVE-{self._seq}"

    def send(self, save_copy: bool = True) -> None:
        self.send_calls.append(save_copy)


def test_update_draft_remove_attachments_by_name_by_id_and_combined_with_subject(
    tmp_path,
):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    draft = _StatefulDraft("RAW-REM-1")
    account._by_id["RAW-REM-1"] = draft
    d_alias = ctx.aliaser.alias_for("RAW-REM-1", "d")

    r1 = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "filename": "notes.txt",
        "content_base64": _b64(b"alpha"),
    })
    r2 = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "filename": "budget.pdf",
        "content_base64": _b64(b"%PDF-1.4"),
    })
    r3 = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "filename": "summary.csv",
        "content_base64": _b64(b"a,b\n1,2\n"),
    })
    assert r1["ok"] and r2["ok"] and r3["ok"]
    id_notes = r1["added"]["id"]
    id_budget = r2["added"]["id"]
    id_summary = r3["added"]["id"]
    assert id_notes.startswith("a") and id_budget.startswith("a") and id_summary.startswith("a")
    assert "RAW-ATT-" not in json.dumps(r3)
    assert r3["attachments"] == [
        {"name": "budget.pdf", "size": 8, "id": id_budget},
        {"name": "notes.txt", "size": 5, "id": id_notes},
        {"name": "summary.csv", "size": 8, "id": id_summary},
    ]

    # 1. Remove by attachment name only (no save() call when no draft fields change).
    rem_by_name = call(ctx, "update_draft", {
        "draft_id": d_alias,
        "remove_attachments": ["notes.txt"],
    })
    assert rem_by_name["ok"] is True
    assert draft.save_calls == []
    assert len(draft.detach_calls) == 1
    assert [a.name for a in draft.detach_calls[0]] == ["notes.txt"]
    assert rem_by_name["attachments"] == [
        {"name": "budget.pdf", "size": 8, "id": id_budget},
        {"name": "summary.csv", "size": 8, "id": id_summary},
    ]

    # 2. Remove by attachment id (a-alias) combined with a subject change.
    rem_combo = call(ctx, "update_draft", {
        "draft_id": d_alias,
        "subject": "Updated Q3 packet",
        "remove_attachments": [id_budget],
    })
    assert rem_combo["ok"] is True
    assert rem_combo["preview"]["subject"] == "Updated Q3 packet"
    assert draft.subject == "Updated Q3 packet"
    assert draft.save_calls == [["subject"]]
    assert len(draft.detach_calls) == 2
    assert [a.name for a in draft.detach_calls[1]] == ["budget.pdf"]
    assert rem_combo["attachments"] == [
        {"name": "summary.csv", "size": 8, "id": id_summary},
    ]
    assert account.fetch_projections[-1] == writes._DRAFT_UPDATE_ATTACH_FIELDS

    # 3. Remove the final attachment by raw EWS attachment id -> empty list remains.
    raw_summary_id = ctx.aliaser.resolve(id_summary)
    rem_last = call(ctx, "update_draft", {
        "draft_id": d_alias,
        "remove_attachments": [raw_summary_id],
    })
    assert rem_last["ok"] is True
    assert rem_last["attachments"] == []
    assert draft.attachments == []


def test_update_draft_remove_attachments_unknown_ambiguous_atomic_and_non_draft(
    tmp_path,
):
    account = make_account()
    ctx = make_ctx(tmp_path, account)
    draft = _StatefulDraft("RAW-REM-2", subject="Original")
    account._by_id["RAW-REM-2"] = draft
    d_alias = ctx.aliaser.alias_for("RAW-REM-2", "d")

    r_keep = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "filename": "keep.txt",
        "content_base64": _b64(b"keep"),
    })
    r_dup1 = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "filename": "report.pdf",
        "content_base64": _b64(b"v1"),
    })
    r_dup2 = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "filename": "report.pdf",
        "content_base64": _b64(b"version-2"),
    })
    id_keep = r_keep["added"]["id"]
    id_dup1 = r_dup1["added"]["id"]
    id_dup2 = r_dup2["added"]["id"]

    # Unknown name and unknown alias both fail with validation naming the entry.
    for bad_sel in ("missing.pdf", "a999"):
        res_unknown = call(ctx, "update_draft", {
            "draft_id": d_alias,
            "remove_attachments": [bad_sel],
        })
        assert res_unknown["ok"] is False
        assert res_unknown["error"]["code"] == "validation"
        assert bad_sel in res_unknown["error"]["message"]
        assert draft.detach_calls == []
        assert len(draft.attachments) == 3

    # All-or-nothing: one valid + one unknown removes NOTHING and saves NOTHING.
    res_atomic = call(ctx, "update_draft", {
        "draft_id": d_alias,
        "subject": "Must not persist",
        "remove_attachments": ["keep.txt", "no-such-file.txt"],
    })
    assert res_atomic["ok"] is False
    assert res_atomic["error"]["code"] == "validation"
    assert "no-such-file.txt" in res_atomic["error"]["message"]
    assert draft.detach_calls == []
    assert draft.save_calls == []
    assert draft.subject == "Original"
    assert len(draft.attachments) == 3

    # Ambiguous name refuses and surfaces the attachment ids to choose from.
    res_ambig = call(ctx, "update_draft", {
        "draft_id": d_alias,
        "remove_attachments": ["report.pdf"],
    })
    assert res_ambig["ok"] is False
    assert res_ambig["error"]["code"] == "validation"
    assert "report.pdf" in res_ambig["error"]["message"]
    assert id_dup1 in res_ambig["error"]["message"]
    assert id_dup2 in res_ambig["error"]["message"]
    assert "RAW-ATT-" not in json.dumps(res_ambig)
    assert draft.detach_calls == []

    # Choosing one of the reported ids removes only that copy.
    res_disambig = call(ctx, "update_draft", {
        "draft_id": d_alias,
        "remove_attachments": [id_dup1],
    })
    assert res_disambig["ok"] is True
    assert res_disambig["attachments"] == [
        {"name": "keep.txt", "size": 4, "id": id_keep},
        {"name": "report.pdf", "size": 9, "id": id_dup2},
    ]

    # Item outside Drafts is refused before any detach.
    inbox_item = _StatefulDraft("RAW-INBOX-MSG", folder_id="FOLDER-INBOX")
    inbox_item.attach(FileAttachment(name="keep.txt", content=b"x", content_type="text/plain"))
    account._by_id["RAW-INBOX-MSG"] = inbox_item
    m_alias = ctx.aliaser.alias_for("RAW-INBOX-MSG", "m")
    res_outside = call(ctx, "update_draft", {
        "draft_id": m_alias,
        "remove_attachments": ["keep.txt"],
    })
    assert res_outside["ok"] is False
    assert res_outside["error"]["code"] == "validation"
    assert "not a draft" in res_outside["error"]["message"]
    assert inbox_item.detach_calls == []

    # Empty list or blank selector fails validation before touching Exchange.
    fetches_before = len(account.fetch_projections)
    for invalid_list in ([], ["   "], [123]):
        bad = call(ctx, "update_draft", {
            "draft_id": d_alias,
            "remove_attachments": invalid_list,
        })
        assert bad["ok"] is False
        assert bad["error"]["code"] == "validation"
    assert len(account.fetch_projections) == fetches_before

    # Huge unknown selector echoed in error is cut to 80 characters.
    huge_sel = "x" * 200_000
    res_huge = call(ctx, "update_draft", {
        "draft_id": d_alias,
        "remove_attachments": [huge_sel],
    })
    assert res_huge["ok"] is False
    assert res_huge["error"]["code"] == "validation"
    err_msg = res_huge["error"]["message"]
    assert ("x" * 80) in err_msg
    assert ("x" * 81) not in err_msg
    assert len(err_msg) < 200


def test_update_draft_remove_attachments_partial_failure_preserves_fields_and_reports_state(
    tmp_path,
):
    class PerCallDetachDraft(_StatefulDraft):
        """Mirrors exchangelib saved-item detach: one server call per attachment."""

        fail_at = 1

        def detach(self, atts: Any) -> None:
            items = list(atts)
            self.detach_calls.append(items)
            for idx, att in enumerate(items):
                if idx == self.fail_at:
                    raise ErrorServerBusy("server busy")
                self.attachments.remove(att)
                att.attachment_id = None
                att.parent_item = None

    account = make_account()
    ctx = make_ctx(tmp_path, account)
    draft = PerCallDetachDraft("RAW-REM-PARTIAL", subject="Original subject")
    account._by_id["RAW-REM-PARTIAL"] = draft
    d_alias = ctx.aliaser.alias_for("RAW-REM-PARTIAL", "d")

    added_ids: dict[str, str] = {}
    for name in ("one.txt", "two.txt", "three.txt"):
        added = call(ctx, "add_draft_attachment", {
            "draft_id": d_alias,
            "filename": name,
            "content_base64": _b64(name.encode("utf-8")),
        })
        assert added["ok"] is True
        added_ids[name] = added["added"]["id"]

    # 1. First deletion fails (fail_at=0): nothing removed, nothing saved.
    draft.fail_at = 0
    res_zero = call(ctx, "update_draft", {
        "draft_id": d_alias,
        "subject": "NEW SUBJECT",
        "remove_attachments": ["one.txt", "two.txt", "three.txt"],
    })
    assert res_zero["ok"] is False
    assert res_zero["error"]["code"] == "throttled"
    assert "removed" not in res_zero
    assert draft.subject == "Original subject"
    assert draft.save_calls == []
    assert [a.name for a in draft.attachments] == ["one.txt", "two.txt", "three.txt"]

    # 2. Second deletion fails (fail_at=1): one.txt was removed on server,
    #    two.txt and three.txt remain, and subject was NOT modified or saved.
    draft.fail_at = 1
    res_partial = call(ctx, "update_draft", {
        "draft_id": d_alias,
        "subject": "NEW SUBJECT",
        "remove_attachments": ["one.txt", "two.txt", "three.txt"],
    })
    assert res_partial["ok"] is False
    assert res_partial["error"]["code"] == "throttled"
    assert draft.subject == "Original subject"
    assert draft.save_calls == []
    assert [a.name for a in draft.attachments] == ["two.txt", "three.txt"]
    expected_removed = [{"name": "one.txt", "size": 7, "id": added_ids["one.txt"]}]
    expected_remaining = [
        {"name": "three.txt", "size": 9, "id": added_ids["three.txt"]},
        {"name": "two.txt", "size": 7, "id": added_ids["two.txt"]},
    ]
    assert res_partial["removed"] == expected_removed
    assert res_partial["error"]["removed"] == expected_removed
    assert res_partial["attachments"] == expected_remaining
    assert res_partial["error"]["attachments"] == expected_remaining
    assert "RAW-ATT-" not in json.dumps(res_partial)

    # 3. Caller continues cleanly from the reported remaining attachments.
    draft.fail_at = 99
    res_retry = call(ctx, "update_draft", {
        "draft_id": d_alias,
        "subject": "NEW SUBJECT",
        "remove_attachments": [a["id"] for a in res_partial["attachments"]],
    })
    assert res_retry["ok"] is True
    assert res_retry["attachments"] == []
    assert draft.attachments == []
    assert draft.subject == "NEW SUBJECT"
    assert draft.save_calls == [["subject"]]


def test_add_draft_attachment_message_id_eml_name_type_size_unknown_and_exclusivity(
    tmp_path,
):
    att_dir = tmp_path / "staging"
    att_dir.mkdir()
    (att_dir / "doc.txt").write_bytes(b"doc")

    account = make_account()
    ctx = make_ctx(
        tmp_path,
        account,
        ews_attachment_dir=str(att_dir),
        ews_max_attachment_mb=1,
    )
    draft = _StatefulDraft("RAW-EML-DRAFT")
    mime_payload = (
        b"From: sender@example.com\r\n"
        b"To: exec@corp.example\r\n"
        b"Subject: Re: Q3/Q4: Plan?\r\n"
        b"MIME-Version: 1.0\r\n\r\n"
        b"Approved for release.\r\n"
    )
    # Source lives in Inbox (not Drafts) and carries unsafe filename chars + bidi.
    source_msg = SimpleNamespace(
        id="RAW-SRC-MSG",
        item_class="IPM.Note",
        size=len(mime_payload),
        parent_folder_id=SimpleNamespace(id="FOLDER-INBOX"),
        subject="Re: Q3/Q4: Plan? \u202e",
        mime_content=mime_payload,
    )
    blank_subject_msg = SimpleNamespace(
        id="RAW-SRC-BLANK",
        item_class="IPM.Note",
        parent_folder_id=SimpleNamespace(id="FOLDER-SENT"),
        subject="  ???  ",
        mime_content=b"Subject: \r\n\r\nMinimal\r\n",
    )

    class _CountingOversizeMsg:
        id = "RAW-SRC-DECLARED-BIG"
        item_class = "IPM.Note"
        subject = "Declared oversize"
        size = 1024 * 1024 + 1
        mime_reads = 0

        @property
        def mime_content(self) -> bytes:
            self.mime_reads += 1
            return b"x" * (1024 * 1024 + 1)

    declared_big = _CountingOversizeMsg()
    understated_big = SimpleNamespace(
        id="RAW-SRC-BIG",
        item_class="IPM.Note",
        size=128,
        parent_folder_id=SimpleNamespace(id="FOLDER-INBOX"),
        subject="Huge thread",
        mime_content=b"x" * (1024 * 1024 + 1),
    )
    bcc_mime = (
        b"From: exec@corp.example\r\n"
        b"To: client@example.com\r\n"
        b"Bcc: secret1@corp.example,\r\n"
        b" secret2@corp.example,\r\n"
        b"\tsecret3@corp.example\r\n"
        b"Cc: peer@corp.example\r\n"
        b"Resent-Bcc: audit@corp.example,\r\n"
        b" compliance@corp.example\r\n"
        b"Subject: Sent with hidden recipients\r\n"
        b"MIME-Version: 1.0\r\n\r\n"
        b"Hello,\r\n"
        b"Bcc: body-line-must-remain@example.com\r\n"
    )
    bcc_msg = SimpleNamespace(
        id="RAW-SRC-BCC",
        item_class="IPM.Note",
        size=len(bcc_mime),
        parent_folder_id=SimpleNamespace(id="FOLDER-SENT"),
        subject="Sent with hidden recipients",
        mime_content=bcc_mime,
    )
    account._by_id.update({
        "RAW-EML-DRAFT": draft,
        "RAW-SRC-MSG": source_msg,
        "RAW-SRC-BLANK": blank_subject_msg,
        "RAW-SRC-DECLARED-BIG": declared_big,
        "RAW-SRC-BIG": understated_big,
        "RAW-SRC-BCC": bcc_msg,
        "RAW-SRC-GONE": ErrorItemNotFound("message deleted"),
    })
    d_alias = ctx.aliaser.alias_for("RAW-EML-DRAFT", "d")
    m_alias = ctx.aliaser.alias_for("RAW-SRC-MSG", "m")
    m_blank = ctx.aliaser.alias_for("RAW-SRC-BLANK", "m")
    m_declared_big = ctx.aliaser.alias_for("RAW-SRC-DECLARED-BIG", "m")
    m_big = ctx.aliaser.alias_for("RAW-SRC-BIG", "m")
    m_bcc = ctx.aliaser.alias_for("RAW-SRC-BCC", "m")
    m_gone = ctx.aliaser.alias_for("RAW-SRC-GONE", "m")

    # 1. Attach message as .eml: sanitised subject name, message/rfc822, exact MIME bytes.
    res = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "message_id": f" {m_alias} ",
    })
    assert res["ok"] is True
    assert res["added"]["name"] == "Re_ Q3_Q4_ Plan.eml"
    assert res["added"]["size"] == len(mime_payload)
    assert res["added"]["id"].startswith("a")
    assert len(draft.attach_calls) == 1
    attached_fa = draft.attach_calls[0]
    assert isinstance(attached_fa, FileAttachment)
    assert attached_fa.name == "Re_ Q3_Q4_ Plan.eml"
    assert attached_fa.content == mime_payload
    assert attached_fa.content_type == "message/rfc822"
    assert account.fetch_projections[-3:] == [
        writes._DRAFT_ATTACH_FIELDS,
        writes._ATTACH_MESSAGE_META_FIELDS,
        writes._ATTACH_MESSAGE_MIME_FIELDS,
    ]

    # 2. Bcc and Resent-Bcc headers (including folded lines) are stripped from
    #    the attached .eml copy while body lines stay intact.
    res_bcc = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "message_id": m_bcc,
    })
    assert res_bcc["ok"] is True
    attached_bcc = draft.attach_calls[-1].content
    assert b"secret1@corp.example" not in attached_bcc
    assert b"secret2@corp.example" not in attached_bcc
    assert b"secret3@corp.example" not in attached_bcc
    assert b"audit@corp.example" not in attached_bcc
    assert b"compliance@corp.example" not in attached_bcc
    assert b"Resent-Bcc" not in attached_bcc
    assert attached_bcc == (
        b"From: exec@corp.example\r\n"
        b"To: client@example.com\r\n"
        b"Cc: peer@corp.example\r\n"
        b"Subject: Sent with hidden recipients\r\n"
        b"MIME-Version: 1.0\r\n\r\n"
        b"Hello,\r\n"
        b"Bcc: body-line-must-remain@example.com\r\n"
    )
    assert res_bcc["added"]["size"] == len(attached_bcc)

    # Blank/punctuation-only subject falls back to message.eml.
    res_blank = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "message_id": m_blank,
    })
    assert res_blank["ok"] is True
    assert res_blank["added"]["name"] == "message.eml"
    assert draft.attach_calls[-1].content_type == "message/rfc822"

    # 3. Declared size ceiling checked BEFORE downloading MIME content; real
    #    MIME byte length also checked after download if declared size was small.
    attach_count_before = len(draft.attach_calls)
    proj_before_declared = len(account.fetch_projections)
    res_decl_big = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "message_id": m_declared_big,
    })
    assert res_decl_big["ok"] is False
    assert res_decl_big["error"]["code"] == "validation"
    assert "exceed" in res_decl_big["error"]["message"]
    assert declared_big.mime_reads == 0
    assert account.fetch_projections[proj_before_declared:] == [
        writes._DRAFT_ATTACH_FIELDS,
        writes._ATTACH_MESSAGE_META_FIELDS,
    ]
    assert len(draft.attach_calls) == attach_count_before

    res_big = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "message_id": m_big,
    })
    assert res_big["ok"] is False
    assert res_big["error"]["code"] == "validation"
    assert "exceed" in res_big["error"]["message"]
    assert len(draft.attach_calls) == attach_count_before

    # 4. Self-attach and non-mail source items refused with validation.
    res_self = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "message_id": d_alias,
    })
    assert res_self["ok"] is False
    assert res_self["error"]["code"] == "validation"
    assert len(draft.attach_calls) == attach_count_before

    CachingProtocol.clear_cache()
    try:
        real_acc = Account(
            primary_smtp_address="exec@corp.example",
            config=Configuration(
                service_endpoint="https://ews.example.com/EWS/Exchange.asmx",
                credentials=Credentials("svc", "secret"),
                auth_type=BASIC,
                version=Version(build=EXCHANGE_2016),
            ),
            autodiscover=False,
            access_type=DELEGATE,
            default_timezone=EWSTimeZone("UTC"),
        )
        non_mail_sources = [
            CalendarItem(account=real_acc, id="RAW-NM-CAL", subject="Sync"),
            MeetingRequest(account=real_acc, id="RAW-NM-REQ", subject="Invite"),
            MeetingCancellation(account=real_acc, id="RAW-NM-CAN", subject="Cancelled"),
            Contact(account=real_acc, id="RAW-NM-CON"),
            Task(account=real_acc, id="RAW-NM-TSK", subject="Todo"),
            SimpleNamespace(
                id="RAW-NM-ICS",
                subject="Board meeting",
                mime_content=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
            ),
            SimpleNamespace(
                id="RAW-NM-CLASS",
                item_class="IPM.Appointment",
                subject="Calendar class",
                mime_content=b"Subject: x\r\n\r\nx\r\n",
            ),
        ]
        for idx, nm_item in enumerate(non_mail_sources):
            rid = f"RAW-NON-MAIL-{idx}"
            account._by_id[rid] = nm_item
            nm_alias = ctx.aliaser.alias_for(rid, "e")
            r_nm = call(ctx, "add_draft_attachment", {
                "draft_id": d_alias,
                "message_id": nm_alias,
            })
            assert r_nm["ok"] is False, idx
            assert r_nm["error"]["code"] == "validation", idx
        assert len(draft.attach_calls) == attach_count_before
    finally:
        CachingProtocol.clear_cache()

    # 5. Sanitised .eml filename: format/control chars, reserved device names,
    #    and 120-char / 200-byte caps before .eml.
    assert writes._eml_filename("invoice\u202efdp.exe") == "invoicefdp.exe.eml"
    assert writes._eml_filename("pay\u200f\u061c\u200b\ufeffnow") == "paynow.eml"
    assert writes._eml_filename("abc\u009b\x07def") == "abcdef.eml"
    assert writes._eml_filename("a\u0085b\u2028c\u2029d") == "a b c d.eml"
    assert writes._eml_filename("line1\r\nline2\tend") == "line1 line2 end.eml"
    for reserved in (
        "CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9",
        "CON.txt", "con .x", "LPT1.report", "COM\u00b9", "CON.eml",
    ):
        fname = writes._eml_filename(reserved)
        assert fname.startswith("_"), (reserved, fname)
        assert fname.endswith(".eml"), (reserved, fname)
    for long_subj in ("x" * 300, "\u0645" * 200, "\U0001f600" * 200):
        fname = writes._eml_filename(long_subj)
        assert fname.endswith(".eml")
        stem = fname[:-4]
        assert 1 <= len(stem) <= 120
        assert len(stem.encode("utf-8")) <= 200
    assert writes._eml_filename("x" * 300) == ("x" * 120) + ".eml"
    assert writes._eml_filename("\u0645" * 200) == ("\u0645" * 100) + ".eml"
    assert writes._eml_filename("\U0001f600" * 200) == ("\U0001f600" * 50) + ".eml"

    # 6. Unknown message id: stale alias -> validation; missing upstream item -> not_found.
    res_stale_alias = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "message_id": "m999",
    })
    assert res_stale_alias["ok"] is False
    assert res_stale_alias["error"]["code"] == "validation"
    assert len(draft.attach_calls) == attach_count_before

    res_missing_item = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "message_id": m_gone,
    })
    assert res_missing_item["ok"] is False
    assert res_missing_item["error"]["code"] == "not_found"
    assert len(draft.attach_calls) == attach_count_before

    # 7. The three forms (content_base64, path, message_id) are mutually exclusive.
    fetches_before = len(account.fetch_projections)
    for bad_combo in (
        {"draft_id": d_alias},
        {"draft_id": d_alias, "filename": "a.txt", "content_base64": _b64(b"a"),
         "path": "doc.txt"},
        {"draft_id": d_alias, "filename": "a.txt", "content_base64": _b64(b"a"),
         "message_id": m_alias},
        {"draft_id": d_alias, "path": "doc.txt", "message_id": m_alias},
        {"draft_id": d_alias, "filename": "a.txt", "content_base64": _b64(b"a"),
         "path": "doc.txt", "message_id": m_alias},
    ):
        refused = call(ctx, "add_draft_attachment", bad_combo)
        assert refused["ok"] is False, bad_combo
        assert refused["error"]["code"] == "validation", bad_combo
    assert len(account.fetch_projections) == fetches_before
    assert len(draft.attach_calls) == attach_count_before


def test_send_draft_refuses_token_issued_before_removal_or_attached_message(
    tmp_path,
):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    draft = _StatefulDraft("RAW-SEND-BIND")
    source_msg = SimpleNamespace(
        id="RAW-PRIOR-MSG",
        item_class="IPM.Note",
        parent_folder_id=SimpleNamespace(id="FOLDER-INBOX"),
        subject="Prior approval",
        mime_content=b"From: a@example.com\r\nSubject: Prior approval\r\n\r\nYes.\r\n",
    )
    account._by_id.update({
        "RAW-SEND-BIND": draft,
        "RAW-PRIOR-MSG": source_msg,
    })
    d_alias = ctx.aliaser.alias_for("RAW-SEND-BIND", "d")
    m_alias = ctx.aliaser.alias_for("RAW-PRIOR-MSG", "m")

    call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "filename": "one.txt",
        "content_base64": _b64(b"first"),
    })
    call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "filename": "two.txt",
        "content_base64": _b64(b"second"),
    })

    # 1. Mint send token, remove an attachment, then try to confirm -> refused.
    p1_before_remove = call(ctx, "send_draft", {"draft_id": d_alias})
    assert p1_before_remove["requires_confirmation"] is True
    assert p1_before_remove["preview"]["attachment_count"] == 2

    rem = call(ctx, "update_draft", {
        "draft_id": d_alias,
        "remove_attachments": ["two.txt"],
    })
    assert rem["ok"] is True

    stale_after_remove = call(ctx, "send_draft", {
        "draft_id": d_alias,
        "confirm_token": p1_before_remove["confirm_token"],
    })
    assert stale_after_remove["ok"] is False
    assert stale_after_remove["error"]["code"] == "confirm_invalid"
    assert draft.send_calls == []

    # 2. Mint fresh send token, attach a message (.eml), then confirm -> refused.
    p1_before_eml = call(ctx, "send_draft", {"draft_id": d_alias})
    assert p1_before_eml["requires_confirmation"] is True
    assert p1_before_eml["preview"]["attachment_count"] == 1
    assert p1_before_eml["preview"]["attachments"] == [{"name": "one.txt", "size": 5}]

    added_eml = call(ctx, "add_draft_attachment", {
        "draft_id": d_alias,
        "message_id": m_alias,
    })
    assert added_eml["ok"] is True

    stale_after_eml = call(ctx, "send_draft", {
        "draft_id": d_alias,
        "confirm_token": p1_before_eml["confirm_token"],
    })
    assert stale_after_eml["ok"] is False
    assert stale_after_eml["error"]["code"] == "confirm_invalid"
    assert draft.send_calls == []

    # 3. Re-previewing after the .eml attachment binds the updated list and sends cleanly.
    p1_fresh = call(ctx, "send_draft", {"draft_id": d_alias})
    assert p1_fresh["preview"]["attachment_count"] == 2
    assert p1_fresh["preview"]["attachments"] == [
        {"name": "Prior approval.eml", "size": len(source_msg.mime_content)},
        {"name": "one.txt", "size": 5},
    ]
    sent = call(ctx, "send_draft", {
        "draft_id": d_alias,
        "confirm_token": p1_fresh["confirm_token"],
    })
    assert sent["ok"] is True and sent["sent"] is True
    assert draft.send_calls == [True]


def _cal_standin(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": "RAW-CAL-ITEM",
        "changekey": "CK-CAL",
        "item_class": "IPM.Appointment",
        "subject": "Calendar entry",
        "start": datetime(2026, 11, 20, 9, 0, tzinfo=LOCAL_TZ),
        "end": datetime(2026, 11, 20, 10, 0, tzinfo=LOCAL_TZ),
        "type": "Single",
        "is_meeting": False,
        "my_response_type": "Organizer",
        "organizer": SimpleNamespace(email_address="exec@corp.example"),
        "required_attendees": None,
        "optional_attendees": None,
        "resources": None,
    }
    base.update(overrides)
    ns = SimpleNamespace(**base)
    ns.cancel = MagicMock(name="cancel")
    ns.move_to_trash = MagicMock(name="move_to_trash")
    return ns


def test_cancel_event_plain_appointment_meeting_and_foreign_organizer(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)

    def _att(email: str) -> SimpleNamespace:
        return SimpleNamespace(mailbox=SimpleNamespace(email_address=email))

    # --- Row 1: Not a CalendarItem -> validation, nothing happens ---
    mail_fake = SimpleNamespace(
        id="RAW-NC-MAIL",
        subject="Invoice 4471",
        parent_folder_id=SimpleNamespace(id="FOLDER-INBOX"),
        cancel=MagicMock(),
        move_to_trash=MagicMock(),
    )
    draft_fake = SimpleNamespace(
        id="RAW-NC-DRAFT",
        subject="Unsent draft",
        parent_folder_id=SimpleNamespace(id=DRAFTS_ID),
        cancel=MagicMock(),
        move_to_trash=MagicMock(),
    )
    for idx, (nc_item, kind) in enumerate(((mail_fake, "m"), (draft_fake, "d"))):
        rid = f"RAW-NC-FAKE-{idx}"
        account._by_id[rid] = nc_item
        alias = ctx.aliaser.alias_for(rid, kind)
        res_nc = call(ctx, "cancel_event", {"event_id": alias})
        assert res_nc["ok"] is False
        assert res_nc["error"]["code"] == "validation"
        assert "confirm_token" not in res_nc
        nc_item.cancel.assert_not_called()
        nc_item.move_to_trash.assert_not_called()

    CachingProtocol.clear_cache()
    try:
        real_acc = Account(
            primary_smtp_address="exec@corp.example",
            config=Configuration(
                service_endpoint="https://ews.example.com/EWS/Exchange.asmx",
                credentials=Credentials("svc", "secret"),
                auth_type=BASIC,
                version=Version(build=EXCHANGE_2016),
            ),
            autodiscover=False,
            access_type=DELEGATE,
            default_timezone=EWSTimeZone("UTC"),
        )
        for idx, cls in enumerate(
            (Message, MeetingRequest, MeetingCancellation, Contact, Task)
        ):
            rid = f"RAW-NC-REAL-{idx}"
            account._by_id[rid] = cls(account=real_acc, id=rid, changekey="CK")
            alias = ctx.aliaser.alias_for(rid, "e")
            res_real_nc = call(ctx, "cancel_event", {"event_id": alias})
            assert res_real_nc["ok"] is False, cls.__name__
            assert res_real_nc["error"]["code"] == "validation", cls.__name__

        # Default-constructed real CalendarItem (my_response_type=None, is_meeting=None)
        # fails closed at Row 4 because is_meeting is missing.
        bare_cal = CalendarItem(account=real_acc, id="RAW-BARE-CAL", changekey="CK")
        account._by_id["RAW-BARE-CAL"] = bare_cal
        e_bare = ctx.aliaser.alias_for("RAW-BARE-CAL", "e")
        res_bare = call(ctx, "cancel_event", {"event_id": e_bare})
        assert res_bare["ok"] is False
        assert res_bare["error"]["code"] == "validation"
    finally:
        CachingProtocol.clear_cache()

    # --- Row 2: my_response_type == 'Organizer' ---
    # 2a. is_meeting=True with attendee lists None AND organizer as an alias of
    #     our mailbox -> ours, action='cancel' (1 projected fetch per phase).
    mtg_alias_org = _cal_standin(
        id="RAW-ROW2A",
        subject="Alias-organized meeting",
        is_meeting=True,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="abdul.alias@other-domain.example"),
        required_attendees=None,
        optional_attendees=None,
        resources=None,
    )
    account._by_id["RAW-ROW2A"] = mtg_alias_org
    e_2a = ctx.aliaser.alias_for("RAW-ROW2A", "e")
    proj_0 = len(account.fetch_projections)
    p1_2a = call(ctx, "cancel_event", {"event_id": e_2a, "message": "Moving"})
    assert p1_2a["ok"] is True and p1_2a["requires_confirmation"] is True
    assert p1_2a["preview"]["action"] == "cancel"
    assert p1_2a["preview"]["attendee_count"] == 0
    assert account.fetch_projections[proj_0:] == [writes._EVENT_CANCEL_FIELDS]
    p2_2a = call(ctx, "cancel_event", {
        "event_id": e_2a,
        "message": "Moving",
        "confirm_token": p1_2a["confirm_token"],
    })
    assert p2_2a["ok"] is True and p2_2a["cancelled"] is True
    assert account.fetch_projections[proj_0:] == [
        writes._EVENT_CANCEL_FIELDS,
        writes._EVENT_CANCEL_FIELDS,
    ]
    mtg_alias_org.cancel.assert_called_once_with(new_body="Moving")
    mtg_alias_org.move_to_trash.assert_not_called()

    # 2b. Organizer + is_meeting=False + non-empty attendee list -> cancel.
    mtg_2b = _cal_standin(
        id="RAW-ROW2B",
        is_meeting=False,
        my_response_type="Organizer",
        required_attendees=[_att("a@corp.example"), _att("b@corp.example")],
        optional_attendees=[_att("c@corp.example")],
    )
    account._by_id["RAW-ROW2B"] = mtg_2b
    e_2b = ctx.aliaser.alias_for("RAW-ROW2B", "e")
    p1_2b = call(ctx, "cancel_event", {"event_id": e_2b})
    assert p1_2b["preview"]["action"] == "cancel"
    assert p1_2b["preview"]["attendee_count"] == 3
    p2_2b = call(ctx, "cancel_event", {
        "event_id": e_2b,
        "confirm_token": p1_2b["confirm_token"],
    })
    assert p2_2b["ok"] is True and p2_2b["cancelled"] is True
    mtg_2b.cancel.assert_called_once_with()
    mtg_2b.move_to_trash.assert_not_called()

    # 2c. Organizer + is_meeting=False + empty attendee lists -> delete (trash, no notice).
    appt = _cal_standin(
        id="RAW-APPT",
        subject="Focus block",
        is_meeting=False,
        my_response_type="Organizer",
    )
    account._by_id["RAW-APPT"] = appt
    e_appt = ctx.aliaser.alias_for("RAW-APPT", "e")
    p1_appt = call(ctx, "cancel_event", {"event_id": e_appt})
    assert p1_appt["ok"] is True and p1_appt["requires_confirmation"] is True
    assert p1_appt["preview"]["event_id"] == e_appt
    assert p1_appt["preview"]["subject"] == "Focus block"
    assert p1_appt["preview"]["action"] == "delete"
    assert p1_appt["preview"]["attendee_count"] == 0
    appt.move_to_trash.assert_not_called()
    p2_appt = call(ctx, "cancel_event", {
        "event_id": e_appt,
        "confirm_token": p1_appt["confirm_token"],
    })
    assert p2_appt["ok"] is True and p2_appt["deleted"] is True
    assert "cancelled" not in p2_appt
    appt.move_to_trash.assert_called_once_with(
        send_meeting_cancellations=SEND_TO_NONE
    )
    appt.cancel.assert_not_called()

    # 2d. Organizer + is_meeting=None + empty attendees -> validation.
    row_2d = _cal_standin(
        id="RAW-ROW2D",
        is_meeting=None,
        my_response_type="Organizer",
    )
    account._by_id["RAW-ROW2D"] = row_2d
    e_2d = ctx.aliaser.alias_for("RAW-ROW2D", "e")
    res_2d = call(ctx, "cancel_event", {"event_id": e_2d})
    assert res_2d["ok"] is False and res_2d["error"]["code"] == "validation"

    # --- Row 3: Accept / Decline / Tentative / NoResponseReceived -> validation ---
    # Organizer is set to our OWN address so ONLY Row 3 can trip the refusal.
    for idx, resp_type in enumerate(
        ("Accept", "Decline", "Tentative", "NoResponseReceived")
    ):
        row3 = _cal_standin(
            id=f"RAW-ROW3-{idx}",
            is_meeting=True,
            my_response_type=resp_type,
            organizer=SimpleNamespace(email_address="exec@corp.example"),
            required_attendees=[_att("exec@corp.example")],
        )
        account._by_id[row3.id] = row3
        e_r3 = ctx.aliaser.alias_for(row3.id, "e")
        refused = call(ctx, "cancel_event", {"event_id": e_r3})
        assert refused["ok"] is False, resp_type
        assert refused["error"]["code"] == "validation", resp_type
        assert "respond_to_event" in refused["error"]["message"], resp_type
        assert "confirm_token" not in refused
        row3.cancel.assert_not_called()
        row3.move_to_trash.assert_not_called()

    # --- Row 4: my_response_type is 'Unknown' or None ---
    # 4a. Explicitly is_meeting=False and all attendee lists empty -> delete.
    for idx, unk_resp in enumerate(("Unknown", None)):
        row4a = _cal_standin(
            id=f"RAW-ROW4A-{idx}",
            is_meeting=False,
            my_response_type=unk_resp,
            organizer=None,
        )
        account._by_id[row4a.id] = row4a
        e_4a = ctx.aliaser.alias_for(row4a.id, "e")
        p1_4a = call(ctx, "cancel_event", {"event_id": e_4a})
        assert p1_4a["preview"]["action"] == "delete"
        p2_4a = call(ctx, "cancel_event", {
            "event_id": e_4a,
            "confirm_token": p1_4a["confirm_token"],
        })
        assert p2_4a["ok"] is True and p2_4a["deleted"] is True
        row4a.move_to_trash.assert_called_once_with(
            send_meeting_cancellations=SEND_TO_NONE
        )

    # 4b. Unknown + is_meeting=False + non-empty attendees -> validation (not delete).
    row4b_bad = _cal_standin(
        id="RAW-ROW4B-BAD",
        is_meeting=False,
        my_response_type="Unknown",
        required_attendees=[_att("a@corp.example")],
    )
    account._by_id[row4b_bad.id] = row4b_bad
    res_4b_bad = call(
        ctx, "cancel_event", {"event_id": ctx.aliaser.alias_for(row4b_bad.id, "e")}
    )
    assert res_4b_bad["ok"] is False and res_4b_bad["error"]["code"] == "validation"

    # 4c. Unknown + is_meeting=True + own SMTP address (upper-case / padded) -> cancel.
    row4c = _cal_standin(
        id="RAW-ROW4C",
        is_meeting=True,
        my_response_type="Unknown",
        organizer=SimpleNamespace(email_address="  EXEC@Corp.Example "),
        required_attendees=[_att("peer@corp.example")],
    )
    account._by_id[row4c.id] = row4c
    e_4c = ctx.aliaser.alias_for(row4c.id, "e")
    p1_4c = call(ctx, "cancel_event", {"event_id": e_4c})
    assert p1_4c["preview"]["action"] == "cancel"
    p2_4c = call(ctx, "cancel_event", {
        "event_id": e_4c,
        "confirm_token": p1_4c["confirm_token"],
    })
    assert p2_4c["ok"] is True and p2_4c["cancelled"] is True
    row4c.cancel.assert_called_once_with()

    # 4d. Unknown + is_meeting=True + foreign SMTP organizer -> validation (respond_to_event).
    row4d = _cal_standin(
        id="RAW-ROW4D",
        is_meeting=True,
        my_response_type="Unknown",
        organizer=SimpleNamespace(email_address="chair@example.com"),
        required_attendees=[_att("exec@corp.example")],
    )
    account._by_id[row4d.id] = row4d
    res_4d = call(
        ctx, "cancel_event", {"event_id": ctx.aliaser.alias_for(row4d.id, "e")}
    )
    assert res_4d["ok"] is False and res_4d["error"]["code"] == "validation"
    assert "respond_to_event" in res_4d["error"]["message"]

    # 4e. Unknown / None + is_meeting=True + missing or legacy EX DN organizer
    #     (both with and without attendees) -> validation, never deleted.
    legacy_dn = SimpleNamespace(
        email_address="/O=CORP/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=CHAIR",
        routing_type="EX",
    )
    for idx, (unk_resp, org_val, req_list) in enumerate((
        ("Unknown", legacy_dn, [_att("exec@corp.example")]),
        ("Unknown", legacy_dn, None),
        (None, None, [_att("x@corp.example")]),
        (None, None, None),
    )):
        row4e = _cal_standin(
            id=f"RAW-ROW4E-{idx}",
            is_meeting=True,
            my_response_type=unk_resp,
            organizer=org_val,
            required_attendees=req_list,
        )
        account._by_id[row4e.id] = row4e
        res_4e = call(
            ctx, "cancel_event", {"event_id": ctx.aliaser.alias_for(row4e.id, "e")}
        )
        assert res_4e["ok"] is False, idx
        assert res_4e["error"]["code"] == "validation", idx
        row4e.cancel.assert_not_called()
        row4e.move_to_trash.assert_not_called()

    # 4f. Unknown / None + is_meeting=None -> validation.
    row4f = _cal_standin(
        id="RAW-ROW4F",
        is_meeting=None,
        my_response_type="Unknown",
    )
    account._by_id[row4f.id] = row4f
    res_4f = call(
        ctx, "cancel_event", {"event_id": ctx.aliaser.alias_for(row4f.id, "e")}
    )
    assert res_4f["ok"] is False and res_4f["error"]["code"] == "validation"

    # --- Token binding: time shift, appointment->meeting, series, recipient guard ---
    # 5. Moving start/end by 30 days between phases invalidates BOTH meeting and
    #    plain-appointment tokens.
    for idx, is_mtg in enumerate((True, False)):
        ev_shift = _cal_standin(
            id=f"RAW-SHIFT-{idx}",
            is_meeting=is_mtg,
            required_attendees=[_att("peer@corp.example")] if is_mtg else None,
        )
        account._by_id[ev_shift.id] = ev_shift
        e_shift = ctx.aliaser.alias_for(ev_shift.id, "e")
        p1_s = call(ctx, "cancel_event", {"event_id": e_shift})
        ev_shift.start = ev_shift.start + timedelta(days=30)
        ev_shift.end = ev_shift.end + timedelta(days=30)
        p2_s = call(ctx, "cancel_event", {
            "event_id": e_shift,
            "confirm_token": p1_s["confirm_token"],
        })
        assert p2_s["ok"] is False, is_mtg
        assert p2_s["error"]["code"] == "confirm_invalid", is_mtg
        ev_shift.cancel.assert_not_called()
        ev_shift.move_to_trash.assert_not_called()

    # 6. Turning an appointment into a meeting or changing type to RecurringMaster
    #    between phases invalidates the token.
    appt2 = _cal_standin(id="RAW-APPT-TOCTOU", subject="Hold slot")
    account._by_id["RAW-APPT-TOCTOU"] = appt2
    e_toctou = ctx.aliaser.alias_for("RAW-APPT-TOCTOU", "e")
    p1_toctou = call(ctx, "cancel_event", {"event_id": e_toctou})
    assert p1_toctou["preview"]["action"] == "delete"
    appt2.is_meeting = True
    appt2.required_attendees = [_att("guest@example.com")]
    p2_toctou = call(ctx, "cancel_event", {
        "event_id": e_toctou,
        "confirm_token": p1_toctou["confirm_token"],
    })
    assert p2_toctou["ok"] is False and p2_toctou["error"]["code"] == "confirm_invalid"
    appt2.cancel.assert_not_called()
    appt2.move_to_trash.assert_not_called()

    appt_type = _cal_standin(id="RAW-TYPE-FLIP", type="Single")
    account._by_id["RAW-TYPE-FLIP"] = appt_type
    e_type = ctx.aliaser.alias_for("RAW-TYPE-FLIP", "e")
    p1_type = call(ctx, "cancel_event", {"event_id": e_type})
    appt_type.type = "RecurringMaster"
    p2_type = call(ctx, "cancel_event", {
        "event_id": e_type,
        "confirm_token": p1_type["confirm_token"],
    })
    assert p2_type["ok"] is False and p2_type["error"]["code"] == "confirm_invalid"
    appt_type.move_to_trash.assert_not_called()

    # 7. Recurring series master explicitly flags whole-series cancellation in preview.
    series_master = _cal_standin(
        id="RAW-SERIES-MASTER",
        subject="Weekly standup",
        type="RecurringMaster",
        is_meeting=True,
        required_attendees=[_att("peer@corp.example")],
    )
    account._by_id["RAW-SERIES-MASTER"] = series_master
    e_master = ctx.aliaser.alias_for("RAW-SERIES-MASTER", "e")
    p1_master = call(ctx, "cancel_event", {"event_id": e_master})
    assert p1_master["preview"]["type"] == "RecurringMaster"
    assert p1_master["preview"]["series"] is True
    assert p1_master["preview"]["scope"] == "series"
    assert "entire series" in p1_master["preview"]["note"]

    # 8. Recipient guard blocks denylisted attendees; external attendees emit warnings.
    ext_mtg = _cal_standin(
        id="RAW-EXT-MTG",
        is_meeting=True,
        required_attendees=[_att("peer@corp.example"), _att("guest@partner.example")],
    )
    account._by_id["RAW-EXT-MTG"] = ext_mtg
    e_ext = ctx.aliaser.alias_for("RAW-EXT-MTG", "e")
    p1_ext = call(ctx, "cancel_event", {"event_id": e_ext})
    assert p1_ext["ok"] is True and p1_ext["requires_confirmation"] is True
    assert p1_ext["warnings"] == ["external recipients: guest@partner.example"]

    ctx_deny = full_ctx(
        tmp_path / "deny",
        account,
        ews_recipient_denylist="*@blocked.example",
    )
    blocked_mtg = _cal_standin(
        id="RAW-BLOCKED-MTG",
        is_meeting=True,
        required_attendees=[_att("ceo@blocked.example"), _att("peer@corp.example")],
    )
    account._by_id["RAW-BLOCKED-MTG"] = blocked_mtg
    e_blocked = ctx_deny.aliaser.alias_for("RAW-BLOCKED-MTG", "e")
    res_blocked = call(ctx_deny, "cancel_event", {
        "event_id": e_blocked,
        "message": "Cancelled",
    })
    assert res_blocked["ok"] is False
    assert res_blocked["error"]["code"] == "recipient_blocked"
    assert "confirm_token" not in res_blocked
    blocked_mtg.cancel.assert_not_called()


# --- cancel_event send kill-switch ----------------------------------------------


def test_cancel_event_kill_switch_blocks_meeting_at_phase_one(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account, send_enabled=False, ews_capability_tier="full")
    item = _cal_standin(
        id="RAW-KS-MTG",
        subject="Team review",
        is_meeting=True,
        required_attendees=[
            SimpleNamespace(mailbox=SimpleNamespace(email_address="peer@corp.example"))
        ],
    )
    account._by_id["RAW-KS-MTG"] = item
    alias = ctx.aliaser.alias_for("RAW-KS-MTG", "e")

    res = call(ctx, "cancel_event", {"event_id": alias, "message": "Rescheduling"})
    assert res["ok"] is False
    assert res["error"]["code"] == "kill_switch"
    assert res["error"]["message"] == (
        "cancel_event is blocked: notifying attendees sends mail and "
        "SEND_ENABLED=false on this server."
    )
    assert res["error"]["hint"] == (
        "Cancel the meeting from Outlook, or have the operator flip SEND_ENABLED."
    )
    assert "confirm_token" not in res
    item.cancel.assert_not_called()
    item.move_to_trash.assert_not_called()


def test_cancel_event_with_send_enabled_previews_then_cancels(tmp_path):
    account = make_account()
    ctx = full_ctx(tmp_path, account)
    item = _cal_standin(
        id="RAW-KS-ON",
        subject="Team review",
        is_meeting=True,
        required_attendees=[
            SimpleNamespace(mailbox=SimpleNamespace(email_address="peer@corp.example"))
        ],
    )
    account._by_id["RAW-KS-ON"] = item
    alias = ctx.aliaser.alias_for("RAW-KS-ON", "e")
    kwargs = {"event_id": alias, "message": "Rescheduling"}

    p1 = call(ctx, "cancel_event", kwargs)
    assert p1["ok"] is True and p1["requires_confirmation"] is True
    assert p1["preview"]["action"] == "cancel"
    item.cancel.assert_not_called()

    p2 = call(ctx, "cancel_event", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is True and p2["cancelled"] is True
    item.cancel.assert_called_once_with(new_body="Rescheduling")
    item.move_to_trash.assert_not_called()


def test_cancel_event_plain_appointment_deletes_when_send_disabled(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account, send_enabled=False, ews_capability_tier="full")
    appt = _cal_standin(
        id="RAW-KS-APPT",
        subject="Focus time",
        is_meeting=False,
        my_response_type="Organizer",
    )
    account._by_id["RAW-KS-APPT"] = appt
    alias = ctx.aliaser.alias_for("RAW-KS-APPT", "e")

    p1 = call(ctx, "cancel_event", {"event_id": alias})
    assert p1["ok"] is True and p1["requires_confirmation"] is True
    assert p1["preview"]["action"] == "delete"
    appt.move_to_trash.assert_not_called()

    p2 = call(ctx, "cancel_event", {
        "event_id": alias,
        "confirm_token": p1["confirm_token"],
    })
    assert p2["ok"] is True and p2["deleted"] is True
    appt.move_to_trash.assert_called_once_with(
        send_meeting_cancellations=SEND_TO_NONE
    )
    appt.cancel.assert_not_called()


def test_cancel_event_phase_two_refused_when_send_disabled(tmp_path):
    account = make_account()
    secret = "shared-confirm-secret-for-phase2-test"
    ctx_on = make_ctx(
        tmp_path / "on",
        account,
        send_enabled=True,
        ews_capability_tier="full",
        send_confirm_secret=secret,
    )
    ctx_off = Context(
        settings=make_settings(
            ews_tz=FIXED_OFFSET_TZ,
            send_enabled=False,
            ews_capability_tier="full",
            send_confirm_secret=secret,
        ),
        gateway=FakeGateway(account),
        manager=None,
        aliaser=ctx_on.aliaser,
        audit=AuditLog(str(tmp_path / "off-audit")),
    )
    item = _cal_standin(
        id="RAW-KS-P2",
        subject="Quarterly sync",
        is_meeting=True,
        required_attendees=[
            SimpleNamespace(mailbox=SimpleNamespace(email_address="peer@corp.example"))
        ],
    )
    account._by_id["RAW-KS-P2"] = item
    alias = ctx_on.aliaser.alias_for("RAW-KS-P2", "e")
    kwargs = {"event_id": alias, "message": "Cancelled"}

    p1 = call(ctx_on, "cancel_event", kwargs)
    assert p1["ok"] is True and p1["requires_confirmation"] is True

    p2 = call(ctx_off, "cancel_event", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is False
    assert p2["error"]["code"] == "kill_switch"
    assert "SEND_ENABLED=false" in p2["error"]["message"]
    item.cancel.assert_not_called()
    item.move_to_trash.assert_not_called()

    # Handler defence-in-depth on the freshly fetched entry when invoked directly.
    content = asyncio.run(writes._cancel_event_preview(ctx_on, dict(kwargs)))
    writes._PENDING_CANCEL.set(None)
    with pytest.raises(writes.ToolError) as exc_info:
        asyncio.run(
            writes._cancel_event(
                ctx_off,
                event_id="RAW-KS-P2",
                message="Cancelled",
                _verified_content_hash=writes.preview_content_hash(content),
            )
        )
    assert exc_info.value.code == "kill_switch"
    item.cancel.assert_not_called()


def test_cancel_event_decision_table_validation_precedes_kill_switch(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account, send_enabled=False, ews_capability_tier="full")
    foreign = _cal_standin(
        id="RAW-KS-FOREIGN",
        subject="External sync",
        is_meeting=True,
        my_response_type="Accept",
        organizer=SimpleNamespace(email_address="chair@example.com"),
        required_attendees=[
            SimpleNamespace(mailbox=SimpleNamespace(email_address="exec@corp.example"))
        ],
    )
    non_cal = SimpleNamespace(
        id="RAW-KS-MAIL",
        subject="Invoice",
        parent_folder_id=SimpleNamespace(id="FOLDER-INBOX"),
        cancel=MagicMock(),
        move_to_trash=MagicMock(),
    )
    account._by_id["RAW-KS-FOREIGN"] = foreign
    account._by_id["RAW-KS-MAIL"] = non_cal

    res_foreign = call(
        ctx, "cancel_event", {"event_id": ctx.aliaser.alias_for("RAW-KS-FOREIGN", "e")}
    )
    assert res_foreign["ok"] is False
    assert res_foreign["error"]["code"] == "validation"
    assert "respond_to_event" in res_foreign["error"]["message"]
    assert "confirm_token" not in res_foreign
    foreign.cancel.assert_not_called()

    res_non_cal = call(
        ctx, "cancel_event", {"event_id": ctx.aliaser.alias_for("RAW-KS-MAIL", "m")}
    )
    assert res_non_cal["ok"] is False
    assert res_non_cal["error"]["code"] == "validation"
    assert "confirm_token" not in res_non_cal
    non_cal.cancel.assert_not_called()


def test_cancel_event_modified_occurrence_class_cancel_delete_and_foreign(tmp_path):
    mod_cls = "IPM.OLE.CLASS.{00061055-0000-0000-C000-000000000046}"
    account = make_account()
    ctx_on = make_ctx(
        tmp_path / "on",
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
    )
    ctx_off = make_ctx(
        tmp_path / "off",
        account,
        ews_email="organizer@example.com",
        send_enabled=False,
        ews_capability_tier="full",
    )

    # 1. Modified occurrence organised by us with attendees: action 'cancel' when
    #    sending is enabled, and refused with kill_switch when SEND_ENABLED=false.
    ours_mtg = _cal_standin(
        id="RAW-MOD-OCC-MTG",
        item_class=mod_cls,
        type="Exception",
        is_meeting=True,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="organizer@example.com"),
        required_attendees=[
            SimpleNamespace(
                mailbox=SimpleNamespace(email_address="colleague@example.com")
            )
        ],
    )
    account._by_id["RAW-MOD-OCC-MTG"] = ours_mtg
    e_on = ctx_on.aliaser.alias_for("RAW-MOD-OCC-MTG", "e")
    e_off = ctx_off.aliaser.alias_for("RAW-MOD-OCC-MTG", "e")

    ks = call(ctx_off, "cancel_event", {"event_id": e_off})
    assert ks["ok"] is False
    assert ks["error"]["code"] == "kill_switch"
    assert "confirm_token" not in ks
    ours_mtg.cancel.assert_not_called()

    p1_mtg = call(ctx_on, "cancel_event", {"event_id": e_on, "message": "Moved"})
    assert p1_mtg["ok"] is True and p1_mtg["requires_confirmation"] is True
    assert p1_mtg["preview"]["action"] == "cancel"
    assert p1_mtg["preview"]["type"] == "Exception"
    p2_mtg = call(ctx_on, "cancel_event", {
        "event_id": e_on,
        "message": "Moved",
        "confirm_token": p1_mtg["confirm_token"],
    })
    assert p2_mtg["ok"] is True and p2_mtg["cancelled"] is True
    ours_mtg.cancel.assert_called_once_with(new_body="Moved")

    # 2. Modified occurrence organised by someone else: foreign-organizer validation,
    #    not "not a calendar event".
    foreign_occ = _cal_standin(
        id="RAW-MOD-OCC-FOREIGN",
        item_class=mod_cls,
        type="Exception",
        is_meeting=True,
        my_response_type="Accept",
        organizer=SimpleNamespace(email_address="chair@example.org"),
        required_attendees=[
            SimpleNamespace(
                mailbox=SimpleNamespace(email_address="organizer@example.com")
            )
        ],
    )
    account._by_id["RAW-MOD-OCC-FOREIGN"] = foreign_occ
    res_foreign = call(
        ctx_on,
        "cancel_event",
        {"event_id": ctx_on.aliaser.alias_for("RAW-MOD-OCC-FOREIGN", "e")},
    )
    assert res_foreign["ok"] is False
    assert res_foreign["error"]["code"] == "validation"
    assert "organized by someone else" in res_foreign["error"]["message"]
    assert "not a calendar event" not in res_foreign["error"]["message"]
    foreign_occ.cancel.assert_not_called()

    # 3. Modified occurrence on a plain appointment without attendees: action 'delete'.
    ours_appt = _cal_standin(
        id="RAW-MOD-OCC-APPT",
        item_class=mod_cls,
        type="Exception",
        is_meeting=False,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="organizer@example.com"),
    )
    account._by_id["RAW-MOD-OCC-APPT"] = ours_appt
    e_appt = ctx_off.aliaser.alias_for("RAW-MOD-OCC-APPT", "e")
    p1_appt = call(ctx_off, "cancel_event", {"event_id": e_appt})
    assert p1_appt["ok"] is True and p1_appt["requires_confirmation"] is True
    assert p1_appt["preview"]["action"] == "delete"
    p2_appt = call(ctx_off, "cancel_event", {
        "event_id": e_appt,
        "confirm_token": p1_appt["confirm_token"],
    })
    assert p2_appt["ok"] is True and p2_appt["deleted"] is True
    ours_appt.move_to_trash.assert_called_once_with(
        send_meeting_cancellations=SEND_TO_NONE
    )

    # 4. Lower-case (and whitespace-padded) class spelling is accepted.
    lower_occ = _cal_standin(
        id="RAW-MOD-OCC-LOWER",
        item_class=f"  {mod_cls.lower()}  ",
        type="Exception",
        is_meeting=True,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="organizer@example.com"),
        required_attendees=[
            SimpleNamespace(
                mailbox=SimpleNamespace(email_address="colleague@example.com")
            )
        ],
    )
    account._by_id["RAW-MOD-OCC-LOWER"] = lower_occ
    e_lower = ctx_on.aliaser.alias_for("RAW-MOD-OCC-LOWER", "e")
    p1_lower = call(ctx_on, "cancel_event", {"event_id": e_lower})
    assert p1_lower["ok"] is True and p1_lower["requires_confirmation"] is True
    assert p1_lower["preview"]["action"] == "cancel"


@pytest.mark.parametrize("bad_class", [
    "IPM.OLE.CLASS.{00000000-0000-0000-0000-000000000000}",
    "IPM.OLE.CLASS.{00061055-0000-0000-C000-000000000046}.Extra",
    "IPM.Note",
    "IPM.Schedule.Meeting.Request",
])
def test_cancel_event_refuses_other_non_appointment_item_classes(tmp_path, bad_class):
    account = make_account()
    ctx = make_ctx(
        tmp_path,
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
    )
    bad_item = _cal_standin(
        id="RAW-BAD-CLASS",
        item_class=bad_class,
        type="Exception",
        is_meeting=True,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="organizer@example.com"),
        required_attendees=[
            SimpleNamespace(
                mailbox=SimpleNamespace(email_address="colleague@example.com")
            )
        ],
    )
    account._by_id["RAW-BAD-CLASS"] = bad_item
    res = call(
        ctx, "cancel_event", {"event_id": ctx.aliaser.alias_for("RAW-BAD-CLASS", "e")}
    )
    assert res["ok"] is False
    assert res["error"]["code"] == "validation"
    assert "not a calendar event" in res["error"]["message"]
    assert "confirm_token" not in res
    bad_item.cancel.assert_not_called()
    bad_item.move_to_trash.assert_not_called()


# --- round 2: unified notifying gates (update_event preview, hourly cap, handlers) ---


def _att_box(email: str) -> SimpleNamespace:
    return SimpleNamespace(mailbox=SimpleNamespace(email_address=email))


def _updatable_event(
    raw_id: str,
    *,
    subject: str = "Initial review",
    required: tuple[str, ...] = ("colleague@example.com",),
    optional: tuple[str, ...] = (),
    resources: tuple[str, ...] = (),
) -> SimpleNamespace:
    ev = SimpleNamespace(
        id=raw_id,
        changekey="CK-UPD",
        item_class="IPM.Appointment",
        subject=subject,
        start=datetime(2026, 11, 20, 9, 0, tzinfo=LOCAL_TZ),
        end=datetime(2026, 11, 20, 10, 0, tzinfo=LOCAL_TZ),
        location="Room A",
        type="Single",
        is_meeting=True,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="organizer@example.com"),
        required_attendees=[_att_box(a) for a in required] if required else None,
        optional_attendees=[_att_box(a) for a in optional] if optional else None,
        resources=[_att_box(a) for a in resources] if resources else None,
    )
    ev.save = MagicMock(name="save")
    return ev


def test_update_event_notify_preview_recipient_guard_warnings_and_toctou(tmp_path):
    account = make_account()
    ev_blocked = _updatable_event(
        "RAW-UPD-ALLOW",
        required=("colleague@example.com",),
        optional=("guest@example.org",),
    )
    account._by_id["RAW-UPD-ALLOW"] = ev_blocked
    ctx_allow = make_ctx(
        tmp_path / "allow",
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_recipient_allowlist="*@example.com",
    )
    e_allow = ctx_allow.aliaser.alias_for("RAW-UPD-ALLOW", "e")
    blocked = call(ctx_allow, "update_event", {
        "event_id": e_allow,
        "subject": "Updated review",
        "notify_attendees": True,
    })
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "recipient_blocked"
    assert "confirm_token" not in blocked
    ev_blocked.save.assert_not_called()

    # Open lists: preview shows stored required/optional/resource attendees + external warning.
    ctx = make_ctx(
        tmp_path / "open",
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
    )
    ev_ok = _updatable_event(
        "RAW-UPD-OK",
        required=("colleague@example.com",),
        optional=("guest@example.org",),
        resources=("room@example.com",),
    )
    account._by_id["RAW-UPD-OK"] = ev_ok
    e_ok = ctx.aliaser.alias_for("RAW-UPD-OK", "e")
    kwargs = {
        "event_id": e_ok,
        "subject": "Updated review",
        "notify_attendees": True,
    }
    proj_before = len(account.fetch_projections)
    p1 = call(ctx, "update_event", kwargs)
    assert p1["ok"] is True and p1["requires_confirmation"] is True
    assert p1["preview"]["attendees"] == [
        "colleague@example.com",
        "guest@example.org",
        "room@example.com",
    ]
    assert p1["preview"]["changes"] == {"subject": "Updated review"}
    assert p1["warnings"] == ["external recipients: guest@example.org"]
    assert account.fetch_projections[proj_before:] == [None]
    ev_ok.save.assert_not_called()

    p2 = call(ctx, "update_event", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is True
    assert p2["updated_fields"] == ["subject"]
    assert p2["attendees_notified"] is True
    assert account.fetch_projections[proj_before:] == [None, None, None]
    ev_ok.save.assert_called_once_with(
        update_fields=["subject"],
        send_meeting_invitations=SEND_TO_CHANGED_AND_SAVE_COPY,
    )

    # Stored attendees changed between phases -> confirm_invalid (stale), nothing saved.
    ev_stale = _updatable_event(
        "RAW-UPD-STALE",
        required=("colleague@example.com",),
    )
    account._by_id["RAW-UPD-STALE"] = ev_stale
    e_stale = ctx.aliaser.alias_for("RAW-UPD-STALE", "e")
    stale_kwargs = {
        "event_id": e_stale,
        "subject": "Moved slot",
        "notify_attendees": True,
    }
    p1_stale = call(ctx, "update_event", stale_kwargs)
    assert p1_stale["requires_confirmation"] is True
    ev_stale.optional_attendees = [_att_box("added@example.org")]
    p2_stale = call(ctx, "update_event", {
        **stale_kwargs,
        "confirm_token": p1_stale["confirm_token"],
    })
    assert p2_stale["ok"] is False
    assert p2_stale["error"]["code"] == "confirm_invalid"
    assert "stale" in p2_stale["error"]["message"]
    ev_stale.save.assert_not_called()

    # Handler-level TOCTOU check on its own fetch (between phase-2 preview and handler).
    seq_p1 = _updatable_event("RAW-UPD-SEQ", required=("colleague@example.com",))
    seq_p2 = _updatable_event("RAW-UPD-SEQ", required=("colleague@example.com",))
    seq_handler = _updatable_event(
        "RAW-UPD-SEQ",
        required=("colleague@example.com", "late@example.org"),
    )
    seq_objs = [seq_p1, seq_p2, seq_handler]
    seq_account = SimpleNamespace(
        drafts=SimpleNamespace(id=DRAFTS_ID),
        calendar=SimpleNamespace(id="FOLDER-CAL"),
    )
    seq_account.fetch = lambda pairs, only_fields=None: [seq_objs.pop(0) for _ in pairs]
    ctx_seq = make_ctx(
        tmp_path / "seq",
        seq_account,
        ews_email="organizer@example.com",
        send_enabled=True,
    )
    e_seq = ctx_seq.aliaser.alias_for("RAW-UPD-SEQ", "e")
    seq_kw = {"event_id": e_seq, "subject": "Moved", "notify_attendees": True}
    tok_seq = call(ctx_seq, "update_event", seq_kw)["confirm_token"]
    refused_seq = call(ctx_seq, "update_event", {**seq_kw, "confirm_token": tok_seq})
    assert refused_seq["ok"] is False
    assert refused_seq["error"]["code"] == "confirm_invalid"
    seq_handler.save.assert_not_called()


def test_notifying_calendar_calls_share_hourly_send_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(writes, "CalendarItem", FakeCalendarItem)
    account = make_account()
    ctx = make_ctx(
        tmp_path,
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
        ews_max_sends_per_hour=1,
    )

    draft = make_content_draft(
        subject="Update",
        to=("colleague@example.com",),
        body="Ready for review",
    )
    account._by_id["RAW-CAP-D1"] = draft
    d1 = ctx.aliaser.alias_for("RAW-CAP-D1", "d")

    appt_pre = _cal_standin(
        id="RAW-CAP-APPT-1",
        subject="Hold",
        is_meeting=False,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="organizer@example.com"),
    )
    account._by_id["RAW-CAP-APPT-1"] = appt_pre
    e_appt_pre = ctx.aliaser.alias_for("RAW-CAP-APPT-1", "e")

    # Non-sending create_event and plain-appointment cancel_event do NOT consume the 1/h slot.
    FakeCalendarItem.last = None
    no_inv = call(ctx, "create_event", {
        "subject": "Focus",
        "start": "2026-07-01T09:00",
        "end": "2026-07-01T10:00",
        "attendees": ["colleague@example.com"],
        "send_invitations": False,
    })
    assert no_inv["ok"] is True
    tok_appt_pre = call(ctx, "cancel_event", {"event_id": e_appt_pre})["confirm_token"]
    del_pre = call(ctx, "cancel_event", {
        "event_id": e_appt_pre,
        "confirm_token": tok_appt_pre,
    })
    assert del_pre["ok"] is True and del_pre["deleted"] is True

    # Confirmed send_draft still succeeds and fills the 1/hour cap.
    tok_d1 = call(ctx, "send_draft", {"draft_id": d1})["confirm_token"]
    sent_d1 = call(ctx, "send_draft", {"draft_id": d1, "confirm_token": tok_d1})
    assert sent_d1["ok"] is True and sent_d1["sent"] is True

    # 1. create_event(send_invitations=True) is refused with rate_capped + retry_after_s.
    FakeCalendarItem.last = None
    ce_kw = {
        "subject": "Kickoff",
        "start": "2026-07-02T10:00",
        "end": "2026-07-02T11:00",
        "attendees": ["colleague@example.com"],
        "send_invitations": True,
    }
    tok_ce = call(ctx, "create_event", ce_kw)["confirm_token"]
    capped_ce = call(ctx, "create_event", {**ce_kw, "confirm_token": tok_ce})
    assert capped_ce["ok"] is False
    assert capped_ce["error"]["code"] == "rate_capped"
    assert capped_ce["error"]["retry_after_s"] > 0
    assert FakeCalendarItem.last is None

    # 2. update_event(notify_attendees=True) is refused with rate_capped + retry_after_s.
    ev_upd = _updatable_event("RAW-CAP-UPD", required=("colleague@example.com",))
    account._by_id["RAW-CAP-UPD"] = ev_upd
    e_upd = ctx.aliaser.alias_for("RAW-CAP-UPD", "e")
    ue_kw = {"event_id": e_upd, "subject": "Rescheduled", "notify_attendees": True}
    tok_ue = call(ctx, "update_event", ue_kw)["confirm_token"]
    capped_ue = call(ctx, "update_event", {**ue_kw, "confirm_token": tok_ue})
    assert capped_ue["ok"] is False
    assert capped_ue["error"]["code"] == "rate_capped"
    assert capped_ue["error"]["retry_after_s"] > 0
    ev_upd.save.assert_not_called()

    # 3. cancel_event on a meeting (action='cancel') is refused with rate_capped + retry_after_s.
    mtg = _cal_standin(
        id="RAW-CAP-MTG",
        subject="Sync",
        is_meeting=True,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="organizer@example.com"),
        required_attendees=[_att_box("colleague@example.com")],
    )
    account._by_id["RAW-CAP-MTG"] = mtg
    e_mtg = ctx.aliaser.alias_for("RAW-CAP-MTG", "e")
    can_kw = {"event_id": e_mtg, "message": "Postponed"}
    tok_can = call(ctx, "cancel_event", can_kw)["confirm_token"]
    capped_can = call(ctx, "cancel_event", {**can_kw, "confirm_token": tok_can})
    assert capped_can["ok"] is False
    assert capped_can["error"]["code"] == "rate_capped"
    assert capped_can["error"]["retry_after_s"] > 0
    mtg.cancel.assert_not_called()

    # 4. While the hour is full, non-notifying create_event and plain-appointment delete work.
    no_inv_full = call(ctx, "create_event", {
        "subject": "Solo work",
        "start": "2026-07-03T09:00",
        "end": "2026-07-03T10:00",
        "send_invitations": False,
    })
    assert no_inv_full["ok"] is True

    appt_full = _cal_standin(
        id="RAW-CAP-APPT-2",
        subject="Solo hold",
        is_meeting=False,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="organizer@example.com"),
    )
    account._by_id["RAW-CAP-APPT-2"] = appt_full
    e_appt_full = ctx.aliaser.alias_for("RAW-CAP-APPT-2", "e")
    tok_appt_full = call(ctx, "cancel_event", {"event_id": e_appt_full})["confirm_token"]
    del_full = call(ctx, "cancel_event", {
        "event_id": e_appt_full,
        "confirm_token": tok_appt_full,
    })
    assert del_full["ok"] is True and del_full["deleted"] is True

    # 5. Each of the three rate-capped tokens remained valid and itself counts 1 slot on retry.
    draft2 = make_content_draft(
        subject="Followup",
        to=("colleague@example.com",),
        body="Ping",
    )
    account._by_id["RAW-CAP-D2"] = draft2
    d2 = ctx.aliaser.alias_for("RAW-CAP-D2", "d")
    tok_d2 = call(ctx, "send_draft", {"draft_id": d2})["confirm_token"]

    for name, kw, saved_tok in (
        ("create_event", ce_kw, tok_ce),
        ("update_event", ue_kw, tok_ue),
        ("cancel_event", can_kw, tok_can),
    ):
        reset_send_rate_window()
        ok_retry = call(ctx, name, {**kw, "confirm_token": saved_tok})
        assert ok_retry["ok"] is True, name
        # That calendar notification consumed the 1/hour slot, blocking send_draft.
        blocked_d2 = call(ctx, "send_draft", {"draft_id": d2, "confirm_token": tok_d2})
        assert blocked_d2["ok"] is False and blocked_d2["error"]["code"] == "rate_capped", name


def test_notifying_event_handlers_enforce_kill_switch_directly(tmp_path, monkeypatch):
    monkeypatch.setattr(writes, "CalendarItem", FakeCalendarItem)
    FakeCalendarItem.last = None
    account = make_account()
    ctx_on = make_ctx(
        tmp_path / "on",
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
    )
    ctx_off = make_ctx(
        tmp_path / "off",
        account,
        ews_email="organizer@example.com",
        send_enabled=False,
        ews_capability_tier="full",
    )

    # 1. _create_event called directly with send_invitations=True and send_enabled=False.
    with pytest.raises(writes.ToolError) as ce_exc:
        asyncio.run(
            writes._create_event(
                ctx_off,
                subject="Board",
                start="2026-07-01T10:00",
                end="2026-07-01T11:00",
                attendees=["colleague@example.com"],
                send_invitations=True,
            )
        )
    assert ce_exc.value.code == "kill_switch"
    assert FakeCalendarItem.last is None

    # 2. _update_event called directly with notify_attendees=True and send_enabled=False.
    ev = _updatable_event("RAW-DIRECT-UPD", required=("colleague@example.com",))
    account._by_id["RAW-DIRECT-UPD"] = ev
    upd_content = asyncio.run(
        writes._update_event_preview(
            ctx_on,
            {"event_id": "RAW-DIRECT-UPD", "subject": "Moved", "notify_attendees": True},
        )
    )
    with pytest.raises(writes.ToolError) as ue_exc:
        asyncio.run(
            writes._update_event(
                ctx_off,
                event_id="RAW-DIRECT-UPD",
                subject="Moved",
                notify_attendees=True,
                _verified_content_hash=writes.preview_content_hash(upd_content),
            )
        )
    assert ue_exc.value.code == "kill_switch"
    ev.save.assert_not_called()

    # 3. _cancel_event called directly on a meeting with send_enabled=False.
    mtg = _cal_standin(
        id="RAW-DIRECT-CAN",
        subject="Review",
        is_meeting=True,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="organizer@example.com"),
        required_attendees=[_att_box("colleague@example.com")],
    )
    account._by_id["RAW-DIRECT-CAN"] = mtg
    can_content = asyncio.run(
        writes._cancel_event_preview(
            ctx_on, {"event_id": "RAW-DIRECT-CAN", "message": "Cancelled"}
        )
    )
    writes._PENDING_CANCEL.set(None)
    with pytest.raises(writes.ToolError) as can_exc:
        asyncio.run(
            writes._cancel_event(
                ctx_off,
                event_id="RAW-DIRECT-CAN",
                message="Cancelled",
                _verified_content_hash=writes.preview_content_hash(can_content),
            )
        )
    assert can_exc.value.code == "kill_switch"
    mtg.cancel.assert_not_called()
    mtg.move_to_trash.assert_not_called()


# --- round 4: guard mutation pins & regressions ------------------------------


def test_kill_switch_hints_fit_each_notifying_calendar_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(writes, "CalendarItem", FakeCalendarItem)
    account = make_account()
    ctx = make_ctx(
        tmp_path,
        account,
        ews_email="organizer@example.com",
        send_enabled=False,
        ews_capability_tier="full",
    )
    ev = _updatable_event("RAW-KS-HINT-UPD", required=("colleague@example.com",))
    mtg = _cal_standin(
        id="RAW-KS-HINT-CAN",
        subject="Planning",
        is_meeting=True,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="organizer@example.com"),
        required_attendees=[_att_box("colleague@example.com")],
    )
    account._by_id["RAW-KS-HINT-UPD"] = ev
    account._by_id["RAW-KS-HINT-CAN"] = mtg

    ce = call(ctx, "create_event", {
        "subject": "Sync",
        "start": "2026-11-20T09:00",
        "end": "2026-11-20T10:00",
        "attendees": ["colleague@example.com"],
        "send_invitations": True,
    })
    ue = call(ctx, "update_event", {
        "event_id": ctx.aliaser.alias_for("RAW-KS-HINT-UPD", "e"),
        "subject": "Moved",
        "notify_attendees": True,
    })
    can = call(ctx, "cancel_event", {
        "event_id": ctx.aliaser.alias_for("RAW-KS-HINT-CAN", "e"),
    })
    sd = call(ctx, "send_draft", {"draft_id": "RAW-D1"})

    expected_edit_hint = (
        "Run without notifying attendees, or have the operator flip SEND_ENABLED."
    )
    expected_cancel_hint = (
        "Cancel the meeting from Outlook, or have the operator flip SEND_ENABLED."
    )
    expected_send_hint = (
        "Create a draft instead; sending requires the operator to flip SEND_ENABLED."
    )
    assert ce["error"]["code"] == "kill_switch"
    assert ce["error"]["hint"] == expected_edit_hint
    assert ue["error"]["code"] == "kill_switch"
    assert ue["error"]["hint"] == expected_edit_hint
    assert can["error"]["code"] == "kill_switch"
    assert can["error"]["hint"] == expected_cancel_hint
    assert sd["error"]["code"] == "kill_switch"
    assert sd["error"]["hint"] == expected_send_hint


def test_update_event_token_binds_stored_attendee_identity_same_count(tmp_path):
    account = make_account()
    ctx = make_ctx(
        tmp_path,
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
    )
    ev = _updatable_event("RAW-ATT-SWAP", required=("colleague@example.com",))
    account._by_id["RAW-ATT-SWAP"] = ev
    alias = ctx.aliaser.alias_for("RAW-ATT-SWAP", "e")
    kwargs = {"event_id": alias, "subject": "Moved", "notify_attendees": True}

    p1 = call(ctx, "update_event", kwargs)
    assert p1["ok"] is True and p1["requires_confirmation"] is True
    assert p1["preview"]["attendee_count"] == 1

    # Replace the single attendee with another address while keeping count == 1.
    ev.required_attendees = [_att_box("replacement@example.com")]
    p2 = call(ctx, "update_event", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is False
    assert p2["error"]["code"] == "confirm_invalid"
    assert "stale" in p2["error"]["message"]
    ev.save.assert_not_called()

    # Same-count swap between phase-2 preview and handler fetch is also caught.
    s_p1 = _updatable_event("RAW-ATT-SWAP-H", required=("colleague@example.com",))
    s_p2 = _updatable_event("RAW-ATT-SWAP-H", required=("colleague@example.com",))
    s_h = _updatable_event("RAW-ATT-SWAP-H", required=("replacement@example.com",))
    objs = [s_p1, s_p2, s_h]
    seq_acc = SimpleNamespace(
        drafts=SimpleNamespace(id=DRAFTS_ID),
        calendar=SimpleNamespace(id="FOLDER-CAL"),
    )
    seq_acc.fetch = lambda pairs, only_fields=None: [objs.pop(0) for _ in pairs]
    ctx_h = make_ctx(
        tmp_path / "handler",
        seq_acc,
        ews_email="organizer@example.com",
        send_enabled=True,
    )
    e_h = ctx_h.aliaser.alias_for("RAW-ATT-SWAP-H", "e")
    kw_h = {"event_id": e_h, "subject": "Moved", "notify_attendees": True}
    tok_h = call(ctx_h, "update_event", kw_h)["confirm_token"]
    res_h = call(ctx_h, "update_event", {**kw_h, "confirm_token": tok_h})
    assert res_h["ok"] is False
    assert res_h["error"]["code"] == "confirm_invalid"
    s_h.save.assert_not_called()


@pytest.mark.parametrize("field, preview_val, tampered_val", [
    ("subject", "Agenda A", "Agenda B"),
    ("start", "2026-11-20T09:15", "2026-11-20T09:30"),
    ("end", "2026-11-20T10:15", "2026-11-20T10:45"),
    ("location", "Room A", "Room B"),
    ("body", "Notes v1", "Notes v2"),
    ("reminder_minutes", 15, 30),
])
def test_update_event_token_binds_each_changed_field(
    tmp_path, field, preview_val, tampered_val,
):
    account = make_account()
    ctx = make_ctx(
        tmp_path,
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
    )
    ev = _updatable_event("RAW-CHG-BIND", required=("colleague@example.com",))
    account._by_id["RAW-CHG-BIND"] = ev
    alias = ctx.aliaser.alias_for("RAW-CHG-BIND", "e")

    p1 = call(ctx, "update_event", {
        "event_id": alias,
        field: preview_val,
        "notify_attendees": True,
    })
    assert p1["ok"] is True and p1["requires_confirmation"] is True

    p2 = call(ctx, "update_event", {
        "event_id": alias,
        field: tampered_val,
        "notify_attendees": True,
        "confirm_token": p1["confirm_token"],
    })
    assert p2["ok"] is False
    assert p2["error"]["code"] == "confirm_invalid"
    assert "stale" in p2["error"]["message"]
    ev.save.assert_not_called()


def test_update_event_token_rejects_added_field_at_phase_two(tmp_path):
    account = make_account()
    ctx = make_ctx(
        tmp_path,
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
    )
    ev = _updatable_event("RAW-CHG-ADDED", required=("colleague@example.com",))
    account._by_id["RAW-CHG-ADDED"] = ev
    alias = ctx.aliaser.alias_for("RAW-CHG-ADDED", "e")

    p1 = call(ctx, "update_event", {
        "event_id": alias,
        "subject": "Original change",
        "notify_attendees": True,
    })
    assert p1["ok"] is True and p1["requires_confirmation"] is True

    p2 = call(ctx, "update_event", {
        "event_id": alias,
        "subject": "Original change",
        "body": "Added after preview",
        "notify_attendees": True,
        "confirm_token": p1["confirm_token"],
    })
    assert p2["ok"] is False
    assert p2["error"]["code"] == "confirm_invalid"
    assert "stale" in p2["error"]["message"]
    ev.save.assert_not_called()


@pytest.mark.parametrize("start_val, end_val", [
    (EWSDate(2026, 11, 20), EWSDate(2026, 11, 21)),
    (date(2026, 11, 20), date(2026, 11, 21)),
])
def test_update_event_notify_succeeds_on_all_day_date_event(
    tmp_path, start_val, end_val,
):
    account = make_account()
    ctx = make_ctx(
        tmp_path,
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
    )
    ev = _updatable_event("RAW-ALLDAY-UPD", required=("colleague@example.com",))
    ev.start = start_val
    ev.end = end_val
    ev.is_all_day = True
    account._by_id["RAW-ALLDAY-UPD"] = ev
    alias = ctx.aliaser.alias_for("RAW-ALLDAY-UPD", "e")
    kwargs = {"event_id": alias, "subject": "Offsite day", "notify_attendees": True}

    p1 = call(ctx, "update_event", kwargs)
    assert p1["ok"] is True and p1["requires_confirmation"] is True
    assert p1["preview"]["start"] == "2026-11-20"
    assert p1["preview"]["end"] == "2026-11-21"
    assert account.fetch_projections == [None]

    p2 = call(ctx, "update_event", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is True
    assert p2["updated_fields"] == ["subject"]
    assert p2["attendees_notified"] is True
    assert account.fetch_projections == [None, None, None]
    assert account.fetch_projections[0] == account.fetch_projections[2]
    ev.save.assert_called_once_with(
        update_fields=["subject"],
        send_meeting_invitations=SEND_TO_CHANGED_AND_SAVE_COPY,
    )


@pytest.mark.parametrize("item_class", [
    "IPM.OLE.CLASS.{00061055-0000-0000-C000-000000000046}",
    "IPM.Schedule.Meeting.Custom",
    "ipm.appointment",
])
def test_update_event_accepts_custom_and_modified_occurrence_item_classes(
    tmp_path, monkeypatch, item_class,
):
    account = make_account()
    ctx = make_ctx(
        tmp_path,
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
    )

    ev_silent = _updatable_event("RAW-CLS-SILENT", required=("colleague@example.com",))
    ev_silent.item_class = item_class
    account._by_id["RAW-CLS-SILENT"] = ev_silent
    e_silent = ctx.aliaser.alias_for("RAW-CLS-SILENT", "e")
    res_silent = call(ctx, "update_event", {
        "event_id": e_silent,
        "subject": "Silent update",
        "notify_attendees": False,
    })
    assert res_silent["ok"] is True
    assert res_silent["attendees_notified"] is False
    ev_silent.save.assert_called_once_with(
        update_fields=["subject"],
        send_meeting_invitations=SEND_TO_NONE,
    )

    ev_notify = _updatable_event("RAW-CLS-NOTIFY", required=("colleague@example.com",))
    ev_notify.item_class = item_class
    account._by_id["RAW-CLS-NOTIFY"] = ev_notify
    e_notify = ctx.aliaser.alias_for("RAW-CLS-NOTIFY", "e")
    kw_notify = {
        "event_id": e_notify,
        "subject": "Notified update",
        "notify_attendees": True,
    }
    p1 = call(ctx, "update_event", kw_notify)
    assert p1["ok"] is True and p1["requires_confirmation"] is True
    p2 = call(ctx, "update_event", {**kw_notify, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is True
    assert p2["attendees_notified"] is True
    ev_notify.save.assert_called_once_with(
        update_fields=["subject"],
        send_meeting_invitations=SEND_TO_CHANGED_AND_SAVE_COPY,
    )

    saved_non_cal: list[Dict[str, Any]] = []
    monkeypatch.setattr(Message, "save", lambda self, **kw: saved_non_cal.append(kw))
    non_cal = Message(id="RAW-NON-CAL-ITEM", subject="Original")
    account._by_id["RAW-NON-CAL-ITEM"] = non_cal
    e_non_cal = ctx.aliaser.alias_for("RAW-NON-CAL-ITEM", "e")
    res_non_cal = call(ctx, "update_event", {
        "event_id": e_non_cal,
        "subject": "Updated non-CalendarItem",
    })
    assert res_non_cal["ok"] is True
    assert non_cal.subject == "Updated non-CalendarItem"
    assert saved_non_cal == [{
        "update_fields": ["subject"],
        "send_meeting_invitations": SEND_TO_NONE,
    }]


def test_update_event_token_minted_without_notify_rejected_when_notifying(tmp_path):
    account = make_account()
    ctx = make_ctx(
        tmp_path,
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
    )
    ev = _updatable_event("RAW-MINT-NO-NOTIFY", required=("colleague@example.com",))
    account._by_id["RAW-MINT-NO-NOTIFY"] = ev
    alias = ctx.aliaser.alias_for("RAW-MINT-NO-NOTIFY", "e")

    tok_implicit = asyncio.run(
        mint_token(ctx, SPEC["update_event"], {"event_id": alias, "subject": "Edited"})
    )
    res1 = call(ctx, "update_event", {
        "event_id": alias,
        "subject": "Edited",
        "notify_attendees": True,
        "confirm_token": tok_implicit,
    })
    assert res1["ok"] is False
    assert res1["error"]["code"] == "confirm_invalid"
    ev.save.assert_not_called()

    tok_false = asyncio.run(
        mint_token(
            ctx,
            SPEC["update_event"],
            {"event_id": alias, "subject": "Edited", "notify_attendees": False},
        )
    )
    res2 = call(ctx, "update_event", {
        "event_id": alias,
        "subject": "Edited",
        "notify_attendees": True,
        "confirm_token": tok_false,
    })
    assert res2["ok"] is False
    assert res2["error"]["code"] == "confirm_invalid"
    ev.save.assert_not_called()


def test_event_name_only_attendees_shown_in_preview_and_blocked_with_list(tmp_path):
    named_only = SimpleNamespace(
        mailbox=SimpleNamespace(name="Personal List", email_address=None)
    )
    blank_only = SimpleNamespace(
        mailbox=SimpleNamespace(name="   ", email_address="")
    )

    for mode, overrides in (
        ("allow", {"ews_recipient_allowlist": "*@example.com"}),
        ("deny", {"ews_recipient_denylist": "*@blocked.example.org"}),
    ):
        account = make_account()
        ctx_list = make_ctx(
            tmp_path / f"list-{mode}",
            account,
            ews_email="organizer@example.com",
            send_enabled=True,
            ews_capability_tier="full",
            **overrides,
        )
        ev_upd = _updatable_event("RAW-UNV-UPD", required=("colleague@example.com",))
        ev_upd.optional_attendees = [named_only]
        account._by_id["RAW-UNV-UPD"] = ev_upd

        mtg_can = _cal_standin(
            id="RAW-UNV-CAN",
            subject="Sync",
            is_meeting=True,
            my_response_type="Organizer",
            organizer=SimpleNamespace(email_address="organizer@example.com"),
            required_attendees=[_att_box("colleague@example.com"), named_only],
        )
        account._by_id["RAW-UNV-CAN"] = mtg_can

        res_upd = call(ctx_list, "update_event", {
            "event_id": ctx_list.aliaser.alias_for("RAW-UNV-UPD", "e"),
            "subject": "Moved",
            "notify_attendees": True,
        })
        res_can = call(ctx_list, "cancel_event", {
            "event_id": ctx_list.aliaser.alias_for("RAW-UNV-CAN", "e"),
        })

        for res in (res_upd, res_can):
            assert res["ok"] is False, mode
            assert res["error"]["code"] == "recipient_blocked", mode
            assert "no address" in res["error"]["message"], mode
            assert "lists" in res["error"]["message"], mode
            assert "confirm_token" not in res, mode
        ev_upd.save.assert_not_called()
        mtg_can.cancel.assert_not_called()

    # With no list configured, preview names them in attendees_without_address and phase 2 runs.
    account_open = make_account()
    ctx_open = make_ctx(
        tmp_path / "open",
        account_open,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
    )
    ev_open = _updatable_event("RAW-OPEN-UPD", required=("colleague@example.com",))
    ev_open.optional_attendees = [named_only, blank_only]
    account_open._by_id["RAW-OPEN-UPD"] = ev_open
    e_open_upd = ctx_open.aliaser.alias_for("RAW-OPEN-UPD", "e")
    p1_upd = call(ctx_open, "update_event", {
        "event_id": e_open_upd,
        "subject": "Moved",
        "notify_attendees": True,
    })
    assert p1_upd["ok"] is True and p1_upd["requires_confirmation"] is True
    assert p1_upd["preview"]["attendee_count"] == 3
    assert p1_upd["preview"]["attendees"] == ["colleague@example.com"]
    assert p1_upd["preview"]["attendees_without_address"] == [
        "(no name)",
        "Personal List",
    ]
    assert "_unverifiable_recipients" not in p1_upd["preview"]
    p2_upd = call(ctx_open, "update_event", {
        "event_id": e_open_upd,
        "subject": "Moved",
        "notify_attendees": True,
        "confirm_token": p1_upd["confirm_token"],
    })
    assert p2_upd["ok"] is True
    ev_open.save.assert_called_once()

    mtg_open = _cal_standin(
        id="RAW-OPEN-CAN",
        subject="Sync",
        is_meeting=True,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="organizer@example.com"),
        required_attendees=[_att_box("colleague@example.com"), named_only, blank_only],
    )
    account_open._by_id["RAW-OPEN-CAN"] = mtg_open
    e_open_can = ctx_open.aliaser.alias_for("RAW-OPEN-CAN", "e")
    p1_can = call(ctx_open, "cancel_event", {"event_id": e_open_can})
    assert p1_can["ok"] is True and p1_can["requires_confirmation"] is True
    assert p1_can["preview"]["attendees"] == ["colleague@example.com"]
    assert p1_can["preview"]["attendees_without_address"] == [
        "(no name)",
        "Personal List",
    ]
    assert "_unverifiable_recipients" not in p1_can["preview"]
    p2_can = call(ctx_open, "cancel_event", {
        "event_id": e_open_can,
        "confirm_token": p1_can["confirm_token"],
    })
    assert p2_can["ok"] is True and p2_can["cancelled"] is True
    mtg_open.cancel.assert_called_once()


def test_event_legacy_dn_and_ex_routing_attendees_listed_and_fail_closed_with_list(
    tmp_path,
):
    legacy_dn_addr = "/o=Org/ou=Group/cn=Recipients/cn=user"
    ex_dn_addr = "/o=Org/ou=Group/cn=Recipients/cn=exuser"
    legacy_att = SimpleNamespace(
        mailbox=SimpleNamespace(name="Directory User", email_address=legacy_dn_addr)
    )
    ex_att = SimpleNamespace(
        mailbox=SimpleNamespace(
            name="Exchange User",
            email_address=ex_dn_addr,
            routing_type="EX",
        )
    )

    for label, special_att, expected_addr in (
        ("legacy", legacy_att, legacy_dn_addr),
        ("ex", ex_att, ex_dn_addr),
    ):
        account_open = make_account()
        ctx_open = make_ctx(
            tmp_path / f"open-{label}",
            account_open,
            ews_email="organizer@example.com",
            send_enabled=True,
            ews_capability_tier="full",
        )
        ev_upd = _updatable_event("RAW-LDN-UPD", required=("colleague@example.com",))
        ev_upd.optional_attendees = [special_att]
        account_open._by_id["RAW-LDN-UPD"] = ev_upd
        e_upd = ctx_open.aliaser.alias_for("RAW-LDN-UPD", "e")

        p1_upd = call(ctx_open, "update_event", {
            "event_id": e_upd,
            "subject": "Moved",
            "notify_attendees": True,
        })
        assert p1_upd["ok"] is True and p1_upd["requires_confirmation"] is True, label
        assert p1_upd["preview"]["attendees"] == sorted(
            ["colleague@example.com", expected_addr]
        ), label
        assert "attendees_without_address" not in p1_upd["preview"], label
        p2_upd = call(ctx_open, "update_event", {
            "event_id": e_upd,
            "subject": "Moved",
            "notify_attendees": True,
            "confirm_token": p1_upd["confirm_token"],
        })
        assert p2_upd["ok"] is True, label
        ev_upd.save.assert_called_once()

        mtg_can = _cal_standin(
            id="RAW-LDN-CAN",
            subject="Sync",
            is_meeting=True,
            my_response_type="Organizer",
            organizer=SimpleNamespace(email_address="organizer@example.com"),
            required_attendees=[_att_box("colleague@example.com"), special_att],
        )
        account_open._by_id["RAW-LDN-CAN"] = mtg_can
        e_can = ctx_open.aliaser.alias_for("RAW-LDN-CAN", "e")

        p1_can = call(ctx_open, "cancel_event", {"event_id": e_can})
        assert p1_can["ok"] is True and p1_can["requires_confirmation"] is True, label
        assert p1_can["preview"]["attendees"] == sorted(
            ["colleague@example.com", expected_addr]
        ), label
        assert "attendees_without_address" not in p1_can["preview"], label
        p2_can = call(ctx_open, "cancel_event", {
            "event_id": e_can,
            "confirm_token": p1_can["confirm_token"],
        })
        assert p2_can["ok"] is True and p2_can["cancelled"] is True, label
        mtg_can.cancel.assert_called_once()

        # With a recipient list set, non-SMTP address strings fail closed with validation.
        account_list = make_account()
        ctx_list = make_ctx(
            tmp_path / f"list-{label}",
            account_list,
            ews_email="organizer@example.com",
            send_enabled=True,
            ews_capability_tier="full",
            ews_recipient_allowlist="*@example.com",
        )
        ev_upd_l = _updatable_event("RAW-LDN-UPD-L", required=("colleague@example.com",))
        ev_upd_l.optional_attendees = [special_att]
        account_list._by_id["RAW-LDN-UPD-L"] = ev_upd_l

        mtg_can_l = _cal_standin(
            id="RAW-LDN-CAN-L",
            subject="Sync",
            is_meeting=True,
            my_response_type="Organizer",
            organizer=SimpleNamespace(email_address="organizer@example.com"),
            required_attendees=[_att_box("colleague@example.com"), special_att],
        )
        account_list._by_id["RAW-LDN-CAN-L"] = mtg_can_l

        res_upd_l = call(ctx_list, "update_event", {
            "event_id": ctx_list.aliaser.alias_for("RAW-LDN-UPD-L", "e"),
            "subject": "Moved",
            "notify_attendees": True,
        })
        res_can_l = call(ctx_list, "cancel_event", {
            "event_id": ctx_list.aliaser.alias_for("RAW-LDN-CAN-L", "e"),
        })
        for res_l in (res_upd_l, res_can_l):
            assert res_l["ok"] is False, label
            assert res_l["error"]["code"] == "validation", label
            assert "confirm_token" not in res_l, label
        ev_upd_l.save.assert_not_called()
        mtg_can_l.cancel.assert_not_called()


def test_event_name_only_attendee_rename_between_phases_invalidates_token(tmp_path):
    account = make_account()
    ctx = make_ctx(
        tmp_path,
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
    )

    ev_upd = _updatable_event("RAW-RENAME-UPD", required=("colleague@example.com",))
    ev_upd.optional_attendees = [
        SimpleNamespace(mailbox=SimpleNamespace(name="Group Alpha", email_address=None))
    ]
    account._by_id["RAW-RENAME-UPD"] = ev_upd
    e_upd = ctx.aliaser.alias_for("RAW-RENAME-UPD", "e")
    kw_upd = {"event_id": e_upd, "subject": "Moved", "notify_attendees": True}
    p1_upd = call(ctx, "update_event", kw_upd)
    assert p1_upd["preview"]["attendees_without_address"] == ["Group Alpha"]
    ev_upd.optional_attendees = [
        SimpleNamespace(mailbox=SimpleNamespace(name="Group Beta", email_address=None))
    ]
    p2_upd = call(ctx, "update_event", {
        **kw_upd,
        "confirm_token": p1_upd["confirm_token"],
    })
    assert p2_upd["ok"] is False
    assert p2_upd["error"]["code"] == "confirm_invalid"
    ev_upd.save.assert_not_called()

    mtg_can = _cal_standin(
        id="RAW-RENAME-CAN",
        subject="Sync",
        is_meeting=True,
        my_response_type="Organizer",
        organizer=SimpleNamespace(email_address="organizer@example.com"),
        required_attendees=[
            _att_box("colleague@example.com"),
            SimpleNamespace(
                mailbox=SimpleNamespace(name="Group Alpha", email_address=None)
            ),
        ],
    )
    account._by_id["RAW-RENAME-CAN"] = mtg_can
    e_can = ctx.aliaser.alias_for("RAW-RENAME-CAN", "e")
    p1_can = call(ctx, "cancel_event", {"event_id": e_can})
    assert p1_can["preview"]["attendees_without_address"] == ["Group Alpha"]
    mtg_can.required_attendees[1] = SimpleNamespace(
        mailbox=SimpleNamespace(name="Group Beta", email_address=None)
    )
    p2_can = call(ctx, "cancel_event", {
        "event_id": e_can,
        "confirm_token": p1_can["confirm_token"],
    })
    assert p2_can["ok"] is False
    assert p2_can["error"]["code"] == "confirm_invalid"
    mtg_can.cancel.assert_not_called()


def test_send_draft_recipient_without_address_listed_as_str_and_fails_validation_with_list(
    tmp_path,
):
    no_addr = SimpleNamespace(name="Personal List", email_address=None)

    account_open = make_account()
    ctx_open = make_ctx(
        tmp_path / "open",
        account_open,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
    )
    draft_open = make_content_draft(
        subject="Report",
        to=("colleague@example.com",),
        body="See attached",
    )
    draft_open.cc_recipients = [no_addr]
    account_open._by_id["RAW-SD-NOADDR"] = draft_open
    d_open = ctx_open.aliaser.alias_for("RAW-SD-NOADDR", "d")

    assert set(writes._draft_base_content(draft_open).keys()) == {
        "subject",
        "to",
        "cc",
        "bcc",
        "body_text",
        "_body_html",
        "attachment_count",
        "attachments",
        "_attachments",
    }

    p1 = call(ctx_open, "send_draft", {"draft_id": d_open})
    assert p1["ok"] is True and p1["requires_confirmation"] is True
    assert p1["preview"]["to"] == ["colleague@example.com"]
    assert p1["preview"]["cc"] == [str(no_addr)]
    p2 = call(ctx_open, "send_draft", {
        "draft_id": d_open,
        "confirm_token": p1["confirm_token"],
    })
    assert p2["ok"] is True and p2["sent"] is True
    draft_open.send.assert_called_once_with(save_copy=True)

    for mode, overrides in (
        ("allow", {"ews_recipient_allowlist": "*@example.com"}),
        ("deny", {"ews_recipient_denylist": "*@blocked.example.org"}),
    ):
        account_list = make_account()
        ctx_list = make_ctx(
            tmp_path / f"list-{mode}",
            account_list,
            ews_email="organizer@example.com",
            send_enabled=True,
            ews_capability_tier="full",
            **overrides,
        )
        draft_list = make_content_draft(
            subject="Report",
            to=("colleague@example.com",),
            body="See attached",
        )
        draft_list.cc_recipients = [no_addr]
        account_list._by_id["RAW-SD-NOADDR-L"] = draft_list
        d_list = ctx_list.aliaser.alias_for("RAW-SD-NOADDR-L", "d")
        res = call(ctx_list, "send_draft", {"draft_id": d_list})
        assert res["ok"] is False, mode
        assert res["error"]["code"] == "validation", mode
        assert "confirm_token" not in res, mode
        draft_list.send.assert_not_called()


def test_update_event_token_binds_stored_start_end_location_target_id_and_scope(
    tmp_path,
):
    account = make_account()
    ctx = make_ctx(
        tmp_path,
        account,
        ews_email="organizer@example.com",
        send_enabled=True,
    )

    # 1. Stored start, end, and location each invalidate the token when changed.
    for tag, attr, new_val in (
        ("start", "start", datetime(2026, 11, 20, 9, 30, tzinfo=LOCAL_TZ)),
        ("end", "end", datetime(2026, 11, 20, 10, 30, tzinfo=LOCAL_TZ)),
        ("location", "location", "Room Z"),
    ):
        ev = _updatable_event(f"RAW-BND-{tag}", required=("colleague@example.com",))
        account._by_id[ev.id] = ev
        alias = ctx.aliaser.alias_for(ev.id, "e")
        kw = {"event_id": alias, "subject": "Moved", "notify_attendees": True}
        p1 = call(ctx, "update_event", kw)
        assert p1["ok"] is True, tag
        setattr(ev, attr, new_val)
        p2 = call(ctx, "update_event", {**kw, "confirm_token": p1["confirm_token"]})
        assert p2["ok"] is False, tag
        assert p2["error"]["code"] == "confirm_invalid", tag
        ev.save.assert_not_called()

    # 2. Resolved target_id (e.g. recurring_master().id) is bound independently of event_id.
    occ_target = _updatable_event("RAW-OCC-TID", required=("colleague@example.com",))
    occ_target.type = "Occurrence"
    master_target = _updatable_event(
        "RAW-MASTER-TID-1", required=("colleague@example.com",)
    )
    master_target.type = "RecurringMaster"
    master_target.refresh = MagicMock()
    occ_target.recurring_master = MagicMock(return_value=master_target)
    account._by_id["RAW-OCC-TID"] = occ_target
    e_tid = ctx.aliaser.alias_for("RAW-OCC-TID", "e")
    kw_tid = {
        "event_id": e_tid,
        "subject": "Series title",
        "scope": "series",
        "notify_attendees": True,
    }
    p1_tid = call(ctx, "update_event", kw_tid)
    assert p1_tid["ok"] is True
    master_target.id = "RAW-MASTER-TID-2"
    p2_tid = call(
        ctx, "update_event", {**kw_tid, "confirm_token": p1_tid["confirm_token"]}
    )
    assert p2_tid["ok"] is False
    assert p2_tid["error"]["code"] == "confirm_invalid"
    master_target.save.assert_not_called()
    occ_target.save.assert_not_called()

    # 3. Token previewed with scope='series' is confirm_invalid without scope
    #    even when the master shares every other attribute with the occurrence.
    occ_scope = _updatable_event("RAW-OCC-SCOPE", required=("colleague@example.com",))
    occ_scope.type = "Occurrence"
    master_same = _updatable_event("RAW-OCC-SCOPE", required=("colleague@example.com",))
    master_same.type = "Occurrence"
    master_same.refresh = MagicMock()
    occ_scope.recurring_master = MagicMock(return_value=master_same)
    account._by_id["RAW-OCC-SCOPE"] = occ_scope
    e_scope = ctx.aliaser.alias_for("RAW-OCC-SCOPE", "e")
    p1_scope = call(ctx, "update_event", {
        "event_id": e_scope,
        "subject": "Renamed",
        "scope": "series",
        "notify_attendees": True,
    })
    assert p1_scope["ok"] is True
    p2_no_scope = call(ctx, "update_event", {
        "event_id": e_scope,
        "subject": "Renamed",
        "notify_attendees": True,
        "confirm_token": p1_scope["confirm_token"],
    })
    assert p2_no_scope["ok"] is False
    assert p2_no_scope["error"]["code"] == "confirm_invalid"
    occ_scope.save.assert_not_called()
    master_same.save.assert_not_called()



