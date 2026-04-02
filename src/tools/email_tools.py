"""Email operation tools for EWS MCP Server."""

import os
from typing import Any, Dict, List
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
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, safe_get, truncate_text, parse_datetime_tz_aware, find_message_across_folders, find_message_for_account, ews_id_to_str, attach_inline_files, INLINE_ATTACHMENTS_SCHEMA


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

    # Strip document-level HTML tags to prevent nested <html><body> issues
    # when embedding in blockquote. Nested document tags break HTML structure.
    return strip_html_document_tags(html)


def strip_html_document_tags(html: str) -> str:
    """
    Strip document-level HTML tags from content.

    When forwarding/replying, the original email body may contain full HTML
    document structure (<html>, <head>, <body>). If we embed this inside
    our blockquote, we get invalid nested HTML that browsers/Exclaimer
    restructure incorrectly, causing content to appear in wrong order.

    Args:
        html: HTML content that may contain document-level tags

    Returns:
        Inner content with document tags stripped
    """
    if not html:
        return html

    # Remove DOCTYPE declaration
    html = re.sub(r'<!DOCTYPE[^>]*>', '', html, flags=re.IGNORECASE)

    # Remove <html> open/close tags (with any attributes)
    html = re.sub(r'</?html[^>]*>', '', html, flags=re.IGNORECASE)

    # Remove entire <head>...</head> section (includes style, meta, etc.)
    html = re.sub(r'<head[^>]*>.*?</head>', '', html, flags=re.IGNORECASE | re.DOTALL)

    # Remove <body> open/close tags but keep the content inside
    html = re.sub(r'</?body[^>]*>', '', html, flags=re.IGNORECASE)

    return html.strip()


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

    # Standard folders map (lowercase for matching)
    folder_map = {
        "inbox": account.inbox,
        "sent": account.sent,
        "drafts": account.drafts,
        "deleted": account.trash,
        "junk": account.junk,
        "trash": account.trash,
        "calendar": account.calendar,
        "contacts": account.contacts,
        "tasks": account.tasks
    }

    # Try 1: Standard folder name (case-insensitive)
    folder_lower = folder_identifier.lower()
    if folder_lower in folder_map:
        return folder_map[folder_lower]

    # Try 2: Folder ID (starts with AAMk or similar Exchange ID pattern)
    # IMPORTANT: Check this BEFORE path parsing, as base64 IDs can contain '/'
    if is_exchange_folder_id(folder_identifier):
        # Folder ID detection - try to find in tree
        def find_folder_by_id(parent, target_id):
            """Recursively search for folder by ID."""
            try:
                parent_id = ews_id_to_str(safe_get(parent, 'id', None)) or ''
                if parent_id == target_id:
                    return parent
                if hasattr(parent, 'children') and parent.children:
                    for child in parent.children:
                        result = find_folder_by_id(child, target_id)
                        if result:
                            return result
            except Exception:
                pass
            return None

        # Search root tree for folder ID
        found_folder = find_folder_by_id(account.root, folder_identifier)
        if found_folder:
            return found_folder
        # If not found as folder ID, don't fall through to path parsing
        raise ToolExecutionError(
            f"Folder ID '{folder_identifier[:20]}...' not found. "
            f"The ID appears to be an Exchange folder ID but could not be located in your mailbox."
        )

    # Try 3: Folder path (e.g., "Inbox/CC" or "Inbox/Projects/2024")
    # Only parse as path if NOT an Exchange ID (which may contain '/')
    if '/' in folder_identifier:
        parts = folder_identifier.split('/')
        parent_name = parts[0].strip().lower()

        # Start from a known parent folder
        if parent_name in folder_map:
            current_folder = folder_map[parent_name]
        else:
            # Default to inbox if parent not recognized
            current_folder = account.inbox

        # Navigate through subfolders
        for subfolder_name in parts[1:]:
            subfolder_name = subfolder_name.strip()
            found = False

            try:
                for child in current_folder.children:
                    if safe_get(child, 'name', '').lower() == subfolder_name.lower():
                        current_folder = child
                        found = True
                        break
            except Exception as e:
                raise ToolExecutionError(
                    f"Error accessing subfolders of '{current_folder.name}': {e}"
                )

            if not found:
                raise ToolExecutionError(
                    f"Subfolder '{subfolder_name}' not found under '{current_folder.name}'"
                )

        return current_folder

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

    # Search under inbox first (most common location for custom folders)
    custom_folder = search_folder_tree(account.inbox, folder_identifier)
    if custom_folder:
        return custom_folder

    # Search under root as fallback
    custom_folder = search_folder_tree(account.root, folder_identifier)
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
                    }
                },
                "required": ["to", "subject", "body"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Send email via EWS."""
        # Get target mailbox for impersonation
        target_mailbox = kwargs.pop("target_mailbox", None)

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
                    "to_address": {
                        "type": "string",
                        "description": "Filter by recipient email (advanced mode)"
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
                    "query": {
                        "type": "string",
                        "description": "Full-text search query (full_text mode)"
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

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Route to appropriate search mode."""
        mode = kwargs.get("mode", "quick")

        if mode == "advanced":
            return await self._search_advanced(**kwargs)
        elif mode == "full_text":
            return await self._search_full_text(**kwargs)
        else:
            return await self._search_quick(**kwargs)

    async def _search_quick(self, **kwargs) -> Dict[str, Any]:
        """Quick search: filter by subject, sender, date, read status, attachments."""
        from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
        from exchangelib.errors import ErrorTimeoutExpired
        import socket

        target_mailbox = kwargs.get("target_mailbox")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Auto-add date range to prevent timeouts in large mailboxes
            if not kwargs.get("start_date") and not kwargs.get("end_date"):
                has_filters = (
                    kwargs.get("subject_contains") or
                    kwargs.get("from_address") or
                    kwargs.get("has_attachments") is not None or
                    kwargs.get("is_read") is not None
                )

                if not has_filters:
                    from datetime import timedelta
                    default_days_back = 30
                    auto_start_date = datetime.now() - timedelta(days=default_days_back)
                    kwargs["start_date"] = auto_start_date.isoformat()
                    self.logger.info(
                        f"No filters or date range provided. Limiting to last {default_days_back} days."
                    )

            folder_name = kwargs.get("folder", "inbox")
            folder = await resolve_folder_for_account(account, folder_name)

            query = folder.all()

            if kwargs.get("subject_contains"):
                query = query.filter(subject__contains=kwargs["subject_contains"])
            if kwargs.get("from_address"):
                query = query.filter(sender=kwargs["from_address"])
            if kwargs.get("has_attachments") is not None:
                query = query.filter(has_attachments=kwargs["has_attachments"])
            if kwargs.get("is_read") is not None:
                query = query.filter(is_read=kwargs["is_read"])
            if kwargs.get("start_date"):
                start = parse_datetime_tz_aware(kwargs["start_date"])
                query = query.filter(datetime_received__gte=start)
            if kwargs.get("end_date"):
                end = parse_datetime_tz_aware(kwargs["end_date"])
                query = query.filter(datetime_received__lte=end)

            query = query.order_by('-datetime_received')
            max_results = kwargs.get("max_results", 50)

            @retry(
                stop=stop_after_attempt(2),
                wait=wait_exponential(multiplier=2, min=4, max=10),
                retry=retry_if_exception_type((ErrorTimeoutExpired, socket.timeout))
            )
            def execute_query():
                results = []
                for item in query[:max_results]:
                    sender = safe_get(item, "sender", None)
                    from_email = ""
                    if sender and hasattr(sender, "email_address"):
                        from_email = sender.email_address or ""

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
                    results.append(email_data)
                return results

            emails = execute_query()

            return format_success_response(
                f"Found {len(emails)} matching emails",
                emails=emails,
                total_count=len(emails),
                mailbox=mailbox
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

            all_results = []
            for folder in folders:
                try:
                    query = folder.filter(combined_filter)
                    sort_field = sort_by
                    if sort_order == "descending" and not sort_field.startswith('-'):
                        sort_field = f"-{sort_field}"
                    query = query.order_by(sort_field)

                    results_per_folder = max_results // len(folders) if len(folders) > 1 else max_results
                    for email in query[:results_per_folder]:
                        all_results.append({
                            "message_id": safe_get(email, 'id', ''),
                            "subject": safe_get(email, 'subject', ''),
                            "from": safe_get(email, 'sender', {}).email_address if hasattr(safe_get(email, 'sender', {}), 'email_address') else '',
                            "to": [r.email_address for r in safe_get(email, 'to_recipients', []) if hasattr(r, 'email_address')],
                            "received_time": safe_get(email, 'datetime_received', '').isoformat() if safe_get(email, 'datetime_received') else None,
                            "is_read": safe_get(email, 'is_read', False),
                            "has_attachments": safe_get(email, 'has_attachments', False),
                            "importance": safe_get(email, 'importance', 'Normal'),
                            "categories": safe_get(email, 'categories', []),
                            "body_preview": truncate_text(safe_get(email, 'text_body', ''), 200),
                            "folder": folder.name
                        })
                except Exception as e:
                    self.logger.warning(f"Error searching folder {folder.name}: {e}")
                    continue

            if len(folders) > 1:
                reverse = (sort_order == "descending")
                if sort_by == "datetime_received":
                    all_results.sort(key=lambda x: x.get("received_time", ""), reverse=reverse)
                elif sort_by == "subject":
                    all_results.sort(key=lambda x: x.get("subject", ""), reverse=reverse)

            all_results = all_results[:max_results]

            return format_success_response(
                f"Found {len(all_results)} result(s)",
                results=all_results,
                count=len(all_results),
                folders_searched=search_scope,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to perform advanced search: {e}")
            raise ToolExecutionError(f"Failed to perform advanced search: {e}")

    async def _search_full_text(self, **kwargs) -> Dict[str, Any]:
        """Full-text search across subject, body, and attachment names."""
        target_mailbox = kwargs.get("target_mailbox")
        query_text = kwargs.get("query")
        search_scope = kwargs.get("search_scope", ["inbox", "sent"])
        max_results = kwargs.get("max_results", 50)
        search_in = kwargs.get("search_in", ["subject", "body"])
        exact_phrase = kwargs.get("exact_phrase", False)

        if not query_text:
            raise ToolExecutionError("query is required for full_text mode")

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

            all_results = []
            for folder in folders_to_search:
                try:
                    q_filters = []
                    if "subject" in search_in:
                        q_filters.append(Q(subject__contains=query_text))
                    if "body" in search_in:
                        q_filters.append(Q(body__contains=query_text))

                    if q_filters:
                        combined_filter = q_filters[0]
                        for f in q_filters[1:]:
                            combined_filter |= f

                        items = folder.filter(combined_filter).order_by('-datetime_received')[:max_results]

                        for item in items:
                            item_text = ""
                            if "subject" in search_in:
                                item_text += safe_get(item, 'subject', '').lower() + " "
                            if "body" in search_in:
                                item_text += safe_get(item, 'text_body', '').lower() + " "

                            attachment_match = False
                            if "attachments" in search_in and hasattr(item, 'attachments') and item.attachments:
                                for att in item.attachments:
                                    att_name = safe_get(att, 'name', '').lower()
                                    if search_query in att_name:
                                        attachment_match = True
                                        break

                            if exact_phrase and search_query not in item_text and not attachment_match:
                                continue

                            result = {
                                "id": ews_id_to_str(safe_get(item, 'id', None)) or '',
                                "subject": safe_get(item, 'subject', ''),
                                "from": safe_get(safe_get(item, 'sender', {}), 'email_address', ''),
                                "to": [r.email_address for r in safe_get(item, 'to_recipients', []) if hasattr(r, 'email_address')],
                                "received": safe_get(item, 'datetime_received', '').isoformat() if safe_get(item, 'datetime_received') else None,
                                "is_read": safe_get(item, 'is_read', False),
                                "has_attachments": safe_get(item, 'has_attachments', False),
                                "folder": safe_get(folder, 'name', 'Unknown'),
                                "preview": safe_get(item, 'text_body', '')[:200] if "body" in search_in else ""
                            }
                            all_results.append(result)

                except Exception as e:
                    self.logger.warning(f"Error searching folder {safe_get(folder, 'name', 'Unknown')}: {e}")
                    continue

            all_results.sort(key=lambda x: x.get('received', ''), reverse=True)
            all_results = all_results[:max_results]

            return format_success_response(
                f"Found {len(all_results)} emails matching '{query_text}'",
                results=all_results,
                query=query_text,
                total_results=len(all_results),
                searched_folders=search_scope,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to perform full-text search: {e}")
            raise ToolExecutionError(f"Failed to perform full-text search: {e}")


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
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Get email details."""
        message_id = kwargs.get("message_id")
        target_mailbox = kwargs.get("target_mailbox")

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
                "attachments": attachment_names
            }

            return format_success_response(
                "Email details retrieved",
                email=email_details,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to get email details: {e}")
            raise ToolExecutionError(f"Failed to get email details: {e}")


class DeleteEmailTool(BaseTool):
    """Tool for deleting emails."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_email",
            "description": "Delete an email by ID (moves to trash).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID to delete"
                    },
                    "permanent": {
                        "type": "boolean",
                        "description": "Permanently delete (hard delete)",
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
        permanent = kwargs.get("permanent", False)
        target_mailbox = kwargs.get("target_mailbox")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find message across all folders (including custom subfolders)
            item = find_message_for_account(account, message_id)

            if permanent:
                item.delete()
                action = "permanently deleted"
            else:
                # Move to trash folder (Deleted Items) so user can recover
                # Note: soft_delete() makes items recoverable but not visible in Deleted Items
                item.move(account.trash)
                action = "moved to trash"

            self.logger.info(f"Email {message_id} {action} in mailbox: {mailbox}")

            return format_success_response(
                f"Email {action}",
                message_id=message_id,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to delete email: {e}")
            raise ToolExecutionError(f"Failed to delete email: {e}")


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
                        "description": "Destination folder (inbox, sent, drafts, deleted, junk)"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id", "destination_folder"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Move email to folder."""
        message_id = kwargs.get("message_id")
        dest_folder_name = kwargs.get("destination_folder", "").lower()
        target_mailbox = kwargs.get("target_mailbox")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get destination folder
            folder_map = {
                "inbox": account.inbox,
                "sent": account.sent,
                "drafts": account.drafts,
                "deleted": account.trash,
                "junk": account.junk
            }

            dest_folder = folder_map.get(dest_folder_name)
            if not dest_folder:
                raise ToolExecutionError(f"Unknown folder: {dest_folder_name}")

            # Find message across all folders (including custom subfolders)
            item = find_message_for_account(account, message_id)
            item.move(dest_folder)

            self.logger.info(f"Email {message_id} moved to {dest_folder_name} in mailbox: {mailbox}")

            return format_success_response(
                f"Email moved to {dest_folder_name}",
                message_id=message_id,
                mailbox=mailbox
            )

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

            # Find the message in various folders
            message = None
            source_folder_name = None

            folders_to_search = [
                ("inbox", account.inbox),
                ("sent", account.sent),
                ("drafts", account.drafts),
                ("deleted", account.trash),
                ("junk", account.junk)
            ]

            for folder_name, folder in folders_to_search:
                try:
                    message = folder.get(id=message_id)
                    if message:
                        source_folder_name = folder_name
                        break
                except Exception:
                    continue

            if not message:
                raise ToolExecutionError(f"Message not found: {message_id}")

            # Get destination folder
            if destination_folder_name:
                folder_map = {
                    "inbox": account.inbox,
                    "sent": account.sent,
                    "drafts": account.drafts,
                    "deleted": account.trash,
                    "junk": account.junk
                }

                destination_folder = folder_map.get(destination_folder_name.lower())
                if not destination_folder:
                    available_folders = list(folder_map.keys())
                    raise ToolExecutionError(
                        f"Unknown destination folder: {destination_folder_name}. "
                        f"Available folders: {', '.join(available_folders)}"
                    )
                dest_name = destination_folder_name
            else:
                # Find folder by ID
                def find_folder_by_id(parent, target_id):
                    """Recursively search for folder by ID."""
                    parent_id = ews_id_to_str(safe_get(parent, 'id', None)) or ''
                    if parent_id == target_id:
                        return parent

                    if hasattr(parent, 'children') and parent.children:
                        for child in parent.children:
                            result = find_folder_by_id(child, target_id)
                            if result:
                                return result
                    return None

                destination_folder = find_folder_by_id(account.root, destination_folder_id)
                if not destination_folder:
                    raise ToolExecutionError(f"Destination folder not found: {destination_folder_id}")
                dest_name = safe_get(destination_folder, 'name', 'Unknown')

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

            # Get the reply subject
            reply_subject = f"RE: {original_subject}" if original_subject else "RE:"

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
            # 1. User's message at top (wrapped in WordSection1 for Exclaimer signature placement)
            user_message = body if body else ""

            # 2. Format the reply headers from original email metadata
            header = format_forward_header(original_message)

            # 3. Get the original email body HTML
            original_body_html = extract_body_html(original_message)
            self.logger.info(f"Extracted original body: {len(original_body_html)} characters")

            # Clean original body - rename WordSection1 to OriginalSection
            # This prevents Exclaimer from placing signature after the original content
            original_body_html = clean_original_body_for_signature(original_body_html)

            # 4. Build headers block
            headers_html = f'''<p style="font-size:11pt;font-family:Calibri,sans-serif;">
<b>From:</b> {header['from']}<br/>
<b>Sent:</b> {header['sent']}<br/>'''
            if header['to']:
                headers_html += f'''<b>To:</b> {header['to']}<br/>'''
            if header['cc']:
                headers_html += f'''<b>Cc:</b> {header['cc']}<br/>'''
            headers_html += f'''<b>Subject:</b> {header['subject']}
</p>'''

            # 5. Construct complete body matching Outlook's exact structure
            # Exclaimer inserts signature after the closing </div> of WordSection1
            # Structure: [user message in WordSection1] → [signature inserted here] → [separator] → [headers] → [original]
            complete_body = f'''<div class="WordSection1">
<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;">{user_message}</p>
</div>
<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">
{headers_html}
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

            # Get the forward subject
            forward_subject = f"FW: {original_subject}" if original_subject else "FW:"

            # Build the complete forward body manually
            # 1. User's message at top (wrapped in WordSection1 for Exclaimer signature placement)
            user_message = body if body else ""

            # 2. Format the forward headers from original email metadata
            header = format_forward_header(original_message)

            # 3. Get the original email body HTML
            original_body_html = extract_body_html(original_message)
            self.logger.info(f"Extracted original body: {len(original_body_html)} characters")

            # Clean original body - rename WordSection1 to OriginalSection
            # This prevents Exclaimer from placing signature after the original content
            original_body_html = clean_original_body_for_signature(original_body_html)

            # 4. Build headers block
            headers_html = f'''<p style="font-size:11pt;font-family:Calibri,sans-serif;">
<b>From:</b> {header['from']}<br/>
<b>Date:</b> {header['sent']}<br/>
<b>Subject:</b> {header['subject']}<br/>'''
            if header['to']:
                headers_html += f'''<b>To:</b> {header['to']}<br/>'''
            if header['cc']:
                headers_html += f'''<b>Cc:</b> {header['cc']}<br/>'''
            headers_html += '''</p>'''

            # 5. Construct complete body matching Outlook's exact structure
            # Exclaimer inserts signature after the closing </div> of WordSection1
            # Structure: [user message in WordSection1] → [signature inserted here] → [separator] → [headers] → [original]
            complete_body = f'''<div class="WordSection1">
<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;">{user_message}</p>
</div>
<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">
{headers_html}
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
