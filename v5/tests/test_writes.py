"""Writes tool pack tests — driven through dispatch() so every dispatcher
gate (kill-switch, tier, two-phase confirm, alias resolution) is exercised
exactly as production would.

exchangelib classes that handlers *construct* (Message, CalendarItem) are
monkeypatched with capture stubs because the real classes refuse non-Account
accounts at __init__; everything fetched from the fake account is a plain
mock. Constants (SEND_TO_NONE, …) are the REAL exchangelib 5.0.3 objects so
drift would fail here first.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from exchangelib import OofSettings
from exchangelib.items import SEND_TO_ALL_AND_SAVE_COPY, SEND_TO_NONE

from conftest import make_settings
from ewsmcp.audit import AuditLog
from ewsmcp.ids import get_aliaser
from ewsmcp.tools import writes
from ewsmcp.tools.base import Context, dispatch

SPEC = {s.name: s for s in writes.TOOLS}
DRAFTS_ID = "FOLDER-DRAFTS"
RIYADH = ZoneInfo("Asia/Riyadh")


@pytest.fixture(autouse=True)
def _fresh_idempotency():
    writes.reset_idempotency_store()
    yield
    writes.reset_idempotency_store()


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


def test_pack_surface_classes_and_confirm_declarations(tmp_path):
    assert len(writes.TOOLS) == 12
    classes = {s.name: s.side_effect_class for s in writes.TOOLS}
    assert classes == {
        "create_draft": "write", "update_draft": "write", "delete_draft": "write",
        "update_messages": "write", "move_messages": "write",
        "create_event": "write", "update_event": "write",
        "send_draft": "send", "respond_to_event": "send", "set_oof": "send",
        "cancel_event": "destructive", "delete_messages": "destructive",
    }
    for name in ("create_draft", "update_draft", "delete_draft",
                 "update_messages", "move_messages"):
        assert SPEC[name].confirm is False
    for name in ("respond_to_event", "cancel_event", "set_oof"):
        assert SPEC[name].confirm is True
    # Conditional-confirm predicates take (ctx, kwargs): the caller is part of
    # the decision now, because an idempotent replay is per-caller.
    ctx = make_ctx(tmp_path, make_account())
    # send_draft: confirm-gated with a CONTENT-BOUND preview hook; the gate
    # is skipped only for an idempotent replay of an already-sent draft.
    assert SPEC["send_draft"].confirm(ctx, {"draft_id": "d1"}) is True
    assert SPEC["send_draft"].preview is not None
    assert SPEC["create_event"].confirm(ctx, {"send_invitations": True}) is True
    assert SPEC["create_event"].confirm(ctx, {}) is False
    assert SPEC["update_event"].confirm(ctx, {"notify_attendees": True}) is True
    assert SPEC["delete_messages"].confirm(ctx, {"disposition": "permanent"}) is True
    assert SPEC["delete_messages"].confirm(ctx, {"disposition": "trash"}) is False


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
    assert ev.kwargs["start"] == datetime(2026, 7, 1, tzinfo=RIYADH)
    assert ev.kwargs["end"] == datetime(2026, 7, 1, 1, 0, tzinfo=RIYADH)
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
