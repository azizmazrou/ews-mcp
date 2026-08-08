"""Who is calling — the one answer every per-caller gate keys off.

Deliberately outside ``ewsmcp.auth``: the dispatcher needs this on every
single call, and importing the auth package would drag ``jwt`` and ``httpx``
into stdio/static mode, which never authenticates anybody.

The static-mode caller is a real ``Caller`` too, not ``None``. That is what
lets one code path serve both modes: the gates below always have a subject
and a mailbox to key on, and there is no "identity missing" branch tempted
to fall back to the configured mailbox.
"""

import hashlib
import hmac
from dataclasses import dataclass

# Subject used when no token was involved (static mode). Not a valid `sub`
# from any issuer, so it can never collide with a real caller.
STATIC_SUBJECT = "-"

# The ASGI scope slot the HTTP layer writes the verified Principal into and
# the tool layer reads back out. It lives here rather than in ``auth`` so the
# server can read it without importing jwt/httpx in stdio mode.
SCOPE_PRINCIPAL_KEY = "ewsmcp.principal"


@dataclass(frozen=True)
class Caller:
    subject: str  # namespace/pool key — stable, never reassigned
    smtp: str     # the mailbox this call is about


def caller_of(ctx) -> Caller:
    """The verified caller, or the configured mailbox in static mode.

    Uses ``principal.subject`` rather than the address on purpose: SMTP
    addresses get reassigned when people leave, and a confirm token or an
    alias namespace must not survive into a successor's hands.
    """
    principal = getattr(ctx, "principal", None)
    if principal is not None:
        return Caller(subject=principal.key, smtp=principal.smtp)
    return Caller(subject=STATIC_SUBJECT, smtp=(ctx.settings.ews_email or "").lower())


def audit_identity(ctx, caller: Caller) -> str:
    """What goes in the audit record.

    Hashed by default: DESIGN.md law #6 keeps real addresses out of tracked
    artifacts, and an audit file should not double as a staff directory.
    ``AUDIT_IDENTITY=smtp`` trades that away for debuggability.
    """
    if caller.subject == STATIC_SUBJECT:
        return STATIC_SUBJECT
    if getattr(ctx.settings, "audit_identity", "hash") == "smtp":
        return caller.smtp
    return namespace_uid(ctx.settings, caller.subject)


def namespace_uid(settings, subject: str) -> str:
    """Stable, opaque per-caller id — also the future DATA_DIR namespace.

    Salted so the same directory listing cannot be dictionary-attacked back
    into a list of employees; truncated because 24 hex chars is already far
    past collision risk for a company-sized population.
    """
    salt = (getattr(settings, "data_dir_namespace_salt", None) or "").encode()
    return hmac.new(salt, subject.encode(), hashlib.sha256).hexdigest()[:24]
