"""Email operation tools for EWS MCP Server."""

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
from ..utils import format_success_response, safe_get, truncate_text, parse_datetime_tz_aware, find_message_across_folders, ews_id_to_str


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


async def resolve_folder(ews_client, folder_identifier: str):
    """
    Resolve folder from name, path, or ID.

    Supports:
    - Standard names: inbox, sent, drafts, deleted, junk
    - Folder paths: Inbox/CC, Inbox/Projects/2024
    - Folder IDs: AAMkADc3MWUy... (base64 encoded, may contain '/' characters)
    - Custom folder names: CC, Archive, Projects
    """
    folder_identifier = folder_identifier.strip()

    # Standard folders map (lowercase for matching)
    folder_map = {
        "inbox": ews_client.account.inbox,
        "sent": ews_client.account.sent,
        "drafts": ews_client.account.drafts,
        "deleted": ews_client.account.trash,
        "junk": ews_client.account.junk,
        "trash": ews_client.account.trash,
        "calendar": ews_client.account.calendar,
        "contacts": ews_client.account.contacts,
        "tasks": ews_client.account.tasks
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
        found_folder = find_folder_by_id(ews_client.account.root, folder_identifier)
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
            current_folder = ews_client.account.inbox

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
    custom_folder = search_folder_tree(ews_client.account.inbox, folder_identifier)
    if custom_folder:
        return custom_folder

    # Search under root as fallback
    custom_folder = search_folder_tree(ews_client.account.root, folder_identifier)
    if custom_folder:
        return custom_folder

    # If all methods fail, provide helpful error
    raise ToolExecutionError(
        f"Folder '{folder_identifier}' not found. "
        f"Available standard folders: {', '.join(folder_map.keys())}. "
        f"For custom folders, use full path (e.g., 'Inbox/CC') or get folder ID from list_folders."
    )


class SendEmailTool(BaseTool):
    """Tool for sending emails."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "send_email",
            "description": "Send an email through Exchange with optional attachments and CC/BCC",
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
                    }
                },
                "required": ["to", "subject", "body"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Send email via EWS."""
        # Validate input
        request = self.validate_input(SendEmailRequest, **kwargs)

        try:
            # Validate recipients before sending (helps catch invalid addresses early)
            all_recipients = request.to + (request.cc or []) + (request.bcc or [])
            invalid_recipients = []
            unresolved_external = []

            for recipient in all_recipients:
                try:
                    # Try to resolve the recipient via EWS
                    resolved = self.ews_client.account.protocol.resolve_names(
                        names=[recipient],
                        return_full_contact_data=False
                    )
                    # Check if resolution succeeded
                    if not resolved or not any(resolved):
                        # Recipient couldn't be resolved - determine if internal or external
                        recipient_domain = recipient.split('@')[1] if '@' in recipient else ''
                        sender_domain = self.ews_client.account.primary_smtp_address.split('@')[1]

                        if recipient_domain == sender_domain:
                            # Internal address that can't be resolved - error
                            invalid_recipients.append(recipient)
                        else:
                            # External address that can't be resolved - warning
                            unresolved_external.append(recipient)
                            self.logger.warning(f"Could not verify external recipient: {recipient}")
                except Exception as e:
                    # resolve_names failed - likely external address
                    recipient_domain = recipient.split('@')[1] if '@' in recipient else ''
                    sender_domain = self.ews_client.account.primary_smtp_address.split('@')[1]
                    if recipient_domain == sender_domain:
                        invalid_recipients.append(recipient)
                    else:
                        unresolved_external.append(recipient)
                        self.logger.warning(f"Could not validate recipient {recipient}: {e}")

            # Raise error if any internal recipients are invalid
            if invalid_recipients:
                raise ToolExecutionError(
                    f"Invalid or non-existent recipients: {', '.join(invalid_recipients)}"
                )

            # Warn user about unresolved external recipients
            if unresolved_external:
                self.logger.warning(
                    f"Warning: {len(unresolved_external)} external recipient(s) could not be verified "
                    f"and may bounce: {', '.join(unresolved_external[:3])}"
                    + ("..." if len(unresolved_external) > 3 else "")
                )

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
                    account=self.ews_client.account,
                    subject=request.subject,
                    body=HTMLBody(email_body),
                    to_recipients=[Mailbox(email_address=email) for email in request.to]
                )
                self.logger.info("Using HTMLBody for HTML content")
            else:
                message = Message(
                    account=self.ews_client.account,
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

            # Add attachments if provided (must save before sending when attachments are present)
            if request.attachments:
                for file_path in request.attachments:
                    try:
                        with open(file_path, 'rb') as f:
                            content = f.read()
                            attachment = FileAttachment(
                                name=file_path.split('/')[-1],
                                content=content
                            )
                            message.attach(attachment)
                    except Exception as e:
                        self.logger.warning(f"Failed to attach file {file_path}: {e}")

                # Save message with attachments first (required for attachments to be included)
                message.save()
                self.logger.info(f"Message saved with {len(request.attachments)} attachment(s)")

                # CRITICAL: Verify body still exists after save()
                if not message.body or len(str(message.body).strip()) == 0:
                    raise ToolExecutionError(
                        "Message body was stripped during save()! "
                        "This may indicate encoding issue or Exchange policy blocking content."
                    )
                self.logger.info(f"Body preserved after save(): {len(str(message.body))} characters")

                # Then send
                message.send()
                self.logger.info(f"Message sent with attachments to {', '.join(request.to)}")
            else:
                # Send directly if no attachments
                message.send()
                self.logger.info(f"Message sent (no attachments) to {', '.join(request.to)}")

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
                recipients=request.to
            )

        except Exception as e:
            self.logger.error(f"Failed to send email: {e}")
            raise ToolExecutionError(f"Failed to send email: {e}")


class ReadEmailsTool(BaseTool):
    """Tool for reading emails from inbox."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "read_emails",
            "description": "Read emails from a specified folder (default: inbox)",
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
                    }
                }
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Read emails from folder."""
        folder_name = kwargs.get("folder", "inbox")
        max_results = kwargs.get("max_results", 50)
        unread_only = kwargs.get("unread_only", False)

        try:
            # Get folder - supports standard names, paths, and folder IDs
            folder = await resolve_folder(self.ews_client, folder_name)
            self.logger.info(f"Resolved folder '{folder_name}' to: {safe_get(folder, 'name', folder_name)}")

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

            self.logger.info(f"Retrieved {len(emails)} emails from {folder_name}")

            return format_success_response(
                f"Retrieved {len(emails)} emails",
                emails=emails,
                total_count=len(emails),
                folder=folder_name
            )

        except Exception as e:
            self.logger.error(f"Failed to read emails: {e}")
            raise ToolExecutionError(f"Failed to read emails: {e}")


class SearchEmailsTool(BaseTool):
    """Tool for searching emails with filters."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "search_emails",
            "description": "Search emails with various filters (subject, sender, date range, etc.)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Folder to search in (standard names: inbox, sent, drafts; paths: Inbox/CC; or folder ID)",
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
                    }
                }
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Search emails with filters."""
        from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
        from exchangelib.errors import ErrorTimeoutExpired
        import socket

        try:
            # Auto-add date range to prevent timeouts in large mailboxes
            if not kwargs.get("start_date") and not kwargs.get("end_date"):
                # If no other specific filters are provided, enforce a default date range
                has_filters = (
                    kwargs.get("subject_contains") or
                    kwargs.get("from_address") or
                    kwargs.get("has_attachments") is not None or
                    kwargs.get("is_read") is not None
                )

                if not has_filters:
                    # No filters at all - default to last 30 days to prevent timeout
                    from datetime import timedelta
                    default_days_back = 30
                    auto_start_date = datetime.now() - timedelta(days=default_days_back)
                    kwargs["start_date"] = auto_start_date.isoformat()
                    self.logger.info(
                        f"No filters or date range provided. Automatically limiting search to last {default_days_back} days "
                        f"to prevent timeout. Specify start_date/end_date to search a different range."
                    )
                else:
                    # Has filters but no date range - warn but allow
                    self.logger.warning(
                        "Searching without date range may be slow for large mailboxes. "
                        "Consider adding start_date/end_date for better performance."
                    )

            # Get folder - supports standard names, paths, and folder IDs
            folder_name = kwargs.get("folder", "inbox")
            folder = await resolve_folder(self.ews_client, folder_name)
            self.logger.info(f"Resolved folder '{folder_name}' to: {safe_get(folder, 'name', folder_name)}")

            # Build query
            query = folder.all()

            # Apply filters
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

            # Order and limit
            query = query.order_by('-datetime_received')
            max_results = kwargs.get("max_results", 50)

            # Retry wrapper for EWS query execution
            @retry(
                stop=stop_after_attempt(2),
                wait=wait_exponential(multiplier=2, min=4, max=10),
                retry=retry_if_exception_type((ErrorTimeoutExpired, socket.timeout))
            )
            def execute_query():
                """Execute EWS query with retry logic."""
                results = []
                for item in query[:max_results]:
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
                    results.append(email_data)
                return results

            # Execute with retry
            emails = execute_query()

            self.logger.info(f"Found {len(emails)} emails matching search criteria")

            return format_success_response(
                f"Found {len(emails)} matching emails",
                emails=emails,
                total_count=len(emails)
            )

        except (ErrorTimeoutExpired, socket.timeout) as e:
            self.logger.error(f"Search timed out: {e}")
            # Provide helpful error message with suggestions
            error_msg = (
                f"Search timed out. Try these optimizations:\n"
                f"1. Add a date range (start_date and end_date)\n"
                f"2. Reduce max_results (currently {kwargs.get('max_results', 50)})\n"
                f"3. Add more specific filters\n"
                f"4. Increase REQUEST_TIMEOUT in .env (current: {self.ews_client.config.request_timeout}s)\n"
                f"Example: search_emails(subject_contains='Re: xxx', start_date='2024-11-01', max_results=20)"
            )
            raise ToolExecutionError(error_msg)
        except Exception as e:
            self.logger.error(f"Failed to search emails: {e}")
            raise ToolExecutionError(f"Failed to search emails: {e}")


class GetEmailDetailsTool(BaseTool):
    """Tool for getting full email details."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_email_details",
            "description": "Get full details of a specific email by ID",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Email message ID"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Get email details."""
        message_id = kwargs.get("message_id")

        try:
            # Find message across all folders (including custom subfolders)
            item = find_message_across_folders(self.ews_client, message_id)

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
                email=email_details
            )

        except Exception as e:
            self.logger.error(f"Failed to get email details: {e}")
            raise ToolExecutionError(f"Failed to get email details: {e}")


class DeleteEmailTool(BaseTool):
    """Tool for deleting emails."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_email",
            "description": "Delete an email by ID (moves to trash)",
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
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Delete email."""
        message_id = kwargs.get("message_id")
        permanent = kwargs.get("permanent", False)

        try:
            # Find message across all folders (including custom subfolders)
            item = find_message_across_folders(self.ews_client, message_id)

            if permanent:
                item.delete()
                action = "permanently deleted"
            else:
                # Move to trash folder (Deleted Items) so user can recover
                # Note: soft_delete() makes items recoverable but not visible in Deleted Items
                item.move(self.ews_client.account.trash)
                action = "moved to trash"

            self.logger.info(f"Email {message_id} {action}")

            return format_success_response(
                f"Email {action}",
                message_id=message_id
            )

        except Exception as e:
            self.logger.error(f"Failed to delete email: {e}")
            raise ToolExecutionError(f"Failed to delete email: {e}")


class MoveEmailTool(BaseTool):
    """Tool for moving emails between folders."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "move_email",
            "description": "Move an email to a different folder",
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
                    }
                },
                "required": ["message_id", "destination_folder"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Move email to folder."""
        message_id = kwargs.get("message_id")
        dest_folder_name = kwargs.get("destination_folder", "").lower()

        try:
            # Get destination folder
            folder_map = {
                "inbox": self.ews_client.account.inbox,
                "sent": self.ews_client.account.sent,
                "drafts": self.ews_client.account.drafts,
                "deleted": self.ews_client.account.trash,
                "junk": self.ews_client.account.junk
            }

            dest_folder = folder_map.get(dest_folder_name)
            if not dest_folder:
                raise ToolExecutionError(f"Unknown folder: {dest_folder_name}")

            # Find message across all folders (including custom subfolders)
            item = find_message_across_folders(self.ews_client, message_id)
            item.move(dest_folder)

            self.logger.info(f"Email {message_id} moved to {dest_folder_name}")

            return format_success_response(
                f"Email moved to {dest_folder_name}",
                message_id=message_id
            )

        except Exception as e:
            self.logger.error(f"Failed to move email: {e}")
            raise ToolExecutionError(f"Failed to move email: {e}")


class UpdateEmailTool(BaseTool):
    """Tool for updating email properties."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "update_email",
            "description": "Update email properties (read status, flags, categories, importance)",
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
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Update email properties."""
        message_id = kwargs.get("message_id")

        if not message_id:
            raise ToolExecutionError("message_id is required")

        try:
            # Find the message across common folders
            message = None
            folders_to_search = [
                self.ews_client.account.inbox,
                self.ews_client.account.sent,
                self.ews_client.account.drafts
            ]

            for folder in folders_to_search:
                try:
                    message = folder.get(id=message_id)
                    if message:
                        break
                except Exception:
                    continue

            if not message:
                raise ToolExecutionError(f"Message not found: {message_id}")

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

            self.logger.info(f"Email {message_id} updated: {updates}")

            return format_success_response(
                "Email updated successfully",
                message_id=message_id,
                updates=updates
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
            "description": "Copy an email to another folder (keeping original in current location)",
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

        if not message_id:
            raise ToolExecutionError("message_id is required")

        if not destination_folder_name and not destination_folder_id:
            raise ToolExecutionError("Either destination_folder or destination_folder_id is required")

        try:
            # Find the message in various folders
            message = None
            source_folder_name = None

            folders_to_search = [
                ("inbox", self.ews_client.account.inbox),
                ("sent", self.ews_client.account.sent),
                ("drafts", self.ews_client.account.drafts),
                ("deleted", self.ews_client.account.trash),
                ("junk", self.ews_client.account.junk)
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
                    "inbox": self.ews_client.account.inbox,
                    "sent": self.ews_client.account.sent,
                    "drafts": self.ews_client.account.drafts,
                    "deleted": self.ews_client.account.trash,
                    "junk": self.ews_client.account.junk
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

                destination_folder = find_folder_by_id(self.ews_client.account.root, destination_folder_id)
                if not destination_folder:
                    raise ToolExecutionError(f"Destination folder not found: {destination_folder_id}")
                dest_name = safe_get(destination_folder, 'name', 'Unknown')

            # Copy the message (exchangelib uses .copy() method)
            copied_message = message.copy(to_folder=destination_folder)

            subject = safe_get(message, 'subject', 'No Subject')

            self.logger.info(f"Copied email '{subject}' from {source_folder_name} to {dest_name}")

            return format_success_response(
                f"Email copied from {source_folder_name} to {dest_name}",
                message_id=message_id,
                copied_message_id=ews_id_to_str(safe_get(copied_message, 'id', None)) or '' if copied_message else '',
                subject=subject,
                source_folder=source_folder_name,
                destination_folder=dest_name
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
            "description": "Reply to an existing email while preserving the conversation thread. Uses Exchange's built-in reply mechanism to maintain In-Reply-To headers, conversation ID, and thread relationship.",
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

        if not message_id:
            raise ToolExecutionError("message_id is required")
        if not body:
            raise ToolExecutionError("body is required")

        try:
            # Find the original message across folders
            original_message = find_message_across_folders(self.ews_client, message_id)

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

            # Create reply using exchangelib's built-in methods
            # This automatically preserves conversation ID, In-Reply-To, and References headers
            if reply_all:
                reply = original_message.create_reply_all(subject=None, body=None, to_recipients=None)
                self.logger.info("Creating reply-all message")
            else:
                reply = original_message.create_reply(subject=None, body=None, to_recipients=None)
                self.logger.info("Creating reply message")

            # Detect if body is HTML or plain text
            is_html = bool(re.search(r'<[^>]+>', body))

            # Format the reply body with quoted original message
            original_date = safe_get(original_message, "datetime_sent", None)
            date_str = original_date.strftime("%A, %B %d, %Y %I:%M %p") if original_date else "Unknown Date"

            # Build recipients string for quote header
            to_str = "; ".join(original_to) if original_to else ""
            cc_str = "; ".join(original_cc) if original_cc else ""

            # Get original body
            original_body_html = str(safe_get(original_message, "body", "") or "")

            # Build the complete reply body with quote
            if is_html:
                # HTML reply format
                quote_header = f"""
<hr style="border: none; border-top: 1px solid #ccc; margin: 20px 0;">
<p style="margin: 0; color: #666;">
<strong>From:</strong> {original_from_email}<br>
<strong>Sent:</strong> {date_str}<br>
<strong>To:</strong> {to_str}<br>"""
                if cc_str:
                    quote_header += f"""<strong>Cc:</strong> {cc_str}<br>"""
                quote_header += f"""<strong>Subject:</strong> {original_subject}
</p>
<br>
"""
                complete_body = f"{body}{quote_header}{original_body_html}"
                reply.body = HTMLBody(complete_body)
                self.logger.info("Using HTMLBody for HTML reply content")
            else:
                # Plain text reply format
                separator = "\n\n" + "─" * 40 + "\n"
                quote_header = f"From: {original_from_email}\n"
                quote_header += f"Sent: {date_str}\n"
                quote_header += f"To: {to_str}\n"
                if cc_str:
                    quote_header += f"Cc: {cc_str}\n"
                quote_header += f"Subject: {original_subject}\n\n"

                # Use text_body for plain text
                original_text = safe_get(original_message, "text_body", "") or ""
                complete_body = f"{body}{separator}{quote_header}{original_text}"
                reply.body = Body(complete_body)
                self.logger.info("Using Body (plain text) for reply content")

            # Add attachments if provided
            if attachments:
                for file_path in attachments:
                    try:
                        with open(file_path, 'rb') as f:
                            content = f.read()
                            attachment = FileAttachment(
                                name=file_path.split('/')[-1],
                                content=content
                            )
                            reply.attach(attachment)
                            self.logger.info(f"Attached file: {file_path}")
                    except FileNotFoundError:
                        raise ToolExecutionError(f"Attachment file not found: {file_path}")
                    except PermissionError:
                        raise ToolExecutionError(f"Permission denied reading attachment: {file_path}")
                    except Exception as e:
                        self.logger.warning(f"Failed to attach file {file_path}: {e}")

            # Send the reply
            reply.send()
            self.logger.info(f"Reply sent successfully to {original_from_email}")

            # Determine who the reply was sent to
            reply_to_list = []
            if reply_all:
                reply_to_list = [original_from_email] + [e for e in original_to + original_cc
                                                        if e != self.ews_client.account.primary_smtp_address]
            else:
                reply_to_list = [original_from_email]

            return format_success_response(
                "Reply sent successfully",
                original_subject=original_subject,
                reply_to=reply_to_list,
                reply_all=reply_all,
                attachments_count=len(attachments) if attachments else 0
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
            "description": "Forward an existing email to new recipients while preserving the original content, formatting, and attachments.",
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

        if not message_id:
            raise ToolExecutionError("message_id is required")
        if not to_recipients:
            raise ToolExecutionError("to recipients are required")

        try:
            # Find the original message across folders
            original_message = find_message_across_folders(self.ews_client, message_id)

            # Get original message details
            original_subject = safe_get(original_message, "subject", "") or ""
            original_sender = safe_get(original_message, "sender", None)
            original_from_email = ""
            original_from_name = ""
            if original_sender:
                original_from_email = original_sender.email_address or "" if hasattr(original_sender, "email_address") else ""
                original_from_name = original_sender.name or "" if hasattr(original_sender, "name") else ""

            # Get original recipients
            original_to = [r.email_address for r in (safe_get(original_message, "to_recipients", []) or [])
                          if r and hasattr(r, "email_address") and r.email_address]
            original_cc = [r.email_address for r in (safe_get(original_message, "cc_recipients", []) or [])
                         if r and hasattr(r, "email_address") and r.email_address]

            # Get original date
            original_date = safe_get(original_message, "datetime_sent", None)
            date_str = original_date.strftime("%A, %B %d, %Y %I:%M %p") if original_date else "Unknown Date"

            # Create forward using exchangelib's built-in method
            forward = original_message.create_forward(subject=None, body=None, to_recipients=None)
            self.logger.info("Creating forward message")

            # Set recipients
            forward.to_recipients = [Mailbox(email_address=email) for email in to_recipients]
            if cc_recipients:
                forward.cc_recipients = [Mailbox(email_address=email) for email in cc_recipients]
            if bcc_recipients:
                forward.bcc_recipients = [Mailbox(email_address=email) for email in bcc_recipients]

            # Build recipients string for forward header
            to_str = "; ".join(original_to) if original_to else ""
            cc_str = "; ".join(original_cc) if original_cc else ""

            # Get original body
            original_body_html = str(safe_get(original_message, "body", "") or "")

            # Detect if user's message body is HTML
            is_html = bool(re.search(r'<[^>]+>', body)) if body else False
            # Also check if original body is HTML
            is_original_html = bool(re.search(r'<[^>]+>', original_body_html))

            # Format display name
            from_display = f"{original_from_name} <{original_from_email}>" if original_from_name else original_from_email

            # Build the complete forward body
            if is_html or is_original_html:
                # HTML forward format
                forward_header = f"""
<hr style="border: none; border-top: 1px solid #ccc; margin: 20px 0;">
<p style="margin: 0; color: #666; font-family: Arial, sans-serif;">
---------- Forwarded message ----------<br>
<strong>From:</strong> {from_display}<br>
<strong>Sent:</strong> {date_str}<br>
<strong>To:</strong> {to_str}<br>"""
                if cc_str:
                    forward_header += f"""<strong>Cc:</strong> {cc_str}<br>"""
                forward_header += f"""<strong>Subject:</strong> {original_subject}
</p>
<br>
"""
                user_message = f"<div>{body}</div>" if body else ""
                complete_body = f"{user_message}{forward_header}{original_body_html}"
                forward.body = HTMLBody(complete_body)
                self.logger.info("Using HTMLBody for HTML forward content")
            else:
                # Plain text forward format
                separator = "\n\n" + "─" * 40 + "\n"
                forward_header = "---------- Forwarded message ----------\n"
                forward_header += f"From: {from_display}\n"
                forward_header += f"Sent: {date_str}\n"
                forward_header += f"To: {to_str}\n"
                if cc_str:
                    forward_header += f"Cc: {cc_str}\n"
                forward_header += f"Subject: {original_subject}\n\n"

                # Use text_body for plain text
                original_text = safe_get(original_message, "text_body", "") or ""
                user_message = f"{body}\n" if body else ""
                complete_body = f"{user_message}{separator}{forward_header}{original_text}"
                forward.body = Body(complete_body)
                self.logger.info("Using Body (plain text) for forward content")

            # Count original attachments that will be included
            original_attachments = safe_get(original_message, "attachments", []) or []
            original_attachment_count = len([att for att in original_attachments
                                            if att and hasattr(att, "name") and att.name])

            # The create_forward method automatically includes original attachments
            self.logger.info(f"Forward will include {original_attachment_count} original attachment(s)")

            # Add additional attachments if provided
            if additional_attachments:
                for file_path in additional_attachments:
                    try:
                        with open(file_path, 'rb') as f:
                            content = f.read()
                            attachment = FileAttachment(
                                name=file_path.split('/')[-1],
                                content=content
                            )
                            forward.attach(attachment)
                            self.logger.info(f"Attached additional file: {file_path}")
                    except FileNotFoundError:
                        raise ToolExecutionError(f"Attachment file not found: {file_path}")
                    except PermissionError:
                        raise ToolExecutionError(f"Permission denied reading attachment: {file_path}")
                    except Exception as e:
                        self.logger.warning(f"Failed to attach file {file_path}: {e}")

            # Send the forward
            forward.send()
            self.logger.info(f"Email forwarded successfully to {', '.join(to_recipients)}")

            return format_success_response(
                "Email forwarded successfully",
                original_subject=original_subject,
                forwarded_to=to_recipients,
                cc=cc_recipients if cc_recipients else None,
                bcc=bcc_recipients if bcc_recipients else None,
                attachments_included=original_attachment_count,
                additional_attachments=len(additional_attachments) if additional_attachments else 0
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to forward email: {e}")
            raise ToolExecutionError(f"Failed to forward email: {e}")
