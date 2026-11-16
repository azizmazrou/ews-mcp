"""Dependency-free ToolError and HTTP status mapping (DESIGN.md §Errors).

Kept free of third-party imports so isolated extraction subprocesses can raise
and serialize ``ToolError`` without loading ``exchangelib`` or network stacks.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

HTTP_BY_CODE: Dict[str, int] = {
    "validation": 400,
    "auth_failed": 401,
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
    ) -> None:
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
