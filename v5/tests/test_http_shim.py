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


def _drive(app, path, messages, method="POST", headers=None, scope=None):
    scope = scope if scope is not None else {}
    scope.update({"type": "http", "path": path, "method": method,
                  "headers": list(headers or [])})
    queue = list(messages)
    sent = []

    async def receive():
        return queue.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def _header(sent, name):
    start = next(m for m in sent if m["type"] == "http.response.start")
    for key, value in start["headers"]:
        if bytes(key).lower() == name:
            return bytes(value).decode()
    return None


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


# --- AUTH_MODE=oidc ----------------------------------------------------------
#
# The resource server sits in front of the same dispatcher, so these drive the
# REAL app closure with REAL signed tokens — only the JWKS transport is faked.

def _oidc_app(tmp_path, rsa_keys, **overrides):
    from conftest import make_binder, make_verifier, oidc_settings
    settings = oidc_settings(**overrides)
    ctx = _ctx(tmp_path)
    ctx.settings = settings
    ctx.binder = make_binder(settings)
    return build_app(ctx, settings, verifier=make_verifier(rsa_keys, settings))


def _bearer(token):
    return [(b"authorization", f"Bearer {token}".encode())]


def test_oidc_accepts_a_valid_token_and_dispatches(tmp_path, rsa_keys):
    from conftest import make_token
    app = _oidc_app(tmp_path, rsa_keys)
    sent = _drive(app, "/api/tools/echo",
                  [{"type": "http.request", "body": b'{"q":"hi"}', "more_body": False}],
                  headers=_bearer(make_token(rsa_keys)))
    status, body = _status_and_body(sent)
    assert status == 200
    assert body["got"] == {"q": "hi"}


def test_oidc_refuses_an_unauthenticated_call(tmp_path, rsa_keys):
    app = _oidc_app(tmp_path, rsa_keys)
    sent = _post(app, "echo", {"q": "hi"})
    status, body = _status_and_body(sent)
    assert status == 401
    assert body["error"]["code"] == "auth_failed"
    assert 'error="invalid_token"' in _header(sent, b"www-authenticate")


def test_oidc_refuses_an_expired_token_with_a_challenge(tmp_path, rsa_keys):
    import time

    from conftest import make_token
    app = _oidc_app(tmp_path, rsa_keys)
    past = int(time.time()) - 7200
    sent = _drive(app, "/api/tools/echo",
                  [{"type": "http.request", "body": b"{}", "more_body": False}],
                  headers=_bearer(make_token(rsa_keys, iat=past, exp=past + 60)))
    status, _ = _status_and_body(sent)
    assert status == 401
    challenge = _header(sent, b"www-authenticate")
    assert 'error="invalid_token"' in challenge
    assert "oauth-protected-resource" in challenge


def test_oidc_serves_each_caller_their_own_mailbox(tmp_path, rsa_keys):
    """With a per-caller upstream there is no configured mailbox to protect:
    a second identity is served, and served THEIR OWN mailbox."""
    from conftest import make_token

    from ewsmcp.identity import SCOPE_PRINCIPAL_KEY
    app = _oidc_app(tmp_path, rsa_keys)
    scope = {}
    status, _ = _status_and_body(_drive(
        app, "/api/tools/echo",
        [{"type": "http.request", "body": b"{}", "more_body": False}],
        headers=_bearer(make_token(rsa_keys, sub="someone-else",
                                   upn="other@corp.example")),
        scope=scope))
    assert status == 200
    assert scope[SCOPE_PRINCIPAL_KEY].smtp == "other@corp.example"


def test_oidc_puts_the_principal_in_the_scope(tmp_path, rsa_keys):
    """The plumbing contract with the tool layer: what lands in the scope here
    is what `call_tool` reads back out (see test_mcp_sdk_pins.py)."""
    from conftest import make_token

    from ewsmcp.auth import SCOPE_PRINCIPAL_KEY
    app = _oidc_app(tmp_path, rsa_keys)
    scope = {}
    _drive(app, "/api/tools/echo",
           [{"type": "http.request", "body": b"{}", "more_body": False}],
           headers=_bearer(make_token(rsa_keys)), scope=scope)
    assert scope[SCOPE_PRINCIPAL_KEY].smtp == "exec@corp.example"


def test_oidc_still_requires_the_api_key_when_one_is_set(tmp_path, rsa_keys):
    """Defence in depth: a leaked JWT alone must not reach the mailbox."""
    from conftest import make_token
    app = _oidc_app(tmp_path, rsa_keys, mcp_api_key="perimeter-secret")
    status, _ = _status_and_body(_drive(
        app, "/api/tools/echo",
        [{"type": "http.request", "body": b"{}", "more_body": False}],
        headers=_bearer(make_token(rsa_keys))))
    assert status == 401


def test_health_endpoints_stay_public_in_oidc(tmp_path, rsa_keys):
    app = _oidc_app(tmp_path, rsa_keys)
    for path in ("/livez", "/health", "/version"):
        status, _ = _status_and_body(_drive(app, path, [], method="GET"))
        assert status == 200, path


def test_protected_resource_metadata_is_public(tmp_path, rsa_keys):
    from conftest import AUDIENCE, ISSUER
    app = _oidc_app(tmp_path, rsa_keys)
    status, body = _status_and_body(_drive(
        app, "/.well-known/oauth-protected-resource", [], method="GET"))
    assert status == 200
    assert body["resource"] == AUDIENCE
    assert body["authorization_servers"] == [ISSUER]


def test_protected_resource_metadata_absent_in_static_mode(tmp_path):
    app = build_app(_ctx(tmp_path), make_settings())
    status, _ = _status_and_body(_drive(
        app, "/.well-known/oauth-protected-resource", [], method="GET"))
    assert status == 404


def test_confirm_token_accepted_by_public_schema(tmp_path):
    """Phase-2 REST calls carry confirm_token; validation must use the
    PUBLIC schema (which injects it), not the raw input schema."""
    ctx = _ctx(tmp_path)
    spec = ctx.registry["echo"]
    spec.confirm = True
    app = build_app(ctx, make_settings())
    status, body = _status_and_body(_post(app, "echo", {"q": "x", "confirm_token": "t"}))
    assert status != 400 or "confirm_token" not in json.dumps(body)
