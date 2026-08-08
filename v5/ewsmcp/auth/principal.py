"""The verified caller — everything downstream keys off this object."""

from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class Principal:
    """A caller whose access token has been cryptographically verified.

    Two identity fields, deliberately:

    - ``subject`` (the ``sub`` claim) is the pool and namespace key. SMTP
      addresses get reassigned when people leave, so keying storage on the
      address would hand a new employee the predecessor's alias database and
      cache mirror. ``sub`` is never reassigned.
    - ``smtp`` is what Exchange is asked for (``primary_smtp_address``).

    ``raw_token`` is needed to exchange this token for an EWS-audience one,
    so it is excluded from ``repr``/``eq``: a Principal must never leak token
    material into a log line or a traceback (DESIGN.md law #6).
    """

    subject: str
    smtp: str
    issuer: str
    expires_at: float
    scopes: Tuple[str, ...] = ()
    raw_token: str = field(default="", repr=False, compare=False)

    @property
    def key(self) -> str:
        """Stable identity across issuers — the gateway-pool and DATA_DIR key.

        Scoped by issuer because ``sub`` is only unique within one issuer;
        two IdPs could otherwise collide onto the same mailbox namespace.
        """
        return f"{self.issuer}|{self.subject}"
