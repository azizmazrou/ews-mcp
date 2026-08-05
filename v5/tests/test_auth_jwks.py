"""JWKS caching: key rotation, and the two ways this could hurt someone.

The IdP is a dependency shared by every caller, so both failure directions
matter. Fetch too eagerly and an attacker spraying unknown `kid`s turns this
server into a DoS relay pointed at the IdP. Fetch too lazily — or fail hard
when the IdP hiccups — and every caller is locked out of their mail.
"""

import asyncio

import httpx
import pytest
from conftest import KID, jwks_document, jwks_transport, make_token, make_verifier

from ewsmcp.auth import AuthError, JWKSCache
from ewsmcp.errors import ToolError


def _counting_transport(keys, *, only=None, fail_after=None):
    """JWKS transport that records every request it serves."""
    calls = []

    def handler(request):
        calls.append(request.url)
        if fail_after is not None and len(calls) > fail_after:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json=jwks_document(keys, only=only))

    return httpx.MockTransport(handler), calls


def _cache(transport, **kwargs):
    return JWKSCache("https://idp.corp.example/keys",
                     client=httpx.AsyncClient(transport=transport), **kwargs)


def _run(coro_factory):
    return asyncio.run(coro_factory())


# ------------------------------------------------------------------- caching

def test_key_set_is_fetched_once_and_reused(rsa_keys):
    transport, calls = _counting_transport(rsa_keys)
    cache = _cache(transport)

    async def run():
        for _ in range(5):
            await cache.key_for(KID, "RS256")
        await cache.aclose()

    _run(lambda: run())
    assert len(calls) == 1


def test_expired_ttl_triggers_a_refetch(rsa_keys):
    transport, calls = _counting_transport(rsa_keys)
    cache = _cache(transport, ttl_seconds=0)

    async def run():
        await cache.key_for(KID, "RS256")
        await cache.key_for(KID, "RS256")
        await cache.aclose()

    _run(lambda: run())
    assert len(calls) == 2


def test_concurrent_first_use_fetches_once(rsa_keys):
    """Ten callers arriving together must not become ten JWKS requests."""
    transport, calls = _counting_transport(rsa_keys)
    cache = _cache(transport)

    async def run():
        await asyncio.gather(*(cache.key_for(KID, "RS256") for _ in range(10)))
        await cache.aclose()

    _run(lambda: run())
    assert len(calls) == 1


# ------------------------------------------------------------------ rotation

def test_unknown_kid_triggers_exactly_one_refetch(rsa_keys):
    """A token signed with a freshly rotated key must work without waiting for
    the TTL — and it costs exactly one extra fetch, not one per attempt."""
    published = {KID}
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=jwks_document(rsa_keys, only=published))

    cache = _cache(httpx.MockTransport(handler))

    async def run():
        await cache.key_for(KID, "RS256")  # fetch #1, warms the cache
        published.add("rotated-key")       # the IdP rotates
        key = await cache.key_for("rotated-key", "RS256")  # fetch #2, picks it up
        await cache.aclose()
        return key

    assert _run(lambda: run()) is not None
    assert len(calls) == 2


def test_a_resolved_rotation_does_not_arm_the_cooldown(rsa_keys):
    """Only fruitless refetches are penalised: two rotations in quick
    succession must both be picked up, or a busy IdP locks callers out."""
    published = {KID}
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=jwks_document(rsa_keys, only=published))

    cache = _cache(httpx.MockTransport(handler), min_refetch_seconds=3600)

    async def run():
        await cache.key_for(KID, "RS256")
        published.add("rotated-key")
        await cache.key_for("rotated-key", "RS256")
        published.discard(KID)
        published.add("third-key")
        key = await cache.key_for("third-key", "RS256")
        await cache.aclose()
        return key

    assert _run(lambda: run()) is not None
    assert len(calls) == 3


def test_kid_spraying_is_rate_limited(rsa_keys):
    """THE amplification guard: 50 unknown `kid`s must not become 50 requests
    to the IdP. After the cooldown starts, further misses are served from
    cache and simply rejected."""
    transport, calls = _counting_transport(rsa_keys, only=[KID])
    cache = _cache(transport, min_refetch_seconds=60)

    async def run():
        await cache.key_for(KID, "RS256")  # warm
        for i in range(50):
            with pytest.raises(AuthError) as excinfo:
                await cache.key_for(f"sprayed-{i}", "RS256")
            assert excinfo.value.reason == "unknown_kid"
        await cache.aclose()

    _run(lambda: run())
    assert len(calls) == 2, "one warm-up plus a single cooldown-limited refetch"


def test_cooldown_does_not_block_a_genuinely_expired_cache(rsa_keys):
    """The cooldown exists to stop spraying, not to pin a stale key set."""
    transport, calls = _counting_transport(rsa_keys)
    cache = _cache(transport, ttl_seconds=0, min_refetch_seconds=3600)

    async def run():
        await cache.key_for(KID, "RS256")
        await cache.key_for(KID, "RS256")
        await cache.aclose()

    _run(lambda: run())
    assert len(calls) == 2


# ------------------------------------------------------------- IdP down

def test_idp_outage_keeps_serving_the_last_good_keys(rsa_keys):
    """An IdP blip must not 401 every caller in the company."""
    transport = _counting_transport(rsa_keys, fail_after=1)[0]
    cache = _cache(transport, ttl_seconds=0)

    async def run():
        await cache.key_for(KID, "RS256")           # good fetch
        key = await cache.key_for(KID, "RS256")     # TTL expired, IdP now down
        await cache.aclose()
        return key

    assert _run(lambda: run()) is not None
    assert cache.stale is True


def test_idp_down_before_any_fetch_is_an_upstream_failure(rsa_keys):
    """With nothing cached there is genuinely no way to verify anyone. That is
    a 503 (our dependency is broken), not a 401 (the caller did something
    wrong) — blaming the caller would send them into a pointless retry loop."""
    transport = _counting_transport(rsa_keys, fail_after=0)[0]
    cache = _cache(transport)

    async def run():
        try:
            with pytest.raises(ToolError) as excinfo:
                await cache.key_for(KID, "RS256")
        finally:
            await cache.aclose()
        return excinfo.value

    error = _run(lambda: run())
    assert error.code == "upstream_unavailable"
    assert error.http_status == 503


def test_empty_jwks_document_is_treated_as_a_failure(rsa_keys):
    """An IdP answering 200 with no keys is broken, not authoritative."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"keys": []}))
    cache = _cache(transport)

    async def run():
        try:
            with pytest.raises(ToolError):
                await cache.key_for(KID, "RS256")
        finally:
            await cache.aclose()

    _run(lambda: run())


# ----------------------------------------------------------------- kid-less

def test_missing_kid_resolves_when_the_issuer_publishes_one_key(rsa_keys):
    cache = _cache(jwks_transport(rsa_keys, only=[KID]))

    async def run():
        key = await cache.key_for(None, "RS256")
        await cache.aclose()
        return key

    assert _run(lambda: run()) is not None


def test_missing_kid_is_ambiguous_with_several_keys(rsa_keys):
    """Guessing which of several keys was meant would mean trying them all —
    a signature oracle. Refuse instead."""
    cache = _cache(jwks_transport(rsa_keys))

    async def run():
        try:
            with pytest.raises(AuthError) as excinfo:
                await cache.key_for(None, "RS256")
        finally:
            await cache.aclose()
        return excinfo.value

    assert _run(lambda: run()).reason == "unknown_kid"


# -------------------------------------------------------- end to end rotation

def test_rotated_signing_key_verifies_after_the_refetch(rsa_keys):
    """The whole point, end to end: the IdP starts signing with a new key and
    callers keep working without a restart."""
    published = {KID}
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=jwks_document(rsa_keys, only=published))

    verifier = make_verifier(rsa_keys, transport=httpx.MockTransport(handler))

    async def run():
        try:
            await verifier.verify(make_token(rsa_keys, kid=KID))
            published.add("rotated-key")  # the IdP rotates
            return await verifier.verify(make_token(rsa_keys, kid="rotated-key"))
        finally:
            await verifier.aclose()

    principal = _run(lambda: run())
    assert principal.smtp == "exec@corp.example"
    assert len(calls) == 2
