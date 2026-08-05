"""Identity must survive the trip from the HTTP layer to the tool layer.

The chain is: ``build_app`` verifies the token and writes the Principal into
the ASGI scope → the Streamable HTTP transport hands the tool layer that same
scope → ``call_tool`` reads it back → ``dispatch`` gates on it.

``test_mcp_sdk_pins.py`` pins the SDK hops in isolation. These tests drive the
whole thing with the real app, the real transport and real signed tokens, and
— most importantly — check what happens when the chain BREAKS.
"""

import asyncio
import json
from typing import Any, Dict

import anyio
import pytest
from conftest import (
    make_binder,
    make_settings,
    make_token,
    make_verifier,
    oidc_settings,
)

from ewsmcp.audit import AuditLog
from ewsmcp.auth import Principal
from ewsmcp.http import build_app
from ewsmcp.identity import SCOPE_PRINCIPAL_KEY
from ewsmcp.ids import get_aliaser
from ewsmcp.server import build_mcp_server
from ewsmcp.tools.base import Context, ToolSpec, dispatch

SEEN: list = []


async def _echo(ctx, **kwargs) -> Dict[str, Any]:
    principal = ctx.principal
    SEEN.append(principal.smtp if principal is not None else None)
    return {"ok": True, "caller": principal.smtp if principal else None}


def _ctx(tmp_path, settings) -> Context:
    spec = ToolSpec(
        name="echo", description="echo test tool", side_effect_class="read",
        input_schema={"type": "object", "properties": {}},
        handler=_echo, requires_ews=False,
    )
    ctx = Context(
        settings=settings, gateway=None, manager=None,
        aliaser=get_aliaser(str(tmp_path / "alias")),
        audit=AuditLog(str(tmp_path / "audit")),
    )
    ctx.registry = {"echo": spec}
    return ctx


@pytest.fixture(autouse=True)
def _clear_seen():
    SEEN.clear()
    yield
    SEEN.clear()


# ------------------------------------------------------------------ REST shim

def _drive(app, path, body, headers, scope=None):
    scope = scope if scope is not None else {}
    scope.update({"type": "http", "path": path, "method": "POST",
                  "headers": list(headers)})
    pending = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list = []

    async def receive():
        return pending.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, json.loads(raw or b"{}")


def test_rest_call_carries_the_verified_caller(tmp_path, rsa_keys):
    settings = oidc_settings()
    ctx = _ctx(tmp_path, settings)
    ctx.binder = make_binder(settings)
    app = build_app(ctx, settings, verifier=make_verifier(rsa_keys, settings))
    status, body = _drive(
        app, "/api/tools/echo", b"{}",
        [(b"authorization", f"Bearer {make_token(rsa_keys)}".encode())])
    assert status == 200
    assert body["caller"] == "exec@corp.example"
    assert SEEN == ["exec@corp.example"]


# ------------------------------------------------------------------ /mcp path

async def _mcp_tools_call(ctx, scope_extra):
    """POST a tools/call through the REAL Streamable HTTP transport."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    manager = StreamableHTTPSessionManager(
        app=build_mcp_server(ctx), json_response=True, stateless=True)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "echo", "arguments": {}}}).encode()
    scope = {"type": "http", "method": "POST", "path": "/mcp",
             "headers": [(b"content-type", b"application/json"),
                         (b"accept", b"application/json, text/event-stream")]}
    scope.update(scope_extra)
    pending = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list = []

    async def receive():
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async with manager.run():
        with anyio.move_on_after(10):
            await manager.handle_request(scope, receive, send)
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return json.loads(raw or b"{}")


def test_mcp_call_tool_reads_the_principal_from_the_scope(tmp_path, rsa_keys):
    """End to end through the transport the production server actually uses."""
    principal = Principal(subject="user-alice", smtp="alice@corp.example",
                          issuer="https://idp.corp.example/", expires_at=9e9)
    ctx = _ctx(tmp_path, oidc_settings())
    anyio.run(_mcp_tools_call, ctx, {SCOPE_PRINCIPAL_KEY: principal})
    assert SEEN == ["alice@corp.example"]


def test_mcp_call_fails_closed_when_identity_is_lost(tmp_path, rsa_keys):
    """THE regression this whole design fears: an SDK change stops attaching
    the request, so no principal arrives. The call must be REFUSED — falling
    back to the configured mailbox would serve it to an anonymous caller."""
    ctx = _ctx(tmp_path, oidc_settings())
    result = anyio.run(_mcp_tools_call, ctx, {})  # nothing in the scope
    payload = json.dumps(result)
    assert "auth_failed" in payload
    assert "no verified caller identity" in payload
    assert SEEN == [], "the handler must never have run"


def test_json_rpc_batches_are_rejected_by_the_transport(tmp_path, rsa_keys):
    """One HTTP request carries one bearer token, so it can only mean one
    caller. Batching is what would put that in question — and the SDK rejects
    batches outright (the MCP spec dropped them in 2025-06-18), so the
    question does not arise.

    Pinned rather than assumed: if a future SDK re-introduces batching, this
    fails and forces a decision about per-message identity BEFORE a batch can
    quietly execute several callers' work under one token.
    """
    principal = Principal(subject="user-alice", smtp="alice@corp.example",
                          issuer="https://idp.corp.example/", expires_at=9e9)
    ctx = _ctx(tmp_path, oidc_settings())

    async def run():
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        manager = StreamableHTTPSessionManager(
            app=build_mcp_server(ctx), json_response=True, stateless=True)
        body = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "echo", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "echo", "arguments": {}}},
        ]).encode()
        scope = {"type": "http", "method": "POST", "path": "/mcp",
                 "headers": [(b"content-type", b"application/json"),
                             (b"accept", b"application/json, text/event-stream")],
                 SCOPE_PRINCIPAL_KEY: principal}
        pending = [{"type": "http.request", "body": body, "more_body": False}]
        sent: list = []

        async def receive():
            return pending.pop(0) if pending else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        async with manager.run():
            with anyio.move_on_after(10):
                await manager.handle_request(scope, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    assert int(anyio.run(run)) == 400
    assert SEEN == [], "no handler may run from a rejected batch"


# ------------------------------------------------------- static mode unchanged

def test_static_mode_needs_no_principal(tmp_path):
    """The canary: stdio and the existing deployment must not have gained an
    identity requirement."""
    ctx = _ctx(tmp_path, make_settings())
    result = asyncio.run(dispatch(ctx, ctx.registry["echo"], {}))
    assert result["ok"] is True
    assert SEEN == [None]


def test_oidc_dispatch_without_a_principal_is_refused(tmp_path):
    ctx = _ctx(tmp_path, oidc_settings())
    result = asyncio.run(dispatch(ctx, ctx.registry["echo"], {}))
    assert result["ok"] is False
    assert result["error"]["code"] == "auth_failed"
    assert SEEN == []
