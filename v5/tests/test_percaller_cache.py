"""Per-caller mirrors, warmed from inside the caller's own requests.

Without a long-lived credential the background sync engine cannot run, and
losing the mirror costs FTS5 (Arabic) search, `find_similar` and
`waiting_on`. Syncing with the token already in hand restores them — for the
people actually using the server, and for nobody else.

The tests below are mostly about what must NOT happen: a sync must never
fail a tool call, never add latency to it, never run twice at once for one
caller, and never let one caller read another's mirror.
"""

import asyncio
from pathlib import Path

from conftest import ISSUER, make_binder, oidc_settings

from ewsmcp.audit import AuditLog
from ewsmcp.auth import Principal
from ewsmcp.cache.percaller import CallerCaches
from ewsmcp.ids import get_aliaser
from ewsmcp.tools.base import Context, ToolSpec, dispatch

ALICE = Principal(subject="user-alice", smtp="alice@corp.example", issuer=ISSUER,
                  expires_at=9e9, raw_token="alice-token")
BOB = Principal(subject="user-bob", smtp="bob@corp.example", issuer=ISSUER,
                expires_at=9e9, raw_token="bob-token")


class _Gateway:
    """Stands in for Exchange: records the sync work handed to it."""

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, fn):
        self.calls += 1
        self.started.set()
        if self.fail:
            raise RuntimeError("Exchange said no")


def _ctx(tmp_path, settings=None) -> Context:
    settings = settings or oidc_settings()
    ctx = Context(settings=settings, gateway=None, manager=None,
                  aliaser=get_aliaser(str(tmp_path / "mem")),
                  audit=AuditLog(str(tmp_path / "data")))
    ctx.binder = make_binder(settings)
    return ctx


async def _handler(ctx, **kwargs):
    return {"ok": True}


SPEC = ToolSpec(name="t", description="test", side_effect_class="read",
                input_schema={"type": "object", "properties": {}},
                handler=_handler, requires_ews=False)


# ------------------------------------------------------------------ isolation

def test_each_caller_gets_their_own_mirror(tmp_path):
    """A shared mirror would be one person's mail readable by everyone —
    the FTS5 index does not know who a row belongs to."""
    caches = CallerCaches(oidc_settings())
    try:
        alice = caches.store_for(ALICE)
        bob = caches.store_for(BOB)
        assert alice is not None and bob is not None
        assert alice.db_path != bob.db_path
    finally:
        asyncio.run(caches.aclose())


def test_the_same_caller_reuses_one_mirror(tmp_path):
    caches = CallerCaches(oidc_settings())
    try:
        assert caches.store_for(ALICE) is caches.store_for(ALICE)
    finally:
        asyncio.run(caches.aclose())


def test_mirror_directories_do_not_name_anyone(tmp_path):
    settings = oidc_settings()
    caches = CallerCaches(settings)
    try:
        caches.store_for(ALICE)
        names = [p.name for p in (Path(settings.data_dir) / "users").iterdir()]
        assert names
        assert not any("alice" in n or "corp.example" in n for n in names)
    finally:
        asyncio.run(caches.aclose())


def test_the_binder_hands_each_caller_their_own_store(tmp_path):
    ctx = _ctx(tmp_path)

    async def run():
        try:
            a = await ctx.binder.bind(ctx, ALICE)
            b = await ctx.binder.bind(ctx, BOB)
            return a.cache, b.cache
        finally:
            await ctx.binder.aclose()

    alice_cache, bob_cache = asyncio.run(run())
    assert alice_cache is not None
    assert alice_cache is not bob_cache


def test_no_shared_mirror_is_built_when_callers_have_their_own(tmp_path):
    """build_context must not open a process-wide store in this mode."""
    from ewsmcp.server import build_context
    ctx = build_context(oidc_settings(data_dir=str(tmp_path / "d")))
    assert ctx.cache is None


# ------------------------------------------------------------------ eviction

def test_stores_are_closed_when_evicted(tmp_path):
    """Each CacheStore holds a SQLite writer connection; dropping the
    reference without closing would leak a file handle per departed caller."""
    caches = CallerCaches(oidc_settings(), max_entries=1)
    closed = []
    try:
        first = caches.store_for(ALICE)
        first.close = lambda: closed.append(True)
        caches.store_for(BOB)  # over capacity, evicts alice
        assert closed == [True]
        assert caches.stats()["mirrors"] == 1
    finally:
        asyncio.run(caches.aclose())


# ------------------------------------------------------------- the sync cycle

def test_a_stale_mirror_is_synced_after_the_call(tmp_path):
    """The whole point: the mirror is warmed with the token already in hand,
    without anyone having to hold a long-lived credential."""
    ctx = _ctx(tmp_path)
    gateway = _Gateway()

    async def run():
        try:
            call_ctx = await ctx.binder.bind(ctx, ALICE)
            call_ctx.gateway = gateway
            await dispatch(call_ctx, SPEC, {})
            await asyncio.sleep(0)  # let the fire-and-forget task run
            await asyncio.gather(*ctx.binder.caches._tasks, return_exceptions=True)
        finally:
            await ctx.binder.aclose()

    asyncio.run(run())
    assert gateway.calls >= 1


def test_a_fresh_mirror_is_not_resynced(tmp_path):
    """A burst of tool calls must not become a burst of SyncFolderItems runs
    against one mailbox."""
    ctx = _ctx(tmp_path)
    gateway = _Gateway()

    async def run():
        try:
            call_ctx = await ctx.binder.bind(ctx, ALICE)
            call_ctx.gateway = gateway
            for _ in range(5):
                await dispatch(call_ctx, SPEC, {})
                await asyncio.gather(*ctx.binder.caches._tasks,
                                     return_exceptions=True)
        finally:
            await ctx.binder.aclose()

    asyncio.run(run())
    assert gateway.calls <= 2, "only the first call should have synced"


def test_a_failing_sync_never_fails_the_tool_call(tmp_path):
    """THE safety property. The call has already returned by the time a cycle
    runs, and a stale mirror is not an outage — reads fall back to live."""
    ctx = _ctx(tmp_path)
    gateway = _Gateway(fail=True)

    async def run():
        try:
            call_ctx = await ctx.binder.bind(ctx, ALICE)
            call_ctx.gateway = gateway
            result = await dispatch(call_ctx, SPEC, {})
            await asyncio.gather(*ctx.binder.caches._tasks, return_exceptions=True)
            # Read the stats BEFORE shutdown clears the entries.
            return result, ctx.binder.caches.stats()
        finally:
            await ctx.binder.aclose()

    result, stats = asyncio.run(run())
    assert result["ok"] is True
    assert stats["degraded"] == 1, "the failure must be recorded, just not raised"


def test_only_one_cycle_per_caller_runs_at_a_time(tmp_path):
    """A burst of concurrent calls from one caller must not become concurrent
    SyncFolderItems runs against their single mailbox."""
    ctx = _ctx(tmp_path)
    ctx.gateway = _Gateway()
    ctx.principal = ALICE

    async def run():
        caches = ctx.binder.caches
        try:
            caches.store_for(ALICE)
            first = caches.schedule(ctx, ALICE)
            second = caches.schedule(ctx, ALICE)  # while the first is in flight
            await asyncio.gather(first, return_exceptions=True)
            return first, second
        finally:
            await ctx.binder.aclose()

    first, second = asyncio.run(run())
    assert first is not None
    assert second is None


def test_shutdown_cancels_in_flight_cycles(tmp_path):
    """A sync must not outlive the server, nor the token it is using."""
    ctx = _ctx(tmp_path)
    ctx.gateway = _Gateway()

    async def run():
        caches = ctx.binder.caches
        caches.store_for(ALICE)
        ctx.principal = ALICE
        task = caches.schedule(ctx, ALICE)
        assert task is not None
        await ctx.binder.aclose()
        return task

    task = asyncio.run(run())
    assert task.done()


def test_static_mode_keeps_the_shared_background_engine(tmp_path):
    """The canary: single-mailbox deployments must not have been changed."""
    from conftest import make_settings

    from ewsmcp.server import build_context
    ctx = build_context(make_settings(data_dir=str(tmp_path / "d")))
    assert ctx.cache is not None
    assert ctx.binder is None
