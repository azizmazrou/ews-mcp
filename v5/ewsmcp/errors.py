"""Error taxonomy — every failure the model sees is one of these codes,
with a hint written for the model, never a traceback (DESIGN.md §Errors)."""

from typing import Any, Dict, Optional

HTTP_BY_CODE = {
    "validation": 400,
    "auth_failed": 401,
    "tier_blocked": 403,
    "kill_switch": 403,
    "forbidden": 403,
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
