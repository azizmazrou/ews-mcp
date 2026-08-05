"""OAuth2 resource-server layer (DESIGN.md §Transports).

Only reachable in ``AUTH_MODE=oidc``; ``static`` never imports it at request
time. The pieces: a verified :class:`Principal`, a JWKS cache that survives
key rotation and IdP blips, a token verifier whose algorithm list is an
allowlist, and the ASGI glue that carries identity in the request scope.
"""

from .asgi import (
    PROTECTED_RESOURCE_PATH,
    SCOPE_PRINCIPAL_KEY,
    authenticate,
    bearer_token,
    protected_resource_metadata,
)
from .binding import CallerBinder, build_binder
from .errors import AuthError, blocked, invalid_token
from .jwks import JWKSCache
from .obo import ExchangeToken, TokenExchanger
from .principal import Principal
from .verify import TokenVerifier

__all__ = [
    "PROTECTED_RESOURCE_PATH",
    "SCOPE_PRINCIPAL_KEY",
    "AuthError",
    "CallerBinder",
    "ExchangeToken",
    "JWKSCache",
    "Principal",
    "TokenExchanger",
    "TokenVerifier",
    "authenticate",
    "bearer_token",
    "blocked",
    "build_binder",
    "invalid_token",
    "protected_resource_metadata",
]
