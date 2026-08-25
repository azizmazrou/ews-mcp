"""MCP wiring: low-level Server, annotations, structured output, lifecycle."""

import logging
from typing import Any, Dict, List, Optional

from mcp.server import Server
from mcp.types import Tool, ToolAnnotations

from .audit import AuditLog
from .config import Settings
from .errors import ToolError
from .gateway.client import EWSGateway
from .gateway.connection import ConnectionManager
from .ids import NullAliaser, get_aliaser
from .multiuser import UserContextCache, extract_credentials_from_request
from .tools import build_registry
from .tools.base import Context, dispatch

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
    if settings.ews_multi_user:
        # No shared mailbox in this mode — gateway/aliaser/audit/cache/
        # semantic are all per-user (see multiuser.UserContextCache). This
        # Context exists only to hold the (stateless, tier-filtered) tool
        # registry that every per-user Context then reuses.
        ctx = Context(
            settings=settings, gateway=None, manager=None,
            aliaser=NullAliaser(), audit=_NullAudit(),
        )
        build_registry(ctx)
        return ctx
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
    if settings.ews_cache_enabled:
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
    if ctx.gateway is None:
        # Multi-user mode: no shared mailbox to warm up — each per-user
        # gateway (built lazily in multiuser.UserContextCache) connects on
        # that user's first real tool call instead.
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


def build_mcp_server(ctx: Context, user_cache: Optional[UserContextCache] = None) -> Server:
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
        active_ctx = ctx
        if user_cache is not None:
            try:
                request = server.request_context.request
            except LookupError:
                request = None
            try:
                email, password = extract_credentials_from_request(request)
            except ToolError as err:
                return err.to_dict()
            active_ctx = user_cache.get(email, password)
        return await dispatch(active_ctx, spec, dict(arguments or {}), transport="mcp")

    return server


async def run_stdio(settings: Settings) -> None:
    from mcp.server.stdio import stdio_server

    if settings.ews_multi_user:
        raise RuntimeError(
            "EWS_MULTI_USER=true requires MCP_TRANSPORT=http — stdio is a "
            "single, already-authenticated local process with no per-request "
            "identity to attach credentials to."
        )
    ctx = build_context(settings)
    server = build_mcp_server(ctx)
    await start_connection_manager(ctx)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

