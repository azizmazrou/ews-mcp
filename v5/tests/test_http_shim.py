"""REST shim hardening (Phase B critical group #6).

Drives the ASGI app closure directly: schema validation against the tool's
PUBLIC schema, non-dict body rejection, request-size cap, and the
http.disconnect handling that used to hang the receive loop forever.
"""

import asyncio
import json

from conftest import make_settings

from ewsmcp.audit import AuditLog
from ewsmcp.http import MAX_BODY_BYTES, build_app
from ewsmcp.ids import get_aliaser
from ewsmcp.tools.base import Context, ToolSpec


async def _echo(ctx, **kwargs):
    return {"ok": True, "got": kwargs}


def _ctx(tmp_path) -> Context:
    spec = ToolSpec(
        name="echo", description="echo test tool", side_effect_class="read",
        input_schema={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=_echo, requires_ews=False,
    )
    ctx = Context(
        settings=make_settings(),
        gateway=None, manager=None,
        aliaser=get_aliaser(str(tmp_path / "alias")),
        audit=AuditLog(str(tmp_path / "audit")),
    )
    ctx.registry = {"echo": spec}
    return ctx


def _drive(app, path, messages, method="POST"):
    scope = {"type": "http", "path": path, "method": method, "headers": []}
    queue = list(messages)
    sent = []

    async def receive():
        return queue.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def _status_and_body(sent):
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(raw or b"{}")


def _post(app, name, payload):
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    return _drive(app, f"/api/tools/{name}",
                  [{"type": "http.request", "body": body, "more_body": False}])


def test_valid_call_dispatches(tmp_path):
    app = build_app(_ctx(tmp_path), make_settings())
    status, body = _status_and_body(_post(app, "echo", {"q": "hi"}))
    assert status == 200
    assert body["got"] == {"q": "hi"}


def test_non_dict_body_is_rejected(tmp_path):
    app = build_app(_ctx(tmp_path), make_settings())
    status, body = _status_and_body(_post(app, "echo", ["not", "a", "dict"]))
    assert status == 400
    assert body["error"]["code"] == "validation"
    assert "object" in body["error"]["message"]


def test_schema_violation_is_rejected_before_dispatch(tmp_path):
    app = build_app(_ctx(tmp_path), make_settings())
    status, body = _status_and_body(_post(app, "echo", {"q": 5}))
    assert status == 400
    assert body["error"]["code"] == "validation"
    status, body = _status_and_body(_post(app, "echo", {"nope": 1}))
    assert status == 400
    assert "openapi" in body["error"]["hint"].lower()


def test_unknown_tool_404(tmp_path):
    app = build_app(_ctx(tmp_path), make_settings())
    status, body = _status_and_body(_post(app, "nope", {}))
    assert status == 404


def test_oversize_body_is_capped(tmp_path):
    app = build_app(_ctx(tmp_path), make_settings())
    huge = b'{"q": "' + b"x" * MAX_BODY_BYTES + b'"}'
    status, body = _status_and_body(_post(app, "echo", huge))
    assert status == 413
    assert body["error"]["code"] == "validation"


def test_disconnect_mid_body_does_not_hang_or_crash(tmp_path):
    app = build_app(_ctx(tmp_path), make_settings())
    sent = _drive(app, "/api/tools/echo", [
        {"type": "http.request", "body": b'{"q":', "more_body": True},
        {"type": "http.disconnect"},
    ])
    assert sent == []  # no response to a vanished client — and no hang


def test_oauth_discovery_paths_404_even_behind_api_key(tmp_path):
    """We implement no OAuth at all — these MUST be 404, never 401.

    A 401 here reads to a spec-compliant MCP client (LibreChat included)
    as "OAuth required, no metadata available", which surfaced as a
    dead-end OAuth prompt even though this server only wants headers.
    """
    app = build_app(_ctx(tmp_path), make_settings(mcp_api_key="secret"))
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
        "/mcp/.well-known/openid-configuration",
        "/register",
    ):
        status, _ = _status_and_body(_drive(app, path, [
            {"type": "http.request", "body": b"", "more_body": False}], method="GET"))
        assert status == 404, path


def test_confirm_token_accepted_by_public_schema(tmp_path):
    """Phase-2 REST calls carry confirm_token; validation must use the
    PUBLIC schema (which injects it), not the raw input schema."""
    ctx = _ctx(tmp_path)
    spec = ctx.registry["echo"]
    spec.confirm = True
    app = build_app(ctx, make_settings())
    status, body = _status_and_body(_post(app, "echo", {"q": "x", "confirm_token": "t"}))
    assert status != 400 or "confirm_token" not in json.dumps(body)
