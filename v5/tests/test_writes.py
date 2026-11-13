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
from datetime import date, datetime
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
    Credentials,
    EWSDate,
    EWSDateTime,
    EWSTimeZone,
    FileAttachment,
    OofSettings,
)
from exchangelib.attachments import AttachmentId
from exchangelib.items import SEND_TO_ALL_AND_SAVE_COPY, SEND_TO_NONE
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

from conftest import FIXED_OFFSET_TZ, make_settings
from ewsmcp.audit import AuditLog
from ewsmcp.confirm import reset_consumed_tokens
from ewsmcp.dates import parse_when
from ewsmcp.ids import get_aliaser
from ewsmcp.tools import writes
from ewsmcp.tools.base import Context, dispatch

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


def test_create_event_with_invites_blocked_by_kill_switch_at_phase_two(
        tmp_path, monkeypatch):
    account = make_account()
    ctx = make_ctx(tmp_path, account)  # send_enabled defaults to False
    monkeypatch.setattr(writes, "CalendarItem", FakeCalendarItem)
    FakeCalendarItem.last = None
    kwargs = {"subject": "Board", "start": "2026-07-01T10:00",
              "end": "2026-07-01T11:00", "attendees": ["x@ext.example"],
              "send_invitations": True}
    p1 = call(ctx, "create_event", kwargs)
    assert p1["requires_confirmation"] is True  # preview-only mode still works
    p2 = call(ctx, "create_event", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["error"]["code"] == "kill_switch"
    assert FakeCalendarItem.last is None  # nothing was built or saved


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
    ctx = make_ctx(tmp_path, account)  # send_enabled False
    ev = MagicMock()
    account._by_id["RAW-E2"] = ev
    kwargs = {"event_id": "RAW-E2", "subject": "Moved", "notify_attendees": True}
    p1 = call(ctx, "update_event", kwargs)
    assert p1["requires_confirmation"] is True
    p2 = call(ctx, "update_event", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["error"]["code"] == "kill_switch"
    ev.save.assert_not_called()


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
    ev = MagicMock()
    account._by_id["RAW-E3"] = ev
    kwargs = {"event_id": "RAW-E3", "message": "postponed"}
    p1 = call(ctx, "cancel_event", kwargs)
    assert p1["requires_confirmation"] is True
    ev.cancel.assert_not_called()
    p2 = call(ctx, "cancel_event", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["cancelled"] is True
    ev.cancel.assert_called_once_with(new_body="postponed")


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
