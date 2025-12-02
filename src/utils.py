"""Utility functions for EWS MCP Server."""

from datetime import datetime
from typing import Any, Dict, List, Optional, Callable, Union
import logging
import os
import functools
import json
from exchangelib import EWSTimeZone, EWSDateTime, EWSDate
from exchangelib.errors import (
    RateLimitError,
    ErrorServerBusy,
    ErrorTimeoutExpired,
    TransportError,
    ResponseMessageError
)
from requests.exceptions import HTTPError, ConnectionError, Timeout
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)
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


def parse_datetime_tz_aware(dt_str: str) -> EWSDateTime:
    """Parse ISO 8601 datetime string and return as EWSDateTime with EWSTimeZone.

    This ensures all datetime objects used with exchangelib have the correct timezone format.
    """
    if not dt_str:
        return None

    try:
        # Parse the datetime string
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))

        # Convert to EWSDateTime with configured timezone
        return make_tz_aware(dt)
    except ValueError:
        return None


def parse_date_tz_aware(date_str: str) -> EWSDate:
    """Parse ISO 8601 date/datetime string and return as EWSDate.

    Used for task due_date and start_date fields which only accept EWSDate, not EWSDateTime.
    Accepts both date-only strings (2025-11-15) and full datetime strings (2025-11-15T17:00:00+03:00).
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


def sanitize_html(html: str) -> str:
    """Basic HTML sanitization."""
    # In production, use a proper HTML sanitizer like bleach
    return html


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
        """Convert non-serializable objects to serializable format."""
        # Handle datetime objects
        if isinstance(obj, datetime):
            return obj.isoformat()

        # Handle EWSDateTime and EWSDate
        if isinstance(obj, (EWSDateTime, EWSDate)):
            return obj.isoformat() if hasattr(obj, 'isoformat') else str(obj)

        # Handle EWS ID-like objects
        if hasattr(obj, 'id'):
            result = ews_id_to_str(obj)
            if result is not None:
                return result

        # Handle objects with __dict__
        if hasattr(obj, '__dict__'):
            try:
                return make_json_serializable(vars(obj))
            except Exception:
                pass

        # Try string conversion
        try:
            return str(obj)
        except Exception:
            return f"<non-serializable: {type(obj).__name__}>"


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
    """Format error as response dictionary."""
    logger = logging.getLogger(__name__)
    error_msg = f"{context}: {str(error)}" if context else str(error)
    logger.error(error_msg)

    return {
        "success": False,
        "message": error_msg,
        "error_type": type(error).__name__
    }


def format_success_response(message: str, **kwargs) -> Dict[str, Any]:
    """Format success response."""
    response = {
        "success": True,
        "message": message
    }
    response.update(kwargs)
    return response


def handle_ews_errors(func: Callable) -> Callable:
    """Decorator to handle EWS errors with retry logic.

    This decorator:
    1. Automatically retries on rate limit, server busy, timeout, and HTTP errors
    2. Uses exponential backoff (2s, 4s, 8s, 16s)
    3. Maximum 4 retry attempts
    4. Returns structured error responses
    5. Logs all errors for debugging

    Usage:
        @handle_ews_errors
        async def my_tool_execute(self, **kwargs):
            # Your EWS operations here
            pass
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        # Custom retry condition for HTTP errors (502, 503, 504)
        def should_retry_http_error(exception):
            """Check if HTTP error should be retried (502, 503, 504)."""
            if isinstance(exception, HTTPError):
                if hasattr(exception, 'response') and exception.response is not None:
                    status_code = exception.response.status_code
                    # Retry on transient server errors
                    return status_code in [502, 503, 504]
            return False

        # Custom retry condition combining exception types and HTTP errors
        def should_retry(exception):
            """Determine if exception should be retried."""
            # Always retry these exception types
            if isinstance(exception, (
                RateLimitError,
                ErrorServerBusy,
                ErrorTimeoutExpired,
                TransportError,
                ConnectionError,
                Timeout
            )):
                return True
            # Retry specific HTTP errors
            return should_retry_http_error(exception)

        # Create a retry decorator for this specific call
        retry_decorator = retry(
            retry=should_retry,
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=2, min=2, max=16),
            reraise=True
        )

        try:
            # Apply retry logic to the function
            retried_func = retry_decorator(func)
            return await retried_func(*args, **kwargs)
        except RateLimitError as e:
            logger = logging.getLogger(__name__)
            logger.error(f"Rate limit exceeded: {e}")
            return format_error_response(
                e,
                context="Rate limit exceeded. Please try again later."
            )
        except ErrorServerBusy as e:
            logger = logging.getLogger(__name__)
            logger.error(f"Exchange server busy: {e}")
            return format_error_response(
                e,
                context="Exchange server is currently busy. Please try again."
            )
        except ErrorTimeoutExpired as e:
            logger = logging.getLogger(__name__)
            logger.error(f"Operation timeout: {e}")
            return format_error_response(
                e,
                context="Operation timed out. Please try again."
            )
        except TransportError as e:
            logger = logging.getLogger(__name__)
            logger.error(f"Network/transport error: {e}")
            return format_error_response(
                e,
                context="Network error occurred. Please check connectivity."
            )
        except HTTPError as e:
            logger = logging.getLogger(__name__)
            status_code = e.response.status_code if hasattr(e, 'response') and e.response else 'unknown'
            logger.error(f"HTTP error {status_code}: {e}")
            if status_code in [502, 503, 504]:
                return format_error_response(
                    e,
                    context=f"Server temporarily unavailable (HTTP {status_code}). Retried but failed."
                )
            return format_error_response(
                e,
                context=f"HTTP error {status_code} occurred."
            )
        except (ConnectionError, Timeout) as e:
            logger = logging.getLogger(__name__)
            logger.error(f"Connection error: {e}")
            return format_error_response(
                e,
                context="Connection error. Please check network connectivity and try again."
            )
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.error(f"Unexpected error in {func.__name__}: {e}", exc_info=True)
            return format_error_response(
                e,
                context=f"Unexpected error in {func.__name__}"
            )

    return wrapper


def find_message_across_folders(ews_client, message_id):
    """
    Search for a message across multiple folders.

    This function searches common folders for a message by ID, including
    custom subfolders. This is necessary because Exchange Web Services
    requires knowing which folder a message is in to retrieve it.

    Args:
        ews_client: The EWS client instance
        message_id: The Exchange message ID to find

    Returns:
        The message item if found

    Raises:
        ToolExecutionError if message not found in any folder
    """
    from .exceptions import ToolExecutionError

    # List of common folders to search (in priority order)
    folders_to_search = [
        ("inbox", ews_client.account.inbox),
        ("sent", ews_client.account.sent),
        ("drafts", ews_client.account.drafts),
        ("deleted", ews_client.account.trash),
        ("junk", ews_client.account.junk),
    ]

    # Also search custom subfolders under inbox (like CC, Archive, etc.)
    try:
        for child in ews_client.account.inbox.children:
            child_name = safe_get(child, 'name', 'unknown')
            folders_to_search.append((f"inbox/{child_name}", child))
    except Exception:
        pass  # If we can't list subfolders, continue with standard folders

    # Search each folder for the message
    for folder_name, folder in folders_to_search:
        try:
            item = folder.get(id=message_id)
            if item:
                return item
        except Exception:
            # Message not in this folder, continue searching
            continue

    # If we get here, message wasn't found in any folder
    raise ToolExecutionError(
        f"Message not found: {message_id}. "
        f"The message may have been deleted, moved to a folder not in the search path, "
        f"or the ID may be invalid."
    )
