"""Attachment models for EWS MCP v3.0."""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class AttachmentType(str, Enum):
    """Attachment file types."""
    DOCUMENT = "document"  # PDF, DOCX, DOC, TXT, RTF, ODT
    SPREADSHEET = "spreadsheet"  # XLSX, XLS, CSV, ODS
    PRESENTATION = "presentation"  # PPTX, PPT, ODP
    ARCHIVE = "archive"  # ZIP, TAR, GZ, 7Z, RAR
    IMAGE = "image"  # JPG, PNG, GIF, BMP
    EMAIL = "email"  # EML, MSG
    OTHER = "other"  # Unknown or unsupported


class Attachment(BaseModel):
    """
    Email attachment metadata.
    """

    # Core identity
    attachment_id: str = Field(..., description="Unique attachment ID")
    name: str = Field(..., description="Filename")
    message_id: str = Field(..., description="Parent message ID")

    # File properties
    content_type: str = Field("application/octet-stream", description="MIME type")
    size: int = Field(0, description="Size in bytes")
    extension: Optional[str] = Field(None, description="File extension (e.g., '.pdf')")
    attachment_type: AttachmentType = Field(AttachmentType.OTHER, description="Categorized type")

    # Metadata
    is_inline: bool = Field(False, description="Inline (embedded) attachment")
    content_id: Optional[str] = Field(None, description="Content ID for inline attachments")
    last_modified: Optional[datetime] = Field(None, description="Last modified time")

    @property
    def size_kb(self) -> float:
        """Get size in KB."""
        return round(self.size / 1024, 2)

    @property
    def size_mb(self) -> float:
        """Get size in MB."""
        return round(self.size / (1024 * 1024), 2)

    @classmethod
    def detect_type(cls, filename: str) -> AttachmentType:
        """Detect attachment type from filename."""
        ext = filename.lower().split('.')[-1] if '.' in filename else ''

        type_mapping = {
            # Documents
            'pdf': AttachmentType.DOCUMENT,
            'doc': AttachmentType.DOCUMENT,
            'docx': AttachmentType.DOCUMENT,
            'txt': AttachmentType.DOCUMENT,
            'rtf': AttachmentType.DOCUMENT,
            'odt': AttachmentType.DOCUMENT,

            # Spreadsheets
            'xls': AttachmentType.SPREADSHEET,
            'xlsx': AttachmentType.SPREADSHEET,
            'csv': AttachmentType.SPREADSHEET,
            'ods': AttachmentType.SPREADSHEET,

            # Presentations
            'ppt': AttachmentType.PRESENTATION,
            'pptx': AttachmentType.PRESENTATION,
            'odp': AttachmentType.PRESENTATION,

            # Archives
            'zip': AttachmentType.ARCHIVE,
            'tar': AttachmentType.ARCHIVE,
            'gz': AttachmentType.ARCHIVE,
            '7z': AttachmentType.ARCHIVE,
            'rar': AttachmentType.ARCHIVE,

            # Images
            'jpg': AttachmentType.IMAGE,
            'jpeg': AttachmentType.IMAGE,
            'png': AttachmentType.IMAGE,
            'gif': AttachmentType.IMAGE,
            'bmp': AttachmentType.IMAGE,

            # Email
            'eml': AttachmentType.EMAIL,
            'msg': AttachmentType.EMAIL,
        }

        return type_mapping.get(ext, AttachmentType.OTHER)

    @classmethod
    def from_ews_attachment(
        cls,
        attachment: Any,
        message_id: str
    ) -> "Attachment":
        """
        Create Attachment from EWS attachment object.

        Args:
            attachment: exchangelib Attachment object
            message_id: Parent message ID
        """
        from ..utils import safe_get

        name = safe_get(attachment, "name", "unknown")
        extension = f".{name.split('.')[-1]}" if '.' in name else None

        return cls(
            attachment_id=safe_get(attachment, "attachment_id", {}).get("id", ""),
            name=name,
            message_id=message_id,
            content_type=safe_get(attachment, "content_type", "application/octet-stream"),
            size=safe_get(attachment, "size", 0),
            extension=extension,
            attachment_type=cls.detect_type(name),
            is_inline=safe_get(attachment, "is_inline", False),
            content_id=safe_get(attachment, "content_id"),
            last_modified=safe_get(attachment, "last_modified_time"),
        )


class AttachmentContent(BaseModel):
    """
    Extracted content from attachment.

    Supports multiple formats with intelligent parsing.
    """

    # Source
    attachment: Attachment = Field(..., description="Source attachment metadata")

    # Extracted content
    text: Optional[str] = Field(None, description="Extracted text content")
    format: str = Field("unknown", description="Detected format")

    # Structured data (for spreadsheets)
    tables: List[List[List[Any]]] = Field(
        default_factory=list,
        description="Extracted tables (for PDF, DOCX, Excel)"
    )
    sheets: Dict[str, List[List[Any]]] = Field(
        default_factory=dict,
        description="Excel sheets (sheet_name -> data)"
    )
    structured_data: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Structured records (for CSV, Excel)"
    )

    # Images (for presentations, PDFs)
    images: List[Any] = Field(
        default_factory=list,
        description="Extracted images"
    )

    # Metadata
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Document metadata (author, title, dates, etc.)"
    )

    # Archive contents (for ZIP, TAR)
    files: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Files in archive"
    )
    extracted_content: Dict[str, str] = Field(
        default_factory=dict,
        description="Content of readable files in archive"
    )

    # Parsing status
    success: bool = Field(True, description="Parsing successful")
    error: Optional[str] = Field(None, description="Error message if parsing failed")
    warnings: List[str] = Field(
        default_factory=list,
        description="Non-fatal warnings during parsing"
    )

    @property
    def has_text(self) -> bool:
        """Check if text content was extracted."""
        return bool(self.text)

    @property
    def has_tables(self) -> bool:
        """Check if tables were extracted."""
        return len(self.tables) > 0

    @property
    def has_structured_data(self) -> bool:
        """Check if structured data was extracted."""
        return len(self.structured_data) > 0

    @property
    def word_count(self) -> int:
        """Get word count of extracted text."""
        if not self.text:
            return 0
        return len(self.text.split())

    def get_summary(self) -> Dict[str, Any]:
        """Get content summary."""
        summary = {
            "attachment_name": self.attachment.name,
            "format": self.format,
            "success": self.success,
            "has_text": self.has_text,
            "word_count": self.word_count,
            "has_tables": self.has_tables,
            "table_count": len(self.tables),
            "has_images": len(self.images) > 0,
            "image_count": len(self.images),
        }

        if self.attachment.attachment_type == AttachmentType.SPREADSHEET:
            summary["sheet_count"] = len(self.sheets)
            summary["has_structured_data"] = self.has_structured_data
            summary["record_count"] = len(self.structured_data)

        if self.attachment.attachment_type == AttachmentType.ARCHIVE:
            summary["file_count"] = len(self.files)
            summary["extracted_files"] = len(self.extracted_content)

        if self.error:
            summary["error"] = self.error

        if self.warnings:
            summary["warnings"] = self.warnings

        return summary
