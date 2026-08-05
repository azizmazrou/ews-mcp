"""MCP wiring: low-level Server, annotations, structured output, lifecycle."""

import logging
from typing import Any, Dict, List

from mcp.server import Server
from mcp.types import Tool, ToolAnnotations

from .audit import AuditLog
from .config import Settings
from .gateway.client import EWSGateway
from .gateway.connection import ConnectionManager
from .ids import NullAliaser, get_aliaser
from .identity import SCOPE_PRINCIPAL_KEY
from .tools import build_registry
from .tools.base import Context, dispatch, for_principal

logger = logging.getLogger(__name__)

ANNOTATIONS = {
    "read": ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False),
    "write": ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                             idempotentHint=False, openWorldHint=False),
    "destructive": ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                   idempotentHint=False, openWorldHint=False),
    "send": ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                            idempotentHint=False, openWorldHint=True),
}


class _NullAudit:
    def record(self, *args, **kwargs) -> None:
        return None


def build_context(settings: Settings) -> Context:
    gateway = EWSGateway(settings)
    # Aliaser/audit are quality-of-life layers — their storage failing
    # (bad volume, permissions) degrades them to pass-through, never
    # prevents boot: the never-exit contract covers local disks too.
    try:
        aliaser = get_aliaser(f"{settings.data_dir}/memory")
    except Exception as exc:
        logger.error("aliaser init failed (%s) — running with raw EWS ids", exc)
        aliaser = NullAliaser()
    try:
        audit = AuditLog(settings.data_dir)
    except Exception as exc:
        logger.error("audit init failed (%s) — audit disabled", exc)
        audit = _NullAudit()
    cache = None
    # A per-caller upstream gets per-caller mirrors (cache/percaller.py); a
    # shared store here would be one mailbox's mail readable by everyone.
    if settings.ews_cache_enabled and not settings.per_caller_upstream:
        try:
            from .cache import CacheStore
            cache = CacheStore(f"{settings.data_dir}/cache/mirror.db")
            if settings.ews_cache_purge_on_boot:
                cache.purge()
        except Exception as exc:
            logger.error("cache init failed (%s) — running pure-EWS reads", exc)
            cache = None
    semantic = None
    try:
        from .semantic import build_semantic_index
        semantic = build_semantic_index(settings)
    except Exception as exc:
        logger.error("semantic index init failed (%s) — keyword-only", exc)
    ctx = Context(
        settings=settings,
        gateway=gateway,
        manager=None,
        aliaser=aliaser,
        audit=audit,
        cache=cache,
        semantic=semantic,
    )
    build_registry(ctx)
    return ctx


async def start_connection_manager(ctx: Context) -> None:
    if ctx.settings.per_caller_upstream:
        # Nothing to warm: there is no boot-time credential, and probing an
        # arbitrary caller's mailbox would mean issuing EWS calls nobody
        # asked for. Readiness is redefined around what IS checkable.
        logger.info("per-caller upstream: no shared connection to warm")
        return
    manager = ConnectionManager(
        ctx.gateway,
        max_backoff=float(ctx.settings.ews_warmup_max_backoff_seconds),
        heartbeat_seconds=int(ctx.settings.ews_heartbeat_seconds),
    )
    ctx.manager = manager

    async def on_warm() -> None:
        # The sync engine is owned by the warm state: it starts only once
        # Exchange has answered (and keeps running through later outages —
        # its own cycles degrade gracefully).
        if ctx.cache is not None and ctx.sync is None:
            try:
                from .cache import SyncEngine
                ctx.sync = SyncEngine(ctx.settings, ctx.gateway, ctx.cache,
                                      semantic=ctx.semantic)
                await ctx.sync.start()
            except Exception as exc:
                logger.error("sync engine start failed (%s) — cache stays "
                             "stale; reads fall back to live EWS", exc)

    await manager.start(on_warm=on_warm)
    logger.info("Exchange warmup running in background (see /readyz)")


async def _bind_caller(server: Server, ctx: Context) -> Context:
    """Recover the principal the ASGI layer verified for THIS request.

    The Streamable HTTP transport hands the tool layer the same ASGI scope
    dict that ``http.build_app`` wrote the principal into (pinned by
    ``test_mcp_sdk_pins.py``). In stdio, and on any path where the SDK does
    not attach a request, there is simply no principal — and ``dispatch``
    refuses rather than falling back to the configured mailbox.
    """
    try:
        request = getattr(server.request_context, "request", None)
        principal = request.scope.get(SCOPE_PRINCIPAL_KEY) if request else None
    except LookupError:  # no request context (direct call, tests)
        principal = None
    if principal is None:
        return ctx
    if ctx.binder is not None:
        return await ctx.binder.bind(ctx, principal)
    return for_principal(ctx, principal)


def build_mcp_server(ctx: Context) -> Server:
    server = Server("ews-mcp-v5")

    @server.list_tools()
    async def list_tools() -> List[Tool]:
        tools = []
        for spec in ctx.registry.values():
            schema = spec.public_schema()
            tools.append(Tool(
                name=schema["name"],
                description=schema["description"],
                inputSchema=schema["inputSchema"],
                annotations=ANNOTATIONS.get(spec.side_effect_class, ANNOTATIONS["write"]),
            ))
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]):
        spec = ctx.registry.get(name)
        if spec is None:
            return {"ok": False, "error": {
                "code": "validation",
                "message": f"Unknown tool: {name}",
                "hint": f"Available: {', '.join(sorted(ctx.registry))}",
            }}
        call_ctx = await _bind_caller(server, ctx)
        return await dispatch(call_ctx, spec, dict(arguments or {}), transport="mcp")

    return server


async def run_stdio(settings: Settings) -> None:
    from mcp.server.stdio import stdio_server

    ctx = build_context(settings)
    server = build_mcp_server(ctx)
    await start_connection_manager(ctx)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

