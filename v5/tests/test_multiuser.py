"""Multi-user mode: per-request credential extraction + the per-user
Context cache (multiuser.py), plus the config/transport guards around it.
"""

import time

import pytest
from starlette.datastructures import Headers

from conftest import make_settings

from ewsmcp.config import Settings
from ewsmcp.errors import ToolError
from ewsmcp.http import build_app
from ewsmcp.multiuser import (
    EMAIL_HEADER,
    PASSWORD_HEADER,
    UserContextCache,
    extract_credentials_from_asgi_headers,
    extract_credentials_from_request,
)
from ewsmcp.server import build_context, start_connection_manager
from ewsmcp.tools.base import Context


def _asgi_headers(pairs):
    return [(k.encode(), v.encode()) for k, v in pairs]


# --------------------------------------------------------------- extraction

def test_asgi_headers_happy_path():
    email, password = extract_credentials_from_asgi_headers(
        _asgi_headers([("X-EWS-Email", "User@Corp.example"), ("x-ews-password", "pw")]))
    assert email == "user@corp.example"  # lowercased
    assert password == "pw"  # password case preserved


def test_asgi_headers_missing_raises_auth_failed():
    with pytest.raises(ToolError) as exc:
        extract_credentials_from_asgi_headers(_asgi_headers([(EMAIL_HEADER, "a@b.com")]))
    assert exc.value.code == "auth_failed"
    assert exc.value.http_status == 401


def test_request_headers_happy_path():
    request = type("R", (), {"headers": Headers({EMAIL_HEADER: "a@b.com", PASSWORD_HEADER: "pw"})})()
    email, password = extract_credentials_from_request(request)
    assert (email, password) == ("a@b.com", "pw")


def test_request_none_raises_auth_failed():
    with pytest.raises(ToolError):
        extract_credentials_from_request(None)


# --------------------------------------------------------------- cache

def _template(tmp_path) -> Context:
    settings = make_settings(ews_multi_user=True, ews_email=None, mcp_transport="http")
    return build_context(settings)


def test_cache_builds_and_reuses_same_context(tmp_path):
    cache = UserContextCache(_template(tmp_path))
    ctx1 = cache.get("a@b.com", "pw")
    ctx2 = cache.get("a@b.com", "pw")
    assert ctx1 is ctx2
    assert ctx1.settings.ews_email == "a@b.com"
    assert ctx1.settings.ews_password == "pw"


def test_cache_shares_the_template_registry(tmp_path):
    template = _template(tmp_path)
    cache = UserContextCache(template)
    ctx = cache.get("a@b.com", "pw")
    assert ctx.registry is template.registry


def test_cache_rebuilds_on_password_change(tmp_path):
    cache = UserContextCache(_template(tmp_path))
    ctx1 = cache.get("a@b.com", "old-pw")
    ctx2 = cache.get("a@b.com", "new-pw")
    assert ctx1 is not ctx2
    assert ctx2.settings.ews_password == "new-pw"


def test_cache_evicts_oldest_beyond_max_size(tmp_path):
    cache = UserContextCache(_template(tmp_path), max_size=2)
    a = cache.get("a@b.com", "pw")
    cache.get("b@b.com", "pw")
    cache.get("c@b.com", "pw")  # evicts a@b.com (least recently used)
    assert "a@b.com" not in cache._entries
    a2 = cache.get("a@b.com", "pw")
    assert a2 is not a  # rebuilt, not reused


def test_cache_evicts_idle_entries(tmp_path):
    cache = UserContextCache(_template(tmp_path), idle_evict_seconds=0.01)
    cache.get("a@b.com", "pw")
    time.sleep(0.05)
    cache.get("b@b.com", "pw")  # triggers idle sweep
    assert "a@b.com" not in cache._entries


def test_cache_per_user_isolation_of_gateway(tmp_path):
    cache = UserContextCache(_template(tmp_path))
    a = cache.get("a@b.com", "pw")
    b = cache.get("b@b.com", "pw")
    assert a.gateway is not b.gateway
    assert a.settings.ews_email != b.settings.ews_email


def test_cache_gives_each_user_own_memory_aliases(tmp_path):
    cache = UserContextCache(_template(tmp_path))
    a = cache.get("a@b.com", "pw")
    b = cache.get("b@b.com", "pw")
    raw = "AAMkRAW" + "x" * 140
    alias = a.aliaser.alias_for(raw, "m", changekey="ck", internet_message_id="<i@x>")
    assert alias.startswith("m") and len(alias) < 10
    assert a.aliaser.alias_for(raw, "m") == alias
    assert a.aliaser.resolve(alias) == raw
    assert a.aliaser.resolve(raw) == raw
    assert a.aliaser.imid_for(alias) == "<i@x>"
    with pytest.raises(KeyError):
        b.aliaser.resolve(alias)
    assert a.aliaser.rebind(raw, "AAMkNEW") == alias
    assert a.aliaser.resolve(alias) == "AAMkNEW"


# --------------------------------------------------------------- config

def test_ews_email_required_when_not_multi_user():
    with pytest.raises(Exception, match="EWS_EMAIL"):
        Settings(ews_server_url="https://mail.corp.example/EWS/Exchange.asmx")


def test_ews_email_optional_in_multi_user_mode():
    s = Settings(ews_server_url="https://mail.corp.example/EWS/Exchange.asmx",
                ews_multi_user=True, mcp_transport="http", data_dir="/tmp/ewsmcp-test-mu")
    assert s.ews_email is None


# --------------------------------------------------------------- server wiring

def test_build_context_multi_user_has_no_shared_gateway(tmp_path):
    ctx = _template(tmp_path)
    assert ctx.gateway is None
    assert ctx.cache is None
    assert ctx.semantic is None
    assert len(ctx.registry) > 0


async def _noop():
    return None


def test_start_connection_manager_is_a_noop_without_gateway(tmp_path):
    import asyncio
    ctx = _template(tmp_path)
    asyncio.run(start_connection_manager(ctx))
    assert ctx.manager is None


def test_run_stdio_rejects_multi_user(tmp_path):
    import asyncio
    from ewsmcp.server import run_stdio
    settings = make_settings(ews_multi_user=True, ews_email=None, mcp_transport="stdio")
    with pytest.raises(RuntimeError, match="MCP_TRANSPORT=http"):
        asyncio.run(run_stdio(settings))


# --------------------------------------------------------------- HTTP shim

def test_rest_call_without_credentials_is_401(tmp_path):
    import asyncio
    import json

    ctx = _template(tmp_path)
    cache = UserContextCache(ctx)
    app = build_app(ctx, make_settings(), user_cache=cache)
    name = next(iter(ctx.registry))

    sent = []

    async def drive():
        scope = {"type": "http", "path": f"/api/tools/{name}", "method": "POST", "headers": []}
        queue = [{"type": "http.request", "body": b"{}", "more_body": False}]

        async def receive():
            return queue.pop(0)

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)

    asyncio.run(drive())
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))
    assert status == 401
    assert body["error"]["code"] == "auth_failed"


# --------------------------------------------------------------- impersonation

def _imp_settings(**overrides):
    base = dict(ews_multi_user=True, ews_email=None, mcp_transport="http",
                ews_multi_user_auth="impersonation", mcp_api_key="k")
    base.update(overrides)
    return make_settings(**base)


def test_impersonation_requires_service_account():
    with pytest.raises(Exception, match="EWS_USERNAME and EWS_PASSWORD"):
        _imp_settings(ews_username=None)


def test_impersonation_requires_api_key():
    with pytest.raises(Exception, match="MCP_API_KEY"):
        _imp_settings(mcp_api_key=None)


def test_impersonation_header_names_the_mailbox_only():
    email, password = extract_credentials_from_asgi_headers(
        _asgi_headers([("X-EWS-Email", "User@Corp.example")]), impersonation=True)
    assert (email, password) == ("user@corp.example", None)


def test_impersonation_refuses_a_password_header():
    with pytest.raises(ToolError) as exc:
        extract_credentials_from_asgi_headers(
            _asgi_headers([(EMAIL_HEADER, "a@corp.example"), (PASSWORD_HEADER, "pw")]),
            impersonation=True)
    assert exc.value.code == "auth_failed"


def test_impersonation_missing_mailbox_is_auth_failed():
    with pytest.raises(ToolError) as exc:
        extract_credentials_from_asgi_headers([], impersonation=True)
    assert exc.value.code == "auth_failed"


def test_impersonation_domain_allowlist():
    allowed = frozenset({"corp.example"})
    email, _ = extract_credentials_from_asgi_headers(
        _asgi_headers([(EMAIL_HEADER, "a@corp.example")]), True, allowed)
    assert email == "a@corp.example"
    with pytest.raises(ToolError) as exc:
        extract_credentials_from_asgi_headers(
            _asgi_headers([(EMAIL_HEADER, "a@other.example")]), True, allowed)
    assert exc.value.code == "forbidden"
    assert exc.value.http_status == 403


def test_impersonation_allowed_domains_parsing():
    s = _imp_settings(ews_impersonation_allowed_domains=" Corp.example, @sub.corp.example ,")
    assert s.impersonation_domains == frozenset({"corp.example", "sub.corp.example"})


def test_impersonation_context_keeps_service_account_and_impersonates(tmp_path):
    from exchangelib import IMPERSONATION
    cache = UserContextCache(build_context(_imp_settings()))
    ctx = cache.get("a@corp.example", None)
    assert ctx.settings.ews_email == "a@corp.example"
    assert ctx.settings.ews_username == "svc"
    assert ctx.settings.ews_password == "pw"
    assert ctx.gateway._access_type() == IMPERSONATION
    assert ctx.gateway._configuration().credentials.username == "svc"


def test_password_mode_still_uses_delegate(tmp_path):
    from exchangelib import DELEGATE
    cache = UserContextCache(_template(tmp_path))
    ctx = cache.get("a@b.com", "pw")
    assert ctx.gateway._access_type() == DELEGATE


def test_impersonation_eviction_keeps_the_shared_protocol(tmp_path, monkeypatch):
    from exchangelib.protocol import CachingProtocol
    cache = UserContextCache(build_context(_imp_settings()))
    ctx = cache.get("a@corp.example", None)
    ctx.gateway._account = object()  # stands in for a built Account
    cleared = []
    monkeypatch.setattr(CachingProtocol, "clear_cache", classmethod(lambda cls: cleared.append(1)))
    ctx.gateway.close()
    assert ctx.gateway._account is None
    assert cleared == []


def test_impersonation_reuses_context_per_mailbox(tmp_path):
    cache = UserContextCache(build_context(_imp_settings()))
    assert cache.get("a@corp.example", None) is cache.get("a@corp.example", None)
    assert cache.get("a@corp.example", None) is not cache.get("b@corp.example", None)


# --------------------------------------------------------------- token

import base64 as _b64
import json as _json


def _jwt(claims):
    def part(obj):
        return _b64.urlsafe_b64encode(_json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'RS256', 'typ': 'JWT'})}.{part(claims)}.c2ln"


def _tok_settings(**overrides):
    base = dict(ews_multi_user=True, ews_email=None, mcp_transport="http",
                ews_multi_user_auth="token")
    base.update(overrides)
    return make_settings(**base)


def test_token_mode_mailbox_from_claim():
    tok = _jwt({"upn": "User@Corp.example", "exp": time.time() + 3600})
    email, token = extract_credentials_from_asgi_headers(
        _asgi_headers([("Authorization", f"Bearer {tok}")]), token_auth=True)
    assert (email, token) == ("user@corp.example", tok)


def test_token_mode_header_wins_over_claim():
    tok = _jwt({"upn": "a@corp.example"})
    email, _ = extract_credentials_from_asgi_headers(
        _asgi_headers([("Authorization", f"Bearer {tok}"), (EMAIL_HEADER, "shared@corp.example")]),
        token_auth=True)
    assert email == "shared@corp.example"


def test_token_mode_without_mailbox_is_auth_failed():
    with pytest.raises(ToolError) as exc:
        extract_credentials_from_asgi_headers(
            _asgi_headers([("Authorization", f"Bearer {_jwt({'sub': 'x'})}")]), token_auth=True)
    assert exc.value.code == "auth_failed"


def test_token_mode_refuses_password_header():
    with pytest.raises(ToolError):
        extract_credentials_from_asgi_headers(
            _asgi_headers([("Authorization", f"Bearer {_jwt({'upn': 'a@b.c'})}"),
                           (PASSWORD_HEADER, "pw")]), token_auth=True)


def test_token_transport_error():
    from ewsmcp.multiuser import token_transport_error
    assert token_transport_error(None)[0] == "Bearer"
    assert token_transport_error("Basic abc")[0] == "Bearer"
    expired = token_transport_error(f"Bearer {_jwt({'exp': time.time() - 60})}")
    assert 'error="invalid_token"' in expired[0]
    assert token_transport_error(f"Bearer {_jwt({'exp': time.time() + 10})}") is not None  # leeway
    assert token_transport_error(f"Bearer {_jwt({'exp': time.time() + 3600})}") is None
    assert token_transport_error("Bearer opaque-token") is None


def test_token_mode_context_uses_oauth(tmp_path):
    from exchangelib import OAUTH2, OAuth2AuthorizationCodeCredentials
    exp = time.time() + 3600
    tok = _jwt({"upn": "a@corp.example", "exp": exp})
    cache = UserContextCache(build_context(_tok_settings()))
    ctx = cache.get("a@corp.example", tok)
    assert ctx.settings.ews_password is None and ctx.settings.ews_username is None
    config = ctx.gateway._configuration()
    creds = config.credentials
    assert isinstance(creds, OAuth2AuthorizationCodeCredentials)
    assert creds.access_token["access_token"] == tok
    assert creds.access_token["expires_at"] == exp
    assert config.auth_type == OAUTH2


def test_token_mode_new_token_rebuilds_context(tmp_path):
    cache = UserContextCache(build_context(_tok_settings()))
    a = cache.get("a@corp.example", _jwt({"upn": "a@corp.example", "n": 1}))
    b = cache.get("a@corp.example", _jwt({"upn": "a@corp.example", "n": 2}))
    assert a is not b


def _drive(app, path, headers):
    import asyncio

    sent = []

    async def run():
        scope = {"type": "http", "path": path, "method": "POST",
                 "headers": [(k.lower().encode(), v.encode()) for k, v in headers]}
        queue = [{"type": "http.request", "body": b"{}", "more_body": False}]

        async def receive():
            return queue.pop(0)

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)

    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], dict(start["headers"])


def test_token_mode_expired_token_is_transport_401(tmp_path):
    ctx = build_context(_tok_settings())
    app = build_app(ctx, _tok_settings(), user_cache=UserContextCache(ctx))
    name = next(iter(ctx.registry))
    status, headers = _drive(app, f"/api/tools/{name}",
                             [("Authorization", f"Bearer {_jwt({'upn': 'a@b.c', 'exp': time.time() - 60})}")])
    assert status == 401
    assert b'error="invalid_token"' in headers[b"www-authenticate"]
    status, headers = _drive(app, "/mcp", [])
    assert status == 401
    assert headers[b"www-authenticate"] == b"Bearer"


def test_token_mode_api_key_via_x_api_key(tmp_path):
    settings = _tok_settings(mcp_api_key="k")
    ctx = build_context(settings)
    app = build_app(ctx, settings, user_cache=UserContextCache(ctx))
    expired = f"Bearer {_jwt({'upn': 'a@b.c', 'exp': time.time() - 60})}"
    status, _ = _drive(app, "/mcp", [("Authorization", expired)])
    assert status == 401  # API key gate: no X-API-Key
    status, headers = _drive(app, "/mcp", [("Authorization", expired), ("X-API-Key", "k")])
    assert status == 401 and b"invalid_token" in headers[b"www-authenticate"]
