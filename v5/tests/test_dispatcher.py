"""Dispatcher gate-chain tests: every IDEATION §7-8 gate, in order.

Driven with ``asyncio.run`` against ``dispatch()`` directly.
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Dict

import pytest

from conftest import make_settings
from ewsmcp import confirm as confirm_mod
from ewsmcp.audit import AuditLog
from ewsmcp.confirm import consume_token, make_token, reset_consumed_tokens, token_consumed
from ewsmcp.ids import get_aliaser
from ewsmcp.tools.base import (
    VERIFIED_CONTENT_HASH_ARG,
    Context,
    ToolSpec,
    dispatch,
    mint_token,
    preview_content_hash,
    reset_send_rate_window,
)

RAW_ID = "AAMkAGI4" + "a" * 100


class _Gateway:
    async def call(self, fn):
        raise AssertionError("gateway should not be hit in these tests")


def _ctx(tmp_path, **settings_overrides) -> Context:
    return Context(
        settings=make_settings(**settings_overrides),
        gateway=_Gateway(),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "mem")),
        audit=AuditLog(str(tmp_path / "data")),
    )


def _spec(handler, *, cls="read", confirm=False, requires_ews=True, name="t") -> ToolSpec:
    return ToolSpec(
        name=name, description="test", side_effect_class=cls,
        input_schema={"type": "object", "properties": {}},
        handler=handler, requires_ews=requires_ews, confirm=confirm,
    )


async def _ok_handler(ctx, **kwargs) -> Dict[str, Any]:
    return {"ran": True, "got": kwargs}


# --- kill-switch & tier ---------------------------------------------------


def test_kill_switch_blocks_send_class(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=False, ews_capability_tier="full")
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="send"), {}))
    assert result["ok"] is False and result["error"]["code"] == "kill_switch"


def test_tier_draft_blocks_send_and_destructive(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="draft")
    for cls in ("send", "destructive"):
        result = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls=cls), {}))
        assert result["error"]["code"] in ("tier_blocked", "kill_switch")
    write = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="write"), {}))
    assert write.get("ran") is True


def test_tier_read_blocks_write(tmp_path):
    ctx = _ctx(tmp_path, ews_capability_tier="read")
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="write"), {}))
    assert result["error"]["code"] == "tier_blocked"


# --- cold gate --------------------------------------------------------------


class _ColdManager:
    state = "connecting"

    def status(self):
        return {"state": "connecting", "attempts": 4,
                "last_error": "TransportError: Failed to get auth type",
                "next_retry_in_s": 30, "last_success_age_s": None}


def test_cold_gate_blocks_ews_tools_with_hint(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.manager = _ColdManager()
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler), {}))
    assert result["error"]["code"] == "upstream_unavailable"
    assert "auth type" in result["error"]["message"]
    assert result["error"]["retry_after_s"] == 30


def test_cold_gate_passes_local_tools(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.manager = _ColdManager()
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler, requires_ews=False), {}))
    assert result.get("ran") is True


class _AuthFailedManager:
    state = "auth_failed"

    def __init__(self, *, rejections=1, permanent=False, next_retry_in_s=900):
        self._rejections = rejections
        self._permanent = permanent
        self._next = next_retry_in_s

    def status(self):
        return {
            "state": "auth_failed",
            "attempts": self._rejections,
            "last_error": "UnauthorizedError: HTTP 401",
            "last_success_age_s": None,
            "next_retry_in_s": None if self._permanent else self._next,
            "auth_rejections": self._rejections,
            "auth_permanent": self._permanent,
        }


def test_auth_failed_gate_blocks_ews_tools_with_retry_when_permitted(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.manager = _AuthFailedManager(rejections=1, permanent=False, next_retry_in_s=900)
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler), {}))
    assert result["ok"] is False
    err = result["error"]
    assert err["code"] == "auth_failed"
    assert err["retry_after_s"] == 900
    assert "retrying will not help" in err["hint"].lower()
    assert "restart" in err["hint"].lower()


def test_auth_failed_gate_omits_retry_after_when_permanent(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.manager = _AuthFailedManager(rejections=3, permanent=True, next_retry_in_s=None)
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler), {}))
    assert result["ok"] is False
    err = result["error"]
    assert err["code"] == "auth_failed"
    assert "retry_after_s" not in err
    assert "retrying will not help" in err["hint"].lower()


def test_auth_failed_gate_passes_local_tools(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.manager = _AuthFailedManager(rejections=3, permanent=True)
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler, requires_ews=False), {}))
    assert result.get("ran") is True


# --- two-phase confirm -------------------------------------------------------


def test_confirm_phase1_then_phase2(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full")
    spec = _spec(_ok_handler, cls="send", confirm=True)
    args = {"to": ["x@external.example"], "subject": "s", "body": "b" * 300}
    p1 = asyncio.run(dispatch(ctx, spec, dict(args)))
    assert p1["requires_confirmation"] is True
    assert len(p1["preview"]["body"]) == 200
    assert any("external" in w for w in p1["warnings"])
    p2 = asyncio.run(dispatch(ctx, spec, {**args, "confirm_token": p1["confirm_token"]}))
    assert p2.get("ran") is True
    assert "confirm_token" not in p2["got"]


def test_confirm_rejects_changed_args(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full")
    spec = _spec(_ok_handler, cls="send", confirm=True)
    p1 = asyncio.run(dispatch(ctx, spec, {"to": ["a@b.c"]}))
    p2 = asyncio.run(dispatch(ctx, spec, {"to": ["evil@x.y"],
                                          "confirm_token": p1["confirm_token"]}))
    assert p2["error"]["code"] == "confirm_invalid"


def test_conditional_confirm_callable(tmp_path):
    ctx = _ctx(tmp_path, ews_capability_tier="full")
    spec = _spec(_ok_handler, cls="destructive",
                 confirm=lambda kw: kw.get("permanent") is True)
    soft = asyncio.run(dispatch(ctx, spec, {"permanent": False}))
    assert soft.get("ran") is True
    hard = asyncio.run(dispatch(ctx, spec, {"permanent": True}))
    assert hard["requires_confirmation"] is True


# --- recipient guards & rate cap ----------------------------------------------


def test_denylist_blocks(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full",
               ews_recipient_denylist="*@competitor.example")
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="send"),
                                  {"to": ["ceo@competitor.example"]}))
    assert result["error"]["code"] == "recipient_blocked"


def test_rate_cap(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full",
               ews_max_sends_per_hour=2)
    spec = _spec(_ok_handler, cls="send")
    assert asyncio.run(dispatch(ctx, spec, {})).get("ran") is True
    assert asyncio.run(dispatch(ctx, spec, {})).get("ran") is True
    third = asyncio.run(dispatch(ctx, spec, {}))
    assert third["error"]["code"] == "rate_capped"


# --- alias resolution ------------------------------------------------------------


def test_alias_inputs_resolved(tmp_path):
    ctx = _ctx(tmp_path)
    alias = ctx.aliaser.alias_for(RAW_ID, "m")
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler), {"id": alias}))
    assert result["got"] == {"id": RAW_ID}


def test_stale_alias_clean_error(tmp_path):
    ctx = _ctx(tmp_path)
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler), {"id": "m999"}))
    assert result["error"]["code"] == "validation"


# --- error mapping & circuit ---------------------------------------------------


def test_exception_mapping_throttled(tmp_path):
    class ErrorServerBusy(Exception):
        pass

    async def boom(ctx, **kwargs):
        raise ErrorServerBusy("server asked to back off 40000 ms")

    ctx = _ctx(tmp_path)
    result = asyncio.run(dispatch(ctx, _spec(boom), {}))
    assert result["error"]["code"] == "throttled"
    assert result["error"]["retry_after_s"]


def test_exception_mapping_unauthorized_beats_throttling_and_avoids_401_false_positives():
    from exchangelib.errors import UnauthorizedError
    from ewsmcp.errors import map_exception

    # UnauthorizedError wins even if message text contains throttling keywords.
    auth_err = map_exception(UnauthorizedError("server busy back off ratelimit"))
    assert auth_err.code == "auth_failed"
    assert auth_err.hint is not None
    assert "retrying will not help" in auth_err.hint.lower()

    # Messages that merely contain digits "401" must NOT map to auth_failed.
    assert map_exception(RuntimeError("failed on item 401 in batch")).code == "upstream_error"
    assert map_exception(RuntimeError("id AAMk401xyz corrupted")).code == "upstream_error"
    assert map_exception(ConnectionError("timed out after 4010 ms")).code == "upstream_unavailable"

    class ErrorItemNotFound(Exception):
        pass

    assert map_exception(ErrorItemNotFound("item 401 missing")).code == "not_found"


def test_circuit_opens_after_threshold(tmp_path):
    async def boom(ctx, **kwargs):
        raise ConnectionError("connection reset")

    ctx = _ctx(tmp_path, circuit_failure_threshold=3, circuit_open_seconds=60)
    spec = _spec(boom)
    for _ in range(3):
        asyncio.run(dispatch(ctx, spec, {}))
    result = asyncio.run(dispatch(ctx, spec, {}))
    assert result["error"]["code"] == "upstream_unavailable"
    assert "circuit open" in result["error"]["message"]


def test_open_circuit_passes_get_server_status_and_blocks_ews_tools(tmp_path):
    from ewsmcp.tools import calendar_people

    status_spec = next(s for s in calendar_people.TOOLS if s.name == "get_server_status")

    async def boom(ctx, **kwargs):
        raise ConnectionError("connection reset")

    ctx = _ctx(tmp_path, circuit_failure_threshold=2, circuit_open_seconds=120)
    closed_status = asyncio.run(dispatch(ctx, status_spec, {}))
    assert closed_status["ok"] is True
    assert "circuit_open" not in closed_status["connection"]

    for _ in range(2):
        asyncio.run(dispatch(ctx, _spec(boom), {}))

    status = asyncio.run(dispatch(ctx, status_spec, {}))
    assert status["ok"] is True
    assert status["connection"]["circuit_open"] is True
    assert isinstance(status["connection"]["retry_after_s"], int)
    assert 0 < status["connection"]["retry_after_s"] <= 120

    local = asyncio.run(dispatch(ctx, _spec(_ok_handler, requires_ews=False), {}))
    assert local["ok"] is True and local["ran"] is True

    blocked = asyncio.run(dispatch(ctx, _spec(_ok_handler, requires_ews=True), {}))
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "upstream_unavailable"
    assert isinstance(blocked["error"]["retry_after_s"], int)
    assert 0 < blocked["error"]["retry_after_s"] <= 120


# --- confirm_token hygiene, single-use, preview-hook binding (Phase B) --------


def test_confirm_token_never_reaches_non_confirm_handlers(tmp_path):
    """A stray confirm_token used to leak into handlers → TypeError → 502."""
    ctx = _ctx(tmp_path)
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler),
                                  {"confirm_token": "junk"}))
    assert result.get("ran") is True
    assert result["got"] == {}


def test_handler_typeerror_maps_to_validation(tmp_path):
    async def strict(ctx, *, required_arg):  # noqa: ARG001
        return {"ran": True}

    ctx = _ctx(tmp_path)
    result = asyncio.run(dispatch(ctx, _spec(strict), {"wrong_name": 1}))
    assert result["ok"] is False
    assert result["error"]["code"] == "validation"
    assert "schema" in result["error"]["hint"]


def test_confirm_token_is_single_use(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full")
    spec = _spec(_ok_handler, cls="send", confirm=True)
    args = {"to": ["a@b.c"]}
    token = asyncio.run(dispatch(ctx, spec, dict(args)))["confirm_token"]
    first = asyncio.run(dispatch(ctx, spec, {**args, "confirm_token": token}))
    assert first.get("ran") is True
    replay = asyncio.run(dispatch(ctx, spec, {**args, "confirm_token": token}))
    assert replay["error"]["code"] == "confirm_invalid"
    assert "consumed" in replay["error"]["message"]


def test_token_consumed_is_read_only():
    tok = make_token(
        mailbox="user@example.com",
        action="send_draft",
        target_id="RAW-1",
        chash="hash-1",
        secret="unit-test-secret",
    )["confirm_token"]
    confirm_mod._CONSUMED["expired-sig"] = 0.0

    assert token_consumed(tok) is False
    assert token_consumed(tok) is False
    assert "expired-sig" in confirm_mod._CONSUMED  # read-only: no pruning side effects

    assert consume_token(tok) is True
    assert token_consumed(tok) is True
    assert consume_token(tok) is False
    assert "expired-sig" not in confirm_mod._CONSUMED

    reset_consumed_tokens()
    assert token_consumed(tok) is False


def test_spent_token_cannot_be_replayed_in_expiry_fractional_second(
    tmp_path, monkeypatch,
):
    now_box = [1_700_000_000.0]
    monkeypatch.setattr("ewsmcp.confirm.time.time", lambda: now_box[0])
    monkeypatch.setattr("ewsmcp.tools.base.time.time", lambda: now_box[0])

    ctx = _ctx(
        tmp_path,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
        confirm_ttl_seconds=10,
    )
    spec = _spec(_ok_handler, cls="send", confirm=True)
    kw1 = {"to": ["first@example.com"]}
    kw2 = {"to": ["second@example.com"]}
    tok1 = asyncio.run(dispatch(ctx, spec, dict(kw1)))["confirm_token"]
    tok2 = asyncio.run(dispatch(ctx, spec, dict(kw2)))["confirm_token"]

    now_box[0] = 1_700_000_001.0
    spent1 = asyncio.run(dispatch(ctx, spec, {**kw1, "confirm_token": tok1}))
    assert spent1["ok"] is True and spent1["ran"] is True

    # At expiry + 0.5s, int(now) == exp so verify_token still accepts tok1;
    # consuming tok2 triggers pruning, which must retain tok1 past integer expiry.
    now_box[0] = 1_700_000_010.5
    spent2 = asyncio.run(dispatch(ctx, spec, {**kw2, "confirm_token": tok2}))
    assert spent2["ok"] is True and spent2["ran"] is True

    replayed = asyncio.run(dispatch(ctx, spec, {**kw1, "confirm_token": tok1}))
    assert replayed["ok"] is False
    assert replayed["error"]["code"] == "confirm_invalid"
    assert "consumed" in replayed["error"]["message"]

    # After the grace window elapses, opportunistic pruning cleans up tok1.
    now_box[0] = 1_700_000_010.0 + confirm_mod.CONSUMED_GRACE_SECONDS + 1.0
    kw3 = {"to": ["third@example.com"]}
    tok3 = asyncio.run(dispatch(ctx, spec, dict(kw3)))["confirm_token"]
    spent3 = asyncio.run(dispatch(ctx, spec, {**kw3, "confirm_token": tok3}))
    assert spent3["ok"] is True
    assert tok1.rsplit(".", 1)[-1] not in confirm_mod._CONSUMED


def test_recipient_guard_fires_on_write_class(tmp_path):
    """The old guard only covered class 'send' — dead code, since no
    send-class tool carries recipient kwargs. Drafts/events are where
    recipients actually enter."""
    ctx = _ctx(tmp_path, ews_capability_tier="full",
               ews_recipient_denylist="*@competitor.example")
    result = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="write"),
                                  {"to": ["ceo@competitor.example"], "body": "x"}))
    assert result["error"]["code"] == "recipient_blocked"


def test_allowlist_guard_on_write_class(tmp_path):
    ctx = _ctx(tmp_path, ews_capability_tier="full",
               ews_recipient_allowlist="*@corp.example")
    blocked = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="write"),
                                   {"to": ["out@other.example"]}))
    assert blocked["error"]["code"] == "recipient_blocked"
    ok = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="write"),
                              {"to": ["peer@corp.example"]}))
    assert ok.get("ran") is True


def _preview_spec(handler, contents: list, **kw):
    """Spec whose preview hook pops resolved content off `contents`."""
    async def preview(ctx, kwargs):
        return dict(contents.pop(0))

    return ToolSpec(
        name="t", description="test", side_effect_class=kw.get("cls", "send"),
        input_schema={"type": "object", "properties": {}},
        handler=handler, confirm=True, preview=preview,
    )


def test_preview_hook_phase1_shows_resolved_content(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full")
    content = {"subject": "Q3", "to": ["x@external.example"], "cc": [],
               "bcc": [], "body_text": "b" * 2000, "attachment_count": 1}
    spec = _preview_spec(_ok_handler, [content])
    p1 = asyncio.run(dispatch(ctx, spec, {"draft_id": "RAW-1"}))
    assert p1["requires_confirmation"] is True
    assert p1["preview"]["subject"] == "Q3"
    assert p1["preview"]["to"] == ["x@external.example"]
    assert len(p1["preview"]["body_snippet"]) == 1000
    assert "body_text" not in p1["preview"]
    assert any("external" in w for w in p1["warnings"])


def test_preview_hook_content_change_kills_token(tmp_path):
    """The TOCTOU defense: content edited between preview and confirm."""
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full")
    original = {"subject": "Q3", "to": ["a@corp.example"], "body_text": "safe"}
    tampered = {"subject": "Q3", "to": ["attacker@evil.example"], "body_text": "safe"}
    spec = _preview_spec(_ok_handler, [original, tampered])
    token = asyncio.run(dispatch(ctx, spec, {"draft_id": "RAW-1"}))["confirm_token"]
    p2 = asyncio.run(dispatch(ctx, spec, {"draft_id": "RAW-1",
                                          "confirm_token": token}))
    assert p2["error"]["code"] == "confirm_invalid"
    assert "stale" in p2["error"]["message"]
    assert "content changed" in p2["error"]["hint"]


def test_preview_hook_unchanged_content_executes(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full")
    content = {"subject": "Q3", "to": ["a@corp.example"], "body_text": "safe"}
    spec = _preview_spec(_ok_handler, [dict(content), dict(content)])
    token = asyncio.run(dispatch(ctx, spec, {"draft_id": "RAW-1"}))["confirm_token"]
    p2 = asyncio.run(dispatch(ctx, spec, {"draft_id": "RAW-1",
                                          "confirm_token": token}))
    assert p2.get("ran") is True


def test_preview_hook_resolved_recipients_are_guarded(tmp_path):
    """The draft's REAL recipients pass the guard even though the tool's
    own kwargs carry none — closes the send_draft bypass."""
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full",
               ews_recipient_denylist="*@competitor.example")
    content = {"subject": "s", "to": ["ceo@competitor.example"], "body_text": "x"}
    spec = _preview_spec(_ok_handler, [content])
    p1 = asyncio.run(dispatch(ctx, spec, {"draft_id": "RAW-1"}))
    assert p1["error"]["code"] == "recipient_blocked"
    assert "confirm_token" not in p1


def test_audit_chain_written(tmp_path):
    ctx = _ctx(tmp_path)
    asyncio.run(dispatch(ctx, _spec(_ok_handler), {}))
    asyncio.run(dispatch(ctx, _spec(_ok_handler), {}))
    audit_dir = tmp_path / "data" / "audit"
    files = list(audit_dir.glob("*.jsonl"))
    assert files, "audit file missing"
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    import hashlib
    import json
    prev = "GENESIS"
    for line in lines:
        rec = json.loads(line)
        h = rec.pop("h")
        canonical = json.dumps(rec, sort_keys=True, ensure_ascii=False)
        assert h == hashlib.sha256(f"{prev}|{canonical}".encode()).hexdigest()
        prev = h


# --- recipient guard normalisation & malformed validation --------------------


@pytest.mark.parametrize("spelling", [
    "  leak@blocked.example  ",
    "LEAK@BLOCKED.EXAMPLE",
    "External Contact <leak@blocked.example>",
    "  External Contact <  LEAK@BLOCKED.EXAMPLE  >  ",
])
@pytest.mark.parametrize("field", ["to", "cc", "bcc", "attendees"])
def test_denylist_normalises_whitespace_case_and_angle_brackets(
    tmp_path, spelling, field,
):
    ctx = _ctx(tmp_path, ews_capability_tier="full",
               ews_recipient_denylist="*@blocked.example")
    res = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="write"),
                               {field: [spelling]}))
    assert res["ok"] is False
    assert res["error"]["code"] == "recipient_blocked"


def test_allowlist_normalises_whitespace_case_and_angle_brackets(tmp_path):
    ctx = _ctx(tmp_path, ews_capability_tier="full",
               ews_recipient_allowlist="*@corp.example")
    ok = asyncio.run(dispatch(ctx, _spec(_ok_handler, cls="write"), {
        "to": ["  Team Lead <PEER@CORP.EXAMPLE> "],
    }))
    assert ok.get("ran") is True


@pytest.mark.parametrize("bad_recipient", [
    "",
    "   ",
    "missing-at-sign",
    "@corp.example",
    "local-only@",
    "two@at@corp.example",
    "has space@corp.example",
    "Contact <has space@corp.example>",
    "Contact <peer@corp.example> extra",
    "Contact <peer@corp.example",
    "<peer@corp.example> <leak@blocked.example>",
])
def test_malformed_recipient_is_validation_with_list_and_ignored_without(
    tmp_path, bad_recipient,
):
    deny_ctx = _ctx(tmp_path / "deny", ews_capability_tier="full",
                    ews_recipient_denylist="*@blocked.example")
    res_deny = asyncio.run(dispatch(deny_ctx, _spec(_ok_handler, cls="write"),
                                    {"to": [bad_recipient]}))
    assert res_deny["ok"] is False
    assert res_deny["error"]["code"] == "validation"

    allow_ctx = _ctx(tmp_path / "allow", ews_capability_tier="full",
                     ews_recipient_allowlist="*@corp.example")
    res_allow = asyncio.run(dispatch(allow_ctx, _spec(_ok_handler, cls="write"),
                                     {"to": [bad_recipient]}))
    assert res_allow["ok"] is False
    assert res_allow["error"]["code"] == "validation"

    open_ctx = _ctx(tmp_path / "open", send_enabled=True, ews_capability_tier="full")
    res_open = asyncio.run(dispatch(open_ctx, _spec(_ok_handler, cls="write"),
                                    {"to": [bad_recipient]}))
    assert res_open.get("ran") is True
    phase1 = asyncio.run(dispatch(open_ctx, _spec(_ok_handler, cls="send", confirm=True),
                                  {"to": [bad_recipient]}))
    assert phase1["requires_confirmation"] is True


def test_verified_content_hash_stripped_from_caller_kwargs(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full")
    res = asyncio.run(dispatch(
        ctx,
        _spec(_ok_handler),
        {VERIFIED_CONTENT_HASH_ARG: "spoofed-hash"},
    ))
    assert res.get("ran") is True
    assert res["got"] == {}

    async def explicit_handler(ctx, *, draft_id: str, _verified_content_hash=None):
        return {"draft_id": draft_id, "verified": _verified_content_hash}

    res_explicit = asyncio.run(dispatch(
        ctx,
        _spec(explicit_handler, cls="write"),
        {"draft_id": "RAW-1", VERIFIED_CONTENT_HASH_ARG: "spoofed-hash"},
    ))
    assert res_explicit["ok"] is True
    assert res_explicit["verified"] is None


def test_confirm_gate_hands_verified_content_hash_to_opting_in_handler(tmp_path):
    ctx = _ctx(tmp_path, send_enabled=True, ews_capability_tier="full")
    content = {"subject": "Q3", "to": ["peer@corp.example"], "body_text": "safe"}

    async def send_like(ctx, *, draft_id: str, _verified_content_hash=None):
        return {"draft_id": draft_id, "verified": _verified_content_hash}

    spec = _preview_spec(send_like, [dict(content), dict(content)])
    p1 = asyncio.run(dispatch(
        ctx,
        spec,
        {"draft_id": "RAW-1", VERIFIED_CONTENT_HASH_ARG: "spoofed-p1"},
    ))
    p2 = asyncio.run(dispatch(
        ctx,
        spec,
        {
            "draft_id": "RAW-1",
            "confirm_token": p1["confirm_token"],
            VERIFIED_CONTENT_HASH_ARG: "spoofed-p2",
        },
    ))
    assert p2["ok"] is True
    assert p2["verified"] == preview_content_hash(content)


# --- round 2: notifies declaration, rate-cap reservation & concurrency --------


def test_notifies_enforced_at_gate_one_and_after_preview(tmp_path):
    from ewsmcp.errors import ToolError

    ctx_off = _ctx(
        tmp_path / "off",
        ews_email="organizer@example.com",
        send_enabled=False,
        ews_capability_tier="full",
    )

    arg_spec = ToolSpec(
        name="arg_notify",
        description="test",
        side_effect_class="write",
        input_schema={"type": "object", "properties": {}},
        handler=_ok_handler,
        confirm=lambda kw: bool(kw.get("notify")),
        notifies=lambda kw, _c: bool(kw.get("notify")),
    )
    blocked_arg = asyncio.run(dispatch(ctx_off, arg_spec, {"notify": True}))
    assert blocked_arg["ok"] is False
    assert blocked_arg["error"]["code"] == "kill_switch"
    assert "confirm_token" not in blocked_arg

    allowed_arg = asyncio.run(dispatch(ctx_off, arg_spec, {"notify": False}))
    assert allowed_arg["ok"] is True and allowed_arg["ran"] is True

    async def preview_by_mode(_ctx, kwargs):
        mode = kwargs.get("mode")
        if mode == "invalid":
            raise ToolError("validation", "not our entry")
        return {"action": mode, "attendees": ["peer@example.com"]}

    content_spec = ToolSpec(
        name="content_notify",
        description="test",
        side_effect_class="destructive",
        input_schema={"type": "object", "properties": {}},
        handler=_ok_handler,
        confirm=True,
        preview=preview_by_mode,
        notifies=lambda _kw, c: c is not None and c.get("action") == "cancel",
    )
    val_res = asyncio.run(dispatch(ctx_off, content_spec, {"mode": "invalid"}))
    assert val_res["ok"] is False and val_res["error"]["code"] == "validation"

    ks_res = asyncio.run(dispatch(ctx_off, content_spec, {"mode": "cancel"}))
    assert ks_res["ok"] is False and ks_res["error"]["code"] == "kill_switch"
    assert "confirm_token" not in ks_res

    del_p1 = asyncio.run(dispatch(ctx_off, content_spec, {"mode": "delete"}))
    assert del_p1["ok"] is True and del_p1["requires_confirmation"] is True
    del_p2 = asyncio.run(dispatch(
        ctx_off,
        content_spec,
        {"mode": "delete", "confirm_token": del_p1["confirm_token"]},
    ))
    assert del_p2["ok"] is True and del_p2["ran"] is True


def test_rate_capped_call_preserves_token_until_hour_has_room(tmp_path, monkeypatch):
    now_box = [1_700_000_000.0]
    monkeypatch.setattr("ewsmcp.tools.base.time.time", lambda: now_box[0])
    monkeypatch.setattr("ewsmcp.confirm.time.time", lambda: now_box[0])

    ctx = _ctx(
        tmp_path,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
        ews_max_sends_per_hour=1,
        confirm_ttl_seconds=7200,
    )
    spec = ToolSpec(
        name="notify_write",
        description="test",
        side_effect_class="write",
        input_schema={"type": "object", "properties": {}},
        handler=_ok_handler,
        confirm=lambda kw: bool(kw.get("notify")),
        notifies=lambda kw, _c: bool(kw.get("notify")),
    )

    kw1 = {"subject": "First", "notify": True}
    kw2 = {"subject": "Second", "notify": True}
    tok1 = asyncio.run(dispatch(ctx, spec, dict(kw1)))["confirm_token"]
    tok2 = asyncio.run(dispatch(ctx, spec, dict(kw2)))["confirm_token"]

    first = asyncio.run(dispatch(ctx, spec, {**kw1, "confirm_token": tok1}))
    assert first["ok"] is True and first["ran"] is True

    capped = asyncio.run(dispatch(ctx, spec, {**kw2, "confirm_token": tok2}))
    assert capped["ok"] is False
    assert capped["error"]["code"] == "rate_capped"
    assert capped["error"]["retry_after_s"] == 3600

    # Advance past the 1-hour send window while tok2 (TTL 7200s) is still valid.
    now_box[0] += 3601.0
    retried = asyncio.run(dispatch(ctx, spec, {**kw2, "confirm_token": tok2}))
    assert retried["ok"] is True and retried["ran"] is True

    # A second use after execution is rejected as consumed.
    replayed = asyncio.run(dispatch(ctx, spec, {**kw2, "confirm_token": tok2}))
    assert replayed["ok"] is False
    assert replayed["error"]["code"] == "confirm_invalid"
    assert "consumed" in replayed["error"]["message"]


def test_concurrent_confirmed_sends_at_cap_one_allow_one_and_preserve_other_token(
    tmp_path,
):
    ctx = _ctx(
        tmp_path,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
        ews_max_sends_per_hour=1,
    )

    async def slow_preview(_ctx, kwargs):
        await asyncio.sleep(0.01)
        return {"subject": kwargs["id"], "to": ["peer@example.com"], "body_text": "hi"}

    spec = ToolSpec(
        name="concurrent_send",
        description="test",
        side_effect_class="send",
        input_schema={"type": "object", "properties": {}},
        handler=_ok_handler,
        confirm=True,
        preview=slow_preview,
        resolve_ids=False,
    )

    async def scenario():
        p1_a = await dispatch(ctx, spec, {"id": "A"})
        p1_b = await dispatch(ctx, spec, {"id": "B"})
        tok_a = p1_a["confirm_token"]
        tok_b = p1_b["confirm_token"]

        res_a, res_b = await asyncio.gather(
            dispatch(ctx, spec, {"id": "A", "confirm_token": tok_a}),
            dispatch(ctx, spec, {"id": "B", "confirm_token": tok_b}),
        )
        outcomes = [(res_a, "A", tok_a), (res_b, "B", tok_b)]
        winners = [x for x in outcomes if x[0].get("ok") is True]
        losers = [x for x in outcomes if x[0].get("ok") is False]
        assert len(winners) == 1 and winners[0][0]["ran"] is True
        assert len(losers) == 1
        loser_res, loser_id, loser_tok = losers[0]
        assert loser_res["error"]["code"] == "rate_capped"
        assert loser_res["error"]["retry_after_s"] > 0

        # Free the window: the refused call's token was never consumed and works now.
        reset_send_rate_window()
        retry = await dispatch(ctx, spec, {"id": loser_id, "confirm_token": loser_tok})
        assert retry["ok"] is True and retry["ran"] is True

    asyncio.run(scenario())


def test_same_token_twice_concurrently_at_cap_two_counts_only_one_slot(tmp_path):
    ctx = _ctx(
        tmp_path,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
        ews_max_sends_per_hour=2,
    )

    async def slow_preview(_ctx, kwargs):
        await asyncio.sleep(0.01)
        return {"subject": kwargs["id"], "to": ["peer@example.com"], "body_text": "hi"}

    spec = ToolSpec(
        name="double_use",
        description="test",
        side_effect_class="send",
        input_schema={"type": "object", "properties": {}},
        handler=_ok_handler,
        confirm=True,
        preview=slow_preview,
        resolve_ids=False,
    )

    async def scenario():
        tok1 = (await dispatch(ctx, spec, {"id": "1"}))["confirm_token"]
        tok2 = (await dispatch(ctx, spec, {"id": "2"}))["confirm_token"]
        tok3 = (await dispatch(ctx, spec, {"id": "3"}))["confirm_token"]

        r1, r2 = await asyncio.gather(
            dispatch(ctx, spec, {"id": "1", "confirm_token": tok1}),
            dispatch(ctx, spec, {"id": "1", "confirm_token": tok1}),
        )
        ok_calls = [r for r in (r1, r2) if r.get("ok") is True]
        err_calls = [r for r in (r1, r2) if r.get("ok") is False]
        assert len(ok_calls) == 1 and ok_calls[0]["ran"] is True
        assert len(err_calls) == 1
        assert err_calls[0]["error"]["code"] == "confirm_invalid"
        assert "consumed" in err_calls[0]["error"]["message"]

        # Only 1 of the 2 hourly slots was kept; a second distinct send succeeds.
        second = await dispatch(ctx, spec, {"id": "2", "confirm_token": tok2})
        assert second["ok"] is True and second["ran"] is True

        # Now 2 of 2 slots are used: a third send hits rate_capped.
        third = await dispatch(ctx, spec, {"id": "3", "confirm_token": tok3})
        assert third["ok"] is False and third["error"]["code"] == "rate_capped"

    asyncio.run(scenario())


def test_bad_token_while_hour_full_returns_confirm_invalid_not_rate_capped(tmp_path):
    ctx = _ctx(
        tmp_path,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
        ews_max_sends_per_hour=1,
    )
    spec = _spec(_ok_handler, cls="send", confirm=True)
    tok1 = asyncio.run(dispatch(ctx, spec, {"to": ["peer@example.com"]}))["confirm_token"]
    tok_stale = asyncio.run(
        dispatch(ctx, spec, {"to": ["other@example.org"]})
    )["confirm_token"]

    sent = asyncio.run(
        dispatch(ctx, spec, {"to": ["peer@example.com"], "confirm_token": tok1})
    )
    assert sent["ok"] is True and sent["ran"] is True

    # Hour is now full (1/1), yet malformed, bad-signature, and stale tokens
    # all fail verify_token first with confirm_invalid, never rate_capped.
    tampered_tok = tok_stale[:-1] + ("0" if tok_stale[-1] != "0" else "1")
    for bad_tok in ("malformed", tampered_tok, tok_stale):
        res = asyncio.run(
            dispatch(ctx, spec, {"to": ["peer@example.com"], "confirm_token": bad_tok})
        )
        assert res["ok"] is False
        assert res["error"]["code"] == "confirm_invalid"


def test_consumed_token_while_hour_full_returns_confirm_invalid_and_keeps_slot_count(
    tmp_path,
):
    from ewsmcp.tools import base as base_mod

    ctx = _ctx(
        tmp_path,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
        ews_max_sends_per_hour=1,
    )
    spec = _spec(_ok_handler, cls="send", confirm=True)
    args = {"to": ["peer@example.com"]}
    tok = asyncio.run(dispatch(ctx, spec, dict(args)))["confirm_token"]

    first = asyncio.run(dispatch(ctx, spec, {**args, "confirm_token": tok}))
    assert first["ok"] is True and first["ran"] is True
    assert len(base_mod._SEND_TIMES) == 1

    replay = asyncio.run(dispatch(ctx, spec, {**args, "confirm_token": tok}))
    assert replay["ok"] is False
    assert replay["error"]["code"] == "confirm_invalid"
    assert "consumed" in replay["error"]["message"]
    assert len(base_mod._SEND_TIMES) == 1


def test_mint_token_mirrors_confirm_gate_binding(tmp_path):
    ctx = _ctx(
        tmp_path,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
    )

    async def arg_dependent_preview(_ctx, kwargs):
        return {
            "subject": kwargs.get("subject", ""),
            "attendees": ["peer@example.com"],
            "body_text": json.dumps(kwargs, sort_keys=True),
        }

    preview_spec = ToolSpec(
        name="preview_bound",
        description="test",
        side_effect_class="send",
        input_schema={"type": "object", "properties": {}},
        handler=_ok_handler,
        confirm=True,
        preview=arg_dependent_preview,
        resolve_ids=False,
    )
    tok_preview = asyncio.run(
        mint_token(
            ctx,
            preview_spec,
            {
                "event_id": "RAW-1",
                "subject": "Plan",
                "confirm_token": "ignored-token",
                VERIFIED_CONTENT_HASH_ARG: "spoofed-hash",
            },
        )
    )
    res_preview = asyncio.run(
        dispatch(
            ctx,
            preview_spec,
            {"event_id": "RAW-1", "subject": "Plan", "confirm_token": tok_preview},
        )
    )
    assert res_preview["ok"] is True and res_preview["ran"] is True

    arg_spec = _spec(_ok_handler, cls="write", confirm=True)
    tok_arg = asyncio.run(
        mint_token(
            ctx,
            arg_spec,
            {
                "subject": "Board",
                "confirm_token": "ignored-token",
                VERIFIED_CONTENT_HASH_ARG: "spoofed-hash",
            },
        )
    )
    res_arg = asyncio.run(
        dispatch(ctx, arg_spec, {"subject": "Board", "confirm_token": tok_arg})
    )
    assert res_arg["ok"] is True and res_arg["ran"] is True


def test_confirm_gate_releases_reserved_slot_when_consume_token_returns_false(
    tmp_path, monkeypatch,
):
    from ewsmcp.tools import base as base_mod

    ctx = _ctx(
        tmp_path,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
        ews_max_sends_per_hour=2,
    )
    spec = _spec(_ok_handler, cls="send", confirm=True)
    kw1 = {"to": ["first@example.com"]}
    kw2 = {"to": ["second@example.com"]}
    tok1 = asyncio.run(dispatch(ctx, spec, dict(kw1)))["confirm_token"]
    tok2 = asyncio.run(dispatch(ctx, spec, dict(kw2)))["confirm_token"]

    first = asyncio.run(dispatch(ctx, spec, {**kw1, "confirm_token": tok1}))
    assert first["ok"] is True and first["ran"] is True
    slots_before = len(base_mod._SEND_TIMES)
    assert slots_before == 1

    real_consume = base_mod.consume_token
    calls: list[str] = []

    def fake_consume_once(token: str) -> bool:
        if not calls:
            calls.append(token)
            return False
        return real_consume(token)

    monkeypatch.setattr(base_mod, "consume_token", fake_consume_once)

    refused = asyncio.run(dispatch(ctx, spec, {**kw2, "confirm_token": tok2}))
    assert calls == [tok2]
    assert refused["ok"] is False
    assert refused["error"]["code"] == "confirm_invalid"
    assert "consumed" in refused["error"]["message"]
    assert len(base_mod._SEND_TIMES) == slots_before


def test_confirm_gate_two_thread_race_releases_second_reserved_slot(
    tmp_path, monkeypatch,
):
    from ewsmcp.tools import base as base_mod

    ctx = _ctx(
        tmp_path,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
        ews_max_sends_per_hour=2,
    )
    spec = _spec(_ok_handler, cls="send", confirm=True)
    args = {"to": ["peer@example.com"]}
    tok = asyncio.run(dispatch(ctx, spec, dict(args)))["confirm_token"]

    barrier = threading.Barrier(2, timeout=5.0)
    real_token_consumed = base_mod.token_consumed

    def synced_token_consumed(token: str) -> bool:
        already = real_token_consumed(token)
        barrier.wait()
        return already

    monkeypatch.setattr(base_mod, "token_consumed", synced_token_consumed)

    results: list[Dict[str, Any]] = []
    results_lock = threading.Lock()

    def worker() -> None:
        res = asyncio.run(dispatch(ctx, spec, {**args, "confirm_token": tok}))
        with results_lock:
            results.append(res)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)
    assert not t1.is_alive() and not t2.is_alive()

    ok_runs = [r for r in results if r.get("ok") is True]
    err_runs = [r for r in results if r.get("ok") is False]
    assert len(ok_runs) == 1 and ok_runs[0]["ran"] is True
    assert len(err_runs) == 1
    assert err_runs[0]["error"]["code"] == "confirm_invalid"
    assert "consumed" in err_runs[0]["error"]["message"]
    assert len(base_mod._SEND_TIMES) == 1


@pytest.mark.parametrize("bad_shape", [
    {"blocked@example.org": 1},
    123,
    ["peer@example.com", 456],
    ("peer@example.com",),
])
@pytest.mark.parametrize("field", ["to", "cc", "bcc", "attendees"])
def test_recipient_guard_rejects_wrong_shape_with_and_without_lists(
    tmp_path, bad_shape, field,
):
    ran = False

    async def tracking_handler(ctx, **kwargs):
        nonlocal ran
        ran = True
        return {"ran": True}

    open_ctx = _ctx(tmp_path / "open", ews_capability_tier="full")
    res_open = asyncio.run(
        dispatch(open_ctx, _spec(tracking_handler, cls="write"), {field: bad_shape})
    )
    assert res_open["ok"] is False
    assert res_open["error"]["code"] == "validation"
    assert ran is False

    allow_ctx = _ctx(
        tmp_path / "allow",
        ews_capability_tier="full",
        ews_recipient_allowlist="*@example.com",
    )
    res_allow = asyncio.run(
        dispatch(allow_ctx, _spec(tracking_handler, cls="write"), {field: bad_shape})
    )
    assert res_allow["ok"] is False
    assert res_allow["error"]["code"] == "validation"
    assert ran is False

    ok_none = asyncio.run(
        dispatch(allow_ctx, _spec(_ok_handler, cls="write"), {field: None})
    )
    assert ok_none.get("ran") is True


def test_rate_cap_retry_after_s_has_floor_of_one(tmp_path, monkeypatch):
    now_box = [1_700_000_000.0]
    monkeypatch.setattr("ewsmcp.tools.base.time.time", lambda: now_box[0])

    ctx = _ctx(
        tmp_path,
        ews_email="organizer@example.com",
        send_enabled=True,
        ews_capability_tier="full",
        ews_max_sends_per_hour=1,
    )
    spec = _spec(_ok_handler, cls="send")
    first = asyncio.run(dispatch(ctx, spec, {}))
    assert first["ok"] is True and first["ran"] is True

    # 0.1s remaining in the 3600s window -> int(0.1) is 0, floored to 1.
    now_box[0] = 1_700_003_599.9
    capped = asyncio.run(dispatch(ctx, spec, {}))
    assert capped["ok"] is False
    assert capped["error"]["code"] == "rate_capped"
    assert capped["error"]["retry_after_s"] == 1

