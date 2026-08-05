"""Token verification: what gets in, and everything that must not.

Real RSA keys, real signatures, real PyJWT — only the JWKS transport is
faked. A test suite that mocked the verifier would prove nothing here,
because the whole value of this layer is that the cryptography actually runs.
"""

import asyncio
import time

import pytest
from conftest import AUDIENCE, ISSUER, KID, make_token, make_verifier

from ewsmcp.auth import AuthError


def _verify(keys, token, **overrides):
    """Drive the async verifier the way the rest of the suite does."""
    async def run():
        verifier = make_verifier(keys, **overrides)
        try:
            return await verifier.verify(token)
        finally:
            await verifier.aclose()
    return asyncio.run(run())


def _rejects(keys, token, reason, **overrides):
    with pytest.raises(AuthError) as excinfo:
        _verify(keys, token, **overrides)
    assert excinfo.value.reason == reason
    return excinfo.value


# --------------------------------------------------------------- happy path

def test_valid_token_becomes_a_principal(rsa_keys):
    principal = _verify(rsa_keys, make_token(rsa_keys))
    assert principal.subject == "user-0001"
    assert principal.smtp == "exec@corp.example"
    assert principal.issuer == ISSUER
    assert principal.key == f"{ISSUER}|user-0001"


def test_mailbox_is_lowercased(rsa_keys):
    principal = _verify(rsa_keys, make_token(rsa_keys, upn="Exec@Corp.Example"))
    assert principal.smtp == "exec@corp.example"


def test_raw_token_is_kept_but_never_in_repr(rsa_keys):
    """It is needed for the OBO exchange, and it must not leak into a log
    line or a traceback (DESIGN.md law #6)."""
    token = make_token(rsa_keys)
    principal = _verify(rsa_keys, token)
    assert principal.raw_token == token
    assert token not in repr(principal)


# ------------------------------------------------------------ time and shape

def test_expired_token_is_rejected(rsa_keys):
    past = int(time.time()) - 7200
    err = _rejects(rsa_keys, make_token(rsa_keys, iat=past, exp=past + 60), "expired")
    assert err.http_status == 401
    assert err.oauth_error == "invalid_token"


def test_expiry_inside_the_clock_skew_is_accepted(rsa_keys):
    """A caller's clock running slightly fast must not lock them out."""
    just_expired = int(time.time()) - 30
    principal = _verify(
        rsa_keys, make_token(rsa_keys, exp=just_expired), auth_clock_skew_seconds=60)
    assert principal.smtp == "exec@corp.example"


def test_token_not_yet_valid_is_rejected(rsa_keys):
    _rejects(rsa_keys, make_token(rsa_keys, nbf=int(time.time()) + 3600),
                   "not_yet_valid")


def test_garbage_is_rejected_as_malformed(rsa_keys):
    _rejects(rsa_keys, "not-a-jwt-at-all", "malformed")


@pytest.mark.parametrize("claim", ["exp", "iat", "sub"])
def test_required_claims_are_required(rsa_keys, claim):
    _rejects(rsa_keys, make_token(rsa_keys, **{claim: None}), "missing_claim")


# ----------------------------------------------------------- who issued it

def test_wrong_issuer_is_rejected(rsa_keys):
    _rejects(rsa_keys, make_token(rsa_keys, iss="https://evil.example/"),
                   "bad_issuer")


def test_wrong_audience_is_rejected(rsa_keys):
    """THE confused-deputy guard: a token minted for Exchange, or for any
    other API, must not be usable here even though it is perfectly valid."""
    err = _rejects(rsa_keys, make_token(rsa_keys, aud="https://mail.corp.example/"),
                         "bad_audience")
    assert "audience" in (err.hint or "").lower()


def test_audience_list_containing_us_is_accepted(rsa_keys):
    principal = _verify(rsa_keys, make_token(rsa_keys, aud=["other://api", AUDIENCE]))
    assert principal.subject == "user-0001"


# ------------------------------------------------------------------ forgery

def test_alg_none_is_rejected(rsa_keys):
    """Unsigned tokens are refused before the JWKS is even consulted."""
    import jwt
    unsigned = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "x",
                           "upn": "exec@corp.example", "iat": 0, "exp": 9_999_999_999},
                          key="", algorithm="none")
    _rejects(rsa_keys, unsigned, "bad_alg")


def test_hs256_signed_with_the_public_key_is_rejected(rsa_keys):
    """The classic JWKS forgery: take the issuer's PUBLISHED public key, use
    it as an HMAC secret, and claim the token is HS256. Refused because the
    algorithm list is an allowlist that config forbids HS* from entering.

    Assembled by hand on purpose — PyJWT refuses to *sign* with a PEM key, and
    an attacker would simply not use PyJWT. Testing through that guard would
    have tested the attacker's tooling instead of our defence.
    """
    import base64
    import hashlib
    import hmac
    import json

    from cryptography.hazmat.primitives import serialization

    public_pem = rsa_keys[KID].public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    signing_input = b".".join((
        b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode()),
        b64(json.dumps({"iss": ISSUER, "aud": AUDIENCE, "sub": "attacker",
                        "upn": "ceo@corp.example", "iat": 0,
                        "exp": 9_999_999_999}).encode()),
    ))
    signature = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    forged = (signing_input + b"." + b64(signature)).decode()

    _rejects(rsa_keys, forged, "bad_alg")


def test_signature_from_an_unpublished_key_is_rejected(rsa_keys):
    """Right `kid`, wrong private key — the signature check must catch it."""
    token = make_token(rsa_keys, kid=KID, key=rsa_keys["rotated-key"])
    _rejects(rsa_keys, token, "bad_signature", only=[KID])


def test_unknown_kid_is_rejected(rsa_keys):
    _rejects(rsa_keys, make_token(rsa_keys, kid="rotated-key"),
                   "unknown_kid", only=[KID])


# ------------------------------------------------------- claim → mailbox

def test_mailbox_claim_fallback_order(rsa_keys):
    """First configured claim that is non-empty wins."""
    token = make_token(rsa_keys, upn=None, email="fallback@corp.example",
                       preferred_username="last@corp.example")
    principal = _verify(rsa_keys, token)
    assert principal.smtp == "fallback@corp.example"


def test_missing_mailbox_claim_is_rejected(rsa_keys):
    _rejects(rsa_keys, make_token(rsa_keys, upn=None), "no_mailbox_claim")


def test_mailbox_claim_that_is_not_an_address_is_rejected(rsa_keys):
    _rejects(rsa_keys, make_token(rsa_keys, upn="not-an-address"),
                   "bad_mailbox_claim")


def test_configured_claim_name_is_honoured(rsa_keys):
    token = make_token(rsa_keys, upn=None, mail="custom@corp.example")
    principal = _verify(rsa_keys, token, auth_mailbox_claim="mail")
    assert principal.smtp == "custom@corp.example"


# ------------------------------------------------------------ authorisation

def test_domain_allowlist_accepts_a_matching_domain(rsa_keys):
    principal = _verify(rsa_keys, make_token(rsa_keys),
                              auth_email_domain_allowlist="corp.example,*.corp.example")
    assert principal.smtp == "exec@corp.example"


def test_domain_allowlist_blocks_a_guest_identity(rsa_keys):
    """A valid token from a partner/B2B identity must not reach Exchange.
    403 not 401: authentic caller, refused identity — retrying is pointless."""
    err = _rejects(rsa_keys, make_token(rsa_keys, upn="guest@partner.example"),
                         "domain_blocked", auth_email_domain_allowlist="corp.example")
    assert err.http_status == 403
    assert err.oauth_error == "insufficient_scope"


def test_required_scope_is_enforced(rsa_keys):
    _rejects(rsa_keys, make_token(rsa_keys, scope="openid profile"),
                   "insufficient_scope", auth_required_scope="mailbox.read")


def test_required_scope_present_passes(rsa_keys):
    principal = _verify(rsa_keys, make_token(rsa_keys, scope="openid mailbox.read"),
                              auth_required_scope="mailbox.read")
    assert "mailbox.read" in principal.scopes


def test_aad_style_scp_claim_is_understood(rsa_keys):
    principal = _verify(rsa_keys, make_token(rsa_keys, scp="mailbox.read"),
                              auth_required_scope="mailbox.read")
    assert principal.scopes == ("mailbox.read",)


# ------------------------------------------------------------- observability

def test_rejections_are_counted_by_bounded_reason(rsa_keys):
    """Reasons feed a Prometheus label, so the set must stay small and must
    never contain anything derived from the token."""
    verifier = make_verifier(rsa_keys)

    async def run():
        try:
            for token in ("garbage", "also-garbage",
                          make_token(rsa_keys, iss="https://evil.example/")):
                with pytest.raises(AuthError):
                    await verifier.verify(token)
        finally:
            await verifier.aclose()

    asyncio.run(run())
    assert verifier.rejections == {"malformed": 2, "bad_issuer": 1}
