"""Readiness with no boot credential: what /readyz can honestly claim.

The single most valuable check here is the Exchange one. A `401` carrying a
`Bearer` challenge proves the endpoint is up AND that OAuth is still enabled
on the EWS virtual directory — the one upstream assumption this deployment
rests on. If someone disables OAuth there, every caller starts failing and no
restart of ours fixes it, so readiness must go red rather than report "warm".
"""

import asyncio
import json

import httpx
import pytest
from conftest import jwks_transport, make_verifier, oidc_settings

from ewsmcp.readiness import OidcReadiness


def _readiness(rsa_keys, *, exchange=None, idp=None, jwks=None, **overrides):
    """Wire the three probes to independent fake endpoints."""
    settings = oidc_settings(**overrides)

    def handler(request):
        url = str(request.url)
        if url.startswith(settings.ews_server_url):
            return (exchange or _bearer_challenge)(request)
        return (idp or (lambda r: httpx.Response(400, json={"error": "invalid_request"})))(request)

    verifier = make_verifier(rsa_keys, settings,
                             transport=jwks or jwks_transport(rsa_keys))
    return OidcReadiness(settings, verifier,
                         client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _bearer_challenge(request):
    return httpx.Response(401, headers={
        "www-authenticate": 'Bearer client_id="00000002-0000-0ff1-ce00-000000000000", '
                            'trusted_issuers="00000001-0000-0000-c000-000000000000@*"'})


def _status(probe):
    async def run():
        try:
            return await probe.status()
        finally:
            await probe.aclose()
            await probe.verifier.aclose()

    return asyncio.run(run())


# ------------------------------------------------------------------- healthy

def test_everything_reachable_is_ready(rsa_keys):
    status = _status(_readiness(rsa_keys))
    assert status["state"] == "ready"
    assert status["mode"] == "per-caller"
    assert status["checks"]["exchange"]["ok"] is True
    assert status["checks"]["jwks"]["ok"] is True


def test_the_probe_never_needs_anybodys_mailbox(rsa_keys):
    """It must not authenticate as a caller to decide readiness — that would
    mean issuing EWS requests on somebody's real mail that they never asked
    for. The Exchange probe is deliberately UNAUTHENTICATED."""
    seen = []

    def exchange(request):
        seen.append(request)
        return _bearer_challenge(request)

    _status(_readiness(rsa_keys, exchange=exchange))
    assert "authorization" not in {k.lower() for k in seen[0].headers}


# ------------------------------------------------------------ the loud case

def test_oauth_disabled_on_exchange_is_reported_as_degraded(rsa_keys, caplog):
    """Exchange healthy, but offering only Windows auth: our tokens are about
    to be refused by everyone. Reporting ready here would be a lie."""
    def exchange(request):
        return httpx.Response(401, headers={"www-authenticate": "Negotiate, NTLM"})

    with caplog.at_level("ERROR"):
        status = _status(_readiness(rsa_keys, exchange=exchange))
    assert status["state"] == "degraded"
    assert status["checks"]["exchange"]["ok"] is False
    assert "OAuth" in status["checks"]["exchange"]["detail"]
    assert any("no longer offers Bearer" in r.message for r in caplog.records)


def test_unreachable_exchange_is_degraded(rsa_keys):
    def exchange(request):
        raise httpx.ConnectError("no route to host")

    status = _status(_readiness(rsa_keys, exchange=exchange))
    assert status["state"] == "degraded"
    assert "unreachable" in status["checks"]["exchange"]["detail"]


def test_unreachable_idp_is_degraded(rsa_keys):
    def idp(request):
        raise httpx.ConnectError("dns failure")

    status = _status(_readiness(rsa_keys, idp=idp))
    assert status["state"] == "degraded"
    assert status["checks"]["identity_provider"]["ok"] is False


def test_an_idp_that_rejects_a_bare_get_is_still_healthy(rsa_keys):
    """A token endpoint answering 400/405 to an empty GET is working fine —
    we are checking that it ANSWERS, not that it likes the request."""
    status = _status(_readiness(
        rsa_keys, idp=lambda r: httpx.Response(405, json={"error": "wrong method"})))
    assert status["checks"]["identity_provider"]["ok"] is True


def test_no_signing_keys_is_degraded(rsa_keys):
    """Without keys nothing can be verified, so nobody can be served."""
    status = _status(_readiness(
        rsa_keys, jwks=httpx.MockTransport(lambda r: httpx.Response(503))))
    assert status["state"] == "degraded"
    assert status["checks"]["jwks"]["ok"] is False


def test_a_proxy_answering_instead_of_exchange_is_reported_honestly(rsa_keys):
    """200 to an empty POST means something answered, but not the auth layer
    we expect. Reachable — and said to be unverified rather than confirmed."""
    status = _status(_readiness(
        rsa_keys, exchange=lambda r: httpx.Response(200, text="hello from a proxy")))
    assert status["checks"]["exchange"]["ok"] is True
    assert "no Bearer challenge" in status["checks"]["exchange"]["detail"]


# ------------------------------------------------------------------- caching

def test_the_result_is_cached_so_polling_is_not_a_load_generator(rsa_keys):
    """A liveness poll every second must not become a probe every second
    against the IdP and Exchange."""
    hits = []

    def exchange(request):
        hits.append(request)
        return _bearer_challenge(request)

    probe = _readiness(rsa_keys, exchange=exchange)

    async def run():
        try:
            for _ in range(5):
                await probe.status()
        finally:
            await probe.aclose()
            await probe.verifier.aclose()

    asyncio.run(run())
    assert len(hits) == 1


# ------------------------------------------------------------ the endpoint

def test_readyz_reports_503_when_oauth_is_gone(tmp_path, rsa_keys):
    """End to end through the real ASGI app: a container healthcheck must be
    able to see this."""
    from conftest import make_binder

    from ewsmcp.audit import AuditLog
    from ewsmcp.http import build_app
    from ewsmcp.ids import get_aliaser
    from ewsmcp.tools.base import Context

    settings = oidc_settings()
    ctx = Context(settings=settings, gateway=None, manager=None,
                  aliaser=get_aliaser(str(tmp_path / "alias")),
                  audit=AuditLog(str(tmp_path / "audit")))
    ctx.registry = {}
    ctx.binder = make_binder(settings)

    # Build the app with a readiness probe whose Exchange has dropped Bearer.
    import ewsmcp.http as http_module
    original = http_module.OidcReadiness

    def degraded(*args, **kwargs):
        probe = original(*args, **kwargs)
        probe._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(401, headers={"www-authenticate": "NTLM"})))
        return probe

    http_module.OidcReadiness = degraded
    try:
        app = build_app(ctx, settings, verifier=make_verifier(rsa_keys, settings))
    finally:
        http_module.OidcReadiness = original

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "http", "path": "/readyz", "method": "GET",
                     "headers": []}, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = json.loads(b"".join(m.get("body", b"") for m in sent
                               if m["type"] == "http.response.body"))
    assert status == 503
    assert body["status"] == "unavailable"
    assert body["connection"]["checks"]["exchange"]["ok"] is False


@pytest.mark.parametrize("path", ["/livez", "/health"])
def test_liveness_stays_up_regardless(tmp_path, rsa_keys, path):
    """Never-exit boot: liveness must not depend on any upstream."""
    from conftest import make_binder

    from ewsmcp.audit import AuditLog
    from ewsmcp.http import build_app
    from ewsmcp.ids import get_aliaser
    from ewsmcp.tools.base import Context

    settings = oidc_settings()
    ctx = Context(settings=settings, gateway=None, manager=None,
                  aliaser=get_aliaser(str(tmp_path / "alias")),
                  audit=AuditLog(str(tmp_path / "audit")))
    ctx.registry = {}
    ctx.binder = make_binder(settings)
    app = build_app(ctx, settings, verifier=make_verifier(rsa_keys, settings))
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "http", "path": path, "method": "GET",
                     "headers": []}, receive, send))
    assert next(m["status"] for m in sent if m["type"] == "http.response.start") == 200
