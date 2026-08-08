"""What "ready" means when the server holds nobody's credentials.

In static mode readiness is simple: one account, warm or not, and the
connection manager owns the answer. With a per-caller upstream there is no
boot-time credential to warm — and probing an arbitrary caller's mailbox to
manufacture one would mean issuing EWS requests that nobody asked for, on
somebody's real mail. So this probes only what is checkable without any
caller's identity:

1. the IdP's key set is reachable and non-empty — without it no token can be
   verified, so nobody can be served;
2. the IdP's token endpoint answers at all — without it no Exchange token can
   be minted;
3. **Exchange still offers Bearer.** An unauthenticated request should come
   back ``401`` with a ``WWW-Authenticate: Bearer`` challenge. That single
   header proves the endpoint is up *and* that OAuth is still enabled on the
   EWS virtual directory — which is the one upstream assumption this whole
   deployment rests on, and exactly the check an operator would otherwise
   only run by hand, once, before go-live.

A ``401`` offering only ``Negotiate``/``NTLM``/``Basic`` is the loud case:
Exchange is healthy but has stopped accepting the tokens we mint, so every
caller is about to fail. Reporting that as "ready" would be a lie.
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# Probing on every /readyz would turn a liveness poll into a load generator
# against the IdP and Exchange. The result is worth a few seconds of staleness.
CACHE_SECONDS = 10.0
TIMEOUT_SECONDS = 5.0


class OidcReadiness:
    def __init__(self, settings, verifier=None, *,
                 client: Optional[httpx.AsyncClient] = None):
        self.settings = settings
        self.verifier = verifier
        self._client = client
        self._owns_client = client is None
        self._cached: Optional[Dict[str, Any]] = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS,
                                             verify=not self.settings.ews_insecure_skip_verify)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def status(self) -> Dict[str, Any]:
        now = time.time()
        if self._cached is not None and now - self._cached_at < CACHE_SECONDS:
            return self._cached
        async with self._lock:
            now = time.time()
            if self._cached is not None and now - self._cached_at < CACHE_SECONDS:
                return self._cached
            checks = {
                "jwks": await self._check_jwks(),
                "identity_provider": await self._check_idp(),
                "exchange": await self._check_exchange(),
            }
            ready = all(c["ok"] for c in checks.values())
            self._cached = {
                "state": "ready" if ready else "degraded",
                "mode": "per-caller",
                "checks": checks,
            }
            self._cached_at = time.time()
            return self._cached

    # ---------------------------------------------------------------- checks

    async def _check_jwks(self) -> Dict[str, Any]:
        jwks = getattr(self.verifier, "jwks", None)
        if jwks is None:
            return {"ok": False, "detail": "no verifier configured"}
        try:
            # An unknown `kid` is EXPECTED to raise — the call is only here to
            # force a fetch. What matters is whether keys reached the cache.
            await jwks.key_for("readiness-probe-unknown-kid", "RS256")
        except Exception as exc:
            logger.debug("JWKS readiness probe raised as expected: %s", exc)
        keys = len(getattr(jwks, "_keys", {}) or {})
        if keys == 0:
            return {"ok": False, "detail": "no signing keys available"}
        return {"ok": True, "keys": keys, "stale": bool(getattr(jwks, "stale", False))}

    async def _check_idp(self) -> Dict[str, Any]:
        url = self.settings.auth_obo_token_url
        if not url:
            return {"ok": True, "detail": "no token exchange configured"}
        try:
            response = await self._http().get(url)
        except Exception as exc:
            return {"ok": False, "detail": f"unreachable ({type(exc).__name__})"}
        # A token endpoint answering 400/405 to a bare GET is healthy — we are
        # checking that it ANSWERS, not that it likes the request.
        return {"ok": True, "status": response.status_code}

    async def _check_exchange(self) -> Dict[str, Any]:
        url = self.settings.ews_server_url
        try:
            response = await self._http().post(
                url, content=b"", headers={"content-type": "text/xml; charset=utf-8"})
        except Exception as exc:
            return {"ok": False, "detail": f"unreachable ({type(exc).__name__})"}

        challenge = response.headers.get("www-authenticate", "")
        offers_bearer = "bearer" in challenge.lower()
        if response.status_code == 401 and offers_bearer:
            return {"ok": True, "detail": "OAuth offered", "status": 401}
        if response.status_code == 401:
            # Healthy endpoint, wrong auth scheme: every caller is about to
            # fail and no restart of ours will fix it.
            logger.error(
                "Exchange no longer offers Bearer on %s (challenge: %s) — "
                "OAuth appears to be disabled on the EWS virtual directory",
                url, challenge or "none")
            return {"ok": False, "status": 401,
                    "detail": "OAuth not offered on the EWS endpoint"}
        # 200/500 to an empty body means something answered but not the auth
        # layer we expect — usually a proxy in front. Reachable, not verified.
        return {"ok": True, "status": response.status_code,
                "detail": "answered, but no Bearer challenge seen"}
