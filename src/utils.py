"""Utility functions for EWS MCP Server."""

from datetime import datetime
from typing import Any, Dict, List, Optional, Union
import html
import logging
import os
import json
import re
from exchangelib import EWSTimeZone, EWSDateTime, EWSDate
import pytz


def get_timezone():
    """Get the configured timezone as EWSTimeZone."""
    # Get timezone from environment or default to UTC
    tz_name = os.environ.get('TIMEZONE', os.environ.get('TZ', 'UTC'))
    try:
        return EWSTimeZone(tz_name)
    except Exception:
        # Fallback to UTC if timezone not found
        return EWSTimeZone('UTC')


def get_pytz_timezone():
    """Get the configured timezone as pytz timezone."""
    tz_name = os.environ.get('TIMEZONE', os.environ.get('TZ', 'UTC'))
    try:
        return pytz.timezone(tz_name)
    except Exception:
        return pytz.UTC


def make_tz_aware(dt: datetime) -> EWSDateTime:
    """Make a naive datetime timezone-aware as EWSDateTime with EWSTimeZone.

    This is the correct way to create datetime objects for exchangelib.
    """
    if isinstance(dt, EWSDateTime):
        # Already EWSDateTime
        return dt

    tz = get_timezone()

    if dt.tzinfo is not None:
        # Already timezone-aware - convert to target timezone first
        # Get the target timezone as pytz for conversion
        tz_name = os.environ.get('TIMEZONE', os.environ.get('TZ', 'UTC'))
        target_tz = pytz.timezone(tz_name)

        # Convert to target timezone
        dt_converted = dt.astimezone(target_tz)

        # Create EWSDateTime with EWSTimeZone
        return EWSDateTime(
            dt_converted.year, dt_converted.month, dt_converted.day,
            dt_converted.hour, dt_converted.minute, dt_converted.second,
            dt_converted.microsecond,
            tzinfo=tz
        )

    # Naive datetime - create EWSDateTime with configured timezone
    return EWSDateTime(
        dt.year, dt.month, dt.day,
        dt.hour, dt.minute, dt.second,
        dt.microsecond,
        tzinfo=tz
    )


def parse_datetime_tz_aware(dt_str: Optional[str]) -> Optional[EWSDateTime]:
    """Parse ISO 8601 datetime string and return as EWSDateTime with EWSTimeZone.

    Returns None if the input is empty or unparseable. Callers that require
    a value should check for None before using the result (assigning None to
    exchangelib datetime fields is a hard-to-diagnose silent failure).
    """
    if not dt_str:
        return None

    try:
        # Parse the datetime string
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))

        # Convert to EWSDateTime with configured timezone
        return make_tz_aware(dt)
    except ValueError:
        logging.getLogger(__name__).debug(f"parse_datetime_tz_aware: bad value: {dt_str!r}")
        return None


def parse_date_tz_aware(date_str: Optional[str]) -> Optional[EWSDate]:
    """Parse ISO 8601 date/datetime string and return as EWSDate.

    Used for task due_date and start_date fields which only accept EWSDate,
    not EWSDateTime. Accepts date-only ('2025-11-15') and datetime strings.
    Returns None on empty/unparseable input.
    """
    if not date_str:
        return None

    try:
        # Parse the datetime string (works for both date and datetime formats)
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))

        # Convert timezone-aware datetime to target timezone if needed
        if dt.tzinfo is not None:
            tz_name = os.environ.get('TIMEZONE', os.environ.get('TZ', 'UTC'))
            target_tz = pytz.timezone(tz_name)
            dt = dt.astimezone(target_tz)

        # Create EWSDate from the date components only (no time)
        return EWSDate(dt.year, dt.month, dt.day)
    except ValueError:
        logging.getLogger(__name__).debug(f"parse_date_tz_aware: bad value: {date_str!r}")
        return None


def format_datetime(dt: Optional[datetime]) -> Optional[str]:
    """Format datetime to ISO 8601 string."""
    if dt is None:
        return None
    return dt.isoformat()


def parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO 8601 datetime string (legacy - use parse_datetime_tz_aware instead)."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    except ValueError:
        return None


def escape_html(text: Any) -> str:
    """HTML-escape an arbitrary value for safe interpolation into markup.

    Returns an empty string for None. Accepts non-string values by coercing
    to str. Use for every field that is embedded into an HTML template we
    emit (reply/forward quoted headers, user-supplied body when treated as
    plain text, etc.).
    """
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def sanitize_html(html_content: str) -> str:
    """Sanitize HTML that will be sent to Exchange.

    Strips <script> and <style> blocks and dangerous inline handlers. This is
    intentionally conservative — full allowlist sanitisation would require
    bleach/lxml. Use escape_html for text that should never contain markup.
    """
    if not html_content:
        return ""

    cleaned = re.sub(
        r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>",
        "",
        html_content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Strip event handlers (onclick=, onerror=, ...). Matches on attribute
    # boundaries to avoid clobbering CSS selectors.
    cleaned = re.sub(
        r"(?i)\son[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
        "",
        cleaned,
    )
    # Neutralise javascript: URIs inside href/src attributes.
    cleaned = re.sub(
        r"(?i)(href|src)\s*=\s*([\"']?)\s*javascript:",
        r"\1=\2about:blank;",
        cleaned,
    )
    return cleaned


def format_body_for_html(body: Optional[str]) -> str:
    """Return an HTML-safe rendering of a user-supplied body.

    - If the body already contains tags, run it through sanitize_html so
      attacker-controlled markup is neutralised while the author's intent is
      preserved.
    - Otherwise treat it as plain text: HTML-escape and convert newlines to
      <br/> so line breaks survive the round trip.
    """
    if not body:
        return ""
    body = body.strip()
    looks_like_html = bool(re.search(r"<[^>]+>", body))
    if looks_like_html:
        return sanitize_html(body)
    return escape_html(body).replace("\n", "<br/>")


def truncate_text(text: str, max_length: int = 100) -> str:
    """Truncate text to max length."""
    if text is None or not text:
        return ""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."


def safe_get(obj: Any, attr: str, default: Any = None) -> Any:
    """Safely get attribute from object."""
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def ews_id_to_str(ews_id: Any) -> Optional[str]:
    """Convert an EWS ID object to a string.

    EWS objects like FolderId, ItemId, ParentFolderId have an 'id' attribute
    that contains the actual string ID. This function safely extracts that string.

    Args:
        ews_id: An EWS ID object or string

    Returns:
        String representation of the ID, or None if conversion fails
    """
    if ews_id is None:
        return None

    # Already a string
    if isinstance(ews_id, str):
        return ews_id

    # EWS ID objects have an 'id' attribute with the string value
    if hasattr(ews_id, 'id'):
        return str(ews_id.id) if ews_id.id is not None else None

    # Try converting to string as last resort
    try:
        return str(ews_id)
    except Exception:
        return None


def make_json_serializable(obj: Any) -> Any:
    """Recursively convert an object to be JSON serializable.

    Handles EWS objects, datetime objects, and nested structures.

    Args:
        obj: Any object that needs to be JSON serializable

    Returns:
        JSON-serializable version of the object
    """
    if obj is None:
        return None

    # Already JSON-serializable primitives
    if isinstance(obj, (str, int, float, bool)):
        return obj

    # Handle datetime objects
    if isinstance(obj, datetime):
        return obj.isoformat()

    # Handle EWSDateTime and EWSDate
    if isinstance(obj, (EWSDateTime, EWSDate)):
        return obj.isoformat() if hasattr(obj, 'isoformat') else str(obj)

    # Handle lists/tuples
    if isinstance(obj, (list, tuple)):
        return [make_json_serializable(item) for item in obj]

    # Handle dictionaries
    if isinstance(obj, dict):
        return {str(k): make_json_serializable(v) for k, v in obj.items()}

    # Handle EWS ID-like objects (FolderId, ItemId, ParentFolderId, etc.)
    # These have an 'id' attribute containing the actual string ID
    if hasattr(obj, 'id'):
        return ews_id_to_str(obj)

    # Handle objects with __dict__ (convert to dict)
    if hasattr(obj, '__dict__'):
        try:
            return make_json_serializable(vars(obj))
        except Exception:
            pass

    # Last resort: convert to string
    try:
        return str(obj)
    except Exception:
        return f"<non-serializable: {type(obj).__name__}>"


class EWSJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles EWS objects.

    Use this encoder when calling json.dumps on data that may contain
    EWS objects like FolderId, ItemId, ParentFolderId, etc.

    Example:
        json.dumps(data, cls=EWSJSONEncoder)
    """

    def default(self, obj: Any) -> Any:
        """Convert non-serializable objects."""
        result = make_json_serializable(obj)
        if isinstance(result, (dict, list)):
            return result
        if isinstance(result, str):
            return result
        return str(result)


def safe_json_dumps(obj: Any, **kwargs) -> str:
    """Safely serialize an object to JSON, handling EWS objects.

    Args:
        obj: Object to serialize
        **kwargs: Additional arguments to pass to json.dumps

    Returns:
        JSON string representation
    """
    return json.dumps(obj, cls=EWSJSONEncoder, **kwargs)


def format_error_response(error: Exception, context: str = "") -> Dict[str, Any]:
    """Format error as a short, actionable response."""
    logger = logging.getLogger(__name__)
    error_msg = str(error)

    if context and context != "":
        error_msg = f"{context}: {error_msg}"

    # Truncate very long error messages
    if len(error_msg) > 200:
        error_msg = error_msg[:197] + "..."

    logger.error(error_msg)

    return {
        "success": False,
        "error": error_msg
    }


def format_success_response(message: str, **kwargs) -> Dict[str, Any]:
    """Format success response."""
    response = {
        "success": True,
        "message": message
    }
    response.update(kwargs)
    return response


def find_message_for_account(account, message_id):
    """
    Search for a message across multiple folders for a specific account.

    This function searches common folders for a message by ID, including
    custom subfolders. This is necessary because Exchange Web Services
    requires knowing which folder a message is in to retrieve it.

    Args:
        account: The Exchange Account object (primary or impersonated)
        message_id: The Exchange message ID to find

    Returns:
        The message item if found

    Raises:
        ToolExecutionError if message not found in any folder
    """
    from .exceptions import ToolExecutionError

    # List of common folders to search (in priority order)
    folders_to_search = [
        ("inbox", account.inbox),
        ("sent", account.sent),
        ("drafts", account.drafts),
        ("deleted", account.trash),
        ("junk", account.junk),
    ]

    # Also search subfolders of all standard folders (not just inbox)
    for parent_name, parent_folder in list(folders_to_search):
        try:
            for child in parent_folder.children:
                child_name = safe_get(child, 'name', 'unknown')
                folders_to_search.append((f"{parent_name}/{child_name}", child))
        except Exception:
            pass

    # Search each folder for the message
    for folder_name, folder in folders_to_search:
        try:
            item = folder.get(id=message_id)
            if item:
                return item
        except Exception:
            continue

    # Fallback: search from root recursively for custom top-level folders
    try:
        for folder in account.root.walk():
            if folder in [f for _, f in folders_to_search]:
                continue
            try:
                item = folder.get(id=message_id)
                if item:
                    return item
            except Exception:
                continue
    except Exception:
        pass

    raise ToolExecutionError(f"Message not found: {message_id}")


def find_message_across_folders(ews_client, message_id):
    """
    Search for a message across multiple folders.

    Deprecated: Use find_message_for_account with explicit account parameter.

    Args:
        ews_client: The EWS client instance
        message_id: The Exchange message ID to find

    Returns:
        The message item if found

    Raises:
        ToolExecutionError if message not found in any folder
    """
    return find_message_for_account(ews_client.account, message_id)


def _safe_content_id(file_name: str, index: int, existing: set) -> str:
    """Produce a safe, unique cid: value from an arbitrary file name.

    Content-ID values are embedded into HTML (`cid:...`) and should be ASCII
    with no whitespace, `<`, `>`, quotes, or collision with other inlines in
    the same message.
    """
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", file_name).strip("-")
    if not base:
        base = f"inline-{index}"
    candidate = base
    suffix = 1
    while candidate in existing:
        suffix += 1
        candidate = f"{base}-{suffix}"
    existing.add(candidate)
    return candidate


def attach_inline_files(message, inline_attachments: list) -> int:
    """Attach base64-encoded files to an EWS message or calendar item.

    Args:
        message: An exchangelib Message or CalendarItem object
        inline_attachments: List of dicts with file_name, file_content (base64),
                           optional content_type and is_inline

    Returns:
        Number of attachments added
    """
    if not inline_attachments:
        return 0

    import base64
    from exchangelib import FileAttachment

    count = 0
    used_cids: set = set()
    for index, att in enumerate(inline_attachments):
        file_name = att.get("file_name")
        file_content = att.get("file_content")
        if not file_name or not file_content:
            continue

        is_inline = att.get("is_inline", False)
        content_id = (
            att.get("content_id")
            or (_safe_content_id(file_name, index, used_cids) if is_inline else None)
        )
        if is_inline and content_id:
            used_cids.add(content_id)

        file_attachment = FileAttachment(
            name=file_name,
            content=base64.b64decode(file_content),
            content_type=att.get("content_type", "application/octet-stream"),
            is_inline=is_inline,
            content_id=content_id,
        )
        message.attach(file_attachment)
        count += 1

    return count


# Shared schema for inline_attachments parameter (base64-encoded files)
INLINE_ATTACHMENTS_SCHEMA = {
    "inline_attachments": {
        "description": "Attachments as base64-encoded content. Use when file paths are not accessible (e.g. in cloud/Docker environments).",
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "file_name": {
                    "type": "string",
                    "description": "File name with extension (e.g. 'report.pdf', 'image.png')"
                },
                "file_content": {
                    "type": "string",
                    "description": "Base64-encoded file content"
                },
                "content_type": {
                    "type": "string",
                    "default": "application/octet-stream",
                    "description": "MIME type (e.g. 'image/png', 'application/pdf')"
                },
                "is_inline": {
                    "type": "boolean",
                    "default": False,
                    "description": "True = embedded in body (use cid:file_name to reference in HTML)"
                }
            },
            "required": ["file_name", "file_content"]
        }
    }
}
