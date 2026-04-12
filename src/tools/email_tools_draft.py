"""Draft email tools for EWS MCP Server."""
import os
import re
from typing import Any, Dict
from datetime import datetime
from exchangelib import Message, Mailbox, FileAttachment, HTMLBody, Body

from .base import BaseTool
from .email_tools import (
    extract_body_html,
    clean_original_body_for_signature,
    format_forward_header,
    copy_attachments_to_message,
)
from ..models import SendEmailRequest
from ..exceptions import ToolExecutionError
from ..utils import (
    format_success_response,
    safe_get,
    find_message_for_account,
    ews_id_to_str,
    attach_inline_files,
    INLINE_ATTACHMENTS_SCHEMA,
)


class CreateDraftTool(BaseTool):
    """Tool for creating draft emails in the Drafts folder."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "create_draft",
            "description": "Create a draft email in the Drafts folder for review before sending. The draft appears in OWA/Outlook and can be edited and sent manually.",
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
                        "description": "Email address to create draft on behalf of (requires impersonation/delegate access)"
                    }
                },
                "required": ["to", "subject", "body"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Create draft email via EWS and save to Drafts folder."""
        target_mailbox = kwargs.pop("target_mailbox", None)
        request = self.validate_input(SendEmailRequest, **kwargs)

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            email_body = request.body.strip()

            # Strip CDATA wrapper if present
            if email_body.startswith('<![CDATA[') and email_body.endswith(']]>'):
                email_body = email_body[9:-3].strip()

            if not email_body:
                raise ToolExecutionError("Email body is empty after processing")

            is_html = bool(re.search(r'<[^>]+>', email_body))

            # Create message with appropriate body type
            if is_html:
                message = Message(
                    account=account,
                    subject=request.subject,
                    body=HTMLBody(email_body),
                    to_recipients=[Mailbox(email_address=email) for email in request.to],
                    folder=account.drafts,
                )
            else:
                message = Message(
                    account=account,
                    subject=request.subject,
                    body=Body(email_body),
                    to_recipients=[Mailbox(email_address=email) for email in request.to],
                    folder=account.drafts,
                )

            # Add CC/BCC
            if request.cc:
                message.cc_recipients = [Mailbox(email_address=email) for email in request.cc]
            if request.bcc:
                message.bcc_recipients = [Mailbox(email_address=email) for email in request.bcc]

            # Set importance
            message.importance = request.importance.value

            # Add file attachments
            attachment_count = 0
            if request.attachments:
                for file_path in request.attachments:
                    try:
                        file_name = os.path.basename(file_path)
                        with open(file_path, 'rb') as f:
                            content = f.read()
                            attachment = FileAttachment(name=file_name, content=content)
                            message.attach(attachment)
                            attachment_count += 1
                    except FileNotFoundError:
                        raise ToolExecutionError(f"Attachment file not found: {file_path}")
                    except Exception as e:
                        raise ToolExecutionError(f"Failed to attach file {file_path}: {e}")

            # Add inline attachments
            inline_count = attach_inline_files(message, kwargs.get("inline_attachments", []))
            attachment_count += inline_count

            # Save as draft instead of sending
            message.save()

            self.logger.info(f"Draft saved for {', '.join(request.to)} with {attachment_count} attachment(s)")

            return format_success_response(
                "Draft created successfully — check your Drafts folder in OWA/Outlook",
                message_id=ews_id_to_str(message.id) if hasattr(message, 'id') else None,
                created_time=datetime.now().isoformat(),
                recipients=request.to,
                subject=request.subject,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to create draft: {e}")
            raise ToolExecutionError(f"Failed to create draft: {e}")


class CreateReplyDraftTool(BaseTool):
    """Tool for creating reply drafts in the Drafts folder."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "create_reply_draft",
            "description": "Create a reply draft in the Drafts folder for review before sending.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Exchange message ID of the email to reply to"
                    },
                    "body": {
                        "type": "string",
                        "description": "Optional reply body to include in the draft"
                    },
                    "reply_all": {
                        "type": "boolean",
                        "description": "If true, create a reply-all draft; otherwise create a reply draft",
                        "default": False
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths to attach to the draft reply (optional)"
                    },
                    **INLINE_ATTACHMENTS_SCHEMA,
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to create the draft on behalf of (requires impersonation/delegate access)"
                    }
                },
                "required": ["message_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Create a reply draft via EWS and save it to Drafts."""
        message_id = kwargs.get("message_id")
        reply_all = kwargs.get("reply_all", False)
        attachments = kwargs.get("attachments", [])
        target_mailbox = kwargs.get("target_mailbox")
        body = (kwargs.get("body") or "").strip()

        if not message_id:
            raise ToolExecutionError("message_id is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            original_message = find_message_for_account(account, message_id)
            original_subject = safe_get(original_message, "subject", "") or ""
            reply_subject = f"RE: {original_subject}" if original_subject else "RE:"
            original_sender = safe_get(original_message, "sender", None)
            original_from_email = ""
            if original_sender and hasattr(original_sender, "email_address"):
                original_from_email = original_sender.email_address or ""

            original_to = [
                r.email_address
                for r in (safe_get(original_message, "to_recipients", []) or [])
                if r and hasattr(r, "email_address") and r.email_address
            ]
            original_cc = [
                r.email_address
                for r in (safe_get(original_message, "cc_recipients", []) or [])
                if r and hasattr(r, "email_address") and r.email_address
            ]

            if reply_all:
                seen = set()
                reply_to_recipients = []
                for email in [original_from_email] + original_to + original_cc:
                    if not email or email == account.primary_smtp_address or email in seen:
                        continue
                    seen.add(email)
                    reply_to_recipients.append(Mailbox(email_address=email))
            else:
                reply_to_recipients = [Mailbox(email_address=original_from_email)]

            header = format_forward_header(original_message)
            original_body_html = extract_body_html(original_message)
            original_body_html = clean_original_body_for_signature(original_body_html)

            headers_html = f'''<p style="font-size:11pt;font-family:Calibri,sans-serif;">
<b>From:</b> {header['from']}<br/>
<b>Sent:</b> {header['sent']}<br/>'''
            if header["to"]:
                headers_html += f'''<b>To:</b> {header['to']}<br/>'''
            if header["cc"]:
                headers_html += f'''<b>Cc:</b> {header['cc']}<br/>'''
            headers_html += f'''<b>Subject:</b> {header['subject']}
</p>'''

            complete_body = f'''<div class="WordSection1">
<p class="MsoNormal" style="font-size:11pt;font-family:Calibri,sans-serif;">{body}</p>
</div>
<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">
{headers_html}
</div>
{original_body_html}'''

            message = Message(
                account=account,
                subject=reply_subject,
                body=HTMLBody(complete_body),
                to_recipients=reply_to_recipients,
                folder=account.drafts,
            )

            inline_count, _ = copy_attachments_to_message(original_message, message)
            attachment_count = 0

            for file_path in attachments:
                try:
                    file_name = os.path.basename(file_path)
                    with open(file_path, "rb") as f:
                        content = f.read()
                    message.attach(FileAttachment(name=file_name, content=content))
                    attachment_count += 1
                except FileNotFoundError:
                    raise ToolExecutionError(f"Attachment file not found: {file_path}")
                except PermissionError:
                    raise ToolExecutionError(f"Permission denied reading attachment: {file_path}")
                except Exception as e:
                    raise ToolExecutionError(f"Failed to attach file {file_path}: {e}")

            inline_b64_count = attach_inline_files(message, kwargs.get("inline_attachments", []))
            attachment_count += inline_b64_count
            attachment_count += inline_count

            message.save()
            draft_message_id = ews_id_to_str(message.id)
            reply_to = header.get("from", "")

            self.logger.info(f"Reply draft saved for message {message_id} in mailbox: {mailbox}")

            return format_success_response(
                "Reply draft created successfully - check your Drafts folder in OWA/Outlook",
                message_id=draft_message_id,
                original_message_id=message_id,
                original_subject=original_subject,
                reply_subject=reply_subject,
                reply_to=reply_to,
                reply_all=reply_all,
                attachments_count=attachment_count,
                inline_attachments_preserved=inline_count,
                created_time=datetime.now().isoformat(),
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to create reply draft: {e}")
            raise ToolExecutionError(f"Failed to create reply draft: {e}")
