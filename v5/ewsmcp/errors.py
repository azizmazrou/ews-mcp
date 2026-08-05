"""Error taxonomy — every failure the model sees is one of these codes,
with a hint written for the model, never a traceback (DESIGN.md §Errors)."""

from typing import Any, Dict, Optional

HTTP_BY_CODE = {
    "validation": 400,
    "auth_failed": 401,
    "identity_blocked": 403,
    "tier_blocked": 403,
    "kill_switch": 403,
    "recipient_blocked": 403,
    "confirm_invalid": 409,
    "not_found": 404,
    "throttled": 429,
    "rate_capped": 429,
    "upstream_unavailable": 503,
    "upstream_error": 502,
    "internal": 500,
}


class ToolError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        hint: Optional[str] = None,
        retry_after_s: Optional[int] = None,
    ):
        super().__init__(message)
        self.code = code if code in HTTP_BY_CODE else "internal"
        self.message = message
        self.hint = hint
        self.retry_after_s = retry_after_s

    def to_dict(self) -> Dict[str, Any]:
        err: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            err["hint"] = self.hint
        if self.retry_after_s is not None:
            err["retry_after_s"] = self.retry_after_s
        return {"ok": False, "error": err}

    @property
    def http_status(self) -> int:
        return HTTP_BY_CODE[self.code]


def _quoted(value: str) -> str:
    """RFC 7230 quoted-string: backslash and double-quote must be escaped, and
    control characters dropped — an unescaped value would let a crafted error
    description forge extra auth parameters in the header."""
    cleaned = "".join(c for c in value if c >= " " and c != "\x7f")
    return '"' + cleaned.replace("\\", "\\\\").replace('"', '\\"') + '"'


def www_authenticate(
    realm: str = "ews-mcp",
    error: Optional[str] = None,
    desc: Optional[str] = None,
    resource_metadata: Optional[str] = None,
) -> bytes:
    """Build an RFC 6750 ``WWW-Authenticate: Bearer`` challenge value.

    ``error`` is one of the RFC 6750 codes (``invalid_token``,
    ``invalid_request``, ``insufficient_scope``); ``resource_metadata`` points
    at ``/.well-known/oauth-protected-resource`` so a generic MCP client can
    discover the authorization server. NEVER pass token material in ``desc``
    (DESIGN.md law #6).
    """
    parts = [f"realm={_quoted(realm)}"]
    if error:
        parts.append(f"error={_quoted(error)}")
    if desc:
        parts.append(f"error_description={_quoted(desc)}")
    if resource_metadata:
        parts.append(f"resource_metadata={_quoted(resource_metadata)}")
    return ("Bearer " + ", ".join(parts)).encode("ascii", "replace")


def map_exception(exc: Exception) -> ToolError:
    """Classify an arbitrary upstream/library exception."""
    if isinstance(exc, ToolError):
        return exc
    name = type(exc).__name__
    text = f"{name}: {exc}"
    lowered = f"{name} {exc}".lower()
    if "errorserverbusy" in lowered or "back off" in lowered or "ratelimit" in lowered:
        return ToolError(
            "throttled", text,
            hint="Exchange asked us to slow down. Retry after the delay.",
            retry_after_s=60,
        )
    if "erroritemnotfound" in lowered or "errorinvalidid" in lowered:
        return ToolError(
            "not_found", text,
            hint="The item id is stale (items move). Re-run search_messages and use a fresh id.",
        )
    if "unauthorized" in lowered or "401" in lowered or "invalid credentials" in lowered:
        return ToolError("auth_failed", text, hint="Upstream Exchange rejected our credentials.")
    if any(k in lowered for k in ("connection", "timeout", "timed out", "transport", "auth type")):
        return ToolError(
            "upstream_unavailable", text,
            hint="Exchange is unreachable or refusing fresh sessions; check /readyz.",
            retry_after_s=30,
        )
    return ToolError("upstream_error", text)
