"""Core domain models for EWS MCP v3.0.

Person-centric architecture with first-class Person entities.
"""

from .person import Person, PersonSource, EmailAddress, CommunicationStats
from .email_message import EmailMessage, MessageImportance, MessageSensitivity
from .thread import ConversationThread, ThreadMessage
from .attachment import Attachment, AttachmentContent

__all__ = [
    "Person",
    "PersonSource",
    "EmailAddress",
    "CommunicationStats",
    "EmailMessage",
    "MessageImportance",
    "MessageSensitivity",
    "ConversationThread",
    "ThreadMessage",
    "Attachment",
    "AttachmentContent",
]
