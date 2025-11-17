"""Pydantic models for EWS MCP Server."""

from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, List, Literal
from datetime import datetime
from enum import Enum
from pathlib import Path


class ImportanceLevel(str, Enum):
    """Email importance levels."""
    LOW = "Low"
    NORMAL = "Normal"
    HIGH = "High"


class SensitivityLevel(str, Enum):
    """Email sensitivity levels."""
    NORMAL = "Normal"
    PERSONAL = "Personal"
    PRIVATE = "Private"
    CONFIDENTIAL = "Confidential"


class ResponseType(str, Enum):
    """Meeting response types."""
    ACCEPT = "Accept"
    TENTATIVE = "Tentative"
    DECLINE = "Decline"


# Email Models
class SendEmailRequest(BaseModel):
    """Request model for sending email with attachment validation."""
    to: List[EmailStr] = Field(..., description="Recipient email addresses")
    subject: str = Field(..., min_length=1, description="Email subject")
    body: str = Field(..., description="Email body (HTML supported)")
    cc: Optional[List[EmailStr]] = Field(None, description="CC recipients")
    bcc: Optional[List[EmailStr]] = Field(None, description="BCC recipients")
    importance: ImportanceLevel = ImportanceLevel.NORMAL
    sensitivity: SensitivityLevel = SensitivityLevel.NORMAL
    attachments: Optional[List[str]] = Field(None, description="Attachment file paths")

    @field_validator("attachments")
    @classmethod
    def validate_attachments(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        """Validate that attachment files exist and are readable."""
        if v:
            for file_path in v:
                path = Path(file_path)
                if not path.exists():
                    raise ValueError(f"Attachment file not found: {file_path}")
                if not path.is_file():
                    raise ValueError(f"Attachment path is not a file: {file_path}")
                # Check if file is readable by attempting to open it
                try:
                    with open(file_path, 'rb') as f:
                        # Just check if we can open it, don't read content
                        pass
                except PermissionError:
                    raise ValueError(f"Permission denied: Cannot read attachment file: {file_path}")
                except Exception as e:
                    raise ValueError(f"Cannot access attachment file {file_path}: {str(e)}")
        return v


class SendEmailResponse(BaseModel):
    """Response model for sent email."""
    message_id: str
    sent_time: datetime
    success: bool
    message: str


class EmailSearchRequest(BaseModel):
    """Request model for email search."""
    folder: str = Field(default="inbox", description="Folder to search in")
    subject_contains: Optional[str] = Field(None, description="Subject contains text")
    from_address: Optional[EmailStr] = Field(None, description="From email address")
    has_attachments: Optional[bool] = Field(None, description="Has attachments")
    is_read: Optional[bool] = Field(None, description="Is read status")
    start_date: Optional[datetime] = Field(None, description="Start date")
    end_date: Optional[datetime] = Field(None, description="End date")
    max_results: int = Field(default=50, le=1000, description="Maximum results")


class EmailDetails(BaseModel):
    """Email details model."""
    message_id: str
    subject: str
    from_address: str
    to_addresses: List[str]
    cc_addresses: Optional[List[str]] = None
    body: str
    body_preview: str
    received_time: datetime
    is_read: bool
    has_attachments: bool
    importance: str
    sensitivity: str


# Calendar Models
class CreateAppointmentRequest(BaseModel):
    """Request model for creating appointment."""
    subject: str = Field(..., min_length=1, description="Appointment subject")
    start_time: datetime = Field(..., description="Start time")
    end_time: datetime = Field(..., description="End time")
    location: Optional[str] = Field(None, description="Meeting location")
    body: Optional[str] = Field(None, description="Appointment body")
    attendees: Optional[List[EmailStr]] = Field(None, description="Attendee email addresses")
    is_all_day: bool = Field(default=False, description="All day event")
    reminder_minutes: Optional[int] = Field(15, description="Reminder minutes before")

    @field_validator("end_time")
    @classmethod
    def validate_end_time(cls, v: datetime, info) -> datetime:
        """Validate end time is after start time."""
        start_time = info.data.get("start_time")
        if start_time and v <= start_time:
            raise ValueError("end_time must be after start_time")
        return v


class AppointmentDetails(BaseModel):
    """Appointment details model."""
    item_id: str
    subject: str
    start_time: datetime
    end_time: datetime
    location: Optional[str] = None
    organizer: str
    attendees: List[str]
    body: Optional[str] = None
    is_all_day: bool
    response_status: Optional[str] = None


class MeetingResponse(BaseModel):
    """Request model for meeting response."""
    item_id: str = Field(..., description="Meeting item ID")
    response: ResponseType = Field(..., description="Response type")
    message: Optional[str] = Field(None, description="Optional response message")


# Contact Models
class CreateContactRequest(BaseModel):
    """Request model for creating contact."""
    given_name: str = Field(..., min_length=1, description="First name")
    surname: str = Field(..., min_length=1, description="Last name")
    email_address: EmailStr = Field(..., description="Email address")
    phone_number: Optional[str] = Field(None, description="Phone number")
    company: Optional[str] = Field(None, description="Company name")
    job_title: Optional[str] = Field(None, description="Job title")
    department: Optional[str] = Field(None, description="Department")


class ContactDetails(BaseModel):
    """Contact details model."""
    item_id: str
    display_name: str
    given_name: str
    surname: str
    email_address: str
    phone_number: Optional[str] = None
    company: Optional[str] = None
    job_title: Optional[str] = None
    department: Optional[str] = None


# Task Models
class CreateTaskRequest(BaseModel):
    """Request model for creating task."""
    subject: str = Field(..., min_length=1, description="Task subject")
    body: Optional[str] = Field(None, description="Task body")
    due_date: Optional[datetime] = Field(None, description="Due date")
    start_date: Optional[datetime] = Field(None, description="Start date")
    importance: ImportanceLevel = ImportanceLevel.NORMAL
    reminder_time: Optional[datetime] = Field(None, description="Reminder time")


class TaskDetails(BaseModel):
    """Task details model."""
    item_id: str
    subject: str
    body: Optional[str] = None
    status: str
    percent_complete: int
    due_date: Optional[datetime] = None
    start_date: Optional[datetime] = None
    importance: str
    is_complete: bool


# Generic Response Models
class OperationResponse(BaseModel):
    """Generic operation response."""
    success: bool
    message: str
    item_id: Optional[str] = None
    details: Optional[dict] = None


class ListResponse(BaseModel):
    """Generic list response."""
    items: List[dict]
    total_count: int
    has_more: bool


# Attachment Models
class ReadAttachmentRequest(BaseModel):
    """Request model for reading attachment content."""
    message_id: str = Field(..., description="Email message ID")
    attachment_name: str = Field(..., description="Name of attachment to read")
    extract_tables: bool = Field(default=False, description="Extract tables from document")
    max_pages: int = Field(default=50, ge=1, le=500, description="Maximum pages for PDF")


# Contact Intelligence Models
class FindPersonRequest(BaseModel):
    """Request model for finding person across multiple sources."""
    query: str = Field(..., min_length=1, description="Name, email, or domain to search")
    search_scope: Literal["all", "gal", "email_history", "domain"] = Field(
        default="all",
        description="Where to search: all, gal (Global Address List), email_history, or domain"
    )
    include_stats: bool = Field(default=True, description="Include communication statistics")
    time_range_days: int = Field(default=365, ge=1, le=1825, description="Days back to search")
    max_results: int = Field(default=50, ge=1, le=100, description="Maximum results to return")


class CommunicationHistoryRequest(BaseModel):
    """Request model for getting communication history."""
    email: EmailStr = Field(..., description="Email address to analyze")
    days_back: int = Field(default=365, ge=1, le=1825, description="Days back to analyze")
    max_emails: int = Field(default=100, ge=1, le=500, description="Maximum emails to retrieve")
