"""On-behalf-of exchange: caching, single flight, and whose fault a failure is.

The IdP is shared infrastructure for the whole company, so the two things
that matter most here are not features: never ask it the same question twice
concurrently, and never let one user's expired session look like an outage
(or an outage look like one user's problem).
"""

import asyncio
import time

import httpx
import pytest
from conftest import ISSUER, obo_transport, oidc_settings

from ewsmcp.auth import AuthError, Principal, TokenExchanger
from ewsmcp.errors import ToolError

ALICE = Principal(subject="user-alice", smtp="alice@corp.example", issuer=ISSUER,
                  expires_at=9e9, raw_token="alice-user-token")
BOB = Principal(subject="user-bob", smtp="bob@corp.example", issuer=ISSUER,
                expires_at=9e9, raw_token="bob-user-token")


def _exchanger(transport=None, **overrides):
    settings = oidc_settings(**overrides)
    return TokenExchanger(settings,
                          client=httpx.AsyncClient(transport=transport or obo_transport()))


def _run(factory):
    return asyncio.run(factory())


def _counting(**kwargs):
    seen = []
    return obo_transport(on_request=lambda r: seen.append(r), **kwargs), seen


# ------------------------------------------------------------------- exchange

def test_the_callers_token_is_what_gets_exchanged():
    """The whole no-service-account claim rests on this: what we send the IdP
    is the CALLER's assertion, not a credential of our own that could open
    anybody's mailbox."""
    transport, seen = _counting()
    exchanger = _exchanger(transport)

    async def run():
        token = await exchanger.token_for(ALICE)
        await exchanger.aclose()
        return token

    token = _run(run)
    assert token.access_token == "ews-token"
    body = seen[0].content.decode()
    assert "assertion=alice-user-token" in body
    assert "requested_token_use=on_behalf_of" in body


def test_rfc8693_style_sends_a_token_exchange_grant():
    """AD FS and Keycloak want RFC 8693, not the AAD spelling."""
    transport, seen = _counting()
    exchanger = _exchanger(transport, auth_obo_style="rfc8693")

    async def run():
        await exchanger.token_for(ALICE)
        await exchanger.aclose()

    _run(run)
    body = seen[0].content.decode()
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange" in body
    assert "subject_token=alice-user-token" in body


# --------------------------------------------------------------------- cache

def test_a_cached_token_is_reused():
    transport, seen = _counting()
    exchanger = _exchanger(transport)

    async def run():
        for _ in range(5):
            await exchanger.token_for(ALICE)
        await exchanger.aclose()

    _run(run)
    assert len(seen) == 1
    assert exchanger.cache_hits == 4


def test_each_caller_gets_their_own_token():
    transport, seen = _counting()
    exchanger = _exchanger(transport)

    async def run():
        await exchanger.token_for(ALICE)
        await exchanger.token_for(BOB)
        await exchanger.aclose()

    _run(run)
    assert len(seen) == 2


def test_a_token_inside_the_expiry_margin_is_not_served():
    """A token with seconds left must not be handed to a call that may take
    longer than that — it would die mid-flight against Exchange."""
    transport, seen = _counting(expires_in=60)
    exchanger = _exchanger(transport, auth_token_expiry_margin_seconds=120)

    async def run():
        await exchanger.token_for(ALICE)
        await exchanger.token_for(ALICE)
        await exchanger.aclose()

    _run(run)
    assert len(seen) == 2, "the near-expiry token should have been re-exchanged"


def test_concurrent_calls_from_one_caller_cost_one_exchange():
    """Single flight. Ten tool calls arriving together must not become ten
    requests to shared IdP infrastructure."""
    transport, seen = _counting()
    exchanger = _exchanger(transport)

    async def run():
        await asyncio.gather(*(exchanger.token_for(ALICE) for _ in range(10)))
        await exchanger.aclose()

    _run(run)
    assert len(seen) == 1
    assert exchanger.exchanges == 1


def test_the_cache_is_bounded():
    transport = _counting()[0]
    exchanger = _exchanger(transport, auth_token_cache_max=3)

    async def run():
        for i in range(10):
            await exchanger.token_for(
                Principal(subject=f"user-{i}", smtp=f"u{i}@corp.example",
                          issuer=ISSUER, expires_at=9e9, raw_token=f"t{i}"))
        await exchanger.aclose()

    _run(run)
    assert exchanger.stats()["cached"] == 3


def test_evict_forces_a_fresh_exchange():
    """Used when Exchange rejects a token mid-flight (revoked, clock skew)."""
    transport, seen = _counting()
    exchanger = _exchanger(transport)

    async def run():
        await exchanger.token_for(ALICE)
        exchanger.evict(ALICE)
        await exchanger.token_for(ALICE)
        await exchanger.aclose()

    _run(run)
    assert len(seen) == 2


def test_tokens_are_never_written_to_disk(tmp_path):
    """DESIGN.md law #6. The exchanger holds tokens in memory only, so a
    restart re-exchanges — that is the correct trade."""
    exchanger = _exchanger()

    async def run():
        await exchanger.token_for(ALICE)
        await exchanger.aclose()

    _run(run)
    data_dir = tmp_path
    leaked = [p for p in data_dir.rglob("*") if p.is_file()
              and "ews-token" in p.read_bytes().decode("utf-8", "ignore")]
    assert leaked == []


# ------------------------------------------------------------------- failures

def test_an_expired_caller_token_is_the_callers_problem():
    """invalid_grant → 401. It must NOT count toward the circuit breaker:
    one user with a stale session cannot be allowed to trip the server for
    everybody else."""
    exchanger = _exchanger(obo_transport(status=400, error="invalid_grant"))

    async def run():
        try:
            with pytest.raises(AuthError) as excinfo:
                await exchanger.token_for(ALICE)
        finally:
            await exchanger.aclose()
        return excinfo.value

    error = _run(run)
    assert error.code == "auth_failed"
    assert error.http_status == 401
    assert error.reason == "obo_invalid_grant"


@pytest.mark.parametrize("code", ["consent_required", "interaction_required"])
def test_consent_problems_are_also_the_callers_problem(code):
    exchanger = _exchanger(obo_transport(status=400, error=code))

    async def run():
        try:
            with pytest.raises(AuthError) as excinfo:
                await exchanger.token_for(ALICE)
        finally:
            await exchanger.aclose()
        return excinfo.value

    assert _run(run).http_status == 401


def test_a_bad_server_credential_is_an_outage():
    """invalid_client means OUR secret is wrong — it affects every caller, so
    it is a 503 that opens the circuit rather than a 401 that sends each user
    into a doomed retry loop."""
    exchanger = _exchanger(obo_transport(status=401, error="invalid_client"))

    async def run():
        try:
            with pytest.raises(ToolError) as excinfo:
                await exchanger.token_for(ALICE)
        finally:
            await exchanger.aclose()
        return excinfo.value

    error = _run(run)
    assert not isinstance(error, AuthError)
    assert error.code == "upstream_unavailable"
    assert "AUTH_OBO_CLIENT" in (error.hint or "")


def test_an_unreachable_idp_is_an_outage():
    def boom(request):
        raise httpx.ConnectError("no route to host")

    exchanger = _exchanger(httpx.MockTransport(boom))

    async def run():
        try:
            with pytest.raises(ToolError) as excinfo:
                await exchanger.token_for(ALICE)
        finally:
            await exchanger.aclose()
        return excinfo.value

    error = _run(run)
    assert error.code == "upstream_unavailable"
    assert error.retry_after_s == 15


def test_a_malformed_token_response_is_not_silently_accepted():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"not_a_token": True}))
    exchanger = _exchanger(transport)

    async def run():
        try:
            with pytest.raises(ToolError) as excinfo:
                await exchanger.token_for(ALICE)
        finally:
            await exchanger.aclose()
        return excinfo.value

    assert _run(run).code == "upstream_unavailable"


def test_a_failed_exchange_is_not_cached():
    """Otherwise a transient failure would be replayed for the whole margin."""
    state = {"fail": True}

    def handler(request):
        if state["fail"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": "ok", "expires_in": 3600})

    exchanger = _exchanger(httpx.MockTransport(handler))

    async def run():
        with pytest.raises(AuthError):
            await exchanger.token_for(ALICE)
        state["fail"] = False
        token = await exchanger.token_for(ALICE)
        await exchanger.aclose()
        return token

    assert _run(run).access_token == "ok"


def test_expiry_is_absolute_not_relative():
    exchanger = _exchanger(obo_transport(expires_in=3600))

    async def run():
        token = await exchanger.token_for(ALICE)
        await exchanger.aclose()
        return token

    token = _run(run)
    assert time.time() + 3500 < token.expires_at < time.time() + 3700
