"""Access-token verification: bytes on the wire → a Principal, or a refusal.

The rules are deliberately boring and all of them are mandatory. The two
that carry the most weight:

- ``algorithms`` is an ALLOWLIST built from configuration, never read from
  the token header. That makes ``alg: none`` unrepresentable, and it is what
  blocks the classic forgery where an attacker signs ``HS256`` using the
  issuer's *published* public key as the HMAC secret. Config refuses ``HS*``
  outright, so this is two locks on one door.
- ``aud`` must be THIS server's resource id. A token minted for Exchange, or
  for any other API, is refused even when perfectly valid — accepting it
  would make this server a confused deputy.
"""

import fnmatch
import logging
from typing import Any, Dict, List, Optional, Tuple

import jwt

from ..config import Settings, split_csv
from .errors import AuthError, blocked, invalid_token
from .jwks import JWKSCache
from .principal import Principal

logger = logging.getLogger(__name__)

# Claims every token must carry. `sub` is not optional here even though the
# spec allows it: it is the identity everything downstream is keyed on.
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub"]

_DECODE_ERRORS: Tuple[Tuple[type, str, str], ...] = (
    (jwt.ExpiredSignatureError, "expired", "the token has expired"),
    (jwt.ImmatureSignatureError, "not_yet_valid", "the token is not valid yet"),
    (jwt.InvalidAudienceError, "bad_audience",
     "the token was not issued for this server"),
    (jwt.InvalidIssuerError, "bad_issuer", "the token was issued by an unknown provider"),
    (jwt.MissingRequiredClaimError, "missing_claim", "the token is missing a required claim"),
    (jwt.InvalidSignatureError, "bad_signature", "the token signature is invalid"),
    (jwt.InvalidAlgorithmError, "bad_alg", "the token signature algorithm is not accepted"),
)


class TokenVerifier:
    def __init__(self, settings: Settings, jwks: JWKSCache):
        self.settings = settings
        self.jwks = jwks
        self.algorithms = [a.upper() for a in split_csv(settings.auth_allowed_algs)]
        self.mailbox_claims = split_csv(settings.auth_mailbox_claim)
        self.domain_allowlist = [d.lower() for d in split_csv(settings.auth_email_domain_allowlist)]
        self.required_scopes = split_csv(settings.auth_required_scope)
        self.rejections: Dict[str, int] = {}

    @classmethod
    def from_settings(cls, settings: Settings, **jwks_kwargs) -> "TokenVerifier":
        jwks = JWKSCache(
            settings.auth_jwks_url or "",
            ttl_seconds=settings.auth_jwks_ttl_seconds,
            min_refetch_seconds=settings.auth_jwks_min_refetch_seconds,
            **jwks_kwargs,
        )
        return cls(settings, jwks)

    async def aclose(self) -> None:
        await self.jwks.aclose()

    # ------------------------------------------------------------------ main

    async def verify(self, raw: str) -> Principal:
        try:
            return await self._verify(raw)
        except AuthError as err:
            self.rejections[err.reason] = self.rejections.get(err.reason, 0) + 1
            # Reason only: the token, its claims and the issuer's diagnostics
            # never reach the log (DESIGN.md law #6).
            logger.info("token rejected (%s)", err.reason)
            raise

    async def _verify(self, raw: str) -> Principal:
        try:
            header = jwt.get_unverified_header(raw)
        except Exception:
            raise invalid_token("the credential is not a well-formed JWT",
                                reason="malformed") from None

        alg = str(header.get("alg") or "")
        if alg.upper() not in self.algorithms:
            # Checked before touching the JWKS so an attacker cannot use an
            # exotic `alg` to drive key fetches.
            raise invalid_token(
                "the token signature algorithm is not accepted",
                reason="bad_alg",
                hint=f"This server accepts {', '.join(self.algorithms)}.",
            )

        key = await self.jwks.key_for(header.get("kid"), alg)
        try:
            claims = jwt.decode(
                raw,
                key,
                algorithms=self.algorithms,  # allowlist, NOT header.alg
                audience=self.settings.auth_audience,
                issuer=self.settings.auth_issuer,
                leeway=self.settings.auth_clock_skew_seconds,
                options={"require": REQUIRED_CLAIMS},
            )
        except Exception as exc:
            raise self._decode_error(exc) from None

        return Principal(
            subject=str(claims["sub"]),
            smtp=self._mailbox(claims),
            issuer=str(claims["iss"]),
            expires_at=float(claims["exp"]),
            scopes=self._scopes(claims),
            raw_token=raw,
        )

    # ------------------------------------------------------------- internals

    def _decode_error(self, exc: Exception) -> AuthError:
        for kind, reason, message in _DECODE_ERRORS:
            if isinstance(exc, kind):
                hint = None
                if reason in ("expired", "not_yet_valid"):
                    hint = "Acquire a fresh access token and retry."
                elif reason == "bad_audience":
                    hint = ("Request a token whose audience is this server's "
                            "resource id, not Exchange's.")
                return invalid_token(message, reason=reason, hint=hint)
        return invalid_token("the token could not be verified", reason="invalid")

    def _mailbox(self, claims: Dict[str, Any]) -> str:
        """First non-empty configured claim wins, then shape + domain checks."""
        for name in self.mailbox_claims:
            value = claims.get(name)
            if isinstance(value, str) and value.strip():
                address = value.strip().lower()
                break
        else:
            raise invalid_token(
                "the token carries no mailbox address",
                reason="no_mailbox_claim",
                hint=f"Expected one of {', '.join(self.mailbox_claims)} in the token.",
            )

        local, sep, domain = address.rpartition("@")
        if not sep or not local or not domain or " " in address:
            raise invalid_token(
                "the token's mailbox claim is not an email address",
                reason="bad_mailbox_claim",
            )
        if self.domain_allowlist and not any(
            fnmatch.fnmatch(domain, pattern) for pattern in self.domain_allowlist
        ):
            # Deliberately 403, not 401: the caller is authentic and retrying
            # with a fresh token changes nothing.
            raise blocked(
                "this identity's mail domain is not allowed on this server",
                reason="domain_blocked",
                hint="AUTH_EMAIL_DOMAIN_ALLOWLIST governs which domains may connect.",
            )
        return address

    def _scopes(self, claims: Dict[str, Any]) -> Tuple[str, ...]:
        raw: Optional[Any] = claims.get("scope") or claims.get("scp")
        if isinstance(raw, str):
            granted: List[str] = raw.split()
        elif isinstance(raw, (list, tuple)):
            granted = [str(s) for s in raw]
        else:
            granted = []
        missing = [s for s in self.required_scopes if s not in granted]
        if missing:
            raise blocked(
                "the token is missing a required scope",
                reason="insufficient_scope",
                hint=f"AUTH_REQUIRED_SCOPE demands {', '.join(self.required_scopes)}.",
            )
        return tuple(granted)
