"""EmailService - Email operations for EWS MCP v3.0."""

import logging
from typing import List, Optional
from datetime import datetime

from exchangelib import Message, HTMLBody, FileAttachment
from exchangelib.properties import Body

from ..core.email_message import EmailMessage, MessageImportance, MessageSensitivity
from ..utils import safe_get


class EmailService:
    """
    Service for email operations.

    Handles sending, searching, and retrieving emails.
    """

    def __init__(self, ews_client):
        """
        Initialize EmailService.

        Args:
            ews_client: EWSClient instance
        """
        self.ews_client = ews_client
        self.logger = logging.getLogger(__name__)

    async def send_email(
        self,
        to: List[str],
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        body_type: str = "HTML",
        importance: MessageImportance = MessageImportance.NORMAL,
        attachments: Optional[List[str]] = None,
        conversation_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[List[str]] = None
    ) -> str:
        """
        Send an email message.

        Args:
            to: To recipients
            subject: Email subject
            body: Email body
            cc: CC recipients
            bcc: BCC recipients
            body_type: HTML or Text
            importance: Importance level
            attachments: File paths to attach
            conversation_id: Thread/conversation ID
            in_reply_to: Message ID being replied to
            references: Thread references

        Returns:
            Message ID of sent message
        """
        self.logger.info(f"Sending email to: {to}")

        try:
            # Create message
            message = Message(
                account=self.ews_client.account,
                folder=self.ews_client.account.sent,
                subject=subject,
                to_recipients=to,
            )

            # Set body
            if body_type.upper() == "HTML":
                message.body = HTMLBody(body)
            else:
                message.body = Body(body)

            # Set optional fields
            if cc:
                message.cc_recipients = cc
            if bcc:
                message.bcc_recipients = bcc

            message.importance = importance.value

            # Thread preservation
            if conversation_id:
                message.conversation_id = conversation_id
            if in_reply_to:
                message.in_reply_to = in_reply_to
            if references:
                message.references = references

            # Add attachments
            if attachments:
                for file_path in attachments:
                    with open(file_path, 'rb') as f:
                        content = f.read()
                        filename = file_path.split('/')[-1]
                        attachment = FileAttachment(
                            name=filename,
                            content=content
                        )
                        message.attach(attachment)

            # Send message
            message.send()

            message_id = message.id if hasattr(message, 'id') else str(message.message_id) if hasattr(message, 'message_id') else "unknown"

            self.logger.info(f"Email sent successfully: {message_id}")

            return message_id

        except Exception as e:
            self.logger.error(f"Failed to send email: {e}")
            raise

    async def get_message(self, message_id: str) -> Optional[EmailMessage]:
        """
        Get a specific message by ID.

        Args:
            message_id: Message ID

        Returns:
            EmailMessage object or None
        """
        try:
            # Search in multiple folders
            for folder_name in ['inbox', 'sent', 'drafts', 'deleted']:
                folder = getattr(self.ews_client.account, folder_name)
                try:
                    message = folder.get(id=message_id)
                    if message:
                        return EmailMessage.from_ews_message(message)
                except:
                    continue

            return None

        except Exception as e:
            self.logger.error(f"Failed to get message: {e}")
            return None
