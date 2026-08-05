"""One Exchange session per caller, bounded and recycled.

Replaces the single process-global ``Account`` once callers open their own
mailboxes. Three constraints shape it:

- **The Protocol must survive a token refresh.** exchangelib caches Protocol
  objects on ``(endpoint, credentials)`` and deliberately excludes the access
  token from the credentials hash, so the token is mutated IN PLACE. Building
  a fresh credentials object every hour would leak a session pool per caller
  per refresh (pinned in ``test_exchangelib_signatures.py``).
- **Threads must not multiply by callers.** One shared executor is the
  server-wide budget; a per-caller semaphore is each caller's share of it.
- **Evicting one caller must not disturb another.** ``CachingProtocol``'s
  cache is process-wide, so eviction closes only this caller's Protocol.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from exchangelib import OAUTH2, OAuth2AuthorizationCodeCredentials
from oauthlib.oauth2 import OAuth2Token

from ..config import Settings
from .client import EWSGateway

logger = logging.getLogger(__name__)


def _oauth_token(access_token: str, expires_at: float) -> OAuth2Token:
    return OAuth2Token({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": max(1, int(expires_at - time.time())),
    })


@dataclass
class _Entry:
    gateway: EWSGateway
    credentials: OAuth2AuthorizationCodeCredentials
    mailbox: str
    last_used: float = field(default_factory=time.time)


class GatewayPool:
    def __init__(self, settings: Settings, executor: Optional[ThreadPoolExecutor] = None):
        self.settings = settings
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max(1, settings.ews_max_concurrency),
            thread_name_prefix="ews",
        )
        self._entries: OrderedDict = OrderedDict()
        self._lock = asyncio.Lock()
        self._sweeper: Optional[asyncio.Task] = None
        self.evictions = 0

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Begin sweeping idle callers.

        A background sweep, not just an opportunistic one on ``acquire``:
        callers who go quiet must release their Exchange session even when
        nobody else calls in.
        """
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_loop())

    async def aclose(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except asyncio.CancelledError:
                pass  # expected: we just cancelled it
            except Exception as exc:
                logger.warning("gateway pool sweeper ended badly: %s", exc)
            self._sweeper = None
        async with self._lock:
            for entry in list(self._entries.values()):
                self._close(entry)
            self._entries.clear()
        self._executor.shutdown(wait=False)

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                async with self._lock:
                    self._sweep_locked()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a sweep failure must not kill the loop
                logger.warning("gateway pool sweep failed: %s", exc)

    # --------------------------------------------------------------- acquire

    async def acquire(self, principal, token) -> EWSGateway:
        """This caller's gateway, with a current token applied in place."""
        key = principal.key
        async with self._lock:
            self._sweep_locked()
            entry = self._entries.get(key)
            if entry is None:
                entry = self._build(principal, token)
                self._entries[key] = entry
                logger.info("opened an Exchange session for a new caller "
                            "(pool size %d)", len(self._entries))
            else:
                self._apply(entry, token)
            entry.last_used = time.time()
            self._entries.move_to_end(key)
            self._evict_over_capacity_locked()
            return entry.gateway

    def _build(self, principal, token) -> _Entry:
        credentials = OAuth2AuthorizationCodeCredentials(
            access_token=_oauth_token(token.access_token, token.expires_at),
        )
        gateway = EWSGateway(
            self.settings,
            mailbox=principal.smtp,
            credentials_provider=lambda: (credentials, OAUTH2),
            executor=self._executor,
            max_inflight=self.settings.ews_max_concurrency_per_user,
        )
        return _Entry(gateway=gateway, credentials=credentials, mailbox=principal.smtp)

    def _apply(self, entry: _Entry, token) -> None:
        """In-place token swap — see the module docstring and the pins."""
        with entry.credentials.lock:
            entry.credentials.access_token = _oauth_token(token.access_token,
                                                          token.expires_at)

    # -------------------------------------------------------------- eviction

    def evict(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._close(entry)

    def _close(self, entry: _Entry) -> None:
        self.evictions += 1
        try:
            # NEVER the global protocol cache: that would drop every other
            # caller's live Protocol along with this one.
            entry.gateway.close()
        except Exception as exc:
            logger.debug("closing a pooled gateway failed: %s", exc)

    def _sweep_locked(self) -> None:
        ttl = self.settings.auth_gateway_idle_ttl_seconds
        if ttl <= 0:
            return
        cutoff = time.time() - ttl
        for key in [k for k, e in self._entries.items() if e.last_used < cutoff]:
            self._close(self._entries.pop(key))

    def _evict_over_capacity_locked(self) -> None:
        limit = max(1, self.settings.auth_gateway_pool_max)
        while len(self._entries) > limit:
            _, entry = self._entries.popitem(last=False)  # least recently used
            self._close(entry)

    # ------------------------------------------------------------------ info

    def stats(self) -> Dict[str, Any]:
        return {
            "active_principals": len(self._entries),
            "evictions": self.evictions,
            "max": self.settings.auth_gateway_pool_max,
            "idle_ttl_s": self.settings.auth_gateway_idle_ttl_seconds,
        }

    def mailboxes(self) -> Tuple[str, ...]:
        """Test/debug helper — never logged, never exposed by a tool."""
        return tuple(e.mailbox for e in self._entries.values())
