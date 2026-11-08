"""Multi-user mode: per-request Exchange credentials, HTTP transport only.

Each LibreChat user supplies their own Exchange email + password via
customUserVars, forwarded as the ``X-EWS-Email`` / ``X-EWS-Password``
headers (see README "Multi-user mode"). This module turns those headers
into a per-user ``Context`` with its own ``EWSGateway`` — reused across
calls from the same user, since autodiscover + auth is too slow to redo
on every tool call — while everything that would otherwise leak between
users (cache mirror, audit log, alias DB, semantic index, connection
manager) stays out of the picture entirely: the per-user Context is built
with those set to ``None``/no-op. Short ids come from a MemoryAliaser held
in that Context only (RAM, per user, gone with the Context).

The tool ``registry`` is the one thing safe to share: ``ToolSpec`` objects
are stateless (built once from the tier-filtered global config), so every
per-user Context reuses the same dict rather than rebuilding it.

With ``EWS_MULTI_USER_AUTH=impersonation`` no user password is involved:
``X-EWS-Email`` only names the mailbox, and every per-user gateway signs
in as the one service account (EWS_USERNAME/EWS_PASSWORD) with exchangelib's
IMPERSONATION access type. A request that still carries ``X-EWS-Password``
is refused rather than ignored, so a misconfigured gateway cannot go on
shipping passwords to a server that has no use for them.

With ``EWS_MULTI_USER_AUTH=token`` the request carries the user's own OAuth
access token for Exchange in ``Authorization: Bearer``; Exchange validates
it. The mailbox is ``X-EWS-Email`` or, without it, the token's ``upn`` /
``email`` / ``preferred_username`` / ``unique_name`` claim. A missing or
expired token is answered with HTTP 401 at the transport
(``token_transport_error``), not as a tool error: an MCP client refreshes
its token only on a 401 from the endpoint itself.
"""

import base64
import binascii
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Optional, Tuple

from .errors import ToolError
from .gateway.client import EWSGateway
from .ids import MemoryAliaser
from .tools.base import Context

logger = logging.getLogger(__name__)

EMAIL_HEADER = "x-ews-email"
PASSWORD_HEADER = "x-ews-password"
AUTHORIZATION_HEADER = "authorization"

# A token this close to expiry counts as expired: the call it authorizes
# may still be running when it lapses.
TOKEN_EXPIRY_LEEWAY_SECONDS = 30
_MAILBOX_CLAIMS = ("upn", "email", "preferred_username", "unique_name")

MAX_CACHED_USERS = 64
IDLE_EVICT_SECONDS = 900  # 15 min of no calls drops the cached gateway


class _NullAudit:
    def record(self, *args, **kwargs) -> None:
        return None


def _decode(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _require(email: Optional[str], password: Optional[str]) -> Tuple[str, str]:
    if not email or not password:
        raise ToolError(
            "auth_failed",
            "missing per-user Exchange credentials",
            hint=f"This server runs in multi-user mode: set the "
                 f"{EMAIL_HEADER} and {PASSWORD_HEADER} headers "
                 "(LibreChat customUserVars) on every request.",
        )
    return email.strip().lower(), password


def _require_mailbox(email: Optional[str], password: Optional[str],
                     allowed_domains: frozenset) -> str:
    if password:
        raise ToolError(
            "auth_failed",
            f"{PASSWORD_HEADER} sent to a server in impersonation mode",
            hint="This server opens mailboxes through a service account and "
                 "never needs a user password: remove the "
                 f"{PASSWORD_HEADER} header from the gateway configuration.",
        )
    if not email:
        raise ToolError(
            "auth_failed",
            "missing mailbox",
            hint=f"This server runs in impersonation mode: set the "
                 f"{EMAIL_HEADER} header on every request.",
        )
    email = email.strip().lower()
    domain = email.rpartition("@")[2]
    if allowed_domains and domain not in allowed_domains:
        raise ToolError(
            "forbidden",
            "mailbox outside the domains this server may impersonate",
            hint="See EWS_IMPERSONATION_ALLOWED_DOMAINS.",
        )
    return email


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def token_claims(token: str) -> dict:
    """Payload of a JWT, signature NOT checked (Exchange checks the token;
    this only reads the mailbox and the expiry). ``{}`` for anything that
    is not a JWT with a JSON object payload."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (binascii.Error, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def token_expires_at(claims: dict) -> Optional[float]:
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return float(exp)


def token_expired(token: str, now: Optional[float] = None) -> bool:
    exp = token_expires_at(token_claims(token))
    if exp is None:
        return False
    return exp <= (time.time() if now is None else now) + TOKEN_EXPIRY_LEEWAY_SECONDS


def token_transport_error(authorization: Optional[str]) -> Optional[Tuple[str, str]]:
    """``(WWW-Authenticate value, message)`` when a token-mode request must
    be answered with HTTP 401 before any tool runs, else ``None``."""
    token = bearer_token(authorization)
    if token is None:
        return 'Bearer', "missing 'Authorization: Bearer <access token>'"
    if token_expired(token):
        return ('Bearer error="invalid_token", error_description="The access token expired"',
                "the access token has expired")
    return None


def _require_token_mailbox(email: Optional[str], password: Optional[str],
                           authorization: Optional[str]) -> Tuple[str, str]:
    if password:
        raise ToolError(
            "auth_failed",
            f"{PASSWORD_HEADER} sent to a server in token mode",
            hint="This server authenticates with the caller's OAuth access "
                 f"token: remove the {PASSWORD_HEADER} header from the "
                 "gateway configuration.",
        )
    token = bearer_token(authorization)
    if token is None:
        raise ToolError(
            "auth_failed",
            "missing access token",
            hint="This server runs in token mode: send the user's Exchange "
                 "access token as 'Authorization: Bearer <token>'.",
        )
    if not email:
        claims = token_claims(token)
        email = next((str(claims[c]) for c in _MAILBOX_CLAIMS
                      if isinstance(claims.get(c), str) and "@" in claims[c]), None)
    if not email:
        raise ToolError(
            "auth_failed",
            "no mailbox: no X-EWS-Email header and no address claim in the token",
            hint=f"Set the {EMAIL_HEADER} header, or issue tokens with a "
                 "upn/email claim.",
        )
    return email.strip().lower(), token


def _resolve(email, password, impersonation: bool,
             allowed_domains: frozenset, token_auth: bool = False,
             authorization: Optional[str] = None) -> Tuple[str, Optional[str]]:
    if token_auth:
        return _require_token_mailbox(email, password, authorization)
    if impersonation:
        return _require_mailbox(email, password, allowed_domains), None
    return _require(email, password)


def asgi_header(headers, wanted: str) -> Optional[str]:
    """First value of header ``wanted`` (lowercase) in a raw ASGI list."""
    for name, value in headers or []:
        if _decode(name).lower() == wanted:
            return _decode(value)
    return None


def extract_credentials_from_asgi_headers(
    headers, impersonation: bool = False, allowed_domains: frozenset = frozenset(),
    token_auth: bool = False,
) -> Tuple[str, Optional[str]]:
    """``headers``: the ASGI scope's raw ``[(name: bytes, value: bytes), ...]`` list.

    Returns ``(email, password)``; under impersonation ``(mailbox, None)``,
    in token mode ``(mailbox, access token)``."""
    email = asgi_header(headers, EMAIL_HEADER)
    password = asgi_header(headers, PASSWORD_HEADER)
    return _resolve(email.strip() if email else None, password, impersonation,
                    allowed_domains, token_auth, asgi_header(headers, AUTHORIZATION_HEADER))


def extract_credentials_from_request(
    request, impersonation: bool = False, allowed_domains: frozenset = frozenset(),
    token_auth: bool = False,
) -> Tuple[str, Optional[str]]:
    """``request``: the raw Starlette ``Request`` reachable via
    ``server.request_context.request`` during MCP tool dispatch under the
    streamable-http transport. ``None`` (stdio, or an SDK version that
    doesn't populate it) is treated the same as missing headers."""
    headers = getattr(request, "headers", None) if request is not None else None
    email = headers.get(EMAIL_HEADER) if headers is not None else None
    password = headers.get(PASSWORD_HEADER) if headers is not None else None
    authorization = headers.get(AUTHORIZATION_HEADER) if headers is not None else None
    return _resolve(email, password, impersonation, allowed_domains, token_auth, authorization)


class UserContextCache:
    """Bounded, idle-evicting cache of per-user ``Context`` objects.

    Keyed by lowercased email. The password is never used as the key
    (only hashed, for change detection) — a password change evicts and
    rebuilds rather than silently reusing a gateway built with the old one.
    """

    def __init__(self, template: Context, max_size: int = MAX_CACHED_USERS,
                idle_evict_seconds: int = IDLE_EVICT_SECONDS):
        self._template = template
        self._max_size = max_size
        self._idle_evict_seconds = idle_evict_seconds
        self._entries: "OrderedDict[str, Tuple[Context, str, float]]" = OrderedDict()
        self._lock = threading.Lock()
        settings = template.settings
        self.impersonation = settings.impersonation
        self.token_auth = settings.token_auth
        self.allowed_domains = settings.impersonation_domains

    def credentials_from_asgi_headers(self, headers) -> Tuple[str, Optional[str]]:
        return extract_credentials_from_asgi_headers(
            headers, self.impersonation, self.allowed_domains, self.token_auth)

    def credentials_from_request(self, request) -> Tuple[str, Optional[str]]:
        return extract_credentials_from_request(
            request, self.impersonation, self.allowed_domains, self.token_auth)

    def get(self, email: str, password: Optional[str]) -> Context:
        # Impersonation: one service account for everybody, no per-user
        # password to detect changes of. Token mode: a refreshed token
        # changes the hash and rebuilds the gateway with the new token.
        pwd_hash = hashlib.sha256((password or "").encode()).hexdigest()
        now = time.time()
        with self._lock:
            self._evict_idle(now)
            entry = self._entries.get(email)
            if entry is not None:
                ctx, cached_hash, _ = entry
                if cached_hash == pwd_hash:
                    self._entries[email] = (ctx, cached_hash, now)
                    self._entries.move_to_end(email)
                    return ctx
                logger.info("cached Exchange password changed for a user — rebuilding gateway")
                ctx.gateway.close()
            ctx = self._build(email, password)
            self._entries[email] = (ctx, pwd_hash, now)
            self._entries.move_to_end(email)
            while len(self._entries) > self._max_size:
                _, (evicted, _, _) = self._entries.popitem(last=False)
                evicted.gateway.close()
            return ctx

    def _evict_idle(self, now: float) -> None:
        stale = [email for email, (_, _, seen) in self._entries.items()
                if now - seen > self._idle_evict_seconds]
        for email in stale:
            ctx, _, _ = self._entries.pop(email)
            ctx.gateway.close()

    def _build(self, email: str, password: Optional[str]) -> Context:
        if self.token_auth:
            settings = self._template.settings.model_copy(update={
                "ews_email": email, "ews_username": None, "ews_password": None,
                "ews_access_token": password,
                "ews_access_token_expires_at": token_expires_at(token_claims(password or "")),
            })
        elif self.impersonation:
            # Keep the service account's EWS_USERNAME/EWS_PASSWORD; only
            # the mailbox changes (EWSGateway uses IMPERSONATION for it).
            settings = self._template.settings.model_copy(update={"ews_email": email})
        else:
            settings = self._template.settings.model_copy(update={
                "ews_email": email, "ews_username": None, "ews_password": password,
            })
        return Context(
            settings=settings,
            gateway=EWSGateway(settings),
            manager=None,
            aliaser=MemoryAliaser(),
            audit=_NullAudit(),
            cache=None,
            sync=None,
            semantic=None,
            registry=self._template.registry,
        )
