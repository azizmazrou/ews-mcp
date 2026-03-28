"""Draft email tool for EWS MCP Server."""
import os
import re
from typing import Any, Dict
from datetime import datetime
from exchangelib import Message, Mailbox, FileAttachment, HTMLBody, Body

from .base import BaseTool
from ..models import SendEmailRequest
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, ews_id_to_str, attach_inline_files, INLINE_ATTACHMENTS_SCHEMA


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
