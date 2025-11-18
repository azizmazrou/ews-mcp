"""Email Message model for EWS MCP v3.0."""

from pydantic import BaseModel, Field
from typing import Optional, List, Any
from datetime import datetime
from enum import Enum


class MessageImportance(str, Enum):
    """Email importance levels."""
    LOW = "Low"
    NORMAL = "Normal"
    HIGH = "High"


class MessageSensitivity(str, Enum):
    """Email sensitivity levels."""
    NORMAL = "Normal"
    PERSONAL = "Personal"
    PRIVATE = "Private"
    CONFIDENTIAL = "Confidential"


class EmailRecipient(BaseModel):
    """Email recipient information."""
    name: str
    email: str
    type: str = "to"  # to, cc, bcc


class EmailMessage(BaseModel):
    """
    Email message entity.

    Represents a single email message with full metadata.
    """

    # Core identity
    message_id: str = Field(..., description="Unique message ID")
    conversation_id: Optional[str] = Field(None, description="Thread/conversation ID")
    in_reply_to: Optional[str] = Field(None, description="ID of message being replied to")
    references: List[str] = Field(
        default_factory=list,
        description="Message IDs in thread history"
    )

    # Message content
    subject: str = Field(..., description="Email subject")
    body: str = Field(..., description="Email body (HTML or plain text)")
    body_type: str = Field("HTML", description="Body format (HTML or Text)")
    text_body: Optional[str] = Field(None, description="Plain text version of body")
    preview: Optional[str] = Field(None, description="Short preview text")

    # Participants
    sender: EmailRecipient = Field(..., description="Message sender")
    to_recipients: List[EmailRecipient] = Field(
        default_factory=list,
        description="To recipients"
    )
    cc_recipients: List[EmailRecipient] = Field(
        default_factory=list,
        description="CC recipients"
    )
    bcc_recipients: List[EmailRecipient] = Field(
        default_factory=list,
        description="BCC recipients"
    )

    # Metadata
    datetime_sent: Optional[datetime] = Field(None, description="When message was sent")
    datetime_received: Optional[datetime] = Field(None, description="When message was received")
    datetime_created: Optional[datetime] = Field(None, description="When message was created")
    importance: MessageImportance = Field(MessageImportance.NORMAL, description="Importance level")
    sensitivity: MessageSensitivity = Field(MessageSensitivity.NORMAL, description="Sensitivity level")

    # State
    is_read: bool = Field(False, description="Read status")
    is_draft: bool = Field(False, description="Draft status")
    has_attachments: bool = Field(False, description="Has attachments")

    # Attachments
    attachment_count: int = Field(0, description="Number of attachments")
    attachment_names: List[str] = Field(
        default_factory=list,
        description="Names of attachments"
    )

    # Folder location
    folder_name: Optional[str] = Field(None, description="Folder containing message")

    @property
    def all_recipients(self) -> List[EmailRecipient]:
        """Get all recipients (to + cc + bcc)."""
        return self.to_recipients + self.cc_recipients + self.bcc_recipients

    @property
    def is_reply(self) -> bool:
        """Check if this is a reply message."""
        return bool(self.in_reply_to) or bool(self.references)

    @property
    def participant_emails(self) -> List[str]:
        """Get all participant email addresses."""
        emails = [self.sender.email]
        emails.extend(r.email for r in self.all_recipients)
        return list(set(emails))  # Deduplicate

    @classmethod
    def from_ews_message(cls, message: Any) -> "EmailMessage":
        """
        Create EmailMessage from EWS message object.

        Args:
            message: exchangelib Message object
        """
        from ..utils import safe_get

        # Extract message ID
        message_id = safe_get(message, "id", safe_get(message, "message_id", ""))

        # Extract conversation ID
        conversation_id = safe_get(message, "conversation_id")
        if hasattr(conversation_id, "id"):
            conversation_id = conversation_id.id

        # Extract sender
        sender_obj = safe_get(message, "sender")
        sender = EmailRecipient(
            name=safe_get(sender_obj, "name", "Unknown") if sender_obj else "Unknown",
            email=safe_get(sender_obj, "email_address", "unknown@example.com") if sender_obj else "unknown@example.com",
            type="from"
        )

        # Extract recipients
        to_recipients = []
        for recipient in safe_get(message, "to_recipients", []) or []:
            to_recipients.append(EmailRecipient(
                name=safe_get(recipient, "name", ""),
                email=safe_get(recipient, "email_address", ""),
                type="to"
            ))

        cc_recipients = []
        for recipient in safe_get(message, "cc_recipients", []) or []:
            cc_recipients.append(EmailRecipient(
                name=safe_get(recipient, "name", ""),
                email=safe_get(recipient, "email_address", ""),
                type="cc"
            ))

        bcc_recipients = []
        for recipient in safe_get(message, "bcc_recipients", []) or []:
            bcc_recipients.append(EmailRecipient(
                name=safe_get(recipient, "name", ""),
                email=safe_get(recipient, "email_address", ""),
                type="bcc"
            ))

        # Extract body
        body = safe_get(message, "body", "")
        text_body = safe_get(message, "text_body", "")

        # Determine body type
        body_type = "HTML" if safe_get(message, "body_type", "HTML") == "HTML" else "Text"

        # Extract attachments
        attachments = safe_get(message, "attachments", []) or []
        attachment_names = [safe_get(att, "name", "") for att in attachments]

        # Extract importance and sensitivity
        importance_str = str(safe_get(message, "importance", "Normal"))
        try:
            importance = MessageImportance(importance_str)
        except ValueError:
            importance = MessageImportance.NORMAL

        sensitivity_str = str(safe_get(message, "sensitivity", "Normal"))
        try:
            sensitivity = MessageSensitivity(sensitivity_str)
        except ValueError:
            sensitivity = MessageSensitivity.NORMAL

        return cls(
            message_id=message_id,
            conversation_id=conversation_id,
            in_reply_to=safe_get(message, "in_reply_to"),
            references=safe_get(message, "references", []) or [],
            subject=safe_get(message, "subject", "(No Subject)"),
            body=body,
            body_type=body_type,
            text_body=text_body,
            preview=text_body[:200] if text_body else "",
            sender=sender,
            to_recipients=to_recipients,
            cc_recipients=cc_recipients,
            bcc_recipients=bcc_recipients,
            datetime_sent=safe_get(message, "datetime_sent"),
            datetime_received=safe_get(message, "datetime_received"),
            datetime_created=safe_get(message, "datetime_created"),
            importance=importance,
            sensitivity=sensitivity,
            is_read=safe_get(message, "is_read", False),
            is_draft=safe_get(message, "is_draft", False),
            has_attachments=len(attachments) > 0,
            attachment_count=len(attachments),
            attachment_names=attachment_names,
            folder_name=safe_get(message, "folder", {}).get("name") if hasattr(safe_get(message, "folder"), "get") else None
        )
