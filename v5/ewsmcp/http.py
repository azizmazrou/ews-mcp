"""HTTP serving: Streamable HTTP /mcp + REST shim + health (DESIGN.md §Transports)."""

import hmac
import json
import logging
from typing import Any, Dict, Optional

import jsonschema
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from . import __version__
from .errors import HTTP_BY_CODE, ToolError
from .multiuser import UserContextCache, extract_credentials_from_asgi_headers
from .server import build_context, build_mcp_server, start_connection_manager
from .tools.base import dispatch

logger = logging.getLogger(__name__)

PUBLIC_PATHS = {"/health", "/livez", "/readyz", "/version"}
MAX_BODY_BYTES = 1_048_576  # 1 MiB — tool arguments, not attachments


def _validator_for(spec) -> Any:
    """Compiled validator for the tool's PUBLIC schema (which includes
    confirm_token for two-phase tools), cached on the spec itself so it can
    never go stale against a different spec of the same name."""
    v = getattr(spec, "_rest_validator", None)
    if v is None:
        v = jsonschema.Draft202012Validator(spec.public_schema()["inputSchema"])
        spec._rest_validator = v
    return v


def _authorized(headers, api_key: str) -> bool:
    expected = api_key.encode()
    for name, value in headers or []:
        lname = name.lower() if isinstance(name, bytes) else str(name).encode().lower()
        raw = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
        if lname == b"authorization" and raw.lower().startswith("bearer "):
            if hmac.compare_digest(raw[7:].strip().encode(), expected):
                return True
        elif lname == b"x-api-key":
            if hmac.compare_digest(raw.strip().encode(), expected):
                return True
    return False


async def _send_json(send, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        [b"content-type", b"application/json"],
        [b"content-length", str(len(body)).encode()],
    ]})
    await send({"type": "http.response.body", "body": body})


def _metrics_text(ctx) -> str:
    """Prometheus exposition (text format 0.0.4). Behind the API key like
    every non-health endpoint — scrape with a bearer token."""
    import time as _t
    lines = [
        "# TYPE ewsmcp_uptime_seconds gauge",
        f"ewsmcp_uptime_seconds {int(_t.time() - ctx.started_at)}",
    ]
    state = ctx.manager.state if ctx.manager else "unmanaged"
    lines += ["# TYPE ewsmcp_connection_warm gauge",
              f"ewsmcp_connection_warm {1 if state in ('warm', 'unmanaged') else 0}"]
    lines.append("# TYPE ewsmcp_tool_calls_total counter")
    lines.append("# TYPE ewsmcp_errors_total counter")
    for key, value in sorted(ctx.counters.items()):
        if key.startswith("tool."):
            lines.append(f'ewsmcp_tool_calls_total{{tool="{key[5:]}"}} {value}')
        elif key.startswith("err."):
            lines.append(f'ewsmcp_errors_total{{code="{key[4:]}"}} {value}')
    if ctx.cache is not None:
        try:
            stats = ctx.cache.stats()
            lines.append("# TYPE ewsmcp_cache_rows gauge")
            for table, n in stats.get("rows", {}).items():
                lines.append(f'ewsmcp_cache_rows{{table="{table}"}} {n}')
            lines.append("# TYPE ewsmcp_cache_db_mb gauge")
            lines.append(f"ewsmcp_cache_db_mb {stats.get('db_mb', 0)}")
        except Exception:
            pass
    if ctx.sync is not None:
        status = ctx.sync.status()
        lines.append("# TYPE ewsmcp_sync_cycles_total counter")
        lines.append(f"ewsmcp_sync_cycles_total {status.get('cycles', 0)}")
        age = status.get("last_cycle_age_s")
        if age is not None:
            lines.append("# TYPE ewsmcp_sync_last_cycle_age_seconds gauge")
            lines.append(f"ewsmcp_sync_last_cycle_age_seconds {age}")
        lines.append("# TYPE ewsmcp_sync_degraded gauge")
        lines.append(f"ewsmcp_sync_degraded {1 if status.get('last_error') else 0}")
    if ctx.semantic is not None:
        lines.append("# TYPE ewsmcp_semantic_enabled gauge")
        lines.append("ewsmcp_semantic_enabled 1")
    return "\n".join(lines) + "\n"


def _openapi(ctx) -> Dict[str, Any]:
    paths = {}
    for name, spec in ctx.registry.items():
        schema = spec.public_schema()
        paths[f"/api/tools/{name}"] = {"post": {
            "operationId": name,
            "summary": schema["description"][:120],
            "requestBody": {"content": {"application/json": {"schema": schema["inputSchema"]}}},
            "responses": {"200": {"description": "tool result"}},
        }}
    return {"openapi": "3.0.3",
            "info": {"title": "ews-mcp v5", "version": __version__},
            "paths": paths}


async def _read_json_body(receive, send) -> Optional[Any]:
    """Drain the request body (bounded) and parse JSON.

    Returns the parsed value, or None after having already sent an error
    response. A client disconnect mid-body returns None without sending
    (the old loop hung forever waiting for more http.request messages).
    """
    chunks = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        if message["type"] == "http.request":
            body = message.get("body", b"")
            size += len(body)
            if size > MAX_BODY_BYTES:
                await _send_json(send, 413, {"ok": False, "error": {
                    "code": "validation",
                    "message": f"request body exceeds {MAX_BODY_BYTES} bytes"}})
                return None
            chunks.append(body)
            if not message.get("more_body"):
                break
    try:
        return json.loads(b"".join(chunks) or b"{}")
    except Exception as e:
        await _send_json(send, 400, {"ok": False, "error": {
            "code": "validation", "message": f"invalid JSON body: {e}"}})
        return None


def build_app(ctx, settings, streamable: Optional[Any] = None,
              user_cache: Optional[UserContextCache] = None):
    """ASGI app closure — separated from serve_http so tests can drive it."""
    api_key = settings.mcp_api_key or ""

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await start_connection_manager(ctx)
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        path, method = scope["path"], scope["method"]

        if path == "/livez" and method == "GET":
            return await _send_json(send, 200, {"status": "ok"})
        if path == "/health" and method == "GET":
            return await _send_json(send, 200, {"status": "ok", "tools": len(ctx.registry)})
        if path == "/version" and method == "GET":
            return await _send_json(send, 200, {"version": __version__})
        if path == "/readyz" and method == "GET":
            conn = ctx.manager.status() if ctx.manager else {"state": "unmanaged"}
            warm = conn.get("state") in ("warm", "unmanaged")
            return await _send_json(send, 200 if warm else 503, {
                "status": "ok" if warm else "unavailable",
                "connection": conn, "tools": len(ctx.registry),
            })

        # OAuth discovery/registration — we implement neither. A 401 here
        # (which the api_key gate below would otherwise produce for every
        # path) reads to a spec-compliant MCP client as "OAuth required,
        # but no metadata available", which surfaced as a dead-end OAuth
        # prompt in LibreChat even though this server only ever wanted
        # header-based auth. 404 is the correct "not supported" signal —
        # the client is expected to fall back to its configured headers.
        if "/.well-known/" in path or path == "/register":
            return await _send_json(send, 404, {"ok": False, "error": {
                "code": "validation", "message": "not found"}})

        if api_key and not _authorized(scope.get("headers"), api_key):
            return await _send_json(send, 401, {"ok": False, "error": {
                "code": "auth_failed", "message": "missing or invalid bearer token"}})

        if path == "/mcp":
            if streamable is None:
                return await _send_json(send, 503, {"ok": False, "error": {
                    "code": "upstream_unavailable",
                    "message": "MCP transport not mounted"}})
            return await streamable.handle_request(scope, receive, send)
        if path == "/metrics" and method == "GET":
            body = _metrics_text(ctx).encode()
            await send({"type": "http.response.start", "status": 200, "headers": [
                [b"content-type", b"text/plain; version=0.0.4; charset=utf-8"],
                [b"content-length", str(len(body)).encode()],
            ]})
            return await send({"type": "http.response.body", "body": body})
        if path == "/openapi.json" and method == "GET":
            return await _send_json(send, 200, _openapi(ctx))
        if path == "/api/tools" and method == "GET":
            return await _send_json(send, 200, {"tools": [
                {"name": s.name, "class": s.side_effect_class,
                 "description": s.description[:140]}
                for s in ctx.registry.values()
            ]})
        if path.startswith("/api/tools/") and method == "POST":
            name = path.removeprefix("/api/tools/")
            spec = ctx.registry.get(name)
            if spec is None:
                return await _send_json(send, 404, {"ok": False, "error": {
                    "code": "validation", "message": f"Unknown tool: {name}"}})
            active_ctx = ctx
            if user_cache is not None:
                try:
                    email, password = extract_credentials_from_asgi_headers(scope.get("headers"))
                except ToolError as err:
                    return await _send_json(send, err.http_status, err.to_dict())
                active_ctx = user_cache.get(email, password)
            arguments = await _read_json_body(receive, send)
            if arguments is None:
                return
            if not isinstance(arguments, dict):
                return await _send_json(send, 400, {"ok": False, "error": {
                    "code": "validation",
                    "message": "request body must be a JSON object of tool arguments"}})
            error = jsonschema.exceptions.best_match(
                _validator_for(spec).iter_errors(arguments))
            if error is not None:
                return await _send_json(send, 400, {"ok": False, "error": {
                    "code": "validation", "message": error.message,
                    "hint": f"See the {name} schema in /openapi.json."}})
            result = await dispatch(active_ctx, spec, arguments, transport="rest")
            status = 200
            if isinstance(result, dict) and result.get("ok") is False:
                status = HTTP_BY_CODE.get(result.get("error", {}).get("code", ""), 500)
            return await _send_json(send, status, result)

        return await _send_json(send, 404, {"ok": False, "error": {
            "code": "validation", "message": "not found"}})

    return app


async def serve_http(settings) -> None:
    import uvicorn

    ctx = build_context(settings)
    user_cache = UserContextCache(ctx) if settings.ews_multi_user else None
    mcp_server = build_mcp_server(ctx, user_cache=user_cache)
    streamable = StreamableHTTPSessionManager(app=mcp_server, json_response=False,
                                              stateless=True)
    app = build_app(ctx, settings, streamable, user_cache=user_cache)
    config = uvicorn.Config(app, host=settings.mcp_host, port=settings.mcp_port,
                            log_level=settings.log_level.lower(), http="h11")
    async with streamable.run():
        await uvicorn.Server(config).serve()
