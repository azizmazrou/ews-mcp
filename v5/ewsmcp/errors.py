"""Error taxonomy — every failure the model sees is one of these codes,
with a hint written for the model, never a traceback (DESIGN.md §Errors)."""

import re

from exchangelib.errors import UnauthorizedError

from .toolerror import HTTP_BY_CODE, ToolError

__all__ = ["AUTH_FAILED_HINT", "HTTP_BY_CODE", "ToolError", "map_exception"]

AUTH_FAILED_HINT = (
    "Retrying will not help: credentials are read once at startup and "
    "repeated failures risk locking the directory account. Ask the operator "
    "to check the configured address/username and password, update the "
    "configuration, and restart the server."
)

_AUTH_STATUS_RE = re.compile(
    r"\bunauthorized\b|\bhttp\s+401\b|\bstatus(?:\s+code)?[:=\s]+401\b"
)


def map_exception(exc: Exception) -> ToolError:
    """Classify an arbitrary upstream/library exception."""
    if isinstance(exc, ToolError):
        return exc
    name = type(exc).__name__
    text = f"{name}: {exc}"
    lowered = f"{name} {exc}".lower()
    if isinstance(exc, UnauthorizedError) or name == "UnauthorizedError":
        return ToolError("auth_failed", text, hint=AUTH_FAILED_HINT)
    if "errorserverbusy" in lowered or "back off" in lowered or "ratelimit" in lowered:
        return ToolError(
            "throttled", text,
            hint="Exchange asked us to slow down. Retry after the delay.",
            retry_after_s=60,
        )
    if "erroritemnotfound" in lowered or "errorinvalidid" in lowered:
        return ToolError(
            "not_found", text,
            hint=(
                "The item id is stale (items move). Re-run search_messages "
                "and use a fresh id."
            ),
        )
    if "invalid credentials" in lowered or _AUTH_STATUS_RE.search(lowered):
        return ToolError("auth_failed", text, hint=AUTH_FAILED_HINT)
    if any(
        k in lowered
        for k in ("connection", "timeout", "timed out", "transport", "auth type")
    ):
        return ToolError(
            "upstream_unavailable", text,
            hint="Exchange is unreachable or refusing fresh sessions; check /readyz.",
            retry_after_s=30,
        )
    return ToolError("upstream_error", text)
