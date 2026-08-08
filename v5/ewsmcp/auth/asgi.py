"""ASGI glue: pull the bearer token out, put the Principal into the scope.

The scope is the whole identity-plumbing trick. ``build_app`` verifies the
token once per HTTP request and writes the Principal into the ASGI ``scope``
dict; the Streamable HTTP transport hands that *same* dict to the tool layer
as ``request_context.request.scope`` (pinned by ``test_mcp_sdk_pins.py``).

The scope is created per request by the server and shared with nothing, so
no request can ever observe another's identity — unlike a module global, and
without betting on contextvar propagation across the SDK's task boundaries.
"""

from typing import Any, Iterable, Optional, Tuple

from ..identity import SCOPE_PRINCIPAL_KEY
from .errors import AuthError, invalid_token
from .principal import Principal

PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"


def _header(headers: Optional[Iterable[Tuple[Any, Any]]], name: bytes) -> Optional[str]:
    """Case-insensitive lookup over raw ASGI headers (bytes pairs, but tests
    and some servers hand back str — accept both)."""
    for raw_name, raw_value in headers or []:
        key = raw_name if isinstance(raw_name, bytes) else str(raw_name).encode()
        if key.lower() != name:
            continue
        return (raw_value.decode("utf-8", "replace")
                if isinstance(raw_value, bytes) else str(raw_value))
    return None


def bearer_token(headers: Optional[Iterable[Tuple[Any, Any]]]) -> Optional[str]:
    value = _header(headers, b"authorization")
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


async def authenticate(verifier, headers) -> Principal:
    """Verify the request's bearer token or raise :class:`AuthError`."""
    token = bearer_token(headers)
    if token is None:
        raise invalid_token(
            "this endpoint requires an OAuth2 bearer token",
            reason="missing_token",
            hint=f"Send Authorization: Bearer <access token>. See {PROTECTED_RESOURCE_PATH}.",
        )
    return await verifier.verify(token)


def protected_resource_metadata(settings) -> dict:
    """RFC 9728 document — how a generic MCP client discovers the IdP."""
    return {
        "resource": settings.auth_audience,
        "authorization_servers": [settings.auth_issuer],
        "bearer_methods_supported": ["header"],
    }


__all__ = [
    "PROTECTED_RESOURCE_PATH",
    "SCOPE_PRINCIPAL_KEY",
    "AuthError",
    "authenticate",
    "bearer_token",
    "protected_resource_metadata",
]
