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
