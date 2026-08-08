"""Turn a verified Principal into a Context bound to that caller.

One factory, called from exactly two places (the REST branch of ``http`` and
``call_tool`` in ``server``), so there is a single answer to "what does this
caller get?" — their own Exchange session, their own alias namespace, and
the shared counters/circuit that describe the server rather than the caller.
"""

import logging
from typing import Any, Optional

from ..identity import namespace_uid
from ..ids import get_aliaser
from ..tools.base import for_principal

logger = logging.getLogger(__name__)


class CallerBinder:
    """Owns the per-caller resources and hands out per-request Contexts."""

    def __init__(self, settings, pool, exchanger, caches=None):
        self.settings = settings
        self.pool = pool
        self.exchanger = exchanger
        self.caches = caches

    async def bind(self, root_ctx, principal) -> Any:
        """Per-request Context for ``principal``.

        The OBO exchange and the pool lookup both happen HERE, on the event
        loop and before dispatch — never inside the EWS thread pool, which is
        for blocking Exchange work only.
        """
        ctx = for_principal(root_ctx, principal)
        if not self.settings.per_caller_upstream:
            return ctx
        token = await self.exchanger.token_for(principal)
        ctx.gateway = await self.pool.acquire(principal, token)
        ctx.aliaser = self._aliaser_for(principal, root_ctx)
        if self.caches is not None:
            ctx.cache = self.caches.store_for(principal)
        return ctx

    def schedule_sync(self, ctx) -> Any:
        """Warm this caller's mirror in the background, if it has gone stale.

        Called by the dispatcher AFTER the caller's answer is on its way, so
        it costs them no latency — and it is the only way the mirror can be
        maintained at all without a long-lived credential.
        """
        if self.caches is None or ctx.principal is None or ctx.gateway is None:
            return None
        try:
            return self.caches.schedule(ctx, ctx.principal)
        except Exception as exc:  # housekeeping must never disturb a call
            logger.debug("scheduling an in-request sync failed: %s", exc)
            return None

    def _aliaser_for(self, principal, root_ctx):
        """A private alias namespace per caller.

        Keyed on the token subject, not the address: addresses get reassigned
        when people leave, and a successor must not inherit the previous
        holder's id mappings. The directory name is a salted hash so a listing
        of DATA_DIR/users is not a staff roster (DESIGN.md law #6).
        """
        uid = namespace_uid(self.settings, principal.key)
        try:
            return get_aliaser(f"{self.settings.data_dir}/users/{uid}/memory")
        except Exception as exc:
            # Same posture as boot: a storage failure degrades to raw ids, it
            # never denies a caller their mailbox.
            logger.error("per-caller aliaser init failed (%s) — raw EWS ids", exc)
            return root_ctx.aliaser

    def on_upstream_auth_failure(self, principal) -> None:
        """Exchange rejected the token mid-flight (revoked, clock skew).

        Drop the exchanged token so the retry re-runs OBO. The gateway itself
        stays: its Protocol is still good, only the bearer string was stale.
        """
        self.exchanger.evict(principal)

    async def aclose(self) -> None:
        if self.caches is not None:
            # Before the pool: in-flight sync cycles still hold gateways.
            await self.caches.aclose()
        await self.pool.aclose()
        await self.exchanger.aclose()


def build_binder(settings, executor: Optional[Any] = None) -> CallerBinder:
    from ..gateway.pool import GatewayPool
    from .obo import TokenExchanger

    caches = None
    if settings.ews_cache_enabled:
        from ..cache.percaller import CallerCaches
        caches = CallerCaches(settings)
    return CallerBinder(settings, GatewayPool(settings, executor=executor),
                        TokenExchanger(settings), caches=caches)
