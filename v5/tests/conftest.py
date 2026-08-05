"""v5 test fixtures: import path + per-test alias-store isolation."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ewsmcp.confirm import reset_consumed_tokens  # noqa: E402
from ewsmcp.ids import reset_aliaser_cache  # noqa: E402
from ewsmcp.tools.base import reset_send_rate_window  # noqa: E402
from ewsmcp.tools.writes import reset_idempotency_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_aliaser_cache()
    reset_send_rate_window()
    reset_consumed_tokens()
    reset_idempotency_store()
    yield
    reset_aliaser_cache()
    reset_send_rate_window()
    reset_consumed_tokens()
    reset_idempotency_store()


def make_settings(**overrides):
    """Settings with synthetic Exchange endpoints.

    data_dir intentionally comes from the DATA_DIR env var that
    ``_isolate_stores`` points at tmp_path — the synced-folder guard
    would (correctly) refuse a relative path resolved inside the repo.
    """
    from ewsmcp.config import Settings
    base = dict(
        ews_server_url="https://mail.corp.example/EWS/Exchange.asmx",
        ews_email="exec@corp.example",
        ews_username="svc",
        ews_password="pw",
        mcp_transport="stdio",
    )
    base.update(overrides)
    return Settings(**base)


# --- OIDC fixtures ----------------------------------------------------------
#
# A real RSA keypair per test session, a real JWKS document, real PyJWT
# signing — the only fake is the transport. Mocking the verifier instead
# would test nothing: the whole point is that signature, `aud`, `iss` and
# `exp` checks actually run.

ISSUER = "https://idp.corp.example/"
AUDIENCE = "api://ews-mcp"
JWKS_URL = "https://idp.corp.example/keys"
KID = "test-key-1"


@pytest.fixture(scope="session")
def rsa_keys():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return {
        kid: rsa.generate_private_key(public_exponent=65537, key_size=2048)
        for kid in (KID, "rotated-key", "third-key")
    }


def jwks_document(keys, *, only=None):
    """Public JWKS for the given private keys (optionally a subset)."""
    import json

    from jwt.algorithms import RSAAlgorithm
    entries = []
    for kid, private in keys.items():
        if only is not None and kid not in only:
            continue
        jwk = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
        jwk.update(kid=kid, use="sig", alg="RS256")
        entries.append(jwk)
    return {"keys": entries}


def make_token(keys, *, kid=KID, alg="RS256", key=None, **claims):
    """Mint a signed access token; every claim is overridable."""
    import time

    import jwt
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-0001",
        "upn": "exec@corp.example",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    payload.update({k: v for k, v in claims.items() if v is not None})
    for dropped in [k for k, v in claims.items() if v is None]:
        payload.pop(dropped, None)
    signing_key = key if key is not None else keys[kid]
    headers = {"kid": kid} if kid else {}
    return jwt.encode(payload, signing_key, algorithm=alg, headers=headers)


def jwks_transport(keys, *, only=None, status=200, on_request=None):
    """httpx.MockTransport serving the JWKS — no socket, no real IdP."""
    import httpx

    def handler(request):
        if on_request is not None:
            on_request(request)
        if status != 200:
            return httpx.Response(status, json={"error": "unavailable"})
        return httpx.Response(200, json=jwks_document(keys, only=only))

    return httpx.MockTransport(handler)


def make_binder(settings=None, *, transport=None, **overrides):
    """A CallerBinder whose IdP is faked but whose pool is the real one.

    The gateway builds its ``Account`` lazily, so no Exchange contact happens
    until a tool actually calls — which keeps these tests offline while still
    exercising the real pooling, eviction and in-place refresh code.
    """
    import httpx

    from ewsmcp.auth.binding import CallerBinder
    from ewsmcp.auth.obo import TokenExchanger
    from ewsmcp.cache.percaller import CallerCaches
    from ewsmcp.gateway.pool import GatewayPool
    settings = settings or oidc_settings(**overrides)
    client = httpx.AsyncClient(transport=transport or obo_transport())
    caches = CallerCaches(settings) if settings.ews_cache_enabled else None
    return CallerBinder(settings, GatewayPool(settings),
                        TokenExchanger(settings, client=client), caches=caches)


def make_verifier(keys, settings=None, *, only=None, transport=None, **overrides):
    """A TokenVerifier wired to the fake JWKS endpoint."""
    import httpx

    from ewsmcp.auth import TokenVerifier
    settings = settings or oidc_settings(**overrides)
    client = httpx.AsyncClient(transport=transport or jwks_transport(keys, only=only))
    return TokenVerifier.from_settings(settings, client=client)


OBO_URL = "https://idp.corp.example/token"
EWS_SCOPE = "https://mail.corp.example/.default"


def oidc_settings(**overrides):
    """A complete, coherent oidc config.

    Every caller opens their own mailbox in this mode, so the OBO client is
    mandatory. The mirror stays at its production default (on, and per
    caller); the semantic tier is still refused for want of a tenant column.
    """
    base = dict(
        auth_mode="oidc",
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_jwks_url=JWKS_URL,
        data_dir_namespace_salt="test-salt",
        mcp_transport="http",
        auth_obo_token_url=OBO_URL,
        auth_obo_client_id="ews-mcp",
        auth_obo_client_secret="test-secret",
        auth_ews_scope=EWS_SCOPE,
    )
    base.update(overrides)
    return make_settings(**base)


def obo_transport(*, access_token="ews-token", expires_in=3600, status=200,
                  error="invalid_grant", on_request=None):
    """httpx.MockTransport standing in for the IdP's token endpoint."""
    import httpx

    def handler(request):
        if on_request is not None:
            on_request(request)
        if status != 200:
            return httpx.Response(status, json={"error": error})
        return httpx.Response(200, json={"access_token": access_token,
                                         "token_type": "Bearer",
                                         "expires_in": expires_in})

    return httpx.MockTransport(handler)
