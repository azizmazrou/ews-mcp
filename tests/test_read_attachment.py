"""Execute-path tests for ``ReadAttachmentTool`` (CHANGELOG C1).

History: the PDF/DOCX/XLSX extractor methods (``_read_pdf``, ``_read_docx``,
``_read_excel``) were originally placed on the WRONG class
(``AttachEmailToDraftTool``), making them unreachable from
``ReadAttachmentTool.execute``. Every non-TXT extraction silently fell back
to a generic "Failed to read attachment" error.

The fix moved the methods to the right class — but NO test exercised the
tool, so the regression could re-occur if a future refactor moves methods
back. These tests pin the execute path for every supported file type +
the unsupported-type error path.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.tools.attachment_tools import ReadAttachmentTool


def _attachment(name: str, content: bytes):
    """Bare attachment object — exposes only `.name` and `.content`."""
    att = MagicMock()
    att.name = name
    att.content = content
    return att


def _message_with(attachment):
    msg = MagicMock()
    msg.attachments = [attachment]
    return msg


@pytest.mark.asyncio
async def test_read_attachment_txt_returns_decoded_content(mock_ews_client):
    msg = _message_with(_attachment("notes.txt", b"hello world"))
    with patch(
        "src.tools.attachment_tools.find_message_for_account", return_value=msg
    ):
        tool = ReadAttachmentTool(mock_ews_client)
        out = await tool.execute(message_id="AAMk-1", attachment_name="notes.txt")

    assert out["success"] is True
    assert out["file_name"] == "notes.txt"
    assert out["file_type"] == "txt"
    assert out["content"] == "hello world"
    assert out["file_size"] == len(b"hello world")
    assert out["content_length"] == len("hello world")


@pytest.mark.asyncio
async def test_read_attachment_unsupported_extension_raises(mock_ews_client):
    msg = _message_with(_attachment("photo.png", b"\x89PNG..."))
    with patch(
        "src.tools.attachment_tools.find_message_for_account", return_value=msg
    ):
        tool = ReadAttachmentTool(mock_ews_client)
        with pytest.raises(Exception) as exc_info:
            await tool.execute(message_id="AAMk-1", attachment_name="photo.png")
    assert "unsupported" in str(exc_info.value).lower() or "png" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_read_attachment_missing_attachment_name_is_validation(mock_ews_client):
    tool = ReadAttachmentTool(mock_ews_client)
    with pytest.raises(Exception) as exc_info:
        await tool.execute(message_id="AAMk-1")
    assert "attachment_name" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_read_attachment_attachment_not_found_raises(mock_ews_client):
    msg = _message_with(_attachment("notes.txt", b"hi"))
    with patch(
        "src.tools.attachment_tools.find_message_for_account", return_value=msg
    ):
        tool = ReadAttachmentTool(mock_ews_client)
        with pytest.raises(Exception) as exc_info:
            await tool.execute(
                message_id="AAMk-1", attachment_name="something_else.txt"
            )
    assert "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_read_attachment_empty_content_raises(mock_ews_client):
    msg = _message_with(_attachment("notes.txt", b""))
    with patch(
        "src.tools.attachment_tools.find_message_for_account", return_value=msg
    ):
        tool = ReadAttachmentTool(mock_ews_client)
        with pytest.raises(Exception) as exc_info:
            await tool.execute(message_id="AAMk-1", attachment_name="notes.txt")
    assert "empty" in str(exc_info.value).lower()


def test_read_attachment_class_owns_extractor_methods():
    """C1 regression guard: the PDF/DOCX/XLSX extractor methods MUST be
    on ``ReadAttachmentTool``, not on a sibling class. The original bug
    was exactly this — methods were on ``AttachEmailToDraftTool`` so the
    execute path could never reach them."""
    assert callable(getattr(ReadAttachmentTool, "_read_pdf", None)), (
        "ReadAttachmentTool._read_pdf is missing — extractor methods may "
        "have moved to a sibling class again. See CHANGELOG C1."
    )
    assert callable(getattr(ReadAttachmentTool, "_read_docx", None))
    assert callable(getattr(ReadAttachmentTool, "_read_excel", None))


@pytest.mark.asyncio
async def test_read_attachment_dispatches_to_pdf_reader_for_pdf(mock_ews_client):
    """Pin that ``.pdf`` files invoke ``_read_pdf``. We don't depend on
    pdfplumber being installed for the test — patch the reader."""
    msg = _message_with(_attachment("report.pdf", b"%PDF-1.4 ..."))
    with patch(
        "src.tools.attachment_tools.find_message_for_account", return_value=msg
    ), patch.object(
        ReadAttachmentTool, "_read_pdf", return_value="extracted pdf body"
    ) as mock_pdf:
        tool = ReadAttachmentTool(mock_ews_client)
        out = await tool.execute(
            message_id="AAMk-1", attachment_name="report.pdf"
        )
    mock_pdf.assert_called_once()
    assert out["content"] == "extracted pdf body"
    assert out["file_type"] == "pdf"


@pytest.mark.asyncio
async def test_read_attachment_dispatches_to_docx_reader(mock_ews_client):
    msg = _message_with(_attachment("doc.docx", b"PK..."))
    with patch(
        "src.tools.attachment_tools.find_message_for_account", return_value=msg
    ), patch.object(
        ReadAttachmentTool, "_read_docx", return_value="docx body"
    ) as mock_docx:
        tool = ReadAttachmentTool(mock_ews_client)
        out = await tool.execute(
            message_id="AAMk-1", attachment_name="doc.docx"
        )
    mock_docx.assert_called_once()
    assert out["content"] == "docx body"
    assert out["file_type"] == "docx"


@pytest.mark.asyncio
async def test_read_attachment_dispatches_to_excel_reader_for_xlsx(mock_ews_client):
    msg = _message_with(_attachment("sheet.xlsx", b"PK..."))
    with patch(
        "src.tools.attachment_tools.find_message_for_account", return_value=msg
    ), patch.object(
        ReadAttachmentTool, "_read_excel", return_value="A1: 1\nA2: 2"
    ) as mock_xlsx:
        tool = ReadAttachmentTool(mock_ews_client)
        out = await tool.execute(
            message_id="AAMk-1", attachment_name="sheet.xlsx"
        )
    mock_xlsx.assert_called_once()
    assert out["content"] == "A1: 1\nA2: 2"
    assert out["file_type"] == "xlsx"
