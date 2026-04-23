"""Email operation tools for EWS MCP Server."""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime
from exchangelib import Message, Mailbox, FileAttachment, HTMLBody, Body, Folder, ExtendedProperty
from exchangelib.queryset import Q

# Define Flag as ExtendedProperty for setting email flag status
# See: https://github.com/ecederstrand/exchangelib/issues/85
# Flag values: None = not flagged, 1 = completed, 2 = flagged
class FlagStatus(ExtendedProperty):
    property_tag = 0x1090
    property_type = 'Integer'

# Register the flag property on Message class
Message.register('flag_status_value', FlagStatus)

# Mapping from string flag_status to integer values
FLAG_STATUS_MAP = {
    'NotFlagged': None,
    'Flagged': 2,
    'Complete': 1,
}
import re

from .base import BaseTool
from ..models import SendEmailRequest, EmailSearchRequest, EmailDetails
from ..exceptions import ToolExecutionError, ValidationError
from ..utils import (
    format_success_response, safe_get, truncate_text, parse_datetime_tz_aware,
    find_message_across_folders, find_message_for_account, ews_id_to_str,
    attach_inline_files, INLINE_ATTACHMENTS_SCHEMA,
    escape_html, format_body_for_html, sanitize_html,
    project_fields, ensure_snippet, strip_body_by_default, LIST_DEFAULT_FIELDS,
    ews_call_log,
)
from .folder_tools import find_folder_by_id, get_standard_folder_map


# ---------------------------------------------------------------------------
# Search pagination + field-projection helpers (Issue 2)
# ---------------------------------------------------------------------------
#
# Prior behaviour iterated ``folder.filter(...)[:max_results]`` with a bare
# ``except Exception`` around the for-loop. exchangelib's auto-pagination
# can raise mid-stream (throttling, transient EWS errors) and the old
# handler silently logged + continued, so the response looked successful
# but carried only the items delivered before the failure — hence the
# reported "wider window returns FEWER results than a subset" symptom.
#
# These helpers make the pagination explicit, bounded, and classifiable:
#   * ``_db_fields_for`` maps the public ``fields=[...]`` projection to
#     exchangelib DB field names so ``.only(*db_fields)`` can shrink the
#     EWS payload rather than just trimming it client-side.
#   * ``_query_total`` calls ``query.count()`` once so the response can
#     expose ``total_available`` (EWS ``TotalItemsInView``).
#   * ``_paginate_query`` materialises the result in explicit chunks,
#     narrowly classifies exchangelib errors, and returns a
#     ``FolderQueryOutcome`` so the caller can carry the diagnosis into
#     the response + structured log.
# ---------------------------------------------------------------------------


# Public-field -> tuple of exchangelib DB fields. Unknown public names
# map to () — in that case we skip .only() rather than crash.
_DB_FIELDS_BY_PUBLIC: Dict[str, tuple] = {
    "id": ("id",),
    "message_id": ("id",),
    "subject": ("subject",),
    "from": ("sender",),
    "sender": ("sender",),
    "to": ("to_recipients",),
    "cc": ("cc_recipients",),
    "bcc": ("bcc_recipients",),
    "received_time": ("datetime_received",),
    "received": ("datetime_received",),
    "datetime_received": ("datetime_received",),
    "sent_time": ("datetime_sent",),
    "sent": ("datetime_sent",),
    "datetime_sent": ("datetime_sent",),
    "is_read": ("is_read",),
    "has_attachments": ("has_attachments",),
    "importance": ("importance",),
    "categories": ("categories",),
    # Snippet/preview/body all derive from text_body + body; include both
    # so exchangelib fetches the HTML body too when callers ask for body.
    "snippet": ("text_body",),
    "preview": ("text_body",),
    "body_preview": ("text_body",),
    "body": ("text_body", "body"),
    "body_html": ("body",),
    "conversation_id": ("conversation_id",),
    "thread_id": ("conversation_id",),
}


def _db_fields_for(public_fields: Optional[List[str]]) -> tuple:
    """Map public field names to the exchangelib DB-field tuple.

    Returns an empty tuple when the public list is empty, None, or
    contains any name we don't recognise — in that case the caller
    should skip ``.only()`` to avoid stripping a field exchangelib
    would otherwise have fetched opportunistically.
    """
    if not public_fields:
        return ()
    out: list = []
    seen: set = set()
    for name in public_fields:
        mapped = _DB_FIELDS_BY_PUBLIC.get(str(name).lower())
        if not mapped:
            # Unknown public field (e.g. "folder" which is synthesized) —
            # skip. Callers get the current permissive behaviour.
            continue
        for db_name in mapped:
            if db_name not in seen:
                seen.add(db_name)
                out.append(db_name)
    # Always include id — the tool uses it for the message_id field.
    if "id" not in seen:
        out.insert(0, "id")
    return tuple(out)


# Exchangelib error classifications. We prefer exchangelib.errors.*
# when present; fall back to a str(exception) heuristic to stay robust
# if the library renames a class.
_TRANSIENT_EWS_ERROR_NAMES = frozenset({
    "ErrorServerBusy", "ErrorTimeoutExpired", "ErrorConnectionFailed",
    "ErrorInternalServerError", "ErrorInternalServerTransientError",
    "ErrorMailboxStoreUnavailable", "ErrorMessageSizeExceeded",
    "TransportError", "RateLimitError",
})
_AUTH_EWS_ERROR_NAMES = frozenset({
    "ErrorAccessDenied", "ErrorAccessDeniedForSendingEmail",
    "UnauthorizedError", "Unauthorized",
})
_CONNECTION_EWS_ERROR_NAMES = frozenset({
    "ConnectionError", "RemoteDisconnected", "SSLError",
    "MaxRetryError", "ChunkedEncodingError",
})


def _classify_ews_error(exc: BaseException) -> str:
    """Return a short tag describing an exception: ``TIMEOUT``,
    ``THROTTLED``, ``AUTH_EXPIRED``, ``CONNECTION``, or ``UNKNOWN``.

    Used to build error_code fields in tool responses and to decide
    whether a mid-iteration exception should abort the query with an
    explicit error vs. log and continue.
    """
    name = type(exc).__name__
    msg = str(exc)
    if name == "ErrorServerBusy" or "throttl" in msg.lower() or "ServerBusy" in msg:
        return "THROTTLED"
    if name in _TRANSIENT_EWS_ERROR_NAMES or "timed out" in msg.lower():
        return "TIMEOUT"
    if name in _AUTH_EWS_ERROR_NAMES or "401" in msg or "Unauthorized" in msg:
        return "AUTH_EXPIRED"
    if name in _CONNECTION_EWS_ERROR_NAMES or "Connection aborted" in msg:
        return "CONNECTION"
    return "UNKNOWN"


@dataclass
class FolderQueryOutcome:
    """Result of ``_paginate_query`` for a single folder.

    ``items`` is what we actually collected (possibly partial if an
    error fired mid-iteration). ``error_code`` / ``error_message`` are
    populated on failure and passed into the response so the caller can
    tell a full-but-empty result from a truncated one.
    """
    items: List[Any] = field(default_factory=list)
    total_available: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


def _query_total(query: Any, logger: logging.Logger, folder_label: str) -> Optional[int]:
    """Return ``query.count()`` or None when the call fails.

    Not every exchangelib queryset supports ``.count()`` cheaply —
    treating a failure as "unknown" is strictly better than refusing
    to ship items.
    """
    count_method = getattr(query, "count", None)
    if count_method is None:
        return None
    try:
        return int(count_method())
    except Exception as exc:
        logger.debug(
            "query.count() failed for %s: %s: %s",
            folder_label, type(exc).__name__, exc,
        )
        return None


def _paginate_query(
    query: Any,
    *,
    max_results: int,
    offset: int,
    chunk_size: int,
    logger: logging.Logger,
    folder_label: str,
) -> FolderQueryOutcome:
    """Materialise the query in explicit chunks, capturing partial-failure.

    * Walks ``query[o:o+chunk_size]`` slices in a for-loop, building up
      to ``max_results`` items.
    * Unwraps any mid-iteration exception into a classified error_code
      on the outcome — prior code swallowed these and returned partial
      results as "success".
    * Also calls ``_query_total`` once up front so the response can
      advertise ``total_available``.
    """
    outcome = FolderQueryOutcome()
    outcome.total_available = _query_total(query, logger, folder_label)
    target_offset = max(0, int(offset))
    remaining = max(0, int(max_results))
    cursor = target_offset
    chunk_size = max(1, min(chunk_size, 250))
    while remaining > 0:
        want = min(chunk_size, remaining)
        try:
            batch = list(query[cursor:cursor + want])
        except Exception as exc:
            code = _classify_ews_error(exc)
            # Log full type at WARNING so ops can catch the real cause
            # (the Issue 2 diagnostic the bug report wanted to run live).
            logger.warning(
                "paginate %s offset=%d chunk=%d raised %s: %s",
                folder_label, cursor, want, type(exc).__name__, exc,
            )
            outcome.error_code = code
            outcome.error_message = f"{type(exc).__name__}: {exc}"
            return outcome
        if not batch:
            break
        outcome.items.extend(batch)
        cursor += len(batch)
        remaining -= len(batch)
        # Short batch => queryset exhausted (fewer items than requested).
        if len(batch) < want:
            break
    return outcome


def _build_list_item(
    email: Any,
    *,
    fields: List[str],
    folder_name: str,
) -> Dict[str, Any]:
    """Build the canonical list-endpoint item dict for one Message.

    Pulled out of the three search paths so they share one shape.
    """
    sender = safe_get(email, "sender", None)
    from_email = ""
    if sender and hasattr(sender, "email_address"):
        from_email = sender.email_address or ""
    text_body = safe_get(email, "text_body", "") or ""
    received = safe_get(email, "datetime_received", None)
    received_iso = received.isoformat() if received and hasattr(received, "isoformat") else None
    item = {
        "message_id": ews_id_to_str(safe_get(email, "id", None)) or "",
        "subject": safe_get(email, "subject", "") or "",
        "from": from_email,
        "to": [
            r.email_address for r in (safe_get(email, "to_recipients", []) or [])
            if hasattr(r, "email_address")
        ],
        "received_time": received_iso,
        "is_read": safe_get(email, "is_read", False),
        "has_attachments": safe_get(email, "has_attachments", False),
        "importance": safe_get(email, "importance", "Normal"),
        "categories": safe_get(email, "categories", []) or [],
        "snippet": truncate_text(text_body, 200),
        "folder": folder_name,
    }
    if "body" in fields:
        item["body"] = text_body
    strip_body_by_default(item, keep_body="body" in fields)
    return project_fields(item, fields)


def extract_body_html(message) -> str:
    """
    Properly extract HTML body content from an Exchange message.

    In exchangelib, message.body is an HTMLBody object, not a string.
    The actual HTML content is inside message.body.body.

    Args:
        message: Exchange Message object

    Returns:
        HTML body content as string, or empty string if no body
    """
    body = safe_get(message, "body", None)
    if not body:
        return ""

    # HTMLBody object has inner 'body' property with actual content
    if hasattr(body, 'body') and body.body:
        html = body.body
    elif isinstance(body, str):
        html = body
    else:
        html = str(body) if body else ""

    # Strip CDATA wrappers that may appear in Exchange HTML bodies
    # These cause visible "]]>" text at the bottom of forwarded/replied emails
    if html and '<![CDATA[' in html:
        html = html.replace('<![CDATA[', '').replace(']]>', '')

    # Strip document-level HTML tags to prevent nested <html><body> issues
    # when embedding in quoted content. Nested document tags break HTML structure.
    return strip_html_document_tags(html)


def strip_html_document_tags(html: str) -> str:
    """
    Strip document-level HTML tags from content while preserving styles.

    When forwarding/replying, the original email body may contain full HTML
    document structure (<html>, <head>, <body>). We need to:
    - Remove structural tags (<html>, <body>) to prevent nesting issues
    - Preserve <style> blocks and MSO conditional comments for proper rendering
    - Keep @font-face declarations for Arabic fonts (Sakkal Majalla) and RTL layout

    Args:
        html: HTML content that may contain document-level tags

    Returns:
        Inner content with document structure stripped but styles preserved
    """
    if not html:
        return html

    # Extract <style> blocks to preserve them
    style_blocks = re.findall(r'<style[^>]*>.*?</style>', html, flags=re.IGNORECASE | re.DOTALL)

    # Extract MSO conditional comments (<!--[if gte mso 9]>...<![endif]-->)
    mso_comments = re.findall(r'<!--\[if[^\]]*\]>.*?<!\[endif\]-->', html, flags=re.IGNORECASE | re.DOTALL)

    # Remove DOCTYPE declaration
    html = re.sub(r'<!DOCTYPE[^>]*>', '', html, flags=re.IGNORECASE)

    # Remove <html> open/close tags (with any attributes)
    html = re.sub(r'</?html[^>]*>', '', html, flags=re.IGNORECASE)

    # Remove <head> tags but extract content we want to preserve
    html = re.sub(r'<head[^>]*>.*?</head>', '', html, flags=re.IGNORECASE | re.DOTALL)

    # Remove <body> open/close tags but keep the content inside
    html = re.sub(r'</?body[^>]*>', '', html, flags=re.IGNORECASE)

    # Prepend preserved styles and MSO comments
    preserved_content = '\n'.join(style_blocks + mso_comments)
    if preserved_content:
        html = preserved_content + '\n' + html

    return html.strip()


_FORWARD_PREFIX_RE = re.compile(
    r"^(?:fw|fwd|forward)\s*:\s*", re.IGNORECASE
)
_REPLY_PREFIX_RE = re.compile(r"^(?:re|rply|reply)\s*:\s*", re.IGNORECASE)


def has_forward_prefix(subject: str) -> bool:
    """Check if subject already has a forward prefix (FW:, Fwd:, FWD:, Forward:)."""
    if not subject:
        return False
    return bool(_FORWARD_PREFIX_RE.match(subject))


def has_reply_prefix(subject: str) -> bool:
    """Check if subject already has a reply prefix (RE:, Re:, Reply:)."""
    if not subject:
        return False
    return bool(_REPLY_PREFIX_RE.match(subject))


def add_forward_prefix(subject: str) -> str:
    """Return ``subject`` prefixed with the canonical ``FW:``.

    Normalises any existing variant — ``Fwd:``, ``FWD:``, ``Forward:``,
    mixed case, stray whitespace — to the single ``FW: <body>`` form
    and never stacks ("FW: FW: hello" cannot happen). The C10 fix
    stripped only ``FW:``; Bug 5 in the follow-up widens the strip to
    cover Fwd:/FWD:/Forward: too.
    """
    if not subject:
        return "FW:"
    # Strip leading whitespace before prefix detection so "  Fwd: x"
    # still normalises correctly.
    stripped_input = subject.lstrip()
    stripped = _FORWARD_PREFIX_RE.sub("", stripped_input, count=1).strip()
    if not stripped:
        return "FW:"
    return f"FW: {stripped}"


def add_reply_prefix(subject: str) -> str:
    """Return ``subject`` prefixed with the canonical ``RE:``.

    Same normalisation approach as :func:`add_forward_prefix`: any
    variant (Re:, Reply:) is stripped and replaced with ``RE:``.
    """
    if not subject:
        return "RE:"
    stripped_input = subject.lstrip()
    stripped = _REPLY_PREFIX_RE.sub("", stripped_input, count=1).strip()
    if not stripped:
        return "RE:"
    return f"RE: {stripped}"


def clean_original_body_for_signature(original_body_html: str) -> str:
    """
    Remove or rename WordSection1 from original content to prevent
    Exclaimer/server-side signature systems from placing signature after it.

    Exclaimer looks for the LAST </div> that closes a WordSection1 and
    inserts signature after it. If the original email has WordSection1,
    the signature ends up at the very end instead of after user's message.

    Args:
        original_body_html: The HTML body of the original email

    Returns:
        Cleaned HTML with WordSection1 renamed to OriginalSection
    """
    if not original_body_html:
        return original_body_html

    # Rename WordSection1 to OriginalSection to avoid confusing Exclaimer
    cleaned = original_body_html.replace('class="WordSection1"', 'class="OriginalSection"')
    cleaned = cleaned.replace("class='WordSection1'", "class='OriginalSection'")
    cleaned = cleaned.replace('class=WordSection1', 'class=OriginalSection')

    # Also handle variations with spaces
    cleaned = cleaned.replace('class = "WordSection1"', 'class="OriginalSection"')
    cleaned = cleaned.replace("class = 'WordSection1'", "class='OriginalSection'")

    return cleaned


def format_forward_header(message) -> dict:
    """
    Format the forwarded message header like Outlook.

    - From: Name <email> format
    - To/Cc: Name <email> format
    - Sent: Full date format with day name

    Args:
        message: Exchange Message object

    Returns:
        Dictionary with formatted header fields
    """
    # From: Name <email> format
    # Try multiple sources for sender info (some may be None for distribution lists, etc.)
    sender_name = ""
    sender_email = ""

    # Try sender first (most common)
    sender = safe_get(message, "sender", None)
    if sender:
        sender_name = (sender.name or "") if hasattr(sender, "name") else ""
        sender_email = (sender.email_address or "") if hasattr(sender, "email_address") else ""

    # Fallback 1: Try author if sender.email_address is empty
    if not sender_email:
        author = safe_get(message, "author", None)
        if author:
            if not sender_name:
                sender_name = (author.name or "") if hasattr(author, "name") else ""
            sender_email = (author.email_address or "") if hasattr(author, "email_address") else ""

    # Fallback 2: Try from_ if still empty
    if not sender_email:
        from_field = safe_get(message, "from_", None)
        if from_field:
            if not sender_name:
                sender_name = (from_field.name or "") if hasattr(from_field, "name") else ""
            sender_email = (from_field.email_address or "") if hasattr(from_field, "email_address") else ""

    # Fallback 3: Extract from internet_message_headers as last resort
    if not sender_email:
        headers = safe_get(message, "headers", None) or safe_get(message, "internet_message_headers", None)
        if headers:
            for h in headers:
                header_name = getattr(h, 'name', '') or ''
                if header_name.lower() == 'from':
                    header_value = getattr(h, 'value', '') or ''
                    # Parse "Name <email>" format
                    match = re.search(r'<([^>]+)>', header_value)
                    if match:
                        sender_email = match.group(1)
                        # Also extract name if we don't have it
                        if not sender_name:
                            name_part = header_value[:header_value.find('<')].strip()
                            if name_part:
                                sender_name = name_part.strip('"\'')
                    elif '@' in header_value:
                        sender_email = header_value.strip()
                    break

    # Format as "Name <email>" or just what's available
    # Use HTML entities for angle brackets to prevent browser from hiding email
    if sender_name and sender_email:
        from_str = f"{sender_name} &lt;{sender_email}&gt;"
    elif sender_email:
        from_str = sender_email
    elif sender_name:
        from_str = sender_name
    else:
        from_str = ""

    # To/Cc: Name <email> format
    def format_recipients(recipients):
        if not recipients:
            return ""
        parts = []
        for r in recipients:
            if not r:
                continue
            # Use 'or ""' to convert None to empty string
            name = (r.name or "") if hasattr(r, "name") else ""
            email = (r.email_address or "") if hasattr(r, "email_address") else ""
            if name and email:
                # HTML-escape angle brackets to prevent browser from hiding email
                parts.append(f"{name} &lt;{email}&gt;")
            elif email:
                parts.append(email)
            elif name:
                parts.append(name)
        return '; '.join(parts)

    to_recipients = safe_get(message, "to_recipients", []) or []
    cc_recipients = safe_get(message, "cc_recipients", []) or []

    to_str = format_recipients(to_recipients)
    cc_str = format_recipients(cc_recipients)

    # Date: Full format with day name
    sent_date = safe_get(message, "datetime_sent", None)
    date_str = ""
    if sent_date:
        date_str = sent_date.strftime('%A, %B %d, %Y %I:%M:%S %p')

    return {
        'from': from_str,
        'sent': date_str,
        'to': to_str,
        'cc': cc_str,
        'subject': safe_get(message, "subject", "") or ""
    }


def copy_attachments_to_message(original_message, new_message) -> tuple:
    """
    Copy all attachments from original message to new message,
    preserving inline image properties (content_id, is_inline).

    This is critical for email signatures with embedded images to display correctly.

    Args:
        original_message: Source message with attachments
        new_message: Target message to attach files to

    Returns:
        Tuple of (inline_count, regular_count)
    """
    # Check if target message supports attach method
    # ReplyToItem/ReplyAllToItem objects from create_reply() don't support attach
    if not hasattr(new_message, 'attach') or not callable(getattr(new_message, 'attach', None)):
        return 0, 0

    attachments = safe_get(original_message, "attachments", []) or []
    if not attachments:
        return 0, 0

    inline_count = 0
    regular_count = 0

    for att in attachments:
        if not att or not isinstance(att, FileAttachment):
            continue

        try:
            # Create new attachment preserving ALL properties
            # CRITICAL: content_id is needed for cid: references in HTML
            # CRITICAL: is_inline marks the attachment as embedded
            new_att = FileAttachment(
                name=att.name,
                content=att.content,
                content_type=getattr(att, 'content_type', None),
                content_id=getattr(att, 'content_id', None),  # Preserve for cid: refs
                is_inline=getattr(att, 'is_inline', False)     # Preserve inline flag
            )
            new_message.attach(new_att)

            if getattr(att, 'is_inline', False):
                inline_count += 1
            else:
                regular_count += 1
        except (AttributeError, TypeError):
            # Target message doesn't support attach - skip
            return inline_count, regular_count

    return inline_count, regular_count


def is_exchange_folder_id(identifier: str) -> bool:
    """
    Check if the identifier looks like an Exchange folder/item ID.

    Exchange IDs are base64-encoded strings that typically start with 'AAMk'.
    Base64 can contain '/' characters, so we need to detect IDs before
    attempting to parse as folder paths.

    Args:
        identifier: The folder identifier string

    Returns:
        True if it looks like an Exchange ID, False otherwise
    """
    # Exchange folder/item IDs start with 'AAMk' (base64 encoded)
    # They are typically 100+ characters long
    if identifier.startswith('AAMk') and len(identifier) > 50:
        return True
    # Also check for other common Exchange ID patterns
    if identifier.startswith('AAE') and len(identifier) > 50:
        return True
    return False


async def resolve_folder_for_account(account, folder_identifier: str):
    """
    Resolve folder from name, path, or ID for a specific account.

    Supports:
    - Standard names: inbox, sent, drafts, deleted, junk
    - Folder paths: Inbox/CC, Inbox/Projects/2024
    - Folder IDs: AAMkADc3MWUy... (base64 encoded, may contain '/' characters)
    - Custom folder names: CC, Archive, Projects

    Args:
        account: Exchange Account object (primary or impersonated)
        folder_identifier: Folder name, path, or ID
    """
    folder_identifier = folder_identifier.strip()

    folder_map = get_standard_folder_map(account)
    folder_map["trash"] = account.trash

    def traverse_folder_path(start_folder, path_parts):
        """Walk a folder path from an explicit starting folder."""
        current_folder = start_folder
        for subfolder_name in path_parts:
            found = None
            try:
                for child in list(getattr(current_folder, "children", []) or []):
                    if safe_get(child, "name", "").lower() == subfolder_name.lower():
                        found = child
                        break
            except Exception as e:
                current_name = safe_get(current_folder, "name", "root")
                raise ToolExecutionError(
                    f"Error accessing subfolders of '{current_name}': {e}"
                ) from e

            if not found:
                current_name = safe_get(current_folder, "name", "root")
                raise ToolExecutionError(
                    f"Subfolder '{subfolder_name}' not found under '{current_name}'"
                )
            current_folder = found
        return current_folder

    # Try 1: Standard folder name (case-insensitive)
    folder_lower = folder_identifier.lower()
    if folder_lower in folder_map:
        return folder_map[folder_lower]

    # Try 2: Folder ID (starts with AAMk or similar Exchange ID pattern)
    # IMPORTANT: Check this BEFORE path parsing, as base64 IDs can contain '/'
    if is_exchange_folder_id(folder_identifier):
        found_folder = find_folder_by_id(account.root, folder_identifier)
        if found_folder:
            return found_folder
        # If not found as folder ID, don't fall through to path parsing
        raise ToolExecutionError(
            f"Folder ID '{folder_identifier[:20]}...' not found. "
            f"The ID appears to be an Exchange folder ID but could not be located in your mailbox."
        )

    # Try 3: Folder path (e.g., "Inbox/CC", "/Archive/2024", "Archive/2024")
    if '/' in folder_identifier:
        parts = [part.strip() for part in folder_identifier.split('/') if part.strip()]
        if not parts:
            raise ToolExecutionError("Folder path is empty")

        if folder_identifier.startswith('/'):
            return traverse_folder_path(account.root, parts)

        parent_name = parts[0].lower()
        if parent_name in folder_map:
            return traverse_folder_path(folder_map[parent_name], parts[1:])

        path_errors = []
        for start_folder in (account.root, account.inbox):
            try:
                return traverse_folder_path(start_folder, parts)
            except ToolExecutionError as e:
                path_errors.append(str(e))

        raise ToolExecutionError(
            f"Folder path '{folder_identifier}' not found from mailbox root or inbox. "
            f"Resolution attempts: {' | '.join(path_errors)}"
        )

    # Try 4: Search for custom folder by name (recursively under inbox)
    def search_folder_tree(parent, target_name, max_depth=3, current_depth=0):
        """Recursively search for folder by name."""
        if current_depth >= max_depth:
            return None

        try:
            for child in parent.children:
                child_name = safe_get(child, 'name', '')
                if child_name.lower() == target_name.lower():
                    return child
                # Recurse into subfolders
                found = search_folder_tree(child, target_name, max_depth, current_depth + 1)
                if found:
                    return found
        except Exception:
            pass

        return None

    # Search under root first so top-level custom folders win over inbox children.
    custom_folder = search_folder_tree(account.root, folder_identifier)
    if custom_folder:
        return custom_folder

    # Search under inbox as fallback for mailbox layouts that hide folder tree roots.
    custom_folder = search_folder_tree(account.inbox, folder_identifier)
    if custom_folder:
        return custom_folder

    # If all methods fail, provide helpful error
    raise ToolExecutionError(
        f"Folder '{folder_identifier}' not found. "
        f"Available standard folders: {', '.join(folder_map.keys())}. "
        f"For custom folders, use full path (e.g., 'Inbox/CC') or get folder ID from list_folders."
    )


async def resolve_folder(ews_client, folder_identifier: str):
    """
    Backward-compatible wrapper for resolve_folder_for_account.

    Deprecated: Use resolve_folder_for_account with explicit account parameter.
    """
    return await resolve_folder_for_account(ews_client.account, folder_identifier)


class SendEmailTool(BaseTool):
    """Tool for sending emails."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "send_email",
            "description": "Send an email with optional attachments and CC/BCC.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Recipient email addresses"
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject"
                    },
                    "body": {
                        "type": "string",
                        "description": "Email body (HTML supported)"
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "CC recipients (optional)"
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "BCC recipients (optional)"
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["Low", "Normal", "High"],
                        "description": "Email importance level (optional)"
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Attachment file paths (optional)"
                    },
                    **INLINE_ATTACHMENTS_SCHEMA,
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to send on behalf of (requires impersonation/delegate access)"
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When true, validate inputs and build the Message object but DO NOT send. "
                            "Returns the computed subject/recipients/body preview. Useful for AI agents "
                            "that want to 'what would this send' before committing."
                        )
                    }
                },
                "required": ["to", "subject", "body"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Send email via EWS."""
        # Get target mailbox for impersonation
        target_mailbox = kwargs.pop("target_mailbox", None)
        dry_run = bool(kwargs.pop("dry_run", False))

        # Validate input
        request = self.validate_input(SendEmailRequest, **kwargs)

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Clean and prepare email body
            email_body = request.body.strip()

            # Strip CDATA wrapper if present (CDATA is XML syntax, not needed for Exchange)
            if email_body.startswith('<![CDATA[') and email_body.endswith(']]>'):
                email_body = email_body[9:-3].strip()  # Remove <![CDATA[ and ]]>
                self.logger.info("Stripped CDATA wrapper from email body")

            # Validate body is not empty after processing
            if not email_body:
                raise ToolExecutionError("Email body is empty after processing")

            # Detect if body is HTML or plain text
            is_html = bool(re.search(r'<[^>]+>', email_body))  # Check for HTML tags

            # Log body details for debugging
            body_type = "HTML" if is_html else "Plain Text"
            self.logger.info(f"Email body: {body_type}, {len(email_body)} characters, "
                           f"{len(email_body.encode('utf-8'))} bytes (UTF-8)")

            # Create message with appropriate body type
            # CRITICAL: Use HTMLBody for HTML, Body for plain text
            # Using wrong type causes Exchange to strip content!
            if is_html:
                message = Message(
                    account=account,
                    subject=request.subject,
                    body=HTMLBody(email_body),
                    to_recipients=[Mailbox(email_address=email) for email in request.to]
                )
                self.logger.info("Using HTMLBody for HTML content")
            else:
                message = Message(
                    account=account,
                    subject=request.subject,
                    body=Body(email_body),
                    to_recipients=[Mailbox(email_address=email) for email in request.to]
                )
                self.logger.info("Using Body (plain text) for non-HTML content")

            # Add CC recipients
            if request.cc:
                message.cc_recipients = [Mailbox(email_address=email) for email in request.cc]

            # Add BCC recipients
            if request.bcc:
                message.bcc_recipients = [Mailbox(email_address=email) for email in request.bcc]

            # Set importance
            message.importance = request.importance.value

            # CRITICAL: Verify body was set correctly BEFORE attaching/sending
            if not message.body or len(str(message.body).strip()) == 0:
                raise ToolExecutionError(
                    f"Message body is empty after creation! Original body length: {len(email_body)}, "
                    f"Message body: {message.body}"
                )
            self.logger.info(f"Verified message body set correctly: {len(str(message.body))} characters")

            # Add attachments if provided
            attachment_count = 0
            if request.attachments:
                for file_path in request.attachments:
                    try:
                        # Use os.path.basename to handle both Windows and Unix paths
                        file_name = os.path.basename(file_path)
                        with open(file_path, 'rb') as f:
                            content = f.read()
                            attachment = FileAttachment(
                                name=file_name,
                                content=content
                            )
                            message.attach(attachment)
                            attachment_count += 1
                            self.logger.info(f"Attached file: {file_name} ({len(content)} bytes)")
                    except FileNotFoundError:
                        raise ToolExecutionError(f"Attachment file not found: {file_path}")
                    except PermissionError:
                        raise ToolExecutionError(f"Permission denied reading attachment: {file_path}")
                    except Exception as e:
                        raise ToolExecutionError(f"Failed to attach file {file_path}: {e}")

                self.logger.info(f"Total attachments added: {attachment_count}")

            # Add inline (base64) attachments if provided
            inline_count = attach_inline_files(message, kwargs.get("inline_attachments", []))
            if inline_count > 0:
                attachment_count += inline_count
                self.logger.info(f"Added {inline_count} inline (base64) attachment(s)")

            # Dry-run short-circuit: return a preview of what WOULD be sent
            # without actually calling message.send(). Nothing is persisted
            # to Exchange, no Drafts entry is created.
            if dry_run:
                self.logger.info(
                    f"DRY RUN: would send to {', '.join(request.to)} "
                    f"with {attachment_count} attachment(s)"
                )
                body_preview = email_body[:280] + ("..." if len(email_body) > 280 else "")
                return format_success_response(
                    "Dry run — no email sent",
                    dry_run=True,
                    sent=False,
                    would_send_to=request.to,
                    would_cc=request.cc or [],
                    would_bcc=request.bcc or [],
                    subject=request.subject,
                    body_preview=body_preview,
                    body_type=body_type,
                    attachments_count=attachment_count,
                    importance=request.importance.value,
                    mailbox=mailbox,
                )

            # Send the message (attachments are included automatically)
            message.send()
            self.logger.info(f"Message sent to {', '.join(request.to)} with {attachment_count} attachment(s)")

            # FINAL VERIFICATION: Check message body after send
            if hasattr(message, 'body') and message.body and len(str(message.body).strip()) > 0:
                body_length = len(str(message.body))
                self.logger.info(f"✅ SUCCESS: Email sent with body content ({body_length} characters)")
            else:
                # This should not happen, but if it does, it's critical to know
                raise ToolExecutionError(
                    "CRITICAL: Message body is empty after send! "
                    "Email may have been sent without content. "
                    f"Original body length: {len(email_body)}, "
                    f"Body type: {body_type}"
                )

            return format_success_response(
                "Email sent successfully",
                message_id=ews_id_to_str(message.id) if hasattr(message, 'id') else None,
                sent_time=datetime.now().isoformat(),
                recipients=request.to,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to send email: {e}")
            raise ToolExecutionError(f"Failed to send email: {e}")


class ReadEmailsTool(BaseTool):
    """Tool for reading emails from inbox."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "read_emails",
            "description": "Read emails from a folder (default: inbox).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Folder name (standard names: inbox, sent, drafts; paths: Inbox/CC; or folder ID)",
                        "default": "inbox"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of emails to retrieve",
                        "default": 50,
                        "maximum": 1000
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": "Only return unread emails",
                        "default": False
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to read from (requires impersonation/delegate access)"
                    }
                }
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Read emails from folder."""
        folder_name = kwargs.get("folder", "inbox")
        max_results = kwargs.get("max_results", 50)
        unread_only = kwargs.get("unread_only", False)
        target_mailbox = kwargs.get("target_mailbox")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get folder - supports standard names, paths, and folder IDs
            folder = await resolve_folder_for_account(account, folder_name)
            self.logger.info(f"Resolved folder '{folder_name}' to: {safe_get(folder, 'name', folder_name)} in mailbox: {mailbox}")

            # Build query
            items = folder.all().order_by('-datetime_received')

            if unread_only:
                items = items.filter(is_read=False)

            # Fetch emails
            emails = []
            for item in items[:max_results]:
                # Get sender email safely
                sender = safe_get(item, "sender", None)
                from_email = ""
                if sender and hasattr(sender, "email_address"):
                    from_email = sender.email_address or ""

                # Get text body safely
                text_body = safe_get(item, "text_body", "") or ""

                email_data = {
                    "message_id": ews_id_to_str(safe_get(item, "id", None)) or "unknown",
                    "subject": safe_get(item, "subject", "") or "",
                    "from": from_email,
                    "to": [r.email_address for r in (safe_get(item, "to_recipients", []) or []) if r and hasattr(r, "email_address") and r.email_address],
                    "cc": [r.email_address for r in (safe_get(item, "cc_recipients", []) or []) if r and hasattr(r, "email_address") and r.email_address],
                    "bcc": [r.email_address for r in (safe_get(item, "bcc_recipients", []) or []) if r and hasattr(r, "email_address") and r.email_address],
                    "received_time": safe_get(item, "datetime_received", datetime.now()).isoformat(),
                    "is_read": safe_get(item, "is_read", False),
                    "has_attachments": safe_get(item, "has_attachments", False),
                    "preview": truncate_text(text_body, 200)
                }
                emails.append(email_data)

            self.logger.info(f"Retrieved {len(emails)} emails from {folder_name} in mailbox: {mailbox}")

            return format_success_response(
                f"Retrieved {len(emails)} emails",
                emails=emails,
                total_count=len(emails),
                folder=folder_name,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to read emails: {e}")
            raise ToolExecutionError(f"Failed to read emails: {e}")


class SearchEmailsTool(BaseTool):
    """Unified email search tool with quick/advanced/full_text modes."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "search_emails",
            "description": "Search emails with quick, advanced, or full-text modes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["quick", "advanced", "full_text"],
                        "description": "Search mode: quick (filter by subject/sender/date), advanced (multi-folder with sort/categories/importance), full_text (search across subject/body/attachments)",
                        "default": "quick"
                    },
                    "folder": {
                        "type": "string",
                        "description": "Folder to search (quick mode). Standard names: inbox, sent, drafts; paths: Inbox/CC; or folder ID",
                        "default": "inbox"
                    },
                    "subject_contains": {
                        "type": "string",
                        "description": "Filter by subject containing text"
                    },
                    "from_address": {
                        "type": "string",
                        "description": "Filter by sender email address"
                    },
                    "sender": {
                        "type": "string",
                        "description": "Alias of from_address"
                    },
                    "to_address": {
                        "type": "string",
                        "description": "Filter by recipient email (quick + advanced mode)"
                    },
                    "recipient": {
                        "type": "string",
                        "description": "Alias of to_address"
                    },
                    "query": {
                        "type": "string",
                        "description": "Quick mode: subject-OR-body substring. Full-text mode: search text (also accepted as search_query)."
                    },
                    "search_query": {
                        "type": "string",
                        "description": "Legacy alias for 'query' in full_text mode."
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Result projection. Returned items include only the "
                            "named fields. Default: message_id, subject, from, "
                            "received_time, is_read, has_attachments, snippet. "
                            "Include 'body' / 'body_html' to opt into heavy fields."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": (
                            "Skip this many matches before returning results. "
                            "Pair with the response's ``next_offset`` for paging."
                        ),
                    },
                    "has_attachments": {
                        "type": "boolean",
                        "description": "Filter by attachment presence"
                    },
                    "is_read": {
                        "type": "boolean",
                        "description": "Filter by read status"
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date (ISO 8601 format)"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date (ISO 8601 format)"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results",
                        "default": 50,
                        "maximum": 1000
                    },
                    "keywords": {
                        "type": "string",
                        "description": "Keywords to search in subject and body (advanced mode)"
                    },
                    "body_contains": {
                        "type": "string",
                        "description": "Filter by body containing text (advanced mode)"
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["Low", "Normal", "High"],
                        "description": "Filter by importance level (advanced mode)"
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by email categories (advanced mode)"
                    },
                    "search_scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Folders to search (advanced/full_text mode, e.g. ['inbox', 'sent'])",
                        "default": ["inbox"]
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["datetime_received", "datetime_sent", "from", "subject", "importance"],
                        "description": "Sort field (advanced mode)",
                        "default": "datetime_received"
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["ascending", "descending"],
                        "description": "Sort order (advanced mode)",
                        "default": "descending"
                    },
                    "search_in": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["subject", "body", "attachments"]},
                        "description": "Where to search (full_text mode)",
                        "default": ["subject", "body"]
                    },
                    "exact_phrase": {
                        "type": "boolean",
                        "description": "Search for exact phrase match (full_text mode)",
                        "default": False
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to search in (requires impersonation/delegate access)"
                    }
                }
            }
        }

    # Parameter vocabulary accepted across all modes (schema-level).
    # See _validate_kwargs for the strict check. Quick and advanced modes
    # share most filters; full_text has its own query/search_in/exact_phrase.
    _ALLOWED_PARAMS: set = {
        # Routing
        "mode", "folder", "target_mailbox", "max_results", "offset",
        # Quick + advanced shared
        "subject_contains", "from_address", "to_address", "sender",
        "recipient", "body_contains", "query",
        "has_attachments", "is_read", "importance",
        "categories", "keywords",
        "start_date", "end_date",
        # Advanced-only
        "search_scope", "sort_by", "sort_order",
        # Full-text-only
        "search_query", "search_in", "exact_phrase",
        # Projection
        "fields",
    }

    # Anything in this set gates off the "auto-add last 30 days" default
    # when neither start_date nor end_date was supplied (see _search_quick).
    _QUICK_FILTER_KEYS: tuple = (
        "subject_contains", "from_address", "sender", "to_address", "recipient",
        "body_contains", "query", "has_attachments", "is_read", "importance",
    )

    def _validate_kwargs(self, kwargs: Dict[str, Any]) -> None:
        """Reject unknown params with a 400 (ValidationError) + suggestion.

        Previously unknown params were silently ignored, which let the
        ``query`` / ``sender`` filters disappear into the default "last 30
        days" fallback without any error signal for the caller.
        """
        from difflib import get_close_matches

        unknown = [k for k in kwargs.keys() if k not in self._ALLOWED_PARAMS]
        if not unknown:
            return
        first = unknown[0]
        suggestions = get_close_matches(first, sorted(self._ALLOWED_PARAMS), n=1)
        hint = f"; did you mean {suggestions[0]!r}?" if suggestions else ""
        raise ValidationError(
            f"unknown param {first!r}{hint}. "
            f"Accepted params: {', '.join(sorted(self._ALLOWED_PARAMS))}."
        )

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Route to appropriate search mode."""
        self._validate_kwargs(kwargs)
        mode = kwargs.get("mode", "quick")

        if mode == "advanced":
            return await self._search_advanced(**kwargs)
        elif mode == "full_text":
            return await self._search_full_text(**kwargs)
        else:
            return await self._search_quick(**kwargs)

    async def _search_quick(self, **kwargs) -> Dict[str, Any]:
        """Quick search: filter by subject, sender, date, read status, attachments.

        Accepts a union of filter keys. ``query`` matches subject OR body.
        ``sender`` is a synonym for ``from_address`` (and ``recipient`` for
        ``to_address``) — the schema previously advertised both spellings
        but only the ``*_address`` forms were wired up.
        """
        from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
        from exchangelib.errors import ErrorTimeoutExpired
        from exchangelib import Q
        import socket

        target_mailbox = kwargs.get("target_mailbox")

        # Normalise aliases so the rest of the function only has to check
        # one canonical key per concept.
        from_address = kwargs.get("from_address") or kwargs.get("sender")
        to_address = kwargs.get("to_address") or kwargs.get("recipient")
        subject_contains = kwargs.get("subject_contains")
        body_contains = kwargs.get("body_contains")
        free_text = kwargs.get("query")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Auto-add date range ONLY when *no* filter was supplied. The
            # Bug 1 regression was that this check only considered four
            # filter keys; query/sender/body_contains/etc. used to fall
            # through the default and get replaced by "last 30 days",
            # silently discarding the caller's intent.
            if not kwargs.get("start_date") and not kwargs.get("end_date"):
                has_filters = any([
                    subject_contains, from_address, to_address,
                    body_contains, free_text,
                    kwargs.get("has_attachments") is not None,
                    kwargs.get("is_read") is not None,
                    kwargs.get("importance"),
                ])

                if not has_filters:
                    from datetime import timedelta
                    default_days_back = 30
                    auto_start_date = datetime.now() - timedelta(days=default_days_back)
                    kwargs["start_date"] = auto_start_date.isoformat()
                    self.logger.info(
                        "search_emails quick: no filters and no date range; "
                        f"auto-limiting to last {default_days_back} days"
                    )

            folder_name = kwargs.get("folder", "inbox")
            folder = await resolve_folder_for_account(account, folder_name)

            query = folder.all()

            if subject_contains:
                query = query.filter(subject__contains=subject_contains)
            if body_contains:
                query = query.filter(body__contains=body_contains)
            # `query` is a free-text parameter: subject OR body substring.
            if free_text:
                query = query.filter(
                    Q(subject__contains=free_text) | Q(body__contains=free_text)
                )
            if from_address:
                query = query.filter(sender=from_address)
            if to_address:
                query = query.filter(to_recipients__contains=to_address)
            if kwargs.get("has_attachments") is not None:
                query = query.filter(has_attachments=kwargs["has_attachments"])
            if kwargs.get("is_read") is not None:
                query = query.filter(is_read=kwargs["is_read"])
            if kwargs.get("importance"):
                query = query.filter(importance=kwargs["importance"])
            if kwargs.get("start_date"):
                start = parse_datetime_tz_aware(kwargs["start_date"])
                query = query.filter(datetime_received__gte=start)
            if kwargs.get("end_date"):
                end = parse_datetime_tz_aware(kwargs["end_date"])
                query = query.filter(datetime_received__lte=end)

            query = query.order_by('-datetime_received')
            max_results = kwargs.get("max_results", 50)
            offset = max(0, int(kwargs.get("offset", 0)))

            # Field projection: if the caller supplied ``fields=[...]``,
            # the response items will be restricted to that set. When
            # omitted, we use the list-default (no raw body; snippet only).
            fields = kwargs.get("fields") or list(LIST_DEFAULT_FIELDS)
            only_fields = _db_fields_for(fields)
            if only_fields:
                try:
                    query = query.only(*only_fields)
                except Exception as only_exc:
                    self.logger.debug(
                        "query.only(%s) rejected: %s", only_fields, only_exc,
                    )

            folder_label = safe_get(folder, "name", "inbox")
            start_time = datetime.now()
            outcome = _paginate_query(
                query,
                max_results=max_results,
                offset=offset,
                chunk_size=50,
                logger=self.logger,
                folder_label=folder_label,
            )

            emails: List[Dict[str, Any]] = [
                _build_list_item(e, fields=fields, folder_name=folder_label)
                for e in outcome.items
            ]
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            ews_call_log(
                self.logger, "FindItem",
                duration_ms=duration_ms,
                result_count=len(emails),
                total_available=outcome.total_available,
                page_offset=offset,
                folder=folder_label,
                outcome="ok" if outcome.error_code is None else outcome.error_code,
                extra_fields={"tool": "search_emails.quick"},
            )

            response: Dict[str, Any] = {
                "items": emails,
                "count": len(emails),
                "total_available": outcome.total_available,
                "mailbox": mailbox,
            }
            if (
                outcome.total_available is not None
                and len(emails) + offset < outcome.total_available
            ):
                response["next_offset"] = offset + len(emails)
            if outcome.error_code:
                response["meta"] = {
                    "error_code": outcome.error_code,
                    "error_message": outcome.error_message,
                }

            return format_success_response(
                f"Found {len(emails)} matching emails",
                **response,
            )

        except (ErrorTimeoutExpired, socket.timeout) as e:
            self.logger.error(f"Search timed out: {e}")
            raise ToolExecutionError(
                f"Search timed out. Try adding start_date/end_date, reducing max_results, or adding more filters."
            )
        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to search emails: {e}")
            raise ToolExecutionError(f"Failed to search emails: {e}")

    async def _search_advanced(self, **kwargs) -> Dict[str, Any]:
        """Advanced search: multi-folder with sort, categories, importance, keywords."""
        target_mailbox = kwargs.get("target_mailbox")
        search_scope = kwargs.get("search_scope", ["inbox"])
        max_results = kwargs.get("max_results", 250)
        sort_by = kwargs.get("sort_by", "datetime_received")
        sort_order = kwargs.get("sort_order", "descending")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            folder_map = {
                "inbox": account.inbox,
                "sent": account.sent,
                "drafts": account.drafts,
                "deleted": account.trash,
                "junk": account.junk
            }

            folders = []
            for folder_name in search_scope:
                folder = folder_map.get(folder_name.lower())
                if folder:
                    folders.append(folder)

            if not folders:
                raise ToolExecutionError(f"No valid folders found in search_scope: {search_scope}")

            # Build query filters
            q_filters = []

            if kwargs.get("keywords"):
                kw = kwargs["keywords"]
                q_filters.append(Q(subject__contains=kw) | Q(body__contains=kw))
            if kwargs.get("from_address"):
                q_filters.append(Q(sender=kwargs["from_address"]))
            if kwargs.get("to_address"):
                q_filters.append(Q(to_recipients__contains=kwargs["to_address"]))
            if kwargs.get("subject_contains"):
                q_filters.append(Q(subject__contains=kwargs["subject_contains"]))
            if kwargs.get("body_contains"):
                q_filters.append(Q(body__contains=kwargs["body_contains"]))
            if "has_attachments" in kwargs and kwargs["has_attachments"] is not None:
                q_filters.append(Q(has_attachments=kwargs["has_attachments"]))
            if kwargs.get("importance"):
                q_filters.append(Q(importance=kwargs["importance"]))
            if kwargs.get("categories"):
                q_filters.append(Q(categories__contains=kwargs["categories"]))
            if "is_read" in kwargs and kwargs["is_read"] is not None:
                q_filters.append(Q(is_read=kwargs["is_read"]))
            if kwargs.get("start_date"):
                start_date = parse_datetime_tz_aware(kwargs["start_date"])
                if start_date:
                    q_filters.append(Q(datetime_received__gte=start_date))
            if kwargs.get("end_date"):
                end_date = parse_datetime_tz_aware(kwargs["end_date"])
                if end_date:
                    q_filters.append(Q(datetime_received__lte=end_date))

            if not q_filters:
                raise ToolExecutionError("No valid search filters provided for advanced mode")

            combined_filter = q_filters[0]
            for q_filter in q_filters[1:]:
                combined_filter &= q_filter

            # Field projection (Bug 6). Default: no body.
            fields = kwargs.get("fields") or list(LIST_DEFAULT_FIELDS)
            offset = max(0, int(kwargs.get("offset", 0)))
            only_fields = _db_fields_for(fields)

            all_results: List[Dict[str, Any]] = []
            per_folder_errors: List[Dict[str, Any]] = []
            total_available_sum: Optional[int] = 0
            some_total_unknown = False

            per_folder_budget = (
                max_results // len(folders) if len(folders) > 1 else max_results
            )
            start_time = datetime.now()

            for folder in folders:
                folder_name = safe_get(folder, "name", "Unknown")
                try:
                    query = folder.filter(combined_filter)
                    if only_fields:
                        try:
                            query = query.only(*only_fields)
                        except Exception as only_exc:
                            # .only() can reject unknown fields on older
                            # exchangelib versions — fall back to full
                            # projection rather than fail the search.
                            self.logger.debug(
                                "query.only(%s) rejected on %s: %s",
                                only_fields, folder_name, only_exc,
                            )
                    sort_field = sort_by
                    if sort_order == "descending" and not sort_field.startswith('-'):
                        sort_field = f"-{sort_field}"
                    query = query.order_by(sort_field)
                except Exception as setup_exc:
                    # Filter/sort setup itself failed — classify and carry.
                    per_folder_errors.append({
                        "folder": folder_name,
                        "error_code": _classify_ews_error(setup_exc),
                        "error_message": f"{type(setup_exc).__name__}: {setup_exc}",
                    })
                    self.logger.warning(
                        "advanced-search setup on %s failed: %s: %s",
                        folder_name, type(setup_exc).__name__, setup_exc,
                    )
                    some_total_unknown = True
                    continue

                outcome = _paginate_query(
                    query,
                    max_results=per_folder_budget,
                    offset=offset,
                    chunk_size=50,
                    logger=self.logger,
                    folder_label=folder_name,
                )
                ews_call_log(
                    self.logger, "FindItem",
                    result_count=len(outcome.items),
                    total_available=outcome.total_available,
                    page_offset=offset,
                    folder=folder_name,
                    outcome="ok" if outcome.error_code is None else outcome.error_code,
                    error_type=(
                        outcome.error_message.split(":", 1)[0]
                        if outcome.error_message else None
                    ),
                    extra_fields={"tool": "search_emails.advanced"},
                )

                if outcome.total_available is None:
                    some_total_unknown = True
                elif total_available_sum is not None:
                    total_available_sum += outcome.total_available

                if outcome.error_code:
                    per_folder_errors.append({
                        "folder": folder_name,
                        "error_code": outcome.error_code,
                        "error_message": outcome.error_message,
                    })

                for email in outcome.items:
                    all_results.append(
                        _build_list_item(email, fields=fields, folder_name=folder_name)
                    )

            if len(folders) > 1:
                reverse = (sort_order == "descending")
                if sort_by == "datetime_received":
                    all_results.sort(key=lambda x: x.get("received_time") or "", reverse=reverse)
                elif sort_by == "subject":
                    all_results.sort(key=lambda x: x.get("subject") or "", reverse=reverse)

            all_results = all_results[:max_results]

            # total_available is a sum across folders when every folder
            # reported a count; None if at least one was unknown.
            total_available: Optional[int] = (
                None if some_total_unknown else total_available_sum
            )

            meta: Dict[str, Any] = {}
            if per_folder_errors:
                meta["per_folder_errors"] = per_folder_errors

            response: Dict[str, Any] = {
                "items": all_results,
                "count": len(all_results),
                "total_available": total_available,
                "folders_searched": search_scope,
                "mailbox": mailbox,
            }
            if total_available is not None and len(all_results) + offset < total_available:
                response["next_offset"] = offset + len(all_results)
            if meta:
                response["meta"] = meta

            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            ews_call_log(
                self.logger, "search_emails.advanced_total",
                duration_ms=duration_ms,
                result_count=len(all_results),
                total_available=total_available,
                page_offset=offset,
                outcome="ok" if not per_folder_errors else "partial",
                extra_fields={"folders": len(folders)},
            )

            return format_success_response(
                f"Found {len(all_results)} result(s)",
                **response,
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.exception(f"Failed to perform advanced search: {e}")
            raise ToolExecutionError(
                f"Failed to perform advanced search: {type(e).__name__}: {e}"
            )

    async def _search_full_text(self, **kwargs) -> Dict[str, Any]:
        """Full-text search across subject, body, and attachment names."""
        target_mailbox = kwargs.get("target_mailbox")
        # Accept both ``query`` (current preferred name) and ``search_query``
        # (legacy schema name some callers are pinned to). If both are set,
        # ``query`` wins — that matches the shared-mode semantics used by
        # _search_quick.
        query_text = kwargs.get("query") or kwargs.get("search_query")
        search_scope = kwargs.get("search_scope", ["inbox", "sent"])
        max_results = kwargs.get("max_results", 50)
        search_in = kwargs.get("search_in", ["subject", "body"])
        exact_phrase = kwargs.get("exact_phrase", False)
        # Field projection (Bug 6). Default: list-default keys, no body.
        fields = kwargs.get("fields") or list(LIST_DEFAULT_FIELDS)

        if not query_text:
            # ValidationError maps to HTTP 400 in the openapi_adapter; raising
            # ToolExecutionError caused this to surface as HTTP 500 (Bug 2).
            raise ValidationError(
                "query is required for full_text mode "
                "(accepted under 'query' or 'search_query')"
            )

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            search_query = query_text.lower()

            folder_map = {
                "inbox": account.inbox,
                "sent": account.sent,
                "drafts": account.drafts,
                "deleted": account.trash,
                "junk": account.junk
            }

            folders_to_search = []
            for folder_name in search_scope:
                folder = folder_map.get(folder_name.lower())
                if folder:
                    folders_to_search.append(folder)

            if not folders_to_search:
                raise ToolExecutionError("No valid folders to search")

            only_fields = _db_fields_for(fields)
            offset = max(0, int(kwargs.get("offset", 0)))
            all_results: List[Dict[str, Any]] = []
            per_folder_errors: List[Dict[str, Any]] = []
            total_available_sum: Optional[int] = 0
            some_total_unknown = False
            start_time = datetime.now()

            for folder in folders_to_search:
                folder_name = safe_get(folder, "name", "Unknown")
                try:
                    q_filters = []
                    if "subject" in search_in:
                        q_filters.append(Q(subject__contains=query_text))
                    if "body" in search_in:
                        q_filters.append(Q(body__contains=query_text))

                    if not q_filters:
                        continue

                    combined_filter = q_filters[0]
                    for f in q_filters[1:]:
                        combined_filter |= f

                    query = folder.filter(combined_filter).order_by('-datetime_received')
                    if only_fields:
                        try:
                            query = query.only(*only_fields)
                        except Exception as only_exc:
                            self.logger.debug(
                                "query.only(%s) rejected on %s: %s",
                                only_fields, folder_name, only_exc,
                            )
                except Exception as setup_exc:
                    per_folder_errors.append({
                        "folder": folder_name,
                        "error_code": _classify_ews_error(setup_exc),
                        "error_message": f"{type(setup_exc).__name__}: {setup_exc}",
                    })
                    self.logger.warning(
                        "full-text setup on %s failed: %s: %s",
                        folder_name, type(setup_exc).__name__, setup_exc,
                    )
                    some_total_unknown = True
                    continue

                outcome = _paginate_query(
                    query,
                    max_results=max_results,
                    offset=offset,
                    chunk_size=50,
                    logger=self.logger,
                    folder_label=folder_name,
                )
                ews_call_log(
                    self.logger, "FindItem",
                    result_count=len(outcome.items),
                    total_available=outcome.total_available,
                    page_offset=offset,
                    folder=folder_name,
                    outcome="ok" if outcome.error_code is None else outcome.error_code,
                    extra_fields={"tool": "search_emails.full_text"},
                )
                if outcome.total_available is None:
                    some_total_unknown = True
                elif total_available_sum is not None:
                    total_available_sum += outcome.total_available
                if outcome.error_code:
                    per_folder_errors.append({
                        "folder": folder_name,
                        "error_code": outcome.error_code,
                        "error_message": outcome.error_message,
                    })

                for item in outcome.items:
                    # Exact-phrase post-filter stays client-side: the
                    # EWS ``__contains`` filter already narrowed us.
                    item_text = ""
                    if "subject" in search_in:
                        item_text += (safe_get(item, "subject", "") or "").lower() + " "
                    if "body" in search_in:
                        item_text += (safe_get(item, "text_body", "") or "").lower() + " "
                    attachment_match = False
                    if "attachments" in search_in and hasattr(item, "attachments") and item.attachments:
                        for att in item.attachments:
                            att_name = (safe_get(att, "name", "") or "").lower()
                            if search_query in att_name:
                                attachment_match = True
                                break
                    if exact_phrase and search_query not in item_text and not attachment_match:
                        continue
                    all_results.append(
                        _build_list_item(item, fields=fields, folder_name=folder_name)
                    )

            all_results.sort(
                key=lambda x: x.get("received_time") or "",
                reverse=True,
            )
            all_results = all_results[:max_results]

            total_available: Optional[int] = (
                None if some_total_unknown else total_available_sum
            )

            response: Dict[str, Any] = {
                "items": all_results,
                "count": len(all_results),
                "total_available": total_available,
                "query": query_text,
                "searched_folders": search_scope,
                "mailbox": mailbox,
            }
            if total_available is not None and len(all_results) + offset < total_available:
                response["next_offset"] = offset + len(all_results)
            if per_folder_errors:
                response["meta"] = {"per_folder_errors": per_folder_errors}

            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            ews_call_log(
                self.logger, "search_emails.full_text_total",
                duration_ms=duration_ms,
                result_count=len(all_results),
                total_available=total_available,
                page_offset=offset,
                outcome="ok" if not per_folder_errors else "partial",
                extra_fields={"folders": len(folders_to_search)},
            )

            return format_success_response(
                f"Found {len(all_results)} emails matching '{query_text}'",
                **response,
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.exception(f"Failed to perform full-text search: {e}")
            raise ToolExecutionError(
                f"Failed to perform full-text search: {type(e).__name__}: {e}"
            )


class GetEmailDetailsTool(BaseTool):
    """Tool for getting full email details."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_email_details",
            "description": "Get full details of a specific email by ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to access (requires impersonation/delegate access)"
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional field projection. When supplied, the "
                            "response 'email' object contains only these "
                            "fields. Default (no fields param): the full email "
                            "shape is returned unchanged for backward "
                            "compatibility."
                        ),
                    },
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Get email details."""
        message_id = kwargs.get("message_id")
        target_mailbox = kwargs.get("target_mailbox")
        fields = kwargs.get("fields")  # None -> backward-compat full shape

        # Validate up front. Previously a missing or empty message_id fell
        # through to ``find_message_for_account(account, None)`` which
        # raised a generic exception and surfaced as HTTP 500. A missing
        # required field is a caller error, not a server crash, so raise
        # ValidationError which the openapi_adapter maps to HTTP 400.
        if not message_id or not isinstance(message_id, str) or not message_id.strip():
            raise ValidationError(
                "message_id is required and must be a non-empty string"
            )

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find message across all folders (including custom subfolders)
            item = find_message_for_account(account, message_id)

            # Get sender email safely
            sender = safe_get(item, "sender", None)
            from_email = ""
            if sender and hasattr(sender, "email_address"):
                from_email = sender.email_address or ""

            # Get recipients safely
            to_recipients = safe_get(item, "to_recipients", []) or []
            to_emails = [r.email_address for r in to_recipients if r and hasattr(r, "email_address") and r.email_address]

            cc_recipients = safe_get(item, "cc_recipients", []) or []
            cc_emails = [r.email_address for r in cc_recipients if r and hasattr(r, "email_address") and r.email_address]

            # Get attachments safely
            attachments = safe_get(item, "attachments", []) or []
            attachment_names = [att.name for att in attachments if att and hasattr(att, "name") and att.name]

            email_details = {
                "message_id": ews_id_to_str(safe_get(item, "id", None)) or "unknown",
                "subject": safe_get(item, "subject", "") or "",
                "from": from_email,
                "to": to_emails,
                "cc": cc_emails,
                "body": safe_get(item, "text_body", "") or "",
                "body_html": str(safe_get(item, "body", "") or ""),
                "received_time": safe_get(item, "datetime_received", datetime.now()).isoformat(),
                "sent_time": safe_get(item, "datetime_sent", datetime.now()).isoformat(),
                "is_read": safe_get(item, "is_read", False),
                "has_attachments": safe_get(item, "has_attachments", False),
                "importance": safe_get(item, "importance", "Normal") or "Normal",
                "attachments": attachment_names,
            }

            # Projection: when fields=[...] is provided, return only those
            # keys inside the ``email`` object. Default (no fields) keeps
            # the full shape for backward-compat with existing callers —
            # this is one of the non-negotiable invariants.
            if fields:
                email_details = project_fields(email_details, fields)

            return format_success_response(
                "Email details retrieved",
                email=email_details,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to get email details: {e}")
            raise ToolExecutionError(f"Failed to get email details: {e}")


class GetEmailsBulkTool(BaseTool):
    """Fetch multiple messages in a single EWS round-trip (Issue 5).

    Motivation: clients wanting a weekly-report view had to call
    ``get_email_details`` N times for N messages — O(N) HTTP requests at
    ~1s each. ``account.fetch`` issues a single ``GetItem`` batch so 50
    messages come back in one network hop.
    """

    # Hard ceiling: the EWS server rejects very large GetItem batches,
    # and callers who need >100 items should page explicitly.
    _MAX_MESSAGES_HARD_CAP = 100

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_emails_bulk",
            "description": (
                "Fetch multiple emails by ID in one EWS round-trip. "
                "Uses exchangelib's account.fetch() batch API so N "
                "messages cost one HTTP call, not N."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Message IDs to fetch (from search_emails / list_attachments)",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Projection, same vocabulary as get_email_details. "
                            "Default: full email shape. Use ['message_id', "
                            "'subject', 'from', 'received_time', 'snippet'] "
                            "for a light one."
                        ),
                    },
                    "max_messages": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self._MAX_MESSAGES_HARD_CAP,
                        "default": 50,
                        "description": (
                            "Cap on input list size. Default 50, hard cap "
                            f"{self._MAX_MESSAGES_HARD_CAP}. Use paging for larger sets."
                        ),
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)",
                    },
                },
                "required": ["message_ids"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        from exchangelib import Message

        message_ids = kwargs.get("message_ids") or []
        if not isinstance(message_ids, list) or not message_ids:
            raise ValidationError("message_ids must be a non-empty list")
        # Dedupe while preserving order. Strings only.
        seen: set = set()
        clean_ids: List[str] = []
        for raw in message_ids:
            if not isinstance(raw, str):
                raise ValidationError(
                    f"message_ids must be strings, got {type(raw).__name__}"
                )
            if raw in seen:
                continue
            seen.add(raw)
            clean_ids.append(raw)

        max_messages = int(kwargs.get("max_messages", 50))
        if max_messages < 1:
            raise ValidationError("max_messages must be >= 1")
        if max_messages > self._MAX_MESSAGES_HARD_CAP:
            max_messages = self._MAX_MESSAGES_HARD_CAP
        if len(clean_ids) > max_messages:
            raise ValidationError(
                f"received {len(clean_ids)} ids but max_messages={max_messages}. "
                f"Reduce the list or split into pages."
            )

        target_mailbox = kwargs.get("target_mailbox")
        fields = kwargs.get("fields")  # None = full shape
        account = self.get_account(target_mailbox)
        mailbox = self.get_mailbox_info(target_mailbox)

        # Single GetItem batch — O(1) round trips regardless of N.
        start_time = datetime.now()
        try:
            fetched = list(account.fetch([Message(id=mid) for mid in clean_ids]))
        except Exception as exc:
            self.logger.exception(
                "get_emails_bulk: batch fetch failed: %s: %s",
                type(exc).__name__, exc,
            )
            raise ToolExecutionError(
                f"Bulk fetch failed: {type(exc).__name__}: {exc}"
            )
        duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)

        items: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        # exchangelib returns items in input order; pair by index so we
        # can surface per-id errors correctly.
        for requested_id, fetched_item in zip(clean_ids, fetched):
            if isinstance(fetched_item, BaseException):
                errors.append({
                    "message_id": requested_id,
                    "error_code": (
                        "NOT_FOUND"
                        if "ErrorItemNotFound" in type(fetched_item).__name__
                        else "FETCH_ERROR"
                    ),
                    "error_message": f"{type(fetched_item).__name__}: {fetched_item}",
                })
                continue
            item_dict = self._message_to_dict(fetched_item, fields=fields)
            items.append(item_dict)

        # If exchangelib returned a shorter list than we asked for
        # (shouldn't happen with account.fetch, but be defensive), mark
        # the missing ids.
        if len(fetched) < len(clean_ids):
            missing = clean_ids[len(fetched):]
            for mid in missing:
                errors.append({
                    "message_id": mid,
                    "error_code": "NOT_FOUND",
                    "error_message": "no response entry for id",
                })

        ews_call_log(
            self.logger, "GetItem",
            duration_ms=duration_ms,
            result_count=len(items),
            outcome="ok" if not errors else "partial",
            extra_fields={
                "tool": "get_emails_bulk",
                "requested": len(clean_ids),
                "errors": len(errors),
            },
        )

        return format_success_response(
            f"Fetched {len(items)} of {len(clean_ids)} message(s)",
            items=items,
            count=len(items),
            requested=len(clean_ids),
            errors=errors,
            mailbox=mailbox,
        )

    @staticmethod
    def _message_to_dict(
        message: Any, *, fields: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Render an exchangelib Message to the canonical email dict.

        Shape matches ``GetEmailDetailsTool`` so callers can swap in
        this batch tool without changing their downstream parser.
        """
        sender = safe_get(message, "sender", None)
        from_email = getattr(sender, "email_address", "") or ""

        to_recipients = safe_get(message, "to_recipients", []) or []
        to_emails = [
            r.email_address for r in to_recipients
            if hasattr(r, "email_address") and r.email_address
        ]
        cc_recipients = safe_get(message, "cc_recipients", []) or []
        cc_emails = [
            r.email_address for r in cc_recipients
            if hasattr(r, "email_address") and r.email_address
        ]
        attachments = safe_get(message, "attachments", []) or []
        attachment_names = [
            att.name for att in attachments if hasattr(att, "name") and att.name
        ]

        received = safe_get(message, "datetime_received", None)
        sent = safe_get(message, "datetime_sent", None)

        email_details = {
            "message_id": ews_id_to_str(safe_get(message, "id", None)) or "unknown",
            "subject": safe_get(message, "subject", "") or "",
            "from": from_email,
            "to": to_emails,
            "cc": cc_emails,
            "body": safe_get(message, "text_body", "") or "",
            "body_html": str(safe_get(message, "body", "") or ""),
            "received_time": received.isoformat() if received and hasattr(received, "isoformat") else None,
            "sent_time": sent.isoformat() if sent and hasattr(sent, "isoformat") else None,
            "is_read": safe_get(message, "is_read", False),
            "has_attachments": safe_get(message, "has_attachments", False),
            "importance": safe_get(message, "importance", "Normal") or "Normal",
            "attachments": attachment_names,
        }
        email_details["snippet"] = truncate_text(email_details["body"], 200)
        if fields:
            return project_fields(email_details, fields)
        return email_details


class DeleteEmailTool(BaseTool):
    """Tool for deleting emails."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_email",
            "description": (
                "Delete an email by ID. Default is soft delete (moves to "
                "Deleted Items). Set ``permanent`` (or ``hard_delete`` "
                "alias) true to bypass Trash and remove permanently."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID to delete"
                    },
                    "permanent": {
                        "type": "boolean",
                        "description": "Permanently delete (bypasses Trash). Alias: hard_delete.",
                        "default": False
                    },
                    "hard_delete": {
                        "type": "boolean",
                        "description": "Alias for 'permanent'.",
                        "default": False
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to delete from (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Delete email."""
        message_id = kwargs.get("message_id")
        # Accept both ``permanent`` (canonical) and ``hard_delete`` (alias
        # matching manage_folder / callers' muscle memory). Either truthy
        # value triggers a permanent delete.
        permanent = bool(
            kwargs.get("permanent", False) or kwargs.get("hard_delete", False)
        )
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id or not isinstance(message_id, str) or not message_id.strip():
            raise ValidationError("message_id is required")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find message across all folders (including custom subfolders)
            item = find_message_for_account(account, message_id)

            if permanent:
                # ``item.delete()`` in exchangelib defaults to a
                # MoveToDeletedItems disposal — the item ends up in
                # Trash, defeating "permanent". The correct API is
                # ``item.delete(disposal_type=HARD_DELETE)``; the
                # constant lives in ``exchangelib.items``, not at the
                # top-level ``exchangelib`` package (the previous fix
                # imported from the wrong place and also used the
                # wrong keyword ``delete_type=`` — both produced 500s).
                try:
                    from exchangelib.items import HARD_DELETE
                    item.delete(disposal_type=HARD_DELETE)
                except ImportError:
                    # Fall back to the wire string if the constant
                    # ever moves again. ``disposal_type="HardDelete"``
                    # is accepted by exchangelib in all recent versions.
                    item.delete(disposal_type="HardDelete")
                action = "permanently deleted"
            else:
                # Move to trash folder (Deleted Items) so user can recover.
                item.move(account.trash)
                action = "moved to trash"

            self.logger.info(f"Email {message_id} {action} in mailbox: {mailbox}")

            return format_success_response(
                f"Email {action}",
                message_id=message_id,
                permanent=permanent,
                hard_delete=permanent,
                mailbox=mailbox
            )

        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.exception(f"Failed to delete email: {type(e).__name__}: {e}")
            raise ToolExecutionError(
                f"Failed to delete email: {type(e).__name__}: {e}"
            )


class MoveEmailTool(BaseTool):
    """Tool for moving emails between folders."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "move_email",
            "description": "Move an email to a different folder.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID to move"
                    },
                    "destination_folder": {
                        "type": "string",
                        "description": "Destination folder name or path (e.g. inbox, Inbox/Projects)"
                    },
                    "destination_folder_id": {
                        "type": "string",
                        "description": "Destination folder ID (alternative to destination_folder)"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Move email to folder."""
        message_id = kwargs.get("message_id")
        destination_folder = kwargs.get("destination_folder")
        destination_folder_id = kwargs.get("destination_folder_id")
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")
        if not destination_folder and not destination_folder_id:
            raise ToolExecutionError("Either destination_folder or destination_folder_id is required")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Folder ID takes precedence over folder name/path when both are provided.
            destination_identifier = destination_folder_id or destination_folder
            dest_folder = await resolve_folder_for_account(account, destination_identifier)
            dest_name = safe_get(dest_folder, "name", destination_identifier)

            # Find message across all folders (including custom subfolders)
            item = find_message_for_account(account, message_id)
            item.move(dest_folder)

            self.logger.info(f"Email {message_id} moved to {dest_name} in mailbox: {mailbox}")

            return format_success_response(
                f"Email moved to {dest_name}",
                message_id=message_id,
                destination_folder=dest_name,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to move email: {e}")
            raise ToolExecutionError(f"Failed to move email: {e}")


class UpdateEmailTool(BaseTool):
    """Tool for updating email properties."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "update_email",
            "description": "Update email properties (read status, flags, categories, importance).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID"
                    },
                    "is_read": {
                        "type": "boolean",
                        "description": "Mark as read (true) or unread (false)"
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Email categories (labels)"
                    },
                    "flag_status": {
                        "type": "string",
                        "enum": ["NotFlagged", "Flagged", "Complete"],
                        "description": "Flag status"
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["Low", "Normal", "High"],
                        "description": "Email importance level"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Update email properties."""
        message_id = kwargs.get("message_id")
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find the message across common folders
            message = find_message_for_account(account, message_id)

            # Track what was updated
            updates = {}

            # Update read status
            if "is_read" in kwargs:
                message.is_read = kwargs["is_read"]
                updates["is_read"] = kwargs["is_read"]

            # Update categories
            if "categories" in kwargs:
                message.categories = kwargs["categories"]
                updates["categories"] = kwargs["categories"]

            # Update flag status using ExtendedProperty
            if "flag_status" in kwargs:
                flag_value = FLAG_STATUS_MAP.get(kwargs["flag_status"])
                if kwargs["flag_status"] not in FLAG_STATUS_MAP:
                    raise ToolExecutionError(
                        f"Invalid flag_status: {kwargs['flag_status']}. "
                        f"Valid values: {', '.join(FLAG_STATUS_MAP.keys())}"
                    )
                message.flag_status_value = flag_value
                updates["flag_status"] = kwargs["flag_status"]

            # Update importance
            if "importance" in kwargs:
                message.importance = kwargs["importance"]
                updates["importance"] = kwargs["importance"]

            # Save changes
            message.save()

            self.logger.info(f"Email {message_id} updated in mailbox {mailbox}: {updates}")

            return format_success_response(
                "Email updated successfully",
                message_id=message_id,
                updates=updates,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to update email: {e}")
            raise ToolExecutionError(f"Failed to update email: {e}")


class CopyEmailTool(BaseTool):
    """Tool for copying emails to another folder."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "copy_email",
            "description": "Copy an email to another folder.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID to copy"
                    },
                    "destination_folder": {
                        "type": "string",
                        "description": "Destination folder name",
                        "enum": ["inbox", "sent", "drafts", "deleted", "junk"]
                    },
                    "destination_folder_id": {
                        "type": "string",
                        "description": "Destination folder ID (alternative to destination_folder)"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Copy email to another folder."""
        message_id = kwargs.get("message_id")
        destination_folder_name = kwargs.get("destination_folder")
        destination_folder_id = kwargs.get("destination_folder_id")
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        if not destination_folder_name and not destination_folder_id:
            raise ToolExecutionError("Either destination_folder or destination_folder_id is required")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            message = find_message_for_account(account, message_id)
            source_folder_name = safe_get(safe_get(message, "folder", None), "name", "unknown")

            destination_identifier = destination_folder_id or destination_folder_name
            destination_folder = await resolve_folder_for_account(account, destination_identifier)
            dest_name = safe_get(destination_folder, 'name', destination_identifier)

            # Copy the message (exchangelib uses .copy() method)
            copied_message = message.copy(to_folder=destination_folder)

            subject = safe_get(message, 'subject', 'No Subject')

            self.logger.info(f"Copied email '{subject}' from {source_folder_name} to {dest_name} in mailbox: {mailbox}")

            return format_success_response(
                f"Email copied from {source_folder_name} to {dest_name}",
                message_id=message_id,
                copied_message_id=ews_id_to_str(safe_get(copied_message, 'id', None)) or '' if copied_message else '',
                subject=subject,
                source_folder=source_folder_name,
                destination_folder=dest_name,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to copy email: {e}")
            raise ToolExecutionError(f"Failed to copy email: {e}")


class ReplyEmailTool(BaseTool):
    """Tool for replying to emails while preserving conversation thread."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "reply_email",
            "description": "Reply to an email, preserving the conversation thread.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Exchange message ID of the email to reply to"
                    },
                    "body": {
                        "type": "string",
                        "description": "The reply body (HTML supported)"
                    },
                    "reply_all": {
                        "type": "boolean",
                        "description": "If true, reply to all recipients; if false, reply only to sender",
                        "default": False
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths to attach to the reply (optional)"
                    },
                    **INLINE_ATTACHMENTS_SCHEMA,
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to reply from (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id", "body"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Reply to an email via EWS."""
        message_id = kwargs.get("message_id")
        body = kwargs.get("body", "").strip()
        reply_all = kwargs.get("reply_all", False)
        attachments = kwargs.get("attachments", [])
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")
        if not body:
            raise ToolExecutionError("body is required")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find the original message across folders
            original_message = find_message_for_account(account, message_id)

            # Get original message details for the response
            original_subject = safe_get(original_message, "subject", "") or ""
            original_sender = safe_get(original_message, "sender", None)
            original_from_email = ""
            if original_sender and hasattr(original_sender, "email_address"):
                original_from_email = original_sender.email_address or ""

            # Get original recipients for reply-all
            original_to = [r.email_address for r in (safe_get(original_message, "to_recipients", []) or [])
                          if r and hasattr(r, "email_address") and r.email_address]
            original_cc = [r.email_address for r in (safe_get(original_message, "cc_recipients", []) or [])
                         if r and hasattr(r, "email_address") and r.email_address]

            # IMPORTANT: DO NOT use create_reply()/create_reply_all() - they auto-append content we can't control
            # This causes duplication and wrong order issues with Exclaimer signature placement.
            # Instead, always create a fresh Message with manually constructed body.
            self.logger.info("Creating fresh Message for reply (not using create_reply)")

            # Get the reply subject (avoid duplicate RE: prefix)
            reply_subject = add_reply_prefix(original_subject)

            # Determine recipients for the reply
            if reply_all:
                # Reply all: original sender + all original recipients (except self)
                reply_to_recipients = [Mailbox(email_address=original_from_email)]
                for email in original_to + original_cc:
                    if email != account.primary_smtp_address:
                        reply_to_recipients.append(Mailbox(email_address=email))
                self.logger.info(f"Reply-all to {len(reply_to_recipients)} recipient(s)")
            else:
                # Reply: just original sender
                reply_to_recipients = [Mailbox(email_address=original_from_email)]
                self.logger.info(f"Reply to {original_from_email}")

            # Build the complete reply body manually
            # 1. User's message at top, rendered safely (plain text -> escaped +
            #    <br/>, existing HTML -> lightly sanitised against script/style/
            #    javascript: payloads). See utils.format_body_for_html.
            user_message_html = format_body_for_html(body)

            # 2. Format the reply headers from original email metadata. These
            #    fields originate in an inbound email (attacker-controlled) and
            #    MUST be HTML-escaped before interpolation.
            header = format_forward_header(original_message)
            safe_from = escape_html(header.get('from', ''))
            safe_to = escape_html(header.get('to', ''))
            safe_cc = escape_html(header.get('cc', ''))
            safe_sent = escape_html(header.get('sent', ''))
            safe_subject = escape_html(header.get('subject', ''))

            # 3. Get the original email body HTML (preserves styles but strips document structure)
            original_body_html = sanitize_html(extract_body_html(original_message))
            self.logger.info(f"Extracted original body: {len(original_body_html)} characters")

            # Clean original body - rename WordSection1 to OriginalSection
            # This prevents Exclaimer from placing signature after the original content
            original_body_html = clean_original_body_for_signature(original_body_html)

            # 4. Construct complete body matching Outlook's exact structure
            # - WordSection1: user's new content (Exclaimer injects signature at end of this div)
            # - border-top div: Outlook-style separator (NOT <hr>)
            # - Headers inline in separator div
            # - original body (with OriginalSection class to avoid Exclaimer confusion)
            complete_body = f'''<div class="WordSection1">
<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;">{user_message_html}</p>
</div>
<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">
<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;"><b>From:</b> {safe_from}<br/>
<b>Sent:</b> {safe_sent}<br/>
<b>To:</b> {safe_to}<br/>'''
            if safe_cc:
                complete_body += f'''<b>Cc:</b> {safe_cc}<br/>'''
            complete_body += f'''<b>Subject:</b> {safe_subject}</p>
</div>
{original_body_html}'''

            self.logger.info(f"Constructed complete reply body: {len(complete_body)} characters")

            # Create a new Message with the complete body
            message = Message(
                account=account,
                subject=reply_subject,
                body=HTMLBody(complete_body),
                to_recipients=reply_to_recipients
            )

            # Set threading headers so reply stays in the same conversation
            original_internet_msg_id = safe_get(original_message, "message_id", None)
            original_references = safe_get(original_message, "references", None)
            if original_internet_msg_id:
                message.in_reply_to = original_internet_msg_id
                if original_references:
                    message.references = f"{original_references} {original_internet_msg_id}"
                else:
                    message.references = original_internet_msg_id
                self.logger.info(f"Set threading headers: in_reply_to={original_internet_msg_id}")

            # Copy original inline attachments (signatures, embedded images)
            inline_count, _ = copy_attachments_to_message(original_message, message)
            if inline_count > 0:
                self.logger.info(f"Copied {inline_count} inline attachment(s) from original message")

            # Add new attachments if provided
            new_attachment_count = 0
            if attachments:
                for file_path in attachments:
                    try:
                        # Use os.path.basename for cross-platform path handling
                        file_name = os.path.basename(file_path)
                        with open(file_path, 'rb') as f:
                            content = f.read()
                            attachment = FileAttachment(
                                name=file_name,
                                content=content
                            )
                            message.attach(attachment)
                            new_attachment_count += 1
                            self.logger.info(f"Attached file: {file_name} ({len(content)} bytes)")
                    except FileNotFoundError:
                        raise ToolExecutionError(f"Attachment file not found: {file_path}")
                    except PermissionError:
                        raise ToolExecutionError(f"Permission denied reading attachment: {file_path}")
                    except Exception as e:
                        raise ToolExecutionError(f"Failed to attach file {file_path}: {e}")

            # Add inline (base64) attachments if provided
            inline_b64_count = attach_inline_files(message, kwargs.get("inline_attachments", []))
            if inline_b64_count > 0:
                new_attachment_count += inline_b64_count
                self.logger.info(f"Added {inline_b64_count} inline (base64) attachment(s)")

            # Send the message
            message.send()
            self.logger.info(f"Reply sent to {original_from_email} from mailbox: {mailbox}")

            # Determine who the reply was sent to
            reply_to_list = []
            if reply_all:
                reply_to_list = [original_from_email] + [e for e in original_to + original_cc
                                                        if e != account.primary_smtp_address]
            else:
                reply_to_list = [original_from_email]

            return format_success_response(
                "Reply sent successfully",
                original_subject=original_subject,
                reply_to=reply_to_list,
                reply_all=reply_all,
                attachments_count=new_attachment_count,
                inline_attachments_preserved=inline_count,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to send reply: {e}")
            raise ToolExecutionError(f"Failed to send reply: {e}")


class ForwardEmailTool(BaseTool):
    """Tool for forwarding emails to new recipients while preserving original content."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "forward_email",
            "description": "Forward an email to new recipients with original content and attachments.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Exchange message ID of the email to forward"
                    },
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of recipient email addresses"
                    },
                    "body": {
                        "type": "string",
                        "description": "Optional message to add before the forwarded content"
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "CC recipients (optional)"
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "BCC recipients (optional)"
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional file paths to attach (optional)"
                    },
                    **INLINE_ATTACHMENTS_SCHEMA,
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to forward from (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id", "to"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Forward an email via EWS."""
        message_id = kwargs.get("message_id")
        to_recipients = kwargs.get("to", [])
        body = kwargs.get("body", "").strip() if kwargs.get("body") else ""
        cc_recipients = kwargs.get("cc", [])
        bcc_recipients = kwargs.get("bcc", [])
        additional_attachments = kwargs.get("attachments", [])
        target_mailbox = kwargs.get("target_mailbox")

        if not message_id:
            raise ToolExecutionError("message_id is required")
        if not to_recipients:
            raise ToolExecutionError("to recipients are required")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find the original message across folders
            original_message = find_message_for_account(account, message_id)

            # Get original message details
            original_subject = safe_get(original_message, "subject", "") or ""

            # IMPORTANT: DO NOT use create_forward() - it auto-appends content we can't control
            # This causes duplication and wrong order issues with Exclaimer signature placement.
            # Instead, always create a fresh Message with manually constructed body.
            self.logger.info("Creating fresh Message for forward (not using create_forward)")

            # Get the forward subject (avoid duplicate FW: prefix)
            forward_subject = add_forward_prefix(original_subject)

            # Build the complete forward body manually
            # 1. User's message at top, rendered safely (plain text -> escaped +
            #    <br/>, existing HTML -> lightly sanitised). See utils.format_body_for_html.
            user_message_html = format_body_for_html(body)

            # 2. Format the forward headers from original email metadata; these
            #    are attacker-controlled and must be HTML-escaped.
            header = format_forward_header(original_message)
            safe_from = escape_html(header.get('from', ''))
            safe_to = escape_html(header.get('to', ''))
            safe_cc = escape_html(header.get('cc', ''))
            safe_sent = escape_html(header.get('sent', ''))
            safe_subject = escape_html(header.get('subject', ''))

            # 3. Get the original email body HTML (preserves styles but strips document structure)
            original_body_html = sanitize_html(extract_body_html(original_message))
            self.logger.info(f"Extracted original body: {len(original_body_html)} characters")

            # Clean original body - rename WordSection1 to OriginalSection
            # This prevents Exclaimer from placing signature after the original content
            original_body_html = clean_original_body_for_signature(original_body_html)

            # 4. Build headers block (Outlook format)
            headers_html = f'''<p style="font-size:11pt;font-family:Calibri,sans-serif;">
<b>From:</b> {safe_from}<br/>
<b>Date:</b> {safe_sent}<br/>
<b>Subject:</b> {safe_subject}<br/>'''
            if safe_to:
                headers_html += f'''<b>To:</b> {safe_to}<br/>'''
            if safe_cc:
                headers_html += f'''<b>Cc:</b> {safe_cc}<br/>'''
            headers_html += '''</p>'''

            # 5. Construct complete body matching Outlook's exact structure
            # - WordSection1: user's new content (Exclaimer injects signature at end of this div)
            # - border-top div: Outlook-style separator (NOT <hr>)
            # - divRplyFwdMsg: headers block
            # - original body (with OriginalSection class to avoid Exclaimer confusion)
            complete_body = f'''<div class="WordSection1">
<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;">{user_message_html}</p>
</div>
<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">
<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;"><b>From:</b> {safe_from}<br/>
<b>Sent:</b> {safe_sent}<br/>
<b>To:</b> {safe_to}<br/>'''
            if safe_cc:
                complete_body += f'''<b>Cc:</b> {safe_cc}<br/>'''
            complete_body += f'''<b>Subject:</b> {safe_subject}</p>
</div>
{original_body_html}'''

            self.logger.info(f"Constructed complete forward body: {len(complete_body)} characters")

            # Create a new Message with the complete body
            message = Message(
                account=account,
                subject=forward_subject,
                body=HTMLBody(complete_body),
                to_recipients=[Mailbox(email_address=email) for email in to_recipients]
            )

            # Set threading headers so forward stays in the same conversation
            original_internet_msg_id = safe_get(original_message, "message_id", None)
            original_references = safe_get(original_message, "references", None)
            if original_internet_msg_id:
                message.in_reply_to = original_internet_msg_id
                if original_references:
                    message.references = f"{original_references} {original_internet_msg_id}"
                else:
                    message.references = original_internet_msg_id
                self.logger.info(f"Set threading headers: in_reply_to={original_internet_msg_id}")

            if cc_recipients:
                message.cc_recipients = [Mailbox(email_address=email) for email in cc_recipients]
            if bcc_recipients:
                message.bcc_recipients = [Mailbox(email_address=email) for email in bcc_recipients]

            # Copy original attachments
            inline_count, regular_count = copy_attachments_to_message(original_message, message)
            total_original_attachments = inline_count + regular_count
            self.logger.info(f"Copied {total_original_attachments} attachment(s) from original "
                           f"({inline_count} inline, {regular_count} regular)")

            # Add additional attachments if provided
            additional_attachment_count = 0
            if additional_attachments:
                for file_path in additional_attachments:
                    try:
                        # Use os.path.basename for cross-platform path handling
                        file_name = os.path.basename(file_path)
                        with open(file_path, 'rb') as f:
                            content = f.read()
                            attachment = FileAttachment(
                                name=file_name,
                                content=content
                            )
                            message.attach(attachment)
                            additional_attachment_count += 1
                            self.logger.info(f"Attached additional file: {file_name} ({len(content)} bytes)")
                    except FileNotFoundError:
                        raise ToolExecutionError(f"Attachment file not found: {file_path}")
                    except PermissionError:
                        raise ToolExecutionError(f"Permission denied reading attachment: {file_path}")
                    except Exception as e:
                        raise ToolExecutionError(f"Failed to attach file {file_path}: {e}")

            # Add inline (base64) attachments if provided
            inline_b64_count = attach_inline_files(message, kwargs.get("inline_attachments", []))
            if inline_b64_count > 0:
                additional_attachment_count += inline_b64_count
                self.logger.info(f"Added {inline_b64_count} inline (base64) attachment(s)")

            # Send the message
            message.send()
            self.logger.info(f"Email forwarded to {', '.join(to_recipients)} from mailbox: {mailbox}")

            return format_success_response(
                "Email forwarded successfully",
                original_subject=original_subject,
                forwarded_to=to_recipients,
                cc=cc_recipients if cc_recipients else None,
                bcc=bcc_recipients if bcc_recipients else None,
                attachments_included=total_original_attachments,
                inline_attachments_preserved=inline_count,
                additional_attachments=additional_attachment_count,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to forward email: {e}")
            raise ToolExecutionError(f"Failed to forward email: {e}")
