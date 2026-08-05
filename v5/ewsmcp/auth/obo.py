"""On-behalf-of: the caller's token for this server → a token for Exchange.

This is what makes "no service account" true. The server never holds a
credential that can open every mailbox; it holds a confidential *client*
credential whose only power is to ask the IdP to re-mint the CALLER's own
token for a different audience. Exchange then sees the end user, and opens
their mailbox with DELEGATE access.

Two properties matter operationally:

- **Single flight.** Ten concurrent tool calls from one caller must cost one
  round trip to the IdP, not ten. The IdP is shared infrastructure.
- **Memory only.** Exchanged tokens are never written to ``DATA_DIR``
  (DESIGN.md law #6). A restart re-exchanges; that is the correct trade.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Optional

import httpx

from ..errors import ToolError
from .errors import invalid_token
from .principal import Principal

logger = logging.getLogger(__name__)

# IdP error codes that mean "the CALLER must act", not "the server is broken".
_CALLER_ERRORS = {"invalid_grant", "interaction_required", "consent_required",
                  "login_required"}


@dataclass(frozen=True)
class ExchangeToken:
    access_token: str
    expires_at: float


class TokenExchanger:
    def __init__(
        self,
        settings,
        *,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ):
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._cache: OrderedDict = OrderedDict()
        self._locks: Dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self.exchanges = 0
        self.cache_hits = 0
        self.failures: Dict[str, int] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ cache

    def _fresh(self, key: str) -> Optional[ExchangeToken]:
        token = self._cache.get(key)
        if token is None:
            return None
        margin = self.settings.auth_token_expiry_margin_seconds
        if token.expires_at - margin <= time.time():
            # Expiring within the margin: treat as absent so a long call
            # cannot start with a token that dies mid-flight.
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return token

    def _store(self, key: str, token: ExchangeToken) -> None:
        self._cache[key] = token
        self._cache.move_to_end(key)
        while len(self._cache) > self.settings.auth_token_cache_max:
            evicted, _ = self._cache.popitem(last=False)
            self._locks.pop(evicted, None)

    def evict(self, principal: Principal) -> None:
        """Drop a caller's token — used when Exchange rejects it mid-flight."""
        self._cache.pop(principal.key, None)

    # --------------------------------------------------------------- exchange

    async def token_for(self, principal: Principal) -> ExchangeToken:
        key = principal.key
        cached = self._fresh(key)
        if cached is not None:
            self.cache_hits += 1
            return cached

        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Re-check inside the lock: whoever held it may have just filled
            # the cache, and that is the whole point of single flight.
            cached = self._fresh(key)
            if cached is not None:
                self.cache_hits += 1
                return cached
            token = await self._exchange(principal)
            self._store(key, token)
            return token

    async def _exchange(self, principal: Principal) -> ExchangeToken:
        settings = self.settings
        if settings.auth_obo_style == "rfc8693":
            form = {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": principal.raw_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "audience": settings.auth_ews_scope,
            }
        else:  # aad / ADFS style
            form = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": principal.raw_token,
                "scope": settings.auth_ews_scope,
                "requested_token_use": "on_behalf_of",
            }
        form["client_id"] = settings.auth_obo_client_id or ""
        form["client_secret"] = settings.auth_obo_client_secret or ""

        try:
            response = await self._http().post(settings.auth_obo_token_url or "",
                                               data=form)
        except Exception as exc:
            self._count("network")
            logger.error("OBO token endpoint unreachable (%s: %s)",
                         type(exc).__name__, exc)
            raise ToolError(
                "upstream_unavailable",
                "the identity provider is unreachable, so no Exchange token "
                "could be obtained",
                retry_after_s=15,
            ) from exc

        if response.status_code >= 400:
            raise self._error(response)

        try:
            payload = response.json()
            access_token = payload["access_token"]
            expires_in = float(payload.get("expires_in", 3600))
        except Exception as exc:
            self._count("malformed_response")
            raise ToolError(
                "upstream_unavailable",
                "the identity provider returned an unusable token response",
            ) from exc

        self.exchanges += 1
        return ExchangeToken(access_token=access_token,
                             expires_at=time.time() + expires_in)

    def _error(self, response: httpx.Response) -> Exception:
        """Whose fault is it? The answer decides the code AND the circuit."""
        try:
            code = str(response.json().get("error", "")) or "unknown"
        except Exception:
            code = "unknown"
        self._count(code)

        if code in _CALLER_ERRORS:
            # The caller's own token is stale or unconsented. Their problem to
            # fix, and it must NOT count toward the circuit breaker: one user
            # with an expired session cannot be allowed to trip the server.
            return invalid_token(
                "the identity provider would not exchange this token for "
                "Exchange access",
                reason=f"obo_{code}",
                hint="Acquire a fresh access token and retry; if this persists, "
                     "consent for the Exchange scope may be missing.",
            )
        # invalid_client and friends: OUR credential is wrong or expired. That
        # affects everybody, so it is an outage — and it SHOULD open the
        # circuit, or every caller hammers the IdP with a doomed request.
        logger.error("OBO exchange rejected by the IdP (%s) — check "
                     "AUTH_OBO_CLIENT_ID/SECRET", code)
        return ToolError(
            "upstream_unavailable",
            f"the identity provider rejected this server's OBO client ({code})",
            hint="Operator action: check AUTH_OBO_CLIENT_ID / "
                 "AUTH_OBO_CLIENT_SECRET and the Exchange scope grant.",
            retry_after_s=30,
        )

    def _count(self, reason: str) -> None:
        self.failures[reason] = self.failures.get(reason, 0) + 1

    def stats(self) -> Dict[str, int]:
        return {"cached": len(self._cache), "exchanges": self.exchanges,
                "cache_hits": self.cache_hits}
