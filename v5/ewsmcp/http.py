"""HTTP serving: Streamable HTTP /mcp + REST shim + health (DESIGN.md §Transports)."""

import hmac
import json
import logging
from typing import Any, Dict, Optional

import jsonschema
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from . import __version__
from .auth import (
    PROTECTED_RESOURCE_PATH,
    SCOPE_PRINCIPAL_KEY,
    AuthError,
    TokenVerifier,
    authenticate,
    blocked,
    build_binder,
    protected_resource_metadata,
)
from .errors import HTTP_BY_CODE, ToolError
from .readiness import OidcReadiness
from .server import build_context, build_mcp_server, start_connection_manager
from .tools.base import dispatch, for_principal

logger = logging.getLogger(__name__)

PUBLIC_PATHS = {"/health", "/livez", "/readyz", "/version", PROTECTED_RESOURCE_PATH}
# Endpoints that expose NO caller data — server aggregates and tool schemas.
# A Prometheus scraper has no user identity and must not need one; the shared
# operator key is the right credential for these, even in oidc mode.
OPERATOR_PATHS = {"/metrics", "/openapi.json"}
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


async def _send_json(send, status: int, payload: Dict[str, Any],
                     extra_headers: Optional[list] = None) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode()
    headers = [
        [b"content-type", b"application/json"],
        [b"content-length", str(len(body)).encode()],
    ]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _metrics_text(ctx, verifier=None) -> str:
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
    lines.extend(_identity_metrics(ctx, verifier))
    return "\n".join(lines) + "\n"


def _identity_metrics(ctx, verifier) -> list:
    """Per-caller observability WITHOUT per-caller labels.

    Labelling by mailbox or subject would mean one time series per person per
    tool — thousands, growing with headcount, and a staff directory leaking
    into the metrics endpoint besides. Attribution belongs in the audit log,
    which is hash-chained and identity-stamped; Prometheus gets aggregates,
    and the only labels used here are drawn from small fixed vocabularies.
    """
    lines: list = []
    binder = getattr(ctx, "binder", None)
    if binder is not None:
        stats = binder.pool.stats()
        lines += ["# TYPE ewsmcp_active_principals gauge",
                  f"ewsmcp_active_principals {stats['active_principals']}",
                  "# TYPE ewsmcp_gateway_pool_evictions_total counter",
                  f"ewsmcp_gateway_pool_evictions_total {stats['evictions']}"]
        if binder.caches is not None:
            mirrors = binder.caches.stats()
            lines += ["# TYPE ewsmcp_caller_mirrors gauge",
                      f"ewsmcp_caller_mirrors {mirrors['mirrors']}",
                      "# TYPE ewsmcp_caller_sync_cycles_total counter",
                      f"ewsmcp_caller_sync_cycles_total {mirrors['cycles']}",
                      "# TYPE ewsmcp_caller_mirrors_degraded gauge",
                      f"ewsmcp_caller_mirrors_degraded {mirrors['degraded']}"]
        exchanger = binder.exchanger
        lines += ["# TYPE ewsmcp_obo_exchanges_total counter",
                  f"ewsmcp_obo_exchanges_total {exchanger.exchanges}",
                  "# TYPE ewsmcp_obo_cache_hits_total counter",
                  f"ewsmcp_obo_cache_hits_total {exchanger.cache_hits}"]
        if exchanger.failures:
            lines.append("# TYPE ewsmcp_obo_failures_total counter")
            for reason, count in sorted(exchanger.failures.items()):
                lines.append(f'ewsmcp_obo_failures_total{{reason="{reason}"}} {count}')
    if verifier is not None:
        jwks = getattr(verifier, "jwks", None)
        if jwks is not None:
            lines += ["# TYPE ewsmcp_jwks_refetches_total counter",
                      f"ewsmcp_jwks_refetches_total {jwks.refetches}",
                      "# TYPE ewsmcp_jwks_stale gauge",
                      f"ewsmcp_jwks_stale {1 if jwks.stale else 0}"]
        if verifier.rejections:
            lines.append("# TYPE ewsmcp_jwt_rejections_total counter")
            for reason, count in sorted(verifier.rejections.items()):
                lines.append(f'ewsmcp_jwt_rejections_total{{reason="{reason}"}} {count}')
    return lines


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


def _resource_metadata_url(settings) -> str:
    return PROTECTED_RESOURCE_PATH


async def _authenticate_caller(settings, verifier, scope):
    """Verify the bearer token, and check the server can serve this caller.

    With a per-caller upstream every verified caller gets their OWN mailbox,
    so there is nothing more to check here. Without one the upstream is a
    single static credential: authenticate anyway, but refuse anybody else
    rather than silently serve them the configured mailbox's mail.
    """
    principal = await authenticate(verifier, scope.get("headers"))
    if not settings.per_caller_upstream and principal.smtp != settings.ews_email.lower():
        raise blocked(
            "this server is bound to a single mailbox, which is not yours",
            reason="mailbox_not_served",
            hint="This deployment has no per-caller upstream configured.",
        )
    return principal


def build_app(ctx, settings, streamable: Optional[Any] = None, verifier: Optional[Any] = None):
    """ASGI app closure — separated from serve_http so tests can drive it."""
    api_key = settings.mcp_api_key or ""
    if settings.auth_mode == "oidc" and verifier is None:
        verifier = TokenVerifier.from_settings(settings)
    if settings.per_caller_upstream and ctx.binder is None:
        ctx.binder = build_binder(settings)
    readiness = None
    if settings.per_caller_upstream:
        readiness = OidcReadiness(settings, verifier)

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await start_connection_manager(ctx)
                    if ctx.binder is not None:
                        await ctx.binder.pool.start()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    if verifier is not None:
                        await verifier.aclose()
                    if ctx.binder is not None:
                        await ctx.binder.aclose()
                    if readiness is not None:
                        await readiness.aclose()
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
            if readiness is not None:
                # No shared connection exists to be "warm": readiness is
                # whether a caller COULD be served, probed without holding
                # anyone's credentials.
                conn = await readiness.status()
                ready = conn.get("state") == "ready"
            else:
                conn = ctx.manager.status() if ctx.manager else {"state": "unmanaged"}
                ready = conn.get("state") in ("warm", "unmanaged")
            return await _send_json(send, 200 if ready else 503, {
                "status": "ok" if ready else "unavailable",
                "connection": conn, "tools": len(ctx.registry),
            })

        if path == PROTECTED_RESOURCE_PATH and method == "GET":
            if settings.auth_mode != "oidc":
                return await _send_json(send, 404, {"ok": False, "error": {
                    "code": "not_found",
                    "message": "this server is not an OAuth2 resource server"}})
            return await _send_json(send, 200, protected_resource_metadata(settings))

        # The shared API key is a network perimeter check; in oidc mode it is
        # optional but, when set, required IN ADDITION to a valid token — a
        # leaked JWT alone must not be enough to reach the mailbox.
        api_key_ok = bool(api_key) and _authorized(scope.get("headers"), api_key)
        if api_key and not api_key_ok:
            return await _send_json(send, 401, {"ok": False, "error": {
                "code": "auth_failed", "message": "missing or invalid bearer token"}})

        # Operator endpoints carry no mailbox data, so the operator key alone
        # is enough for them. Without a key configured they still need a
        # verified token — never open, whatever the mode.
        operator_only = path in OPERATOR_PATHS and api_key_ok
        if settings.auth_mode == "oidc" and not operator_only:
            try:
                scope[SCOPE_PRINCIPAL_KEY] = await _authenticate_caller(
                    settings, verifier, scope)
            except AuthError as err:
                return await _send_json(
                    send, err.http_status, err.to_dict(),
                    extra_headers=[[b"www-authenticate", err.challenge(
                        resource_metadata=_resource_metadata_url(settings))]],
                )
            except ToolError as err:  # JWKS unreachable and nothing cached
                return await _send_json(send, err.http_status, err.to_dict())

        if path == "/mcp":
            if streamable is None:
                return await _send_json(send, 503, {"ok": False, "error": {
                    "code": "upstream_unavailable",
                    "message": "MCP transport not mounted"}})
            return await streamable.handle_request(scope, receive, send)
        if path == "/metrics" and method == "GET":
            body = _metrics_text(ctx, verifier).encode()
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
            principal = scope.get(SCOPE_PRINCIPAL_KEY)
            if principal is None:
                call_ctx = ctx
            elif ctx.binder is not None:
                try:
                    call_ctx = await ctx.binder.bind(ctx, principal)
                except ToolError as err:
                    return await _send_json(send, err.http_status, err.to_dict())
            else:
                call_ctx = for_principal(ctx, principal)
            result = await dispatch(call_ctx, spec, arguments, transport="rest")
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
    mcp_server = build_mcp_server(ctx)
    streamable = StreamableHTTPSessionManager(app=mcp_server, json_response=False,
                                              stateless=True)
    app = build_app(ctx, settings, streamable)
    config = uvicorn.Config(app, host=settings.mcp_host, port=settings.mcp_port,
                            log_level=settings.log_level.lower(), http="h11")
    async with streamable.run():
        await uvicorn.Server(config).serve()
