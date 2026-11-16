"""Pure bytes-to-text document extraction for mail attachments (DESIGN.md §DTOs, §Errors).

Reads PDF, Office Open XML (Word, Excel, PowerPoint), RFC-822 (.eml), Exchange
item attachments, HTML, and plain text strictly in memory without executing
macros, launching external programs, touching the network, or writing to disk.

Module-wide output rules:
- Line endings are normalized to ``\\n`` and trailing ASCII spaces (``" "``) at
  the end of every line are removed.
- Assembled text stops at ``MAX_EXTRACTED_CHARS`` (5,000,000 characters) while
  being built and records a truncation note.
- Lone UTF-16 surrogates (U+D800–U+DFFF) are replaced with U+FFFD so extracted
  text is always valid UTF-8.
- Arabic Presentation Forms (U+FB50–U+FDFF and U+FE70–U+FEFC) are mapped via
  ``NFKC`` to searchable base letters without altering superscripts or other
  non-Arabic compatibility characters.
"""

from __future__ import annotations

import email
import email.header
import email.policy
import importlib.util
import io
import math
import posixpath
import re
import time
import zipfile
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import Message as EmailMsg
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from .bodyclean import html_to_text_bounded
from .pdftext import (
    fix_ligature_maps,
    has_rtl_letters,
    laid_out_by_word,
    normalize_arabic_presentation_forms,
    page_reading_order,
)
from .toolerror import ToolError

_UNSET_PYPDF = object()
_pypdf: Any = _UNSET_PYPDF
HAS_PYPDF: bool = importlib.util.find_spec("pypdf") is not None

# --- Named safety and content bounds (every limit has a unit test) -----------

MAX_EXTRACTED_CHARS = 5_000_000
MAX_CELL_CHARS = 2_000
MAX_CELL_REF_CHARS = 10
MAX_EML_HEADER_CHARS = 4_000
MAX_HEADER_ADDRESSES = 50
MAX_ARCHIVE_MEMBERS = 2_000
MAX_MEMBER_BYTES = 30 * 1024 * 1024
MAX_XML_PART_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 100 * 1024 * 1024
MAX_XML_DEPTH = 256
MAX_SHEET_ROWS = 5_000
MAX_SHEET_COLS = 50
MAX_WORKBOOK_SHEETS = 200
MAX_PPTX_SLIDES = 1_000
MAX_PDF_PAGES = 500
MAX_MIME_DEPTH = 64
MAX_EML_PARTS = 1_000
MAX_EML_ATTACHMENTS = 200
MAX_ERROR_MESSAGE_CHARS = 300
MAX_NOTE_CHARS = 300
MAX_RETURNED_NOTES = 8
MAX_ECHOED_TEXT_CHARS = 80
DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

_READ_CHUNK_BYTES = 65_536

ALLOWED_CHARSETS = (
    "utf-8",
    "utf-16",
    "windows-1256",
    "windows-1252",
    "iso-8859-1",
    "iso-8859-6",
)

_CHARSET_ALLOWLIST: Dict[str, str] = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "utf_8": "utf-8",
    "utf-8-sig": "utf-8",
    "utf_8_sig": "utf-8",
    "ascii": "utf-8",
    "us-ascii": "utf-8",
    "windows-1256": "cp1256",
    "cp1256": "cp1256",
    "windows-1252": "cp1252",
    "cp1252": "cp1252",
    "iso-8859-1": "iso-8859-1",
    "iso8859-1": "iso-8859-1",
    "latin-1": "iso-8859-1",
    "latin1": "iso-8859-1",
    "iso-8859-6": "iso-8859-6",
    "iso8859-6": "iso-8859-6",
}

TEXT_EXTENSIONS = (".txt", ".csv", ".md", ".log", ".json")
WORD_EXTENSIONS = (".docx", ".docm")
EXCEL_EXTENSIONS = (".xlsx", ".xlsm")
PPT_EXTENSIONS = (".pptx", ".pptm")
HTML_EXTENSIONS = (".html", ".htm")
EML_EXTENSIONS = (".eml",)

SUPPORTED_TYPES = ("pdf", "docx", "xlsx", "pptx", "eml", "message", "html", "text")
_DOCUMENT_KINDS = frozenset({"pdf", "docx", "xlsx", "pptx", "eml", "message", "html"})
EMPTY_DOCUMENT_NOTE = "Document contains no readable text."

SUPPORTED_TYPES_HINT = (
    "Supported types: pdf (.pdf), docx (.docx, .docm), xlsx (.xlsx, .xlsm), "
    "pptx (.pptx, .pptm), eml (.eml), attached Exchange messages, "
    "html (.html, .htm), and plain text (.txt, .csv, .md, .log, .json, text/*). "
    "Outlook .msg files are not supported; use mode='save' to write binary "
    "attachments to disk."
)

_PYPDF_MISSING_HINT = (
    "Install the optional extra `ews-mcp[extract]` "
    "(e.g. `pip install 'ews-mcp[extract]'`) to read PDF text, "
    "or use mode='save' to write the PDF to disk."
)

_WORD_MIMES = frozenset({
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-word.document.macroenabled.12",
})
_EXCEL_MIMES = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
})
_PPT_MIMES = frozenset({
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint.presentation.macroenabled.12",
})
_HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})
_MSG_MIMES = frozenset({"application/vnd.ms-outlook"})

_META_TAG_CHARSET_RE = re.compile(
    rb"""<meta\b[^>]{0,512}?\bcharset\s*=\s*["']?\s*([A-Za-z0-9._\-]+)""",
    re.IGNORECASE,
)
_SLIDE_PART_RE = re.compile(r"^ppt/slides/slide([0-9]{1,6})\.xml$", re.IGNORECASE)
_BLANK_LINES_RE = re.compile(r"\n(?:[ \t]*+\n){2,}")
_SURROGATE_RE = re.compile(r"[\uD800-\uDFFF]")
_NON_SURROGATEESCAPE_RE = re.compile(r"[\uD800-\uDC7F\uDD00-\uDFFF]")
_MAX_RAW_HEADER_BYTES = MAX_EML_HEADER_CHARS * 4 + 64
_CD_FILENAME_RE = re.compile(
    r"""(?<![A-Za-z0-9_*\-])filename\s*=\s*(?:"([^"]{0,512})"|'([^']{0,512})'|([^\s;]{1,512}))""",
    re.IGNORECASE,
)
_CT_NAME_RE = re.compile(
    r"""(?<![A-Za-z0-9_*\-])name\s*=\s*(?:"([^"]{0,512})"|'([^']{0,512})'|([^\s;]{1,512}))""",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    extracted_from: str
    pages: Optional[int] = None
    slides: Optional[int] = None
    sheets: Optional[int] = None
    pages_read: Optional[int] = None
    slides_read: Optional[int] = None
    sheets_read: Optional[int] = None
    partial: bool = False
    notes: Tuple[str, ...] = ()
    memory_ceiling_enforced: Optional[bool] = None
    loaded_modules: Tuple[str, ...] = ()


def _get_pypdf() -> Any:
    """Import ``pypdf`` lazily when a PDF arrives, respecting test overrides."""
    global _pypdf
    if _pypdf is _UNSET_PYPDF:
        try:
            import pypdf as loaded_pypdf
        except ImportError:
            _pypdf = None
        else:
            _pypdf = loaded_pypdf
    return _pypdf


def pypdf_available() -> bool:
    if _pypdf is _UNSET_PYPDF:
        return importlib.util.find_spec("pypdf") is not None
    return _pypdf is not None


def sanitize_surrogates(text: str) -> str:
    """Replace any lone UTF-16 surrogate code points (U+D800–U+DFFF) with U+FFFD."""
    if not text:
        return ""
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub("\ufffd", text)
    return text


def _safe_snippet(value: Any, limit: int = MAX_ECHOED_TEXT_CHARS) -> str:
    """Sanitize and cap file- or caller-supplied text echoed into messages/notes."""
    raw = sanitize_surrogates(str(value or ""))
    collapsed = " ".join(raw.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())
    if len(collapsed) > limit:
        return collapsed[: max(1, limit - 1)] + "…"
    return collapsed


def cap_message(text: str, limit: int = MAX_ERROR_MESSAGE_CHARS) -> str:
    """Sanitize and cap an error message or note to at most ``limit`` characters."""
    clean = sanitize_surrogates(text or "")
    collapsed = " ".join(clean.replace("\r", " ").replace("\n", " ").split())
    if len(collapsed) > limit:
        return collapsed[: max(1, limit - 1)] + "…"
    return collapsed


def _validation_error(message: str, *, hint: Optional[str] = None) -> ToolError:
    capped_hint = cap_message(hint, MAX_ERROR_MESSAGE_CHARS) if hint else None
    return ToolError("validation", cap_message(message, MAX_ERROR_MESSAGE_CHARS), hint=capped_hint)


def _strip_trailing_line_spaces(text: str) -> str:
    """Remove trailing ASCII spaces at the end of every line (module output rule)."""
    if not text:
        return ""
    return "\n".join(
        line.rstrip(" ")
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    )


class _TextAccumulator:
    """Bounds assembled text to ``max_chars`` while it is being constructed."""

    def __init__(self, max_chars: int = MAX_EXTRACTED_CHARS) -> None:
        self.max_chars = max(1, int(max_chars))
        self._parts: List[str] = []
        self.chars = 0
        self.truncated = False

    def append(self, text: str) -> bool:
        if self.truncated:
            return False
        if not text:
            return True
        remaining = self.max_chars - self.chars
        if remaining <= 0:
            self.truncated = True
            return False
        if len(text) > remaining:
            self._parts.append(text[:remaining])
            self.chars = self.max_chars
            self.truncated = True
            return False
        self._parts.append(text)
        self.chars += len(text)
        return True

    def append_line(self, line: str) -> bool:
        cleaned = line.rstrip(" ")
        if self._parts:
            if not self.append("\n"):
                return False
        return self.append(cleaned)

    def append_block(self, block: str) -> bool:
        if not block:
            return True
        if self._parts:
            if not self.append("\n\n"):
                return False
        return self.append(block)

    def finish(self, *, strip_trailing_spaces: bool = True) -> str:
        joined = "".join(self._parts)
        if strip_trailing_spaces:
            return sanitize_surrogates(_strip_trailing_line_spaces(joined))
        return sanitize_surrogates(joined)


def fold_and_prioritize_notes(
    notes: Iterable[str], *, max_notes: int = MAX_RETURNED_NOTES
) -> Tuple[str, ...]:
    """Order cut notes first, fold repeated notes of one kind with a count, cap at 8."""
    cleaned = [cap_message(n, MAX_NOTE_CHARS) for n in notes if n]
    if not cleaned:
        return ()

    primary_cuts: List[List[Any]] = []
    primary_index: Dict[Tuple[Any, ...], int] = {}
    secondary_cuts: List[List[Any]] = []
    secondary_index: Dict[Tuple[Any, ...], int] = {}
    others: List[List[Any]] = []
    other_index: Dict[Tuple[Any, ...], int] = {}

    def _record(
        bucket: List[List[Any]],
        idx_map: Dict[Tuple[Any, ...], int],
        gkey: Tuple[Any, ...],
        note_str: str,
        extra: Any = None,
    ) -> None:
        pos = idx_map.get(gkey)
        if pos is None:
            idx_map[gkey] = len(bucket)
            bucket.append([gkey, 1, note_str, extra])
        else:
            bucket[pos][1] += 1

    for n in cleaned:
        if n.startswith((
            "Stopped after ",
            "Extracted text stopped at ",
            "Workbook truncated at ",
            "PDF truncated at ",
            "Presentation truncated at ",
            "Message truncated at ",
            "Message MIME nesting exceeded ",
        )):
            _record(primary_cuts, primary_index, ("primary", n), n)
            continue

        if n.startswith("Sheet ") and " was cut at " in n and n.endswith("."):
            sheet_part, reason_dot = n.split(" was cut at ", 1)
            reason = reason_dot[:-1]
            sheet_lbl = sheet_part[len("Sheet ") :]
            _record(
                secondary_cuts,
                secondary_index,
                ("sheet_cut", reason),
                n,
                (reason, sheet_lbl),
            )
            continue

        if " sheets were cut at " in n:
            _record(secondary_cuts, secondary_index, ("folded_sheet_cut", n), n)
            continue

        if n.startswith("Page ") and ":" in n:
            head, rest = n.split(":", 1)
            pnum = head[5:].strip()
            if pnum.isdigit():
                body_msg = rest.strip()
                _record(
                    others,
                    other_index,
                    ("page_colon", body_msg),
                    n,
                    body_msg,
                )
                continue

        if n.startswith("Page ") and " failed to parse (" in n:
            _record(others, other_index, ("page_parse_fail",), n)
            continue

        if n.startswith("Page ") and " layout extraction failed (" in n:
            _record(others, other_index, ("page_layout_fb",), n)
            continue

        _record(others, other_index, ("exact", n), n)

    def _render_entry(entry: List[Any]) -> str:
        gkey, count, first_note, extra = entry
        if count == 1:
            return first_note
        kind = gkey[0]
        if kind == "sheet_cut":
            reason, first_lbl = extra
            return cap_message(
                f"{count} sheets were cut at {reason} (first: {first_lbl}).",
                MAX_NOTE_CHARS,
            )
        if kind == "page_colon":
            body_msg = str(extra)
            if (
                body_msg
                == "read in file order because part of the page is drawn in a way the layout pass does not read."
            ):
                return cap_message(
                    f"{count} pages were read in file order because part of each "
                    "page is drawn in a way the layout pass does not read.",
                    MAX_NOTE_CHARS,
                )
            return cap_message(f"{count} pages: {body_msg}", MAX_NOTE_CHARS)
        if kind == "page_parse_fail":
            return cap_message(
                f"{count} pages failed to parse (first: {first_note}).",
                MAX_NOTE_CHARS,
            )
        if kind == "page_layout_fb":
            return cap_message(
                f"{count} pages failed layout extraction and fell back to plain mode "
                f"(first: {first_note}).",
                MAX_NOTE_CHARS,
            )
        return cap_message(f"{first_note} (x{count})", MAX_NOTE_CHARS)

    rendered: List[str] = []
    for group in (primary_cuts, secondary_cuts, others):
        for entry in group:
            rendered.append(_render_entry(entry))
            if len(rendered) >= max_notes:
                return tuple(rendered[:max_notes])
    return tuple(rendered[:max_notes])


def _finalize_result(
    text: str,
    *,
    extracted_from: str,
    notes: Iterable[str] = (),
    truncated_at: Optional[int] = None,
    pages: Optional[int] = None,
    slides: Optional[int] = None,
    sheets: Optional[int] = None,
    pages_read: Optional[int] = None,
    slides_read: Optional[int] = None,
    sheets_read: Optional[int] = None,
    partial: bool = False,
    preserve_layout: bool = False,
) -> ExtractionResult:
    note_list = [cap_message(n, MAX_NOTE_CHARS) for n in notes if n]
    is_partial = bool(partial) or (truncated_at is not None)
    if truncated_at is not None:
        trunc_msg = f"Extracted text stopped at {truncated_at} characters."
        note_list.append(cap_message(trunc_msg, MAX_NOTE_CHARS))
    clean_body = (
        sanitize_surrogates(text)
        if preserve_layout
        else sanitize_surrogates(_strip_trailing_line_spaces(text))
    )
    if (
        not clean_body.strip()
        and extracted_from in _DOCUMENT_KINDS
        and not any(
            "no text layer" in n.lower() or "no readable text" in n.lower()
            for n in note_list
        )
    ):
        note_list.append(EMPTY_DOCUMENT_NOTE)
    folded_notes = fold_and_prioritize_notes(note_list)
    if folded_notes and extracted_from == "pdf" and truncated_at is None:
        note_lines = "\n".join(f"[Note: {n}]" for n in folded_notes)
        combined = f"{clean_body}\n\n{note_lines}" if clean_body else note_lines
        if len(combined) <= MAX_EXTRACTED_CHARS:
            clean_body = (
                sanitize_surrogates(combined)
                if preserve_layout
                else sanitize_surrogates(_strip_trailing_line_spaces(combined))
            )
    return ExtractionResult(
        text=clean_body,
        extracted_from=extracted_from,
        pages=pages,
        slides=slides,
        sheets=sheets,
        pages_read=pages_read,
        slides_read=slides_read,
        sheets_read=sheets_read,
        partial=is_partial,
        notes=folded_notes,
    )


def _ext_of(name: Optional[str]) -> str:
    raw = (name or "").strip().lower()
    dot = raw.rfind(".")
    return raw[dot:] if dot >= 0 else ""


def _mime_of(content_type: Optional[str]) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def is_msg_attachment(name: Optional[str], content_type: Optional[str]) -> bool:
    return _ext_of(name) == ".msg" or _mime_of(content_type) in _MSG_MIMES


def detect_kind(name: Optional[str], content_type: Optional[str]) -> Optional[str]:
    """Return canonical extractor kind for (name, content_type), or None."""
    if is_msg_attachment(name, content_type):
        return None
    ext = _ext_of(name)
    if ext == ".pdf":
        return "pdf"
    if ext in WORD_EXTENSIONS:
        return "docx"
    if ext in EXCEL_EXTENSIONS:
        return "xlsx"
    if ext in PPT_EXTENSIONS:
        return "pptx"
    if ext in EML_EXTENSIONS:
        return "eml"
    if ext in HTML_EXTENSIONS:
        return "html"
    if ext in TEXT_EXTENSIONS:
        return "text"

    if ext in (
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff",
        ".zip", ".7z", ".rar", ".gz", ".tar", ".bz2", ".xz",
        ".doc", ".xls", ".ppt", ".bin", ".exe", ".dll", ".dmg", ".iso",
        ".mp3", ".mp4", ".wav", ".avi", ".mov", ".p7s", ".p7m",
    ):
        return None

    ct = _mime_of(content_type)
    if ct == "application/pdf":
        return "pdf"
    if ct in _WORD_MIMES:
        return "docx"
    if ct in _EXCEL_MIMES:
        return "xlsx"
    if ct in _PPT_MIMES:
        return "pptx"
    if ct == "message/rfc822":
        return "eml"
    if ct in _HTML_MIMES:
        return "html"
    if ct.startswith("text/") or ct == "application/json":
        return "text"
    return None


def unsupported_type_error(name: Optional[str], content_type: Optional[str]) -> ToolError:
    label = _safe_snippet(name or "attachment")
    if is_msg_attachment(name, content_type):
        return _validation_error(
            f"Attachment {label!r} is an Outlook .msg file, which is not supported "
            "for text extraction in this version.",
            hint=SUPPORTED_TYPES_HINT,
        )
    ct_snip = _safe_snippet(content_type) if content_type else ""
    ct_note = f" ({ct_snip})" if ct_snip else ""
    return _validation_error(
        f"Unsupported attachment type for text extraction: {label!r}{ct_note}.",
        hint=SUPPORTED_TYPES_HINT,
    )


def _check_attachment_ceiling(
    data: bytes, max_bytes: Optional[int] = DEFAULT_MAX_ATTACHMENT_BYTES
) -> None:
    if not isinstance(data, (bytes, bytearray)):
        raise _validation_error("Attachment payload must be bytes.")
    if max_bytes is not None and len(data) > max_bytes:
        raise _validation_error(
            f"Attachment size ({len(data)} bytes) exceeds the server ceiling "
            f"({max_bytes} bytes; EWS_MAX_ATTACHMENT_MB).",
            hint="Use mode='info' for metadata, or raise EWS_MAX_ATTACHMENT_MB.",
        )


def _local(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _attr_local(elem: Any, local_name: str) -> Optional[str]:
    attrib = getattr(elem, "attrib", None) or {}
    if local_name in attrib:
        return attrib[local_name]
    for key, val in attrib.items():
        if _local(key) == local_name:
            return val
    return None


def _check_xml_depth(root: Any, part_name: str, max_depth: int = MAX_XML_DEPTH) -> None:
    """Iterative depth check so no subsequent walker can hit deep trees."""
    stack: List[Tuple[Any, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            raise _validation_error(
                f"XML nesting in {_safe_snippet(part_name)!r} exceeds the "
                f"{max_depth}-level depth limit."
            )
        next_depth = depth + 1
        for child in node:
            stack.append((child, next_depth))


def _parse_xml_bytes(raw: bytes, part_name: str) -> Any:
    if len(raw) > MAX_XML_PART_BYTES:
        raise _validation_error(
            f"XML part {_safe_snippet(part_name)!r} exceeds the "
            f"{MAX_XML_PART_BYTES}-byte XML parser limit."
        )
    try:
        root = DefusedET.fromstring(
            raw, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except MemoryError:
        raise
    except DefusedXmlException as exc:
        raise _validation_error(
            f"Refused unsafe XML in {_safe_snippet(part_name)!r} "
            f"(DTD or entity expansion forbidden): {type(exc).__name__}."
        ) from None
    except (DefusedET.ParseError, UnicodeDecodeError, ValueError) as exc:
        raise _validation_error(
            f"Malformed XML in {_safe_snippet(part_name)!r}: {_safe_snippet(exc)}"
        ) from None
    _check_xml_depth(root, part_name, MAX_XML_DEPTH)
    return root


# ---------------------------------------------------------------------------
# Bounded ZIP reader for Office Open XML (.docx/.docm/.xlsx/.xlsm/.pptx/.pptm)
# ---------------------------------------------------------------------------

_ALLOWED_ZIP_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


class _SafeArchive:
    """In-memory ZIP reader enforcing member count, method, and streamed byte caps."""

    def __init__(self, data: bytes, label: str) -> None:
        if len(data) < 4 or data[:2] != b"PK":
            raise _validation_error(
                f"File is not a valid {label} archive (header does not match extension)."
            )
        try:
            self._zf = zipfile.ZipFile(io.BytesIO(data), "r")
        except MemoryError:
            raise
        except (
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            zlib.error,
            EOFError,
            OSError,
            ValueError,
        ) as exc:
            raise _validation_error(
                f"Corrupt or truncated {label} archive: {_safe_snippet(exc)}"
            ) from None

        try:
            infos = self._zf.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise _validation_error(
                    f"Archive contains {len(infos)} members, exceeding the "
                    f"{MAX_ARCHIVE_MEMBERS}-member limit."
                )
            self._by_exact: Dict[str, zipfile.ZipInfo] = {}
            self._by_lower: Dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                if info.compress_type not in _ALLOWED_ZIP_METHODS:
                    raise _validation_error(
                        f"Archive member {_safe_snippet(info.filename)!r} uses "
                        f"unsupported compression method ({info.compress_type}); "
                        "only stored and deflated members are allowed."
                    )
                if info.flag_bits & 0x1:
                    raise _validation_error(
                        f"Archive member {_safe_snippet(info.filename)!r} is encrypted."
                    )
                norm = info.filename.replace("\\", "/").lstrip("/")
                # In ZIP central directories with duplicate names, the last entry wins.
                self._by_exact[norm] = info
                self._by_lower[norm.lower()] = info
            self._total_read = 0
        except Exception:
            self._zf.close()
            raise

    def close(self) -> None:
        self._zf.close()

    def __enter__(self) -> "_SafeArchive":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def has(self, path: str) -> bool:
        norm = path.replace("\\", "/").lstrip("/")
        return norm in self._by_exact or norm.lower() in self._by_lower

    def names(self) -> List[str]:
        return list(self._by_exact.keys())

    def read_bytes(self, path: str, *, max_part_bytes: int = MAX_MEMBER_BYTES) -> bytes:
        norm = path.replace("\\", "/").lstrip("/")
        lower = norm.lower()
        if lower.endswith(".bin") or "vbaproject" in lower:
            raise _validation_error(
                f"Refusing macro binary part {_safe_snippet(norm)!r}."
            )
        info = self._by_exact.get(norm) or self._by_lower.get(lower)
        if info is None:
            raise _validation_error(
                f"Missing required archive member {_safe_snippet(norm)!r}."
            )
        if info.compress_type not in _ALLOWED_ZIP_METHODS:
            raise _validation_error(
                f"Archive member {_safe_snippet(info.filename)!r} uses unsupported "
                "compression method."
            )
        part_cap = min(MAX_MEMBER_BYTES, int(max_part_bytes))
        chunks: List[bytes] = []
        member_read = 0
        try:
            with self._zf.open(info, "r") as fp:
                while True:
                    block = fp.read(_READ_CHUNK_BYTES)
                    if not block:
                        break
                    member_read += len(block)
                    if member_read > part_cap:
                        raise _validation_error(
                            f"Archive member {_safe_snippet(info.filename)!r} "
                            f"decompressed past the {part_cap}-byte limit."
                        )
                    self._total_read += len(block)
                    if self._total_read > MAX_ARCHIVE_TOTAL_BYTES:
                        raise _validation_error(
                            f"Archive decompressed past the {MAX_ARCHIVE_TOTAL_BYTES}-byte "
                            "total size limit."
                        )
                    chunks.append(block)
        except (ToolError, MemoryError):
            raise
        except (
            zipfile.BadZipFile,
            zlib.error,
            EOFError,
            OSError,
            RuntimeError,
            ValueError,
            NotImplementedError,
        ) as exc:
            raise _validation_error(
                f"Failed reading archive member {_safe_snippet(info.filename)!r}: "
                f"{_safe_snippet(exc)}"
            ) from None
        return b"".join(chunks)

    def read_xml(self, path: str) -> Any:
        raw = self.read_bytes(path, max_part_bytes=MAX_XML_PART_BYTES)
        return _parse_xml_bytes(raw, path)

    def iter_worksheet_rows(self, path: str) -> Iterator[Any]:
        """Stream ``<row>`` elements from an Excel worksheet without buffering the part.

        Enforces ``MAX_ARCHIVE_TOTAL_BYTES``, ``MAX_XML_DEPTH``, and DTD/entity
        prohibitions while allowing worksheets larger than ``MAX_XML_PART_BYTES``
        to yield their first ``MAX_SHEET_ROWS`` rows cleanly.
        """
        norm = path.replace("\\", "/").lstrip("/")
        lower = norm.lower()
        if lower.endswith(".bin") or "vbaproject" in lower:
            raise _validation_error(
                f"Refusing macro binary part {_safe_snippet(norm)!r}."
            )
        info = self._by_exact.get(norm) or self._by_lower.get(lower)
        if info is None:
            raise _validation_error(
                f"Missing required archive member {_safe_snippet(norm)!r}."
            )
        if info.compress_type not in _ALLOWED_ZIP_METHODS:
            raise _validation_error(
                f"Archive member {_safe_snippet(info.filename)!r} uses unsupported "
                "compression method."
            )

        archive_ref = self

        class _BoundedStream:
            def __init__(self, fp: Any) -> None:
                self._fp = fp

            def read(self, size: int = -1) -> bytes:
                if size == 0:
                    return b""
                want = (
                    _READ_CHUNK_BYTES
                    if size is None or size < 0
                    else min(size, _READ_CHUNK_BYTES)
                )
                try:
                    chunk = self._fp.read(want)
                except MemoryError:
                    raise
                except (
                    zipfile.BadZipFile,
                    zlib.error,
                    EOFError,
                    OSError,
                    RuntimeError,
                    ValueError,
                    NotImplementedError,
                ) as exc:
                    raise _validation_error(
                        "Failed reading archive member "
                        f"{_safe_snippet(info.filename)!r}: {_safe_snippet(exc)}"
                    ) from None
                if chunk:
                    archive_ref._total_read += len(chunk)
                    if archive_ref._total_read > MAX_ARCHIVE_TOTAL_BYTES:
                        raise _validation_error(
                            "Archive decompressed past the "
                            f"{MAX_ARCHIVE_TOTAL_BYTES}-byte total size limit."
                        )
                return chunk

        try:
            with self._zf.open(info, "r") as raw_fp:
                bounded = _BoundedStream(raw_fp)
                depth = 0
                in_row = False
                sheet_data_elem: Optional[Any] = None
                root_elem: Optional[Any] = None
                events = DefusedET.iterparse(
                    bounded,
                    events=("start", "end"),
                    forbid_dtd=True,
                    forbid_entities=True,
                    forbid_external=True,
                )
                for event, elem in events:
                    tag = _local(elem.tag)
                    if event == "start":
                        depth += 1
                        if depth > MAX_XML_DEPTH:
                            raise _validation_error(
                                f"XML nesting in {_safe_snippet(path)!r} exceeds the "
                                f"{MAX_XML_DEPTH}-level depth limit."
                            )
                        if depth == 1:
                            root_elem = elem
                        elif tag == "sheetData":
                            sheet_data_elem = elem
                        elif tag == "row" and sheet_data_elem is not None:
                            in_row = True
                    else:
                        depth -= 1
                        if tag == "row" and in_row:
                            yield elem
                            elem.clear()
                            in_row = False
                            if sheet_data_elem is not None:
                                del sheet_data_elem[:]
                        elif tag == "sheetData":
                            elem.clear()
                            sheet_data_elem = None
                        elif not in_row and elem is not root_elem:
                            elem.clear()
                            if root_elem is not None:
                                del root_elem[:]
                if root_elem is not None:
                    root_elem.clear()
        except (ToolError, MemoryError):
            raise
        except DefusedXmlException as exc:
            raise _validation_error(
                f"Refused unsafe XML in {_safe_snippet(path)!r} "
                f"(DTD or entity expansion forbidden): {type(exc).__name__}."
            ) from None
        except (DefusedET.ParseError, UnicodeDecodeError, ValueError) as exc:
            raise _validation_error(
                f"Malformed XML in {_safe_snippet(path)!r}: {_safe_snippet(exc)}"
            ) from None
        except (
            zipfile.BadZipFile,
            zlib.error,
            EOFError,
            OSError,
            RuntimeError,
            NotImplementedError,
        ) as exc:
            raise _validation_error(
                f"Failed reading archive member {_safe_snippet(info.filename)!r}: "
                f"{_safe_snippet(exc)}"
            ) from None


def _resolve_rel_target(base_dir: str, target: str) -> Optional[str]:
    clean = (target or "").strip().replace("\\", "/")
    if not clean:
        return None
    if clean.startswith("/"):
        joined = posixpath.normpath(clean.lstrip("/"))
    else:
        joined = posixpath.normpath(f"{base_dir}/{clean}" if base_dir else clean)
    if joined.startswith("..") or joined.startswith("/"):
        return None
    return joined


def _read_rels_map(archive: _SafeArchive, rels_path: str, base_dir: str) -> Dict[str, str]:
    if not archive.has(rels_path):
        return {}
    root = archive.read_xml(rels_path)
    mapping: Dict[str, str] = {}
    for rel in root:
        if _local(rel.tag) != "Relationship":
            continue
        if (rel.attrib.get("TargetMode") or "").strip().lower() == "external":
            continue
        rel_id = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if not rel_id or not target:
            continue
        resolved = _resolve_rel_target(base_dir, target)
        if resolved:
            mapping[rel_id] = resolved
    return mapping


def _iter_effective_children(elem: Any) -> List[Any]:
    """Return children in document order; for mc:AlternateContent, only mc:Choice."""
    if _local(elem.tag) == "AlternateContent":
        return [c for c in elem if _local(c.tag) == "Choice"]
    out: List[Any] = []
    for child in elem:
        if _local(child.tag) == "AlternateContent":
            out.extend(gc for gc in child if _local(gc.tag) == "Choice")
        else:
            out.append(child)
    return out


# ---------------------------------------------------------------------------
# Word (.docx / .docm) — strictly iterative XML traversal
# ---------------------------------------------------------------------------


def _word_para_text_iter(para_elem: Any, *, in_cell: bool, max_chars: int) -> str:
    """Iteratively extract text of one <w:p>, respecting mc:Choice and bounds."""
    parts: List[str] = []
    char_count = 0
    # Stack items: (element_or_marker_str,)
    stack: List[Any] = list(reversed(_iter_effective_children(para_elem)))

    while stack and char_count < max_chars:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
            char_count += len(item)
            continue
        tag = _local(item.tag)
        if tag in ("del", "instrText", "Fallback"):
            continue
        if tag == "t":
            txt = item.text
            if txt:
                parts.append(txt)
                char_count += len(txt)
            continue
        if tag == "tab":
            tok = " " if in_cell else "\t"
            parts.append(tok)
            char_count += 1
            continue
        if tag in ("br", "cr"):
            tok = " " if in_cell else "\n"
            parts.append(tok)
            char_count += 1
            continue
        if tag == "noBreakHyphen":
            parts.append("-")
            char_count += 1
            continue
        if tag == "tbl":
            nested_rows = _word_table_rows_iter(item, max_chars=max_chars - char_count)
            if nested_rows:
                rendered = (
                    " ".join(r.replace("\t", " ") for r in nested_rows)
                    if in_cell
                    else "\n".join(nested_rows)
                )
                parts.append(rendered)
                char_count += len(rendered)
            continue
        children = _iter_effective_children(item)
        if tag == "p":
            # Nested paragraph (e.g. inside w:txbxContent): separate paragraphs cleanly.
            if parts and parts[-1] not in ("\n", " "):
                sep = " " if in_cell else "\n"
                parts.append(sep)
                char_count += 1
        for child in reversed(children):
            stack.append(child)

    raw = normalize_arabic_presentation_forms("".join(parts))
    if in_cell:
        return " ".join(raw.replace("\t", " ").replace("\r", " ").replace("\n", " ").split())
    return _strip_trailing_line_spaces(raw).strip("\n")


def _word_cell_text_iter(tc_elem: Any, *, max_chars: int) -> str:
    cell_parts: List[str] = []
    used = 0
    stack: List[Any] = list(reversed(_iter_effective_children(tc_elem)))
    while stack and used < max_chars:
        node = stack.pop()
        tag = _local(node.tag)
        if tag == "Fallback":
            continue
        if tag == "p":
            txt = _word_para_text_iter(node, in_cell=True, max_chars=max_chars - used)
            if txt:
                cell_parts.append(txt)
                used += len(txt) + 1
        elif tag == "tbl":
            nested = _word_table_rows_iter(node, max_chars=max_chars - used)
            for row in nested:
                cleaned = " ".join(row.replace("\t", " ").split())
                if cleaned:
                    cell_parts.append(cleaned)
                    used += len(cleaned) + 1
                    if used >= max_chars:
                        break
        elif tag in ("sdt", "sdtContent", "customXml", "Choice"):
            for child in reversed(_iter_effective_children(node)):
                stack.append(child)
    return " ".join(cell_parts)


def _word_table_rows_iter(tbl_elem: Any, *, max_chars: int) -> List[str]:
    rows: List[str] = []
    used = 0
    row_stack: List[Any] = list(reversed(_iter_effective_children(tbl_elem)))
    while row_stack and used < max_chars:
        node = row_stack.pop()
        tag = _local(node.tag)
        if tag == "Fallback":
            continue
        if tag == "tr":
            cells: List[str] = []
            cell_stack: List[Any] = list(reversed(_iter_effective_children(node)))
            while cell_stack and used < max_chars:
                cnode = cell_stack.pop()
                ctag = _local(cnode.tag)
                if ctag == "Fallback":
                    continue
                if ctag == "tc":
                    cell_txt = _word_cell_text_iter(
                        cnode, max_chars=max(1, max_chars - used)
                    )
                    cells.append(cell_txt)
                    used += len(cell_txt) + 1
                elif ctag in ("sdt", "sdtContent", "customXml", "Choice"):
                    for child in reversed(_iter_effective_children(cnode)):
                        cell_stack.append(child)
            if cells:
                rows.append("\t".join(cells))
        elif tag in ("sdt", "sdtContent", "customXml", "Choice"):
            for child in reversed(_iter_effective_children(node)):
                row_stack.append(child)
    return rows


def extract_docx(
    data: bytes,
    *,
    max_bytes: Optional[int] = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_chars: int = MAX_EXTRACTED_CHARS,
    soft_deadline: Optional[float] = None,
) -> ExtractionResult:
    """Extract Word (.docx / .docm) paragraphs and tables iteratively."""
    _check_attachment_ceiling(data, max_bytes)
    with _SafeArchive(data, "Word (.docx)") as archive:
        if not archive.has("word/document.xml"):
            raise _validation_error(
                "File is not a valid Word (.docx) document: missing word/document.xml."
            )
        root = archive.read_xml("word/document.xml")
        if _local(root.tag) != "document":
            raise _validation_error(
                "Invalid Word document XML: root element is not <document>."
            )
        body = next((c for c in root if _local(c.tag) == "body"), root)
        acc = _TextAccumulator(max_chars)
        notes: List[str] = []
        block_stack: List[Any] = list(reversed(_iter_effective_children(body)))
        blocks_done = 0

        stopped_by_clock = False
        while block_stack and not acc.truncated:
            if (
                soft_deadline is not None
                and blocks_done >= 1
                and blocks_done % 32 == 0
                and time.monotonic() >= soft_deadline
            ):
                stopped_by_clock = True
                notes.append(
                    f"Stopped after block {blocks_done} (time limit); "
                    "the rest was not read."
                )
                break
            node = block_stack.pop()
            tag = _local(node.tag)
            if tag == "Fallback":
                continue
            budget = max(1, acc.max_chars - acc.chars + 1)
            if tag == "p":
                txt = _word_para_text_iter(node, in_cell=False, max_chars=budget)
                if txt.strip():
                    acc.append_line(txt)
                blocks_done += 1
            elif tag == "tbl":
                for row in _word_table_rows_iter(node, max_chars=budget):
                    if not acc.append_line(row):
                        break
                blocks_done += 1
            elif tag in ("sdt", "sdtContent", "customXml", "Choice"):
                for child in reversed(_iter_effective_children(node)):
                    block_stack.append(child)

        return _finalize_result(
            acc.finish(),
            extracted_from="docx",
            notes=notes,
            truncated_at=acc.max_chars if acc.truncated else None,
            partial=stopped_by_clock or acc.truncated,
        )


# ---------------------------------------------------------------------------
# PowerPoint (.pptx / .pptm) — strictly iterative XML traversal
# ---------------------------------------------------------------------------


def _dml_para_text_iter(p_elem: Any, *, in_cell: bool, max_chars: int) -> str:
    parts: List[str] = []
    used = 0
    stack: List[Any] = list(reversed(_iter_effective_children(p_elem)))
    while stack and used < max_chars:
        node = stack.pop()
        tag = _local(node.tag)
        if tag == "Fallback":
            continue
        if tag == "t":
            if node.text:
                parts.append(node.text)
                used += len(node.text)
            continue
        if tag == "br":
            parts.append(" " if in_cell else "\n")
            used += 1
            continue
        if tag == "tab":
            parts.append(" " if in_cell else "\t")
            used += 1
            continue
        for child in reversed(_iter_effective_children(node)):
            stack.append(child)

    raw = normalize_arabic_presentation_forms("".join(parts))
    if in_cell:
        return " ".join(raw.replace("\t", " ").replace("\r", " ").replace("\n", " ").split())
    return "\n".join(
        ln.strip()
        for ln in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if ln.strip()
    )


def _dml_table_rows_iter(tbl_elem: Any, *, max_chars: int) -> List[str]:
    rows: List[str] = []
    used = 0
    for tr in _iter_effective_children(tbl_elem):
        if _local(tr.tag) != "tr":
            continue
        cells: List[str] = []
        for tc in _iter_effective_children(tr):
            if _local(tc.tag) != "tc":
                continue
            paras: List[str] = []
            for tx_body in _iter_effective_children(tc):
                if _local(tx_body.tag) != "txBody":
                    continue
                for p in _iter_effective_children(tx_body):
                    if _local(p.tag) == "p":
                        txt = _dml_para_text_iter(
                            p, in_cell=True, max_chars=max(1, max_chars - used)
                        )
                        if txt:
                            paras.append(txt)
                            used += len(txt) + 1
                            if used >= max_chars:
                                break
            cells.append(" ".join(paras))
            if used >= max_chars:
                break
        if cells:
            rows.append("\t".join(cells))
        if used >= max_chars:
            break
    return rows


def _extract_slide_lines_iter(slide_root: Any, *, max_chars: int) -> List[str]:
    lines: List[str] = []
    used = 0
    stack: List[Any] = list(reversed(_iter_effective_children(slide_root)))
    while stack and used < max_chars:
        node = stack.pop()
        tag = _local(node.tag)
        if tag == "Fallback":
            continue
        if tag == "tbl":
            for row in _dml_table_rows_iter(node, max_chars=max(1, max_chars - used)):
                lines.append(row)
                used += len(row) + 1
                if used >= max_chars:
                    break
            continue
        if tag == "txBody":
            for child in _iter_effective_children(node):
                if _local(child.tag) == "p":
                    txt = _dml_para_text_iter(
                        child, in_cell=False, max_chars=max(1, max_chars - used)
                    )
                    if txt:
                        lines.append(txt)
                        used += len(txt) + 1
                        if used >= max_chars:
                            break
            continue
        for child in reversed(_iter_effective_children(node)):
            stack.append(child)
    return lines


def _slide_paths_in_order(archive: _SafeArchive) -> Tuple[List[str], int]:
    has_pres = archive.has("ppt/presentation.xml")
    numbered: List[Tuple[int, str]] = []
    for name in archive.names():
        m = _SLIDE_PART_RE.match(name)
        if m:
            numbered.append((int(m.group(1)), name))
    numbered.sort(key=lambda pair: pair[0])

    if not has_pres and not numbered:
        raise _validation_error(
            "File is not a valid PowerPoint (.pptx) presentation: "
            "missing ppt/presentation.xml."
        )

    if has_pres:
        pres_root = archive.read_xml("ppt/presentation.xml")
        if _local(pres_root.tag) != "presentation":
            raise _validation_error(
                "Invalid PowerPoint XML: root element is not <presentation>."
            )
        rels = _read_rels_map(archive, "ppt/_rels/presentation.xml.rels", "ppt")
        ordered: List[str] = []
        total_listed = 0
        for sld_id_lst in pres_root:
            if _local(sld_id_lst.tag) != "sldIdLst":
                continue
            for sld_id in sld_id_lst:
                if _local(sld_id.tag) != "sldId":
                    continue
                rid = _attr_local(sld_id, "id")
                rel_id = None
                for attr_k, attr_v in sld_id.attrib.items():
                    if attr_k != "id" and _local(attr_k) == "id":
                        rel_id = attr_v
                        break
                target = rels.get(rel_id or "") or (rels.get(rid) if rid else None)
                if target and archive.has(target) and not target.lower().endswith(".bin"):
                    total_listed += 1
                    if len(ordered) < MAX_PPTX_SLIDES:
                        ordered.append(target)
        if ordered:
            return ordered, total_listed

    paths = [name for _num, name in numbered]
    return paths[:MAX_PPTX_SLIDES], len(paths)


def extract_pptx(
    data: bytes,
    *,
    max_bytes: Optional[int] = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_chars: int = MAX_EXTRACTED_CHARS,
    soft_deadline: Optional[float] = None,
) -> ExtractionResult:
    """Extract PowerPoint (.pptx / .pptm) slides in order under `--- Slide N ---`."""
    _check_attachment_ceiling(data, max_bytes)
    with _SafeArchive(data, "PowerPoint (.pptx)") as archive:
        slide_paths, total_slides = _slide_paths_in_order(archive)
        acc = _TextAccumulator(max_chars)
        notes: List[str] = []
        stopped_at_slide: Optional[int] = None
        any_slide_text = False
        slides_done = 0
        for idx, slide_path in enumerate(slide_paths, start=1):
            if acc.truncated:
                break
            if soft_deadline is not None and idx >= 2 and time.monotonic() >= soft_deadline:
                stopped_at_slide = idx - 1
                break
            slide_root = archive.read_xml(slide_path)
            budget = max(1, acc.max_chars - acc.chars + 1)
            slide_lines = _extract_slide_lines_iter(slide_root, max_chars=budget)
            if slide_lines:
                any_slide_text = True
            header = f"--- Slide {idx} ---"
            section = header + ("\n" + "\n".join(slide_lines) if slide_lines else "")
            acc.append_block(section)
            slides_done += 1
        if stopped_at_slide is not None:
            notes.append(
                f"Stopped after slide {stopped_at_slide} of {total_slides} "
                "(time limit); the rest was not read."
            )
        elif total_slides > len(slide_paths):
            notes.append(
                f"Presentation truncated at {len(slide_paths)} of {total_slides} slides."
            )
        body_out = acc.finish() if any_slide_text else ""
        return _finalize_result(
            body_out,
            extracted_from="pptx",
            slides=total_slides,
            slides_read=slides_done,
            notes=notes,
            truncated_at=acc.max_chars if acc.truncated else None,
            partial=(
                stopped_at_slide is not None
                or total_slides > len(slide_paths)
                or acc.truncated
                or slides_done < total_slides
            ),
        )


# ---------------------------------------------------------------------------
# Excel (.xlsx / .xlsm) — iterative XML, <=10-char cell refs, <=2,000 chars/cell
# ---------------------------------------------------------------------------


def _si_text_iter(si_elem: Any, *, max_chars: int = MAX_CELL_CHARS) -> str:
    parts: List[str] = []
    used = 0
    stack: List[Any] = list(reversed(list(si_elem)))
    while stack and used < max_chars:
        node = stack.pop()
        tag = _local(node.tag)
        if tag in ("rPh", "Fallback"):
            continue
        if tag == "t":
            if node.text:
                rem = max_chars - used
                chunk = node.text[:rem]
                parts.append(chunk)
                used += len(chunk)
            continue
        for child in reversed(_iter_effective_children(node)):
            stack.append(child)
    return normalize_arabic_presentation_forms("".join(parts))[:max_chars]


def _read_shared_strings(archive: _SafeArchive) -> List[str]:
    if not archive.has("xl/sharedStrings.xml"):
        return []
    root = archive.read_xml("xl/sharedStrings.xml")
    if _local(root.tag) != "sst":
        raise _validation_error(
            "Invalid Excel sharedStrings.xml: root element is not <sst>."
        )
    return [_si_text_iter(child, max_chars=MAX_CELL_CHARS) for child in root if _local(child.tag) == "si"]


def _col_index(cell_ref: Optional[str], sheet_name: str) -> Optional[int]:
    """Parse an A1-style cell reference in O(len(cell_ref)) with a 10-character cap."""
    if cell_ref is None:
        return None
    ref = cell_ref.strip()
    if not ref:
        return None
    if len(ref) > MAX_CELL_REF_CHARS:
        raise _validation_error(
            f"Invalid cell reference in sheet {_safe_snippet(sheet_name)!r} "
            f"(longer than {MAX_CELL_REF_CHARS} characters)."
        )
    n = len(ref)
    i = 0
    if i < n and ref[i] == "$":
        i += 1
    col_start = i
    col = 0
    while i < n:
        ch = ref[i]
        if "A" <= ch <= "Z":
            col = col * 26 + (ord(ch) - ord("A") + 1)
            i += 1
        elif "a" <= ch <= "z":
            col = col * 26 + (ord(ch) - ord("a") + 1)
            i += 1
        else:
            break
    if i == col_start:
        raise _validation_error(
            f"Invalid cell reference {_safe_snippet(ref)!r} in sheet "
            f"{_safe_snippet(sheet_name)!r}."
        )
    if i < n and ref[i] == "$":
        i += 1
    row_start = i
    while i < n and "0" <= ref[i] <= "9":
        i += 1
    if i == row_start or i != n:
        raise _validation_error(
            f"Invalid cell reference {_safe_snippet(ref)!r} in sheet "
            f"{_safe_snippet(sheet_name)!r}."
        )
    return col - 1


def _clean_cell(val: str) -> str:
    sliced = val[: MAX_CELL_CHARS * 2]
    normalized = normalize_arabic_presentation_forms(sliced)
    cleaned = (
        normalized.replace("\t", " ")
        .replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
    )
    return cleaned[:MAX_CELL_CHARS]


_BUILTIN_DATE_FMT_IDS = frozenset({
    14, 15, 16, 17, 27, 28, 29, 30, 31, 36, 50, 51, 52, 53, 54, 57, 58
})
_BUILTIN_DATETIME_FMT_IDS = frozenset({22})
_BUILTIN_TIME_NO_SEC_IDS = frozenset({18, 20})
_BUILTIN_TIME_SEC_IDS = frozenset({19, 21, 32, 33, 34, 35, 45, 47})
_BUILTIN_ELAPSED_TIME_IDS = frozenset({46})
_MAX_EXCEL_DATE_SERIAL = 2_958_465.99999


def _classify_excel_format_code(code: str) -> Optional[Tuple[str, Any]]:
    """Classify an Excel ``formatCode`` string as date/datetime/time or percent."""
    if not code:
        return None
    # Inspect only the positive-number section (before the first unquoted `;`).
    n = len(code)
    has_y = False
    has_m = False
    has_d = False
    has_h = False
    has_s = False
    elapsed = False
    has_pct = False
    in_frac = False
    pct_decimals = 0
    seen_pct_dot = False

    i = 0
    while i < n:
        ch = code[i]
        if ch == "\"":
            end_q = code.find("\"", i + 1)
            i = (end_q + 1) if end_q >= 0 else n
            continue
        if ch in ("\\", "_", "*"):
            i += 2
            continue
        if ch == "[":
            end_b = code.find("]", i + 1)
            if end_b < 0:
                break
            inner = code[i + 1 : end_b].strip().lower()
            if inner and all(c == "h" for c in inner):
                has_h = True
                elapsed = True
            elif inner and all(c == "m" for c in inner):
                has_m = True
                elapsed = True
            elif inner and all(c == "s" for c in inner):
                has_s = True
                elapsed = True
            i = end_b + 1
            continue
        if ch == ";":
            break

        low = code[i : i + 5].lower()
        if low.startswith("am/pm"):
            has_h = True
            i += 5
            continue
        if low.startswith("a/p"):
            has_h = True
            i += 3
            continue

        lc = ch.lower()
        if lc == "y":
            has_y = True
        elif lc == "m":
            has_m = True
        elif lc == "d":
            has_d = True
        elif lc == "h":
            has_h = True
        elif lc == "s":
            has_s = True
        elif lc == ".":
            if not has_pct:
                in_frac = True
                seen_pct_dot = True
        elif lc in ("0", "#"):
            if in_frac and not has_pct and pct_decimals < 10:
                pct_decimals += 1
        elif lc == "%":
            has_pct = True
            in_frac = False
        i += 1

    if has_y or has_m or has_d or has_h or has_s:
        has_date = has_y or has_d or (has_m and not has_h and not has_s)
        has_time = has_h or has_s
        if has_date and has_time:
            return ("datetime", has_s)
        if has_date:
            return ("date", False)
        return ("time", (has_s, elapsed))

    if has_pct:
        return ("percent", pct_decimals if seen_pct_dot else 0)
    return None


def _classify_excel_num_fmt_id(
    fmt_id: int, custom_codes: Dict[int, str]
) -> Optional[Tuple[str, Any]]:
    if fmt_id in custom_codes:
        return _classify_excel_format_code(custom_codes[fmt_id])
    if fmt_id == 9:
        return ("percent", 0)
    if fmt_id == 10:
        return ("percent", 2)
    if fmt_id in _BUILTIN_DATE_FMT_IDS:
        return ("date", False)
    if fmt_id in _BUILTIN_DATETIME_FMT_IDS:
        return ("datetime", False)
    if fmt_id in _BUILTIN_TIME_NO_SEC_IDS:
        return ("time", (False, False))
    if fmt_id in _BUILTIN_TIME_SEC_IDS:
        return ("time", (True, False))
    if fmt_id in _BUILTIN_ELAPSED_TIME_IDS:
        return ("time", (True, True))
    return None


def _read_excel_xf_formats(
    archive: _SafeArchive, notes: List[str]
) -> Dict[int, Tuple[str, Any]]:
    """Parse ``xl/styles.xml`` into ``{xf_index: (kind, meta)}``; note on failure."""
    if not archive.has("xl/styles.xml"):
        return {}
    try:
        styles_root = archive.read_xml("xl/styles.xml")
        if _local(styles_root.tag) != "styleSheet":
            raise ValueError("root element is not <styleSheet>")
        custom_codes: Dict[int, str] = {}
        num_fmts_el = next(
            (c for c in styles_root if _local(c.tag) == "numFmts"), None
        )
        if num_fmts_el is not None:
            for nf in num_fmts_el:
                if _local(nf.tag) != "numFmt":
                    continue
                raw_id = (nf.attrib.get("numFmtId") or "").strip()
                fcode = nf.attrib.get("formatCode") or ""
                if raw_id and len(raw_id) <= 9:
                    try:
                        custom_codes[int(raw_id)] = fcode[:512]
                    except ValueError:
                        continue

        cell_xfs_el = next(
            (c for c in styles_root if _local(c.tag) == "cellXfs"), None
        )
        if cell_xfs_el is None:
            return {}
        xf_map: Dict[int, Tuple[str, Any]] = {}
        xf_idx = 0
        for xf in cell_xfs_el:
            if _local(xf.tag) != "xf":
                continue
            apply_attr = (xf.attrib.get("applyNumberFormat") or "").strip().lower()
            raw_fmt = (xf.attrib.get("numFmtId") or "0").strip()
            if apply_attr not in ("0", "false") and raw_fmt and len(raw_fmt) <= 9:
                try:
                    fmt_id = int(raw_fmt)
                except ValueError:
                    fmt_id = 0
                spec = _classify_excel_num_fmt_id(fmt_id, custom_codes)
                if spec is not None:
                    xf_map[xf_idx] = spec
            xf_idx += 1
        return xf_map
    except MemoryError:
        raise
    except ToolError:
        if archive._total_read > MAX_ARCHIVE_TOTAL_BYTES:
            raise
        notes.append(
            "Spreadsheet styles could not be read; dates and percentages "
            "are shown as raw numbers."
        )
        return {}
    except Exception:
        notes.append(
            "Spreadsheet styles could not be read; dates and percentages "
            "are shown as raw numbers."
        )
        return {}


def _format_excel_numeric(
    raw_num: str, spec: Tuple[str, Any], *, is_date1904: bool
) -> Optional[str]:
    stripped = raw_num.strip()
    if not stripped or len(stripped) > 64:
        return None
    kind, meta = spec
    if kind == "percent":
        decimals = int(meta)
        try:
            dec_val = Decimal(stripped) * Decimal(100)
            if not dec_val.is_finite():
                return None
            quant = Decimal(1) if decimals <= 0 else Decimal(10) ** (-decimals)
            rounded = dec_val.quantize(quant, rounding=ROUND_HALF_UP)
            return f"{rounded:.{decimals}f}%"
        except (InvalidOperation, ValueError, OverflowError):
            return None

    try:
        val = float(stripped)
    except ValueError:
        return None
    if not math.isfinite(val) or val < 0.0 or val > _MAX_EXCEL_DATE_SERIAL:
        return None

    if kind == "time":
        show_sec, is_elapsed = meta
        if is_elapsed:
            tot_sec = int(round(val * 86400.0))
            hh, rem = divmod(tot_sec, 3600)
            mi, ss = divmod(rem, 60)
            if ss or show_sec:
                return f"{hh:02d}:{mi:02d}:{ss:02d}"
            return f"{hh:02d}:{mi:02d}"
        frac = val - math.floor(val)
        tot_sec = int(round(frac * 86400.0)) % 86400
        hh, rem = divmod(tot_sec, 3600)
        mi, ss = divmod(rem, 60)
        if ss or show_sec:
            return f"{hh:02d}:{mi:02d}:{ss:02d}"
        return f"{hh:02d}:{mi:02d}"

    # "date" or "datetime"
    days_int = int(math.floor(val))
    frac = val - days_int
    tot_sec = int(round(frac * 86400.0))
    if tot_sec >= 86400:
        days_int += 1
        tot_sec -= 86400

    try:
        if is_date1904:
            dt_date = date(1904, 1, 1) + timedelta(days=days_int)
            date_iso = dt_date.isoformat()
        elif days_int == 60:
            date_iso = "1900-02-29"
        elif days_int > 60:
            dt_date = date(1899, 12, 30) + timedelta(days=days_int)
            date_iso = dt_date.isoformat()
        elif days_int >= 1:
            dt_date = date(1899, 12, 31) + timedelta(days=days_int)
            date_iso = dt_date.isoformat()
        else:
            date_iso = "1899-12-31"
    except OverflowError:
        return None

    if tot_sec == 0 and kind == "date":
        return date_iso
    show_sec_dt = bool(meta)
    hh, rem = divmod(tot_sec, 3600)
    mi, ss = divmod(rem, 60)
    if ss or show_sec_dt:
        return f"{date_iso} {hh:02d}:{mi:02d}:{ss:02d}"
    return f"{date_iso} {hh:02d}:{mi:02d}"


def _excel_cell_value(
    cell_elem: Any,
    shared_strings: List[str],
    sheet_name: str,
    xf_formats: Optional[Dict[int, Tuple[str, Any]]] = None,
    *,
    is_date1904: bool = False,
) -> str:
    cell_type = (cell_elem.attrib.get("t") or "").strip()
    if cell_type == "inlineStr":
        is_elem = next((c for c in cell_elem if _local(c.tag) == "is"), None)
        if is_elem is not None:
            return _clean_cell(_si_text_iter(is_elem, max_chars=MAX_CELL_CHARS))
    v_elem = next((c for c in cell_elem if _local(c.tag) == "v"), None)
    raw = v_elem.text if v_elem is not None and v_elem.text is not None else ""
    if not raw:
        return ""
    if cell_type == "s":
        stripped = raw.strip()
        if len(stripped) > 12:
            raise _validation_error(
                f"Invalid shared string index in sheet {_safe_snippet(sheet_name)!r}."
            )
        try:
            idx = int(stripped)
        except ValueError:
            raise _validation_error(
                f"Invalid shared string index {_safe_snippet(raw)!r} in sheet "
                f"{_safe_snippet(sheet_name)!r}."
            ) from None
        if idx < 0 or idx >= len(shared_strings):
            raise _validation_error(
                f"Shared string index {idx} out of range in sheet "
                f"{_safe_snippet(sheet_name)!r}."
            )
        return _clean_cell(shared_strings[idx])
    if cell_type == "b":
        stripped = raw.strip()
        if stripped == "1":
            return "TRUE"
        if stripped == "0":
            return "FALSE"
    if cell_type in ("", "n") and xf_formats:
        style_raw = (cell_elem.attrib.get("s") or "").strip()
        if style_raw and len(style_raw) <= 9 and style_raw.isdigit():
            spec = xf_formats.get(int(style_raw))
            if spec is not None:
                formatted = _format_excel_numeric(
                    raw, spec, is_date1904=is_date1904
                )
                if formatted is not None:
                    return formatted
    return _clean_cell(raw)


def _workbook_sheets_and_props(
    archive: _SafeArchive,
) -> Tuple[List[Tuple[str, str]], int, bool]:
    if not archive.has("xl/workbook.xml"):
        raise _validation_error(
            "File is not a valid Excel (.xlsx) workbook: missing xl/workbook.xml."
        )
    wb_root = archive.read_xml("xl/workbook.xml")
    if _local(wb_root.tag) != "workbook":
        raise _validation_error(
            "Invalid Excel workbook XML: root element is not <workbook>."
        )
    wb_pr = next((c for c in wb_root if _local(c.tag) == "workbookPr"), None)
    d1904_attr = (
        (wb_pr.attrib.get("date1904") or "").strip().lower()
        if wb_pr is not None
        else ""
    )
    is_date1904 = d1904_attr in ("1", "true")

    rels = _read_rels_map(archive, "xl/_rels/workbook.xml.rels", "xl")
    sheets_parent = next((c for c in wb_root if _local(c.tag) == "sheets"), None)
    if sheets_parent is None:
        return [], 0, is_date1904
    result: List[Tuple[str, str]] = []
    total_valid = 0
    sheet_nodes = [c for c in sheets_parent if _local(c.tag) == "sheet"]
    for pos, sheet_node in enumerate(sheet_nodes, start=1):
        raw_name = sheet_node.attrib.get("name") or f"Sheet{pos}"
        name = normalize_arabic_presentation_forms(raw_name)[:MAX_CELL_CHARS]
        rel_id = None
        for attr_k, attr_v in sheet_node.attrib.items():
            if attr_k != "sheetId" and attr_k != "name" and _local(attr_k) == "id":
                rel_id = attr_v
                break
        target = rels.get(rel_id or "")
        if not target:
            cand = f"xl/worksheets/sheet{pos}.xml"
            if archive.has(cand):
                target = cand
            else:
                sid = sheet_node.attrib.get("sheetId")
                cand_sid = (
                    f"xl/worksheets/sheet{sid}.xml"
                    if sid and len(sid) <= 12
                    else ""
                )
                if cand_sid and archive.has(cand_sid):
                    target = cand_sid
        if target and archive.has(target) and not target.lower().endswith(".bin"):
            total_valid += 1
            if len(result) < MAX_WORKBOOK_SHEETS:
                result.append((name, target))
    return result, total_valid, is_date1904


def extract_xlsx(
    data: bytes,
    *,
    max_bytes: Optional[int] = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_chars: int = MAX_EXTRACTED_CHARS,
    soft_deadline: Optional[float] = None,
) -> ExtractionResult:
    """Extract Excel (.xlsx / .xlsm) sheets as tab-separated rows with streaming bounds."""
    _check_attachment_ceiling(data, max_bytes)
    with _SafeArchive(data, "Excel (.xlsx)") as archive:
        sheets, total_sheets, is_date1904 = _workbook_sheets_and_props(archive)
        shared_strings = _read_shared_strings(archive)
        acc = _TextAccumulator(max_chars)
        notes: List[str] = []
        xf_formats = _read_excel_xf_formats(archive, notes)
        stopped_at_sheet: Optional[int] = None
        any_cell_text = False
        any_sheet_cut = False
        sheets_done = 0

        for sheet_idx, (sheet_name, sheet_path) in enumerate(sheets):
            if acc.truncated:
                break
            if (
                soft_deadline is not None
                and sheet_idx >= 1
                and time.monotonic() >= soft_deadline
            ):
                stopped_at_sheet = sheet_idx
                break
            if sheet_idx > 0:
                if not acc.append("\n\n"):
                    break
            header = f"--- Sheet: {sheet_name} ---"
            if not acc.append(header):
                break
            sheets_done += 1

            rows_seen = 0
            rows_cut = False
            cols_cut = False
            row_iter = archive.iter_worksheet_rows(sheet_path)
            try:
                for row_elem in row_iter:
                    if acc.truncated:
                        break
                    rows_seen += 1
                    if rows_seen > MAX_SHEET_ROWS:
                        rows_cut = True
                        break
                    cells_by_col: Dict[int, str] = {}
                    next_col = 0
                    for c_elem in row_elem:
                        if _local(c_elem.tag) != "c":
                            continue
                        explicit_col = _col_index(c_elem.attrib.get("r"), sheet_name)
                        col = explicit_col if explicit_col is not None else next_col
                        next_col = col + 1
                        if col >= MAX_SHEET_COLS:
                            cols_cut = True
                            continue
                        cells_by_col[col] = _excel_cell_value(
                            c_elem,
                            shared_strings,
                            sheet_name,
                            xf_formats,
                            is_date1904=is_date1904,
                        )

                    if cells_by_col:
                        max_col = max(cells_by_col)
                        while max_col >= 0 and cells_by_col.get(max_col, "") == "":
                            max_col -= 1
                        if max_col >= 0:
                            any_cell_text = True
                            row_str = "\t".join(
                                cells_by_col.get(i, "") for i in range(max_col + 1)
                            ).rstrip(" ")
                            if not acc.append("\n" + row_str):
                                break
            finally:
                close_fn = getattr(row_iter, "close", None)
                if callable(close_fn):
                    close_fn()

            if rows_cut or cols_cut:
                any_sheet_cut = True
                reasons: List[str] = []
                if rows_cut:
                    reasons.append(f"{MAX_SHEET_ROWS} rows")
                if cols_cut:
                    reasons.append(f"{MAX_SHEET_COLS} columns")
                safe_sheet = _safe_snippet(sheet_name)
                cut_msg = f"Sheet {safe_sheet!r} was cut at " + " and ".join(reasons) + "."
                notes.append(cut_msg)
                if not acc.truncated:
                    acc.append(f"\n[Note: {cap_message(cut_msg, MAX_NOTE_CHARS)}]")

        if stopped_at_sheet is not None:
            notes.append(
                f"Stopped after sheet {stopped_at_sheet} of {total_sheets} "
                "(time limit); the rest was not read."
            )
        elif total_sheets > len(sheets):
            notes.append(
                f"Workbook truncated at {len(sheets)} of {total_sheets} sheets."
            )

        body_out = acc.finish() if (any_cell_text or notes) else ""
        return _finalize_result(
            body_out,
            extracted_from="xlsx",
            sheets=total_sheets,
            sheets_read=sheets_done,
            notes=notes,
            truncated_at=acc.max_chars if acc.truncated else None,
            partial=(
                stopped_at_sheet is not None
                or total_sheets > len(sheets)
                or any_sheet_cut
                or acc.truncated
                or sheets_done < total_sheets
            ),
        )


# ---------------------------------------------------------------------------
# PDF (.pdf, optional extra ews-mcp[extract] -> pypdf>=6.21,<7)
# ---------------------------------------------------------------------------


def _page_has_form_xobject(page: Any) -> bool:
    """Return True if ``page`` ``/Resources`` contains any ``/Subtype /Form`` XObject."""
    try:
        stack: List[Any] = [page.get("/Resources")]
        visited: set = set()
        while stack and len(visited) < 256:
            res_ref = stack.pop()
            if res_ref is None:
                continue
            ident = getattr(res_ref, "idnum", None)
            res_obj = res_ref.get_object() if hasattr(res_ref, "get_object") else res_ref
            if res_obj is None or not hasattr(res_obj, "get"):
                continue
            rkey = ("idnum", ident) if ident is not None else ("id", id(res_obj))
            if rkey in visited:
                continue
            visited.add(rkey)
            xobj_ref = res_obj.get("/XObject")
            xobj_dict = (
                xobj_ref.get_object() if hasattr(xobj_ref, "get_object") else xobj_ref
            )
            if not hasattr(xobj_dict, "values"):
                continue
            for xref in list(xobj_dict.values()):
                try:
                    xobj = xref.get_object() if hasattr(xref, "get_object") else xref
                    if xobj is None or not hasattr(xobj, "get"):
                        continue
                    if str(xobj.get("/Subtype") or "") == "/Form":
                        return True
                except MemoryError:
                    raise
                except Exception:
                    continue
    except MemoryError:
        raise
    except Exception:
        return False
    return False


def extract_pdf(
    data: bytes,
    *,
    max_bytes: Optional[int] = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_chars: int = MAX_EXTRACTED_CHARS,
    soft_deadline: Optional[float] = None,
) -> ExtractionResult:
    """Extract PDF pages via pypdf layout mode + ewsmcp.pdftext reading-order recovery."""
    pypdf_mod = _get_pypdf()
    if pypdf_mod is None:
        raise _validation_error(
            "PDF text extraction requires the optional 'pypdf' dependency, "
            "which is not installed.",
            hint=_PYPDF_MISSING_HINT,
        )
    _check_attachment_ceiling(data, max_bytes)
    if not data or b"%PDF-" not in data[:1024]:
        raise _validation_error(
            "File is not a valid PDF document (missing %PDF- header)."
        )

    try:
        reader = pypdf_mod.PdfReader(io.BytesIO(data), strict=False)
    except MemoryError:
        raise
    except Exception as exc:
        raise _validation_error(
            f"Invalid or corrupt PDF document: {_safe_snippet(exc)}"
        ) from None

    if getattr(reader, "is_encrypted", False):
        try:
            decrypted = reader.decrypt("")
        except MemoryError:
            raise
        except Exception as exc:
            raise _validation_error(
                f"Encrypted PDF cannot be opened without a password "
                f"({_safe_snippet(exc)}).",
                hint="Use mode='save' and unlock the PDF with its password externally.",
            ) from None
        if not decrypted or int(decrypted) == 0:
            raise _validation_error(
                "PDF is encrypted and requires a password to open.",
                hint="Use mode='save' and unlock the PDF with its password externally.",
            )

    try:
        total_pages = len(reader.pages)
    except MemoryError:
        raise
    except Exception as exc:
        msg = str(exc)
        if "decrypt" in msg.lower() or "password" in msg.lower():
            raise _validation_error(
                "PDF is encrypted and requires a password to open.",
                hint="Use mode='save' and unlock the PDF with its password externally.",
            ) from None
        raise _validation_error(
            f"Corrupt PDF page tree: {_safe_snippet(exc)}"
        ) from None

    # Rewrite multi-letter RTL /ToUnicode ligature entries on the layout reader;
    # keep an unpatched reader opened lazily for plain-mode text when needed.
    patched_maps = fix_ligature_maps(reader)
    word_doc = laid_out_by_word(reader)
    plain_reader: Optional[Any] = None

    def _extract_unpatched_plain(page_index: int, fallback_page: Any) -> str:
        nonlocal plain_reader
        if patched_maps <= 0:
            return fallback_page.extract_text() or ""
        if plain_reader is None:
            plain_reader = pypdf_mod.PdfReader(io.BytesIO(data), strict=False)
            if getattr(plain_reader, "is_encrypted", False):
                plain_reader.decrypt("")
        return plain_reader.pages[page_index].extract_text() or ""

    acc = _TextAccumulator(max_chars)
    notes: List[str] = []
    any_page_failed = False
    any_nonempty_pass = False
    stopped_at_page: Optional[int] = None
    limit = min(total_pages, MAX_PDF_PAGES)
    pages_done = 0

    for idx in range(limit):
        if acc.truncated:
            break
        if soft_deadline is not None and idx >= 1 and time.monotonic() >= soft_deadline:
            stopped_at_page = idx
            break
        pages_done += 1
        try:
            page = reader.pages[idx]
        except MemoryError:
            raise
        except Exception as exc:
            any_page_failed = True
            notes.append(f"Page {idx + 1} failed to parse ({type(exc).__name__}).")
            continue

        try:
            raw_layout = page.extract_text(extraction_mode="layout") or ""
        except MemoryError:
            raise
        except Exception as layout_err:
            try:
                raw_plain_fb = _extract_unpatched_plain(idx, page)
            except MemoryError:
                raise
            except Exception:
                any_page_failed = True
                notes.append(
                    f"Page {idx + 1} failed to parse ({type(layout_err).__name__})."
                )
                continue
            notes.append(
                f"Page {idx + 1} layout extraction failed "
                f"({type(layout_err).__name__}); fell back to plain mode."
            )
            if raw_plain_fb.strip():
                any_nonempty_pass = True
                acc.append_block(raw_plain_fb)
            continue

        layout_ns = len("".join(raw_layout.split()))
        if layout_ns > 0:
            any_nonempty_pass = True

        is_rtl = layout_ns > 0 and has_rtl_letters(raw_layout)
        need_plain = (layout_ns == 0) or is_rtl or _page_has_form_xobject(page)
        raw_plain: Optional[str] = None
        plain_ns = 0
        if need_plain:
            try:
                raw_plain = _extract_unpatched_plain(idx, page)
            except MemoryError:
                raise
            except Exception:
                raw_plain = None
            if raw_plain:
                plain_ns = len("".join(raw_plain.split()))
                if plain_ns > 0:
                    any_nonempty_pass = True

        use_plain_for_invisible = raw_plain is not None and (
            (layout_ns == 0 and plain_ns > 0)
            or (plain_ns >= layout_ns + 20 and plain_ns * 5 > layout_ns * 6)
        )
        if use_plain_for_invisible and raw_plain is not None:
            notes.append(
                f"Page {idx + 1}: read in file order because part of the page "
                "is drawn in a way the layout pass does not read."
            )
            norm_plain = normalize_arabic_presentation_forms(raw_plain)
            if norm_plain.strip():
                acc.append_block(norm_plain)
            continue

        if layout_ns == 0:
            continue

        if is_rtl:
            converted, page_notes = page_reading_order(
                raw_layout, raw_plain, word=word_doc
            )
            for pnote in page_notes:
                notes.append(f"Page {idx + 1}: {pnote}")
            if converted.strip():
                acc.append_block(converted)
        else:
            acc.append_block(raw_layout)

    if stopped_at_page is not None:
        notes.append(
            f"Stopped after page {stopped_at_page} of {total_pages} "
            "(time limit); the rest was not read."
        )
    elif total_pages > MAX_PDF_PAGES:
        notes.append(f"PDF truncated at {MAX_PDF_PAGES} of {total_pages} pages.")

    body = acc.finish(strip_trailing_spaces=False)
    if (
        total_pages > 0
        and not any_nonempty_pass
        and not body.strip()
        and not any_page_failed
        and stopped_at_page is None
    ):
        notes.append("PDF has no text layer (pages contain no extractable text).")
        body = ""

    return _finalize_result(
        body,
        extracted_from="pdf",
        pages=total_pages,
        pages_read=pages_done,
        notes=notes,
        truncated_at=acc.max_chars if acc.truncated else None,
        partial=(
            stopped_at_page is not None
            or total_pages > MAX_PDF_PAGES
            or acc.truncated
            or pages_done < total_pages
        ),
        preserve_layout=True,
    )


# ---------------------------------------------------------------------------
# Charset allow-list, single-pass O(n) HTML extractor, and Plain Text
# ---------------------------------------------------------------------------


def _resolve_allowed_charset(name: Optional[str]) -> str:
    if not name:
        return "utf-8"
    cleaned = name.strip().strip("\"'").lower()
    return _CHARSET_ALLOWLIST.get(cleaned, "utf-8")


def _decode_text_bytes(data: bytes, *, sniff_html_meta: bool = False) -> str:
    """Decode bytes using only the strict charset allow-list and strip BOM."""
    if data.startswith(b"\xef\xbb\xbf"):
        return sanitize_surrogates(data[3:].decode("utf-8", errors="replace").lstrip("\ufeff"))
    if data.startswith(b"\xff\xfe"):
        return sanitize_surrogates(data[2:].decode("utf-16-le", errors="replace").lstrip("\ufeff"))
    if data.startswith(b"\xfe\xff"):
        return sanitize_surrogates(data[2:].decode("utf-16-be", errors="replace").lstrip("\ufeff"))
    codec = "utf-8"
    if sniff_html_meta:
        m = _META_TAG_CHARSET_RE.search(data[:2048])
        if m:
            declared = m.group(1).decode("ascii", errors="ignore")
            codec = _resolve_allowed_charset(declared)
    return sanitize_surrogates(data.decode(codec, errors="replace").lstrip("\ufeff"))


def _html_to_text_bounded(
    html: str, *, max_chars: int = MAX_EXTRACTED_CHARS
) -> Tuple[str, bool]:
    """Single-pass O(n) HTML -> plain text converter shared with ``bodyclean``."""
    if not html:
        return "", False
    text_out, truncated = html_to_text_bounded(
        html,
        max_chars=max(1, int(max_chars)),
        transform=normalize_arabic_presentation_forms,
    )
    return sanitize_surrogates(text_out), truncated


def extract_html(
    data: bytes,
    *,
    max_bytes: Optional[int] = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_chars: int = MAX_EXTRACTED_CHARS,
) -> ExtractionResult:
    """Clean an .html / .htm byte payload in linear time with streaming cap."""
    _check_attachment_ceiling(data, max_bytes)
    decoded = _decode_text_bytes(data, sniff_html_meta=True)
    cleaned, was_truncated = _html_to_text_bounded(decoded, max_chars=max_chars)
    return _finalize_result(
        cleaned,
        extracted_from="html",
        truncated_at=max_chars if was_truncated else None,
    )


def _trim_incomplete_utf8_tail(data: bytes, start: int, end: int) -> int:
    """Step back up to 3 bytes if ``end < len(data)`` splits a UTF-8 sequence."""
    if end >= len(data) or end <= start:
        return end
    k = 1
    while k <= 3 and (end - k) >= start:
        b = data[end - k]
        if 0x80 <= b <= 0xBF:
            k += 1
            continue
        if b < 0x80:
            return end
        if 0xC2 <= b <= 0xDF:
            return (end - k) if k < 2 else end
        if 0xE0 <= b <= 0xEF:
            return (end - k) if k < 3 else end
        if 0xF0 <= b <= 0xF4:
            return end - k
        return end
    return end


def _trim_incomplete_utf16_tail(
    data: bytes, start: int, end: int, *, little_endian: bool
) -> int:
    """Align ``end`` to a 2-byte unit and avoid splitting a UTF-16 surrogate pair."""
    if end <= start:
        return start
    if end < len(data) and ((end - start) & 1):
        end -= 1
    if end < len(data) and (end - start) >= 2:
        b0 = data[end - 2]
        b1 = data[end - 1]
        unit = (b0 | (b1 << 8)) if little_endian else ((b0 << 8) | b1)
        if 0xD800 <= unit <= 0xDBFF:
            end -= 2
    return end


def extract_plain_text(
    data: bytes,
    *,
    max_bytes: Optional[int] = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_chars: int = MAX_EXTRACTED_CHARS,
) -> ExtractionResult:
    """Decode and slice plain-text bytes with BOM handling and bounded byte reads.

    Matches ``main`` semantics (decode and slice, no per-line loop and no
    line-ending normalization) while decoding at most the bytes ``max_chars``
    can require.
    """
    _check_attachment_ceiling(data, max_bytes)
    cap = max(1, int(max_chars))
    total_bytes = len(data)

    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        is_le = data.startswith(b"\xff\xfe")
        u16_codec = "utf-16-le" if is_le else "utf-16-be"
        start = 2
        end1 = _trim_incomplete_utf16_tail(
            data, start, min(total_bytes, start + (cap + 2) * 2), little_endian=is_le
        )
        decoded = data[start:end1].decode(u16_codec, errors="replace")
        end_used = end1
        if len(decoded) <= cap + 1 and end1 < total_bytes:
            end2 = _trim_incomplete_utf16_tail(
                data,
                end1,
                min(total_bytes, start + (cap + 2) * 4),
                little_endian=is_le,
            )
            decoded += data[end1:end2].decode(u16_codec, errors="replace")
            end_used = end2
    else:
        start = 3 if data.startswith(b"\xef\xbb\xbf") else 0
        end1 = _trim_incomplete_utf8_tail(
            data, start, min(total_bytes, start + cap + 4)
        )
        decoded = data[start:end1].decode("utf-8", errors="replace")
        end_used = end1
        if len(decoded) <= cap + 1 and end1 < total_bytes:
            end2 = _trim_incomplete_utf8_tail(
                data, end1, min(total_bytes, start + (cap + 2) * 4)
            )
            decoded += data[end1:end2].decode("utf-8", errors="replace")
            end_used = end2

    if decoded.startswith("\ufeff"):
        decoded = decoded[1:]

    was_truncated = (len(decoded) > cap) or (end_used < total_bytes)
    out_text = sanitize_surrogates(decoded[:cap])
    if was_truncated:
        trunc_note = cap_message(
            f"Extracted text stopped at {cap} characters.", MAX_NOTE_CHARS
        )
        return ExtractionResult(
            text=out_text,
            extracted_from="text",
            partial=True,
            notes=(trunc_note,),
        )
    return ExtractionResult(text=out_text, extracted_from="text")


# ---------------------------------------------------------------------------
# RFC-822 (.eml) via compat32 + manual bounded header decoding & ItemAttachment
# ---------------------------------------------------------------------------


def _raw_header_str(msg: EmailMsg, name: str) -> Optional[Any]:
    """Return the raw compat32 header value from ``msg._headers`` before Header wrapping."""
    raw_list = getattr(msg, "_headers", None)
    if isinstance(raw_list, list):
        target = name.lower()
        for key, val in raw_list:
            if isinstance(key, str) and key.lower() == target:
                return val
    return msg.get(name)


def _decode_bytes_utf8_or_cp1256(
    raw_bytes: bytes, *, cap_for_header: bool = True
) -> Tuple[str, bool]:
    """Decode raw 8-bit bytes as UTF-8 (strict), falling back to Windows-1256."""
    if cap_for_header and len(raw_bytes) > _MAX_RAW_HEADER_BYTES:
        was_byte_cut = True
        work = raw_bytes[:_MAX_RAW_HEADER_BYTES]
    else:
        was_byte_cut = False
        work = raw_bytes

    try:
        return work.decode("utf-8", errors="strict"), was_byte_cut
    except UnicodeDecodeError as exc:
        if was_byte_cut and exc.end == len(work) and (len(work) - exc.start) <= 3:
            try:
                return work[: exc.start].decode("utf-8", errors="strict"), True
            except UnicodeDecodeError:
                pass
        return work.decode("cp1256", errors="replace"), was_byte_cut


def _recover_surrogate_text(
    text: str, *, cap_for_header: bool = True
) -> Tuple[str, bool]:
    """Recover PEP-383 surrogateescape bytes and decode UTF-8 -> Windows-1256 before cutting."""
    if not _SURROGATE_RE.search(text):
        if cap_for_header:
            was_cut = len(text) > MAX_EML_HEADER_CHARS
            return text[:MAX_EML_HEADER_CHARS], was_cut
        return text, False

    if cap_for_header and len(text) > _MAX_RAW_HEADER_BYTES:
        was_pre_cut = True
        work = text[:_MAX_RAW_HEADER_BYTES]
    else:
        was_pre_cut = False
        work = text

    safe_work = (
        _NON_SURROGATEESCAPE_RE.sub("\ufffd", work)
        if _NON_SURROGATEESCAPE_RE.search(work)
        else work
    )
    raw_bytes = safe_work.encode("utf-8", errors="surrogateescape")
    decoded, was_bcut = _decode_bytes_utf8_or_cp1256(
        raw_bytes, cap_for_header=cap_for_header
    )
    if cap_for_header:
        was_cut = was_pre_cut or was_bcut or (len(decoded) > MAX_EML_HEADER_CHARS)
        return decoded[:MAX_EML_HEADER_CHARS], was_cut
    return decoded, False


def _decode_header_chunks(chunks: List[Tuple[Any, Optional[str]]]) -> Tuple[str, bool]:
    pieces: List[str] = []
    any_cut = False
    total_chars = 0
    for part, enc in chunks:
        if isinstance(part, bytes):
            enc_norm = (enc or "").strip().lower()
            if not enc_norm or enc_norm == "unknown-8bit":
                seg, bcut = _decode_bytes_utf8_or_cp1256(part, cap_for_header=True)
                any_cut = any_cut or bcut
            else:
                codec = _resolve_allowed_charset(enc)
                if len(part) > _MAX_RAW_HEADER_BYTES:
                    any_cut = True
                    part = part[:_MAX_RAW_HEADER_BYTES]
                seg = part.decode(codec, errors="replace")
        else:
            seg, scut = _recover_surrogate_text(str(part), cap_for_header=True)
            any_cut = any_cut or scut
        pieces.append(seg)
        total_chars += len(seg)
        if total_chars > MAX_EML_HEADER_CHARS:
            any_cut = True
            break
    joined = "".join(pieces)
    if len(joined) > MAX_EML_HEADER_CHARS:
        any_cut = True
        joined = joined[:MAX_EML_HEADER_CHARS]
    return joined, any_cut


def _decode_raw_header_value(raw_val: Any) -> Tuple[str, bool]:
    """Decode raw 8-bit / RFC-2047 compat32 header value and cut to 4,000 chars."""
    if raw_val is None:
        return "", False

    if isinstance(raw_val, (bytes, bytearray)):
        decoded_b, was_bcut = _decode_bytes_utf8_or_cp1256(
            bytes(raw_val), cap_for_header=True
        )
        was_cut = was_bcut or (len(decoded_b) > MAX_EML_HEADER_CHARS)
        sliced = decoded_b[:MAX_EML_HEADER_CHARS]
    elif isinstance(raw_val, email.header.Header):
        try:
            hdr_chunks = email.header.decode_header(raw_val)
        except MemoryError:
            raise
        except Exception:
            sliced, was_cut = _recover_surrogate_text(str(raw_val), cap_for_header=True)
        else:
            joined_hdr, was_cut = _decode_header_chunks(hdr_chunks)
            folded_hdr = " ".join(
                joined_hdr.replace("\r", " ").replace("\n", " ").split()
            )
            return sanitize_surrogates(folded_hdr), was_cut
    elif isinstance(raw_val, str):
        sliced, was_cut = _recover_surrogate_text(raw_val, cap_for_header=True)
    else:
        sliced, was_cut = _recover_surrogate_text(str(raw_val), cap_for_header=True)

    if "=?" not in sliced:
        folded = " ".join(sliced.replace("\r", " ").replace("\n", " ").split())
        return sanitize_surrogates(folded), was_cut

    try:
        chunks = email.header.decode_header(sliced)
    except MemoryError:
        raise
    except Exception:
        folded = " ".join(sliced.replace("\r", " ").replace("\n", " ").split())
        return sanitize_surrogates(folded), was_cut

    joined, chunk_cut = _decode_header_chunks(chunks)
    folded = " ".join(joined.replace("\r", " ").replace("\n", " ").split())
    return sanitize_surrogates(folded), (was_cut or chunk_cut)


def _format_bounded_address_header(raw_val: Any) -> str:
    """Decode an address header (cut to 4,000 chars) and list at most 50 addresses."""
    decoded, was_cut = _decode_raw_header_value(raw_val)
    if not decoded:
        return ""

    entries: List[str] = []
    cur: List[str] = []
    in_quote = False
    paren_depth = 0
    n = len(decoded)
    i = 0
    while i < n:
        ch = decoded[i]
        if in_quote:
            cur.append(ch)
            if ch == "\\" and i + 1 < n:
                cur.append(decoded[i + 1])
                i += 2
                continue
            if ch == "\"":
                in_quote = False
            i += 1
            continue
        if ch == "\"":
            in_quote = True
            cur.append(ch)
        elif ch == "(":
            paren_depth += 1
            cur.append(ch)
        elif ch == ")" and paren_depth > 0:
            paren_depth -= 1
            cur.append(ch)
        elif ch in (",", ";") and paren_depth == 0:
            item = "".join(cur).strip()
            if item:
                entries.append(item)
            cur = []
        else:
            cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        entries.append(tail)

    if not entries:
        return ""

    shown = [_safe_snippet(e, 160) for e in entries[:MAX_HEADER_ADDRESSES]]
    rendered = ", ".join(shown)
    extra = len(entries) - MAX_HEADER_ADDRESSES
    if extra > 0:
        suffix = f"{extra}+ more" if was_cut else f"{extra} more"
        rendered = f"{rendered} (+{suffix})"
    elif was_cut:
        rendered = f"{rendered} (header truncated at {MAX_EML_HEADER_CHARS} chars)"
    return rendered


def _extract_part_filename(part: EmailMsg) -> Optional[str]:
    raw_cd = _raw_header_str(part, "Content-Disposition")
    if raw_cd is not None:
        cd_text, cd_cut = _decode_raw_header_value(raw_cd)
        if "filename*" not in cd_text.lower():
            m = _CD_FILENAME_RE.search(cd_text)
            if m:
                cand = m.group(1) or m.group(2) or m.group(3) or ""
                if "=?" in cand:
                    cand, _ = _decode_raw_header_value(cand)
                return _safe_snippet(cand, 120) or ("attachment" if cd_cut else None)
            if cd_cut:
                return "attachment"
        elif cd_cut:
            return "attachment"

    raw_ct = _raw_header_str(part, "Content-Type")
    if raw_ct is not None:
        ct_text, ct_cut = _decode_raw_header_value(raw_ct)
        if ct_cut:
            return None
        if "name*" not in ct_text.lower():
            m_ct = _CT_NAME_RE.search(ct_text)
            if m_ct:
                cand_ct = m_ct.group(1) or m_ct.group(2) or m_ct.group(3) or ""
                if "=?" in cand_ct:
                    cand_ct, _ = _decode_raw_header_value(cand_ct)
                return _safe_snippet(cand_ct, 120) or None

    try:
        fname = part.get_filename()
    except MemoryError:
        raise
    except Exception:
        fname = None
    if not fname:
        return None
    decoded, _ = _decode_raw_header_value(fname)
    return _safe_snippet(decoded, 120) or None


def _decode_compat32_part(part: EmailMsg, *, max_bytes_hint: int) -> str:
    payload = part.get_payload(decode=True)
    if isinstance(payload, (bytes, bytearray)):
        raw_bytes = bytes(payload)
        if raw_bytes.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
            return _decode_text_bytes(raw_bytes[:max_bytes_hint], sniff_html_meta=False)
        raw_ct = _raw_header_str(part, "Content-Type")
        charset: Optional[str] = None
        if raw_ct is not None and len(str(raw_ct)) <= MAX_EML_HEADER_CHARS:
            try:
                charset = part.get_content_charset()
            except Exception:
                charset = None
        codec = _resolve_allowed_charset(charset)
        return sanitize_surrogates(
            raw_bytes[:max_bytes_hint].decode(codec, errors="replace").lstrip("\ufeff")
        )
    raw_payload = part.get_payload()
    if isinstance(raw_payload, str):
        s = raw_payload[:max_bytes_hint]
        if _SURROGATE_RE.search(s):
            s, _ = _recover_surrogate_text(s, cap_for_header=False)
        return sanitize_surrogates(s.lstrip("\ufeff"))
    return ""


def _count_eml_non_root_parts(msg: EmailMsg) -> int:
    total = 0
    stack: List[Tuple[EmailMsg, int, bool]] = [(msg, 0, True)]
    seen = 0
    while stack and seen < MAX_EML_PARTS:
        node, depth, is_root = stack.pop()
        if depth > MAX_MIME_DEPTH:
            break
        seen += 1
        if not is_root:
            total += 1
        if node.is_multipart():
            payload = node.get_payload()
            if isinstance(payload, list):
                for child in reversed(payload):
                    if isinstance(child, EmailMsg):
                        stack.append((child, depth + 1, False))
    return max(1, total)


def _format_message_into_accumulator(
    acc: _TextAccumulator,
    *,
    sender: str,
    to: str,
    cc: str,
    date_str: str,
    subject: str,
    body_text: str,
    body_already_truncated: bool,
    attachment_names: List[str],
    extra_attachments: int = 0,
) -> None:
    headers: List[str] = []
    if sender:
        headers.append(f"From: {sender}")
    if to:
        headers.append(f"To: {to}")
    if cc:
        headers.append(f"Cc: {cc}")
    if date_str:
        headers.append(f"Date: {date_str}")
    if subject:
        headers.append(f"Subject: {subject}")

    if headers:
        acc.append("\n".join(headers))

    clean_body = _BLANK_LINES_RE.sub(
        "\n\n", (body_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    )
    if clean_body:
        acc.append_block(clean_body)
    if body_already_truncated:
        acc.truncated = True

    if attachment_names and not acc.truncated:
        att_line = "Attachments: " + ", ".join(attachment_names)
        if extra_attachments > 0:
            att_line += f" (+{extra_attachments} more)"
        acc.append_block(att_line)


def extract_eml(
    data: bytes,
    *,
    max_bytes: Optional[int] = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_chars: int = MAX_EXTRACTED_CHARS,
    soft_deadline: Optional[float] = None,
) -> ExtractionResult:
    """Parse an RFC-822 .eml message with compat32 policy and bounded headers."""
    _check_attachment_ceiling(data, max_bytes)
    if not data or not data.strip():
        raise _validation_error("Empty .eml attachment.")

    # Reject obvious binary headers before feeding email parser.
    head = data[:16]
    if head.startswith((b"PK\x03\x04", b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xd0\xcf\x11\xe0")):
        raise _validation_error("File content is binary, not a valid RFC-822 (.eml) message.")

    try:
        msg = email.message_from_bytes(data, policy=email.policy.compat32)
    except MemoryError:
        raise
    except Exception as exc:
        raise _validation_error(
            f"Failed to parse .eml message: {_safe_snippet(exc)}"
        ) from None

    has_mail_header = any(
        _raw_header_str(msg, h) is not None
        for h in (
            "From", "To", "Cc", "Subject", "Date", "Message-ID",
            "MIME-Version", "Content-Type", "Received", "Sender",
        )
    )
    if not msg.keys() or not has_mail_header:
        raise _validation_error(
            "File is not a valid RFC-822 (.eml) email message (missing mail headers)."
        )

    sender = _format_bounded_address_header(_raw_header_str(msg, "From"))
    to_hdr = _format_bounded_address_header(_raw_header_str(msg, "To"))
    cc_hdr = _format_bounded_address_header(_raw_header_str(msg, "Cc"))
    date_str, _ = _decode_raw_header_value(_raw_header_str(msg, "Date"))
    subject, _ = _decode_raw_header_value(_raw_header_str(msg, "Subject"))

    # Iterative MIME tree walk (bounded depth and part count).
    plain_part: Optional[EmailMsg] = None
    html_part: Optional[EmailMsg] = None
    attachment_candidates: List[Tuple[EmailMsg, Optional[str], str]] = []
    extra_attachments = 0
    parts_seen = 0
    non_root_done = 0
    stopped_at_part: Optional[int] = None
    depth_cut = False
    notes: List[str] = []
    total_parts = _count_eml_non_root_parts(msg) if soft_deadline is not None else 1

    stack: List[Tuple[EmailMsg, int, bool]] = [(msg, 0, True)]
    while stack and parts_seen < MAX_EML_PARTS:
        node, depth, is_root = stack.pop()
        if depth > MAX_MIME_DEPTH:
            depth_cut = True
            continue
        if (
            soft_deadline is not None
            and not is_root
            and non_root_done >= 1
            and time.monotonic() >= soft_deadline
        ):
            stopped_at_part = non_root_done
            break
        parts_seen += 1
        raw_ct = _raw_header_str(node, "Content-Type")
        ct = (
            node.get_content_type()
            if raw_ct is None or len(str(raw_ct)) <= MAX_EML_HEADER_CHARS
            else _mime_of(str(raw_ct)[:256]) or "text/plain"
        )
        raw_cd = _raw_header_str(node, "Content-Disposition")
        disp_head = str(raw_cd)[:64].lower() if raw_cd is not None else ""
        is_att_disp = disp_head.lstrip().startswith("attachment")
        fname = _extract_part_filename(node) if not is_root else None
        if not is_root:
            non_root_done += 1

        if not is_root and (is_att_disp or fname):
            if len(attachment_candidates) < MAX_EML_ATTACHMENTS:
                attachment_candidates.append((node, fname, ct))
            else:
                extra_attachments += 1
            continue

        if node.is_multipart():
            payload = node.get_payload()
            if isinstance(payload, list):
                for child in reversed(payload):
                    if isinstance(child, EmailMsg):
                        stack.append((child, depth + 1, False))
            continue

        if ct == "text/plain" and plain_part is None:
            plain_part = node
        elif ct == "text/html" and html_part is None:
            html_part = node

    parts_cut = bool(stack and parts_seen >= MAX_EML_PARTS and stopped_at_part is None)
    if stopped_at_part is not None:
        notes.append(
            f"Stopped after message part {stopped_at_part} of {total_parts} "
            "(time limit); the rest was not read."
        )
    elif parts_cut:
        notes.append(f"Message truncated at {MAX_EML_PARTS} MIME parts.")
    if depth_cut:
        notes.append(
            f"Message MIME nesting exceeded {MAX_MIME_DEPTH} levels; "
            "deeper parts were skipped."
        )

    # Byte ceiling for decoding a single body part (~4 bytes/char + markup headroom).
    decode_byte_cap = max(max_chars * 6, 1_048_576)
    body_text = ""
    body_truncated = False
    chosen_part: Optional[EmailMsg] = None

    if plain_part is not None:
        cand = _decode_compat32_part(plain_part, max_bytes_hint=decode_byte_cap)
        if cand.strip():
            if len(cand) > max_chars:
                body_text = cand[:max_chars]
                body_truncated = True
            else:
                body_text = cand
            chosen_part = plain_part

    if not body_text and html_part is not None:
        html_raw = _decode_compat32_part(html_part, max_bytes_hint=decode_byte_cap)
        body_text, body_truncated = _html_to_text_bounded(html_raw, max_chars=max_chars)
        chosen_part = html_part

    if not body_text and plain_part is not None and chosen_part is None:
        cand = _decode_compat32_part(plain_part, max_bytes_hint=decode_byte_cap)
        if len(cand) > max_chars:
            body_text = cand[:max_chars]
            body_truncated = True
        else:
            body_text = cand
        chosen_part = plain_part

    attachment_names: List[str] = []
    for att_node, fname, ct in attachment_candidates:
        if att_node is chosen_part:
            continue
        if not fname and ct == "message/rfc822":
            nested = att_node.get_payload()
            nested_msg = (
                nested[0]
                if isinstance(nested, list) and nested and isinstance(nested[0], EmailMsg)
                else None
            )
            subj = ""
            if nested_msg is not None:
                subj, _ = _decode_raw_header_value(_raw_header_str(nested_msg, "Subject"))
            fname = f"{_safe_snippet(subj, 80)}.eml" if subj else "attached-message.eml"
        attachment_names.append(fname or f"attachment ({_safe_snippet(ct, 40)})")

    acc = _TextAccumulator(max_chars)
    _format_message_into_accumulator(
        acc,
        sender=sender,
        to=to_hdr,
        cc=cc_hdr,
        date_str=date_str,
        subject=subject,
        body_text=body_text,
        body_already_truncated=body_truncated,
        attachment_names=attachment_names,
        extra_attachments=extra_attachments,
    )
    return _finalize_result(
        acc.finish(),
        extracted_from="eml",
        notes=notes,
        truncated_at=acc.max_chars if acc.truncated else None,
        partial=(
            stopped_at_part is not None
            or parts_cut
            or depth_cut
            or extra_attachments > 0
            or body_truncated
            or acc.truncated
        ),
    )


def _fmt_mailbox(mb: Any) -> str:
    if mb is None:
        return ""
    if isinstance(mb, str):
        return _safe_snippet(mb.strip(), 160)
    inner = getattr(mb, "mailbox", None)
    if inner is not None:
        mb = inner
    name = _safe_snippet(getattr(mb, "name", "") or "", 120)
    addr = _safe_snippet(getattr(mb, "email_address", "") or "", 120)
    if name and addr and name != addr:
        return f"{name} <{addr}>"
    return addr or name


def _fmt_mailbox_list(items: Any) -> str:
    if not items:
        return ""
    raw_list = list(items)
    formatted = [_fmt_mailbox(it) for it in raw_list[:MAX_HEADER_ADDRESSES]]
    non_empty = [f for f in formatted if f]
    extra = len(raw_list) - MAX_HEADER_ADDRESSES
    rendered = ", ".join(non_empty)
    if extra > 0 and rendered:
        rendered = f"{rendered} (+{extra} more)"
    return rendered


def _fmt_item_dt(value: Any, tz: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _safe_snippet(value.strip(), 80)
    if hasattr(value, "astimezone"):
        try:
            return value.astimezone(ZoneInfo(tz)).isoformat(timespec="minutes")
        except Exception:
            return value.isoformat() if isinstance(value, datetime) else _safe_snippet(value, 80)
    if isinstance(value, date):
        return value.isoformat()
    return _safe_snippet(value, 80)


def serialize_message_item_fields(item: Any, *, tz: str = "UTC") -> Dict[str, Any]:
    """Extract plain JSON-serializable fields from an Exchange ItemAttachment.item.

    Allows ``mail_read.get_attachment`` to release the EWS gateway thread right
    after ``GetAttachment`` and perform all HTML/MIME body parsing inside
    ``extract_worker``.
    """
    if item is None:
        raise _validation_error("Attached message item is empty.")
    sender = _fmt_mailbox(
        getattr(item, "sender", None)
        or getattr(item, "author", None)
        or getattr(item, "organizer", None)
    )
    to_hdr = _fmt_mailbox_list(
        getattr(item, "to_recipients", None)
        or getattr(item, "required_attendees", None)
    )
    cc_hdr = _fmt_mailbox_list(
        getattr(item, "cc_recipients", None)
        or getattr(item, "optional_attendees", None)
    )
    dt_val = (
        getattr(item, "datetime_sent", None)
        or getattr(item, "datetime_received", None)
        or getattr(item, "start", None)
    )
    date_str = _fmt_item_dt(dt_val, tz)
    raw_subj = str(getattr(item, "subject", "") or "").strip()
    subject = sanitize_surrogates(raw_subj[:MAX_EML_HEADER_CHARS])

    text_body = str(getattr(item, "text_body", "") or "")
    raw_body_obj = getattr(item, "body", None)
    body_str = str(raw_body_obj) if raw_body_obj is not None and not text_body.strip() else ""

    nested_atts = list(getattr(item, "attachments", None) or [])
    att_names = [
        _safe_snippet(getattr(att, "name", None) or f"attachment-{idx}", 120)
        for idx, att in enumerate(nested_atts[:MAX_EML_ATTACHMENTS])
    ]
    extra_atts = max(0, len(nested_atts) - MAX_EML_ATTACHMENTS)

    mime = getattr(item, "mime_content", None)
    mime_bytes: Optional[bytes] = None
    if (
        not text_body.strip()
        and not body_str.strip()
        and not any((sender, to_hdr, cc_hdr, subject))
        and isinstance(mime, (bytes, bytearray))
        and mime
    ):
        mime_bytes = bytes(mime)

    return {
        "sender": sender,
        "to": to_hdr,
        "cc": cc_hdr,
        "date_str": date_str,
        "subject": subject,
        "text_body": text_body,
        "body_str": body_str,
        "att_names": att_names,
        "extra_atts": extra_atts,
        "mime_bytes": mime_bytes,
    }


def extract_serialized_message_item(
    fields: Dict[str, Any],
    *,
    mime_bytes: Optional[bytes] = None,
    max_bytes: Optional[int] = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_chars: int = MAX_EXTRACTED_CHARS,
    soft_deadline: Optional[float] = None,
) -> ExtractionResult:
    """Render serialized item-attachment fields (including HTML/MIME) into text."""
    effective_mime = mime_bytes if mime_bytes is not None else fields.get("mime_bytes")
    if isinstance(effective_mime, (bytes, bytearray)) and effective_mime:
        eml_res = extract_eml(
            bytes(effective_mime),
            max_bytes=max_bytes,
            max_chars=max_chars,
            soft_deadline=soft_deadline,
        )
        return ExtractionResult(
            text=eml_res.text,
            extracted_from="message",
            partial=eml_res.partial,
            notes=eml_res.notes,
        )

    text_body = str(fields.get("text_body") or "")
    body_str = str(fields.get("body_str") or "")
    body_truncated = False

    if text_body.strip():
        cleaned = sanitize_surrogates(text_body)
        if len(cleaned) > max_chars:
            body_text = cleaned[:max_chars]
            body_truncated = True
        else:
            body_text = cleaned
    elif body_str:
        clean_raw = sanitize_surrogates(body_str)
        if "<" in clean_raw:
            body_text, body_truncated = _html_to_text_bounded(
                clean_raw, max_chars=max_chars
            )
        elif len(clean_raw) > max_chars:
            body_text = clean_raw[:max_chars]
            body_truncated = True
        else:
            body_text = clean_raw
    else:
        body_text = ""

    extra_atts_count = int(fields.get("extra_atts") or 0)
    acc = _TextAccumulator(max_chars)
    _format_message_into_accumulator(
        acc,
        sender=sanitize_surrogates(str(fields.get("sender") or "")),
        to=sanitize_surrogates(str(fields.get("to") or "")),
        cc=sanitize_surrogates(str(fields.get("cc") or "")),
        date_str=sanitize_surrogates(str(fields.get("date_str") or "")),
        subject=sanitize_surrogates(str(fields.get("subject") or "")),
        body_text=body_text,
        body_already_truncated=body_truncated,
        attachment_names=[
            sanitize_surrogates(str(n)) for n in (fields.get("att_names") or [])
        ],
        extra_attachments=extra_atts_count,
    )
    return _finalize_result(
        acc.finish(),
        extracted_from="message",
        truncated_at=acc.max_chars if acc.truncated else None,
        partial=body_truncated or acc.truncated or extra_atts_count > 0,
    )


def extract_message_item(
    item: Any,
    *,
    tz: str = "UTC",
    max_chars: int = MAX_EXTRACTED_CHARS,
) -> ExtractionResult:
    """Format an attached Exchange item (ItemAttachment.item) as readable text."""
    fields = serialize_message_item_fields(item, tz=tz)
    return extract_serialized_message_item(fields, max_chars=max_chars)


def extract_bytes(
    data: bytes,
    *,
    name: Optional[str] = None,
    content_type: Optional[str] = None,
    max_bytes: Optional[int] = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_chars: int = MAX_EXTRACTED_CHARS,
    soft_deadline: Optional[float] = None,
) -> ExtractionResult:
    """Dispatch bytes extraction by filename / MIME type.

    Every validation or parsing failure raises ``ToolError('validation', ...)``
    with a capped message; ``MemoryError`` propagates so worker memory limits
    report the worker ceiling message cleanly.
    """
    kind = detect_kind(name, content_type)
    if kind is None:
        raise unsupported_type_error(name, content_type)
    try:
        if kind == "pdf":
            return extract_pdf(
                data, max_bytes=max_bytes, max_chars=max_chars, soft_deadline=soft_deadline
            )
        if kind == "docx":
            return extract_docx(
                data, max_bytes=max_bytes, max_chars=max_chars, soft_deadline=soft_deadline
            )
        if kind == "xlsx":
            return extract_xlsx(
                data, max_bytes=max_bytes, max_chars=max_chars, soft_deadline=soft_deadline
            )
        if kind == "pptx":
            return extract_pptx(
                data, max_bytes=max_bytes, max_chars=max_chars, soft_deadline=soft_deadline
            )
        if kind == "eml":
            return extract_eml(
                data, max_bytes=max_bytes, max_chars=max_chars, soft_deadline=soft_deadline
            )
        if kind == "html":
            return extract_html(data, max_bytes=max_bytes, max_chars=max_chars)
        return extract_plain_text(data, max_bytes=max_bytes, max_chars=max_chars)
    except (ToolError, MemoryError):
        raise
    except Exception as exc:
        raise _validation_error(
            f"Failed to read {kind} attachment {_safe_snippet(name or 'attachment')!r}: "
            f"{type(exc).__name__}: {_safe_snippet(exc)}"
        ) from None
