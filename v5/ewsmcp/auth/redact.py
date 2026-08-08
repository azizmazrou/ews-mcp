"""Keep token material out of the logs (DESIGN.md law #6).

Verification code is careful, but third-party libraries are not: httpx,
uvicorn and exchangelib will all happily log a URL, a header dump or an
exception repr that carries a bearer token. A filter on the root handler is
the only place that covers every one of them at once.
"""

import logging
import re
from typing import Optional

# `Bearer <token>` in any header/URL dump, and bare compact JWTs (which always
# start with `ey`, the base64 of `{"`), wherever they turn up.
#
# The 16-character floor on the bearer value keeps prose readable: our own
# "no bearer token was supplied" must not come out as "no bearer [redacted]
# was supplied". No real access token is that short, so nothing credential-
# shaped escapes through the gap.
_PATTERNS = (
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9\-._~+/]{16,}=*"),
    re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}(?:\.[A-Za-z0-9_-]*)?"),
)
_MASK = "[redacted]"


def redact(text: str) -> str:
    text = _PATTERNS[0].sub(lambda m: m.group(1) + _MASK, text)
    return _PATTERNS[1].sub(_MASK, text)


class TokenRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and ("ey" in record.msg or "earer" in record.msg):
            record.msg = redact(record.msg)
        if record.args:
            # Interpolation happens after filtering, so the arguments have to
            # be cleaned too — that is where a formatted token usually hides.
            if isinstance(record.args, dict):
                record.args = {
                    k: redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args
                )
        return True


def install(logger: Optional[logging.Logger] = None) -> None:
    """Attach to every handler on the root logger.

    On the *handlers* rather than the root logger itself: a filter on a logger
    only sees records logged directly to it, not records propagated up from
    child loggers — which is exactly where library tokens would come from.
    """
    root = logger or logging.getLogger()
    token_filter = TokenRedactingFilter()
    for handler in root.handlers:
        if not any(isinstance(f, TokenRedactingFilter) for f in handler.filters):
            handler.addFilter(token_filter)
