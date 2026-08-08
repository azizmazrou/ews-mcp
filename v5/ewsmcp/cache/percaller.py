"""A private mirror per caller, warmed from inside their own requests.

The background `SyncEngine` cannot run under a per-caller upstream: it needs
a long-lived credential, and a delegated access token lives about an hour
with no refresh token stored anywhere by design. Losing the mirror costs
FTS5 (Arabic) search, `find_similar` and `waiting_on` — a real regression.

The way back is to sync with the token that is *already in hand*: after a
caller's tool call completes, if their mirror has gone stale, run one bounded
cycle in the background. The mirror is then warm for people who are actually
using the server, and costs nothing for the rest. Nobody's mailbox is
synced without them asking for something first.

Three properties this must have, in order of how badly they bite:

- **It must never fail a tool call.** The call has already returned by the
  time a cycle starts, and every failure degrades to live reads.
- **One cycle per caller at a time.** Otherwise a burst of tool calls
  becomes a burst of concurrent `SyncFolderItems` runs against one mailbox.
- **Stores must be closed on eviction.** Each `CacheStore` holds a SQLite
  writer connection; a bounded LRU that merely drops references would leak
  file handles as staff turn over.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from ..identity import namespace_uid
from .store import CacheStore
from .sync import SyncEngine

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    store: CacheStore
    last_sync_ts: float = 0.0
    syncing: bool = False
    cycles: int = 0
    last_error: Optional[str] = None
    last_used: float = field(default_factory=time.time)


class CallerCaches:
    """Bounded LRU of per-caller mirrors, plus the opportunistic sync."""

    def __init__(self, settings, max_entries: Optional[int] = None):
        self.settings = settings
        self.max_entries = max_entries or max(1, settings.auth_gateway_pool_max)
        self._entries: OrderedDict = OrderedDict()
        self._lock = asyncio.Lock()
        # Strong references: a bare create_task() can be garbage-collected
        # mid-flight, which would cancel syncs at random under memory pressure.
        self._tasks: Set[asyncio.Task] = set()
        self.evictions = 0

    # ----------------------------------------------------------------- store

    def store_for(self, principal) -> Optional[CacheStore]:
        """This caller's mirror, opened on first use. None if unavailable.

        Returning None is a supported outcome, not an error: the whole cache
        tier is optional and every read has a live fallback.
        """
        key = principal.key
        entry = self._entries.get(key)
        if entry is not None:
            entry.last_used = time.time()
            self._entries.move_to_end(key)
            return entry.store
        uid = namespace_uid(self.settings, key)
        path = f"{self.settings.data_dir}/users/{uid}/cache/mirror.db"
        try:
            store = CacheStore(path)
        except Exception as exc:
            logger.error("per-caller cache init failed (%s) — live reads only", exc)
            return None
        self._entries[key] = _Entry(store=store)
        self._entries.move_to_end(key)
        self._evict_over_capacity()
        return store

    def _evict_over_capacity(self) -> None:
        while len(self._entries) > self.max_entries:
            _, entry = self._entries.popitem(last=False)
            self._close(entry)

    def _close(self, entry: _Entry) -> None:
        self.evictions += 1
        try:
            entry.store.close()
        except Exception as exc:
            logger.debug("closing a per-caller cache failed: %s", exc)

    # ------------------------------------------------------------ sync cycle

    def due(self, principal) -> bool:
        entry = self._entries.get(principal.key)
        if entry is None or entry.syncing:
            return False
        interval = max(5, int(self.settings.ews_cache_sync_seconds))
        return time.time() - entry.last_sync_ts >= interval

    def schedule(self, ctx, principal) -> Optional[asyncio.Task]:
        """Kick one bounded cycle in the background. Never raises, never waits.

        Called after the caller's response is already on its way, so the
        latency cost to them is zero.
        """
        if not self.due(principal):
            return None
        entry = self._entries.get(principal.key)
        if entry is None:
            return None
        entry.syncing = True
        task = asyncio.create_task(self._run_cycle(ctx, entry),
                                   name="cache-sync-in-request")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run_cycle(self, ctx, entry: _Entry) -> None:
        engine = SyncEngine(self.settings, ctx.gateway, entry.store,
                            semantic=ctx.semantic)
        try:
            await engine._cycle()
            entry.last_error = None
            entry.cycles += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Degrade, never surface: the caller's tool call already returned,
            # and reads fall back to live EWS. A stale mirror is not an outage.
            entry.last_error = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning("in-request cache sync failed: %s", entry.last_error)
        finally:
            entry.last_sync_ts = time.time()
            entry.syncing = False

    # ------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for entry in list(self._entries.values()):
            self._close(entry)
        self._entries.clear()

    def stats(self) -> Dict[str, Any]:
        return {
            "mirrors": len(self._entries),
            "evictions": self.evictions,
            "cycles": sum(e.cycles for e in self._entries.values()),
            "syncing": sum(1 for e in self._entries.values() if e.syncing),
            "degraded": sum(1 for e in self._entries.values() if e.last_error),
        }
