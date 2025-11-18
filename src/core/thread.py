"""Conversation Thread model for EWS MCP v3.0."""

from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

from .email_message import EmailMessage, EmailRecipient


class ThreadMessage(BaseModel):
    """
    Simplified message representation for thread display.
    """
    message_id: str
    subject: str
    sender: EmailRecipient
    datetime_sent: Optional[datetime]
    preview: str
    is_read: bool
    has_attachments: bool


class ConversationThread(BaseModel):
    """
    Email conversation thread.

    Represents a complete email conversation with all messages.
    """

    # Core identity
    conversation_id: str = Field(..., description="Unique conversation ID")
    subject: str = Field(..., description="Thread subject")

    # Messages
    messages: List[EmailMessage] = Field(
        default_factory=list,
        description="All messages in chronological order"
    )

    # Participants
    participants: List[EmailRecipient] = Field(
        default_factory=list,
        description="All people involved in thread"
    )

    # Timeline
    started: Optional[datetime] = Field(None, description="When thread started")
    last_activity: Optional[datetime] = Field(None, description="Most recent message")
    message_count: int = Field(0, description="Number of messages")

    # State
    is_read: bool = Field(True, description="All messages read")
    has_unread: bool = Field(False, description="Has unread messages")

    @property
    def duration_days(self) -> Optional[int]:
        """Get thread duration in days."""
        if self.started and self.last_activity:
            return (self.last_activity - self.started).days
        return None

    @property
    def participant_count(self) -> int:
        """Get number of unique participants."""
        return len(self.participants)

    @property
    def latest_message(self) -> Optional[EmailMessage]:
        """Get most recent message."""
        if self.messages:
            return self.messages[-1]
        return None

    @property
    def first_message(self) -> Optional[EmailMessage]:
        """Get first message in thread."""
        if self.messages:
            return self.messages[0]
        return None

    def get_summary(self) -> dict:
        """Get thread summary for display."""
        return {
            "conversation_id": self.conversation_id,
            "subject": self.subject,
            "message_count": self.message_count,
            "participant_count": self.participant_count,
            "started": self.started.isoformat() if self.started else None,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "duration_days": self.duration_days,
            "has_unread": self.has_unread,
            "latest_sender": self.latest_message.sender.name if self.latest_message else None,
            "latest_preview": self.latest_message.preview if self.latest_message else None,
        }

    def add_message(self, message: EmailMessage) -> None:
        """Add a message to the thread."""
        self.messages.append(message)
        self.message_count = len(self.messages)

        # Update timeline
        if message.datetime_sent:
            if not self.started or message.datetime_sent < self.started:
                self.started = message.datetime_sent
            if not self.last_activity or message.datetime_sent > self.last_activity:
                self.last_activity = message.datetime_sent

        # Update read status
        if not message.is_read:
            self.has_unread = True
            self.is_read = False

        # Update participants
        existing_emails = {p.email for p in self.participants}

        # Add sender
        if message.sender.email not in existing_emails:
            self.participants.append(message.sender)
            existing_emails.add(message.sender.email)

        # Add recipients
        for recipient in message.all_recipients:
            if recipient.email not in existing_emails:
                self.participants.append(recipient)
                existing_emails.add(recipient.email)

    def sort_messages_chronologically(self) -> None:
        """Sort messages by sent date."""
        self.messages.sort(
            key=lambda m: m.datetime_sent or datetime.min
        )

    @classmethod
    def from_messages(
        cls,
        conversation_id: str,
        messages: List[EmailMessage]
    ) -> "ConversationThread":
        """
        Create thread from list of messages.

        Args:
            conversation_id: Conversation ID
            messages: List of EmailMessage objects
        """
        if not messages:
            raise ValueError("Must provide at least one message")

        # Sort messages chronologically
        sorted_messages = sorted(
            messages,
            key=lambda m: m.datetime_sent or datetime.min
        )

        # Create thread
        thread = cls(
            conversation_id=conversation_id,
            subject=sorted_messages[0].subject,  # Use first message subject
            messages=[],
            message_count=0
        )

        # Add all messages
        for message in sorted_messages:
            thread.add_message(message)

        return thread
