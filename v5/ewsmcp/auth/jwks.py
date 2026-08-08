"""The IdP's signing keys, cached — with the two failure modes that matter.

1. **Key rotation.** A token signed with a brand-new key arrives with a
   ``kid`` we have never seen. We must refetch, or every caller 401s until
   the TTL happens to expire.
2. **A refetch storm is a weapon.** If an unknown ``kid`` always triggers a
   fetch, anyone can spray random ``kid``s and turn this server into a DoS
   amplifier pointed at the IdP. So forced refetches are rate-limited by
   ``AUTH_JWKS_MIN_REFETCH_SECONDS``.

And one operational rule: an IdP blip must not 401 every caller. The last
good key set keeps being served (flagged ``stale``) until the IdP answers
again.
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import httpx
from jwt import PyJWK

from ..errors import ToolError
from .errors import invalid_token

logger = logging.getLogger(__name__)


class JWKSCache:
    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: int = 3600,
        min_refetch_seconds: int = 60,
        timeout: float = 5.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.url = url
        self.ttl_seconds = ttl_seconds
        self.min_refetch_seconds = min_refetch_seconds
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._keys: Dict[str, Dict[str, Any]] = {}
        self._fetched_at = 0.0
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()
        self.stale = False
        self.refetches = 0

    # ------------------------------------------------------------- lifecycle

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ----------------------------------------------------------------- fetch

    async def _fetch(self) -> None:
        """Replace the key set, or keep the last good one and mark it stale."""
        try:
            response = await self._http().get(self.url)
            response.raise_for_status()
            payload = response.json()
            keys = {
                k["kid"]: k
                for k in payload.get("keys", [])
                if isinstance(k, dict) and k.get("kid")
            }
            if not keys:
                raise ValueError("JWKS document contains no usable keys")
        except Exception as exc:
            self.stale = bool(self._keys)
            level = logging.WARNING if self._keys else logging.ERROR
            logger.log(
                level, "JWKS fetch from %s failed (%s: %s)%s",
                self.url, type(exc).__name__, exc,
                " — serving the last good key set" if self._keys else "",
            )
            if not self._keys:
                raise ToolError(
                    "upstream_unavailable",
                    "the identity provider's key set is unavailable, so no "
                    "token can be verified",
                    hint="Check AUTH_JWKS_URL and IdP reachability; /readyz probes it.",
                    retry_after_s=30,
                ) from exc
            return
        self._keys = keys
        self._fetched_at = time.time()
        self.stale = False
        self.refetches += 1

    async def _ensure(self, force: bool) -> None:
        async with self._lock:
            now = time.time()
            expired = now - self._fetched_at > self.ttl_seconds
            if force:
                # An expired cache always refetches; otherwise the cooldown
                # applies. See `key_for` for when the cooldown is armed.
                if not expired and now < self._cooldown_until:
                    return
            elif self._keys and not expired:
                return
            await self._fetch()

    # ------------------------------------------------------------------- use

    async def key_for(self, kid: Optional[str], alg: str) -> PyJWK:
        await self._ensure(force=False)
        jwk = self._lookup(kid)
        if jwk is None:
            await self._ensure(force=True)  # rotation, maybe
            jwk = self._lookup(kid)
        if jwk is None:
            # The cooldown is armed only by a FRUITLESS forced refetch. A real
            # rotation resolves on the first miss and is never penalised, while
            # sprayed `kid`s never resolve and so always arm it — which is the
            # difference between serving the IdP's users and DoSing the IdP.
            self._cooldown_until = time.time() + self.min_refetch_seconds
            raise invalid_token(
                "the token's signing key is not published by the issuer",
                reason="unknown_kid",
                hint="The key may have been rotated out, or the token was "
                     "issued by a different provider than AUTH_ISSUER.",
            )
        return PyJWK(jwk, algorithm=jwk.get("alg") or alg)

    def _lookup(self, kid: Optional[str]) -> Optional[Dict[str, Any]]:
        if kid:
            return self._keys.get(kid)
        # No `kid` in the header is legal but only unambiguous with one key.
        if len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return None
