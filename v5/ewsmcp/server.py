"""MCP wiring: low-level Server, annotations, structured output, lifecycle."""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from mcp.server import Server
from mcp.types import Tool, ToolAnnotations

from .audit import AuditLog
from .cache import CacheStore, SyncEngine, SyncLease
from .cache.lease import DEFAULT_RETRY_SECONDS, LOCK_FILENAME
from .config import Settings
from .gateway.client import EWSGateway
from .gateway.connection import ConnectionManager
from .ids import NullAliaser, get_aliaser
from .tools import build_registry
from .tools.base import Context, dispatch

logger = logging.getLogger(__name__)

LEASE_RETRY_SECONDS = DEFAULT_RETRY_SECONDS

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


def build_context(
    settings: Settings,
    *,
    lease_retry_seconds: float = LEASE_RETRY_SECONDS,
) -> Context:
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
    lease = None
    if settings.ews_cache_enabled:
        try:
            cache = CacheStore(f"{settings.data_dir}/cache/mirror.db")
        except Exception as exc:
            logger.error("cache init failed (%s) — running pure-EWS reads", exc)
            cache = None
        if cache is not None:
            try:
                candidate = SyncLease(
                    f"{settings.data_dir}/cache/{LOCK_FILENAME}",
                    retry_seconds=lease_retry_seconds,
                )
                candidate.try_acquire()
                lease = candidate
            except Exception as exc:
                logger.error(
                    "sync lease init failed (%s) — running sync without lease",
                    exc,
                )
                lease = None
            if settings.ews_cache_purge_on_boot:
                if lease is None or lease.held:
                    try:
                        cache.purge()
                    except Exception as exc:
                        logger.error(
                            "cache purge failed (%s) — running pure-EWS reads",
                            exc,
                        )
                        if lease is not None:
                            lease.release()
                            lease = None
                        cache.close()
                        cache = None
                else:
                    logger.warning(
                        "EWS_CACHE_PURGE_ON_BOOT skipped: another process "
                        "holds the sync lease"
                    )
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
        lease=lease,
    )
    build_registry(ctx)
    return ctx


async def start_connection_manager(
    ctx: Context,
    *,
    lease_retry_seconds: Optional[float] = None,
) -> None:
    manager = ConnectionManager(
        ctx.gateway,
        max_backoff=float(ctx.settings.ews_warmup_max_backoff_seconds),
        heartbeat_seconds=int(ctx.settings.ews_heartbeat_seconds),
    )
    ctx.manager = manager
    warm = False
    stopped = False
    start_lock = asyncio.Lock()
    retry_task: Optional[asyncio.Task] = None

    async def _maybe_start_sync() -> None:
        # The sync engine requires both a warm Exchange session and either a
        # held sync lease or a degraded lease=None fallback.
        async with start_lock:
            if stopped or not warm or ctx.cache is None or ctx.sync is not None:
                return
            if ctx.lease is not None and not ctx.lease.held:
                return
            try:
                engine = SyncEngine(
                    ctx.settings,
                    ctx.gateway,
                    ctx.cache,
                    semantic=ctx.semantic,
                )
                await engine.start()
                if stopped:
                    await engine.stop()
                    return
                ctx.sync = engine
            except Exception as exc:
                logger.error(
                    "sync engine start failed (%s) — cache stays stale; "
                    "reads fall back to live EWS",
                    exc,
                )

    async def on_warm() -> None:
        nonlocal warm
        warm = True
        await _maybe_start_sync()

    async def _lease_retry_loop(interval: float) -> None:
        try:
            while not stopped:
                await asyncio.sleep(interval)
                if stopped or manager._stopped:
                    return
                lease = ctx.lease
                if lease is None or lease.held:
                    return
                try:
                    acquired = await asyncio.to_thread(lease.try_acquire)
                except OSError as exc:
                    logger.error(
                        "sync lease retry failed (%s) — running sync without lease",
                        exc,
                    )
                    ctx.lease = None
                    await _maybe_start_sync()
                    return
                except Exception as exc:
                    logger.warning("sync lease retry error: %s", exc)
                    continue
                if acquired:
                    logger.info(
                        "sync lease acquired — taking over background cache sync"
                    )
                    await _maybe_start_sync()
                    return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("sync lease retry task exited (%s)", exc)

    async def _stop() -> None:
        nonlocal stopped
        stopped = True
        if retry_task is not None and not retry_task.done():
            retry_task.cancel()
            try:
                await retry_task
            except (asyncio.CancelledError, Exception):
                pass
        async with start_lock:
            if ctx.sync is not None:
                try:
                    await ctx.sync.stop()
                except Exception:
                    pass
        if ctx.manager is not None:
            try:
                await ctx.manager.stop()
            except Exception:
                pass
        if ctx.lease is not None:
            ctx.lease.release()

    setattr(ctx, "_stop_background", _stop)
    await manager.start(on_warm=on_warm)
    if ctx.cache is not None and ctx.lease is not None and not ctx.lease.held:
        raw_interval = (
            lease_retry_seconds
            if lease_retry_seconds is not None
            else getattr(ctx.lease, "retry_seconds", LEASE_RETRY_SECONDS)
        )
        interval = max(0.005, float(raw_interval))
        retry_task = asyncio.create_task(
            _lease_retry_loop(interval),
            name="cache-lease-retry",
        )
    logger.info("Exchange warmup running in background (see /readyz)")


async def stop_connection_manager(ctx: Context) -> None:
    stopper = getattr(ctx, "_stop_background", None)
    if callable(stopper):
        await stopper()
        return
    if ctx.sync is not None:
        try:
            await ctx.sync.stop()
        except Exception:
            pass
    if ctx.manager is not None:
        try:
            await ctx.manager.stop()
        except Exception:
            pass
    if ctx.lease is not None:
        ctx.lease.release()


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
        return await dispatch(ctx, spec, dict(arguments or {}), transport="mcp")

    return server


async def run_stdio(settings: Settings) -> None:
    from mcp.server.stdio import stdio_server

    ctx = build_context(settings)
    server = build_mcp_server(ctx)
    await start_connection_manager(ctx)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await stop_connection_manager(ctx)
        if ctx.cache is not None:
            ctx.cache.close()

