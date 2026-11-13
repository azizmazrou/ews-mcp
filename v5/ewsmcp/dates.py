"""ONE date grammar for every tool argument that names a point in time:

    'today' | '+Nd' (N days from today) | YYYY-MM-DD | ISO-8601 datetime

Dates parse to local midnight in the server timezone; naive datetimes get
the server timezone attached; aware datetimes are converted to the server
timezone. Every tool shares this parser — v3's drift bugs came from three
modules re-implementing similar-but-different rules.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .errors import ToolError

_REL_RE = re.compile(r"^\+(\d{1,4})d$")
GRAMMAR_HINT = "Use 'today', '+Nd' (e.g. '+7d'), YYYY-MM-DD, or an ISO datetime."


def parse_when(value, field: str, tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(
            "validation", f"{field!r} must be a non-empty date string.",
            hint=GRAMMAR_HINT,
        )
    v = value.strip()
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if v.lower() == "today":
        return midnight
    m = _REL_RE.match(v)
    if m:
        return midnight + timedelta(days=int(m.group(1)))
    try:
        dt = datetime.fromisoformat(v)  # date-only parses to midnight
    except ValueError:
        raise ToolError(
            "validation", f"{field!r}: cannot parse {value!r} as a date.",
            hint=GRAMMAR_HINT,
        ) from None
    return dt.astimezone(tz) if dt.tzinfo is not None else dt.replace(tzinfo=tz)
