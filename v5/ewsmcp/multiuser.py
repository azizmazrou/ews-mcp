"""Multi-user mode: per-request Exchange credentials, HTTP transport only.

Each LibreChat user supplies their own Exchange email + password via
customUserVars, forwarded as the ``X-EWS-Email`` / ``X-EWS-Password``
headers (see README "Multi-user mode"). This module turns those headers
into a per-user ``Context`` with its own ``EWSGateway`` — reused across
calls from the same user, since autodiscover + auth is too slow to redo
on every tool call — while everything that would otherwise leak between
users (cache mirror, audit log, alias DB, semantic index, connection
manager) stays out of the picture entirely: the per-user Context is built
with those set to ``None``/no-op.

The tool ``registry`` is the one thing safe to share: ``ToolSpec`` objects
are stateless (built once from the tier-filtered global config), so every
per-user Context reuses the same dict rather than rebuilding it.
"""

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Optional, Tuple

from .errors import ToolError
from .gateway.client import EWSGateway
from .ids import NullAliaser
from .tools.base import Context

logger = logging.getLogger(__name__)

EMAIL_HEADER = "x-ews-email"
PASSWORD_HEADER = "x-ews-password"

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


def extract_credentials_from_asgi_headers(headers) -> Tuple[str, str]:
    """``headers``: the ASGI scope's raw ``[(name: bytes, value: bytes), ...]`` list."""
    email = password = None
    for name, value in headers or []:
        lname = _decode(name).lower()
        if lname == EMAIL_HEADER:
            email = _decode(value).strip()
        elif lname == PASSWORD_HEADER:
            password = _decode(value)
    return _require(email, password)


def extract_credentials_from_request(request) -> Tuple[str, str]:
    """``request``: the raw Starlette ``Request`` reachable via
    ``server.request_context.request`` during MCP tool dispatch under the
    streamable-http transport. ``None`` (stdio, or an SDK version that
    doesn't populate it) is treated the same as missing headers."""
    headers = getattr(request, "headers", None) if request is not None else None
    email = headers.get(EMAIL_HEADER) if headers is not None else None
    password = headers.get(PASSWORD_HEADER) if headers is not None else None
    return _require(email, password)


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

    def get(self, email: str, password: str) -> Context:
        pwd_hash = hashlib.sha256(password.encode()).hexdigest()
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

    def _build(self, email: str, password: str) -> Context:
        settings = self._template.settings.model_copy(update={
            "ews_email": email, "ews_username": None, "ews_password": password,
        })
        return Context(
            settings=settings,
            gateway=EWSGateway(settings),
            manager=None,
            aliaser=NullAliaser(),
            audit=_NullAudit(),
            cache=None,
            sync=None,
            semantic=None,
            registry=self._template.registry,
        )
