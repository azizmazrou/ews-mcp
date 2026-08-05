"""Auth failures, carried in the existing envelope + an RFC 6750 challenge."""

from typing import Optional

from ..errors import ToolError, www_authenticate


class AuthError(ToolError):
    """A rejected caller.

    Reuses the server-wide error taxonomy (``auth_failed`` 401 /
    ``identity_blocked`` 403) so the envelope stays contract-tested, and adds
    the ``WWW-Authenticate`` challenge an OAuth2 client needs to know what to
    do next: ``invalid_token`` means re-acquire and retry, while
    ``insufficient_scope`` means stop.

    ``reason`` is a bounded label for metrics — never free text, never
    anything derived from the token itself.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason: str,
        hint: Optional[str] = None,
        oauth_error: str = "invalid_token",
    ):
        super().__init__(code, message, hint=hint)
        self.reason = reason
        self.oauth_error = oauth_error

    def challenge(self, realm: str = "ews-mcp",
                  resource_metadata: Optional[str] = None) -> bytes:
        # The message is server-authored and token-free by construction; it is
        # still the only thing that reaches the header, never the raw token.
        return www_authenticate(
            realm, error=self.oauth_error, desc=self.message,
            resource_metadata=resource_metadata,
        )


def invalid_token(message: str, *, reason: str, hint: Optional[str] = None) -> AuthError:
    return AuthError("auth_failed", message, reason=reason, hint=hint)


def blocked(message: str, *, reason: str, hint: Optional[str] = None) -> AuthError:
    """Valid token, refused identity — retrying will not help."""
    return AuthError("identity_blocked", message, reason=reason, hint=hint,
                     oauth_error="insufficient_scope")
