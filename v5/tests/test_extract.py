"""Unit and hostile-input tests for pure bytes-to-text extraction (ewsmcp.extract).

All test inputs are constructed in memory using stdlib zipfile, email, struct,
zlib, and a deterministic minimal PDF builder — no binary fixtures on disk.
"""

from __future__ import annotations

import base64
import io
import math
import random
import struct
import time
import zipfile
import zlib
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from ewsmcp import extract, pdftext
from ewsmcp.errors import ToolError

# ---------------------------------------------------------------------------
# In-memory Office / Zip / PDF / EML builders
# ---------------------------------------------------------------------------

from conftest import TIME_MARGIN  # noqa: E402


def _zip_bytes(members: Sequence[Tuple[str, bytes]], *, compress: bool = True) -> bytes:
    buf = io.BytesIO()
    comp = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, "w", compression=comp) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def _raw_zip_single_member(
    name: str, comp_bytes: bytes, *, method: int = 8, flags: int = 0, crc: int = 0, fsize: int = 10
) -> bytes:
    raw_name = name.encode("utf-8")
    csize = len(comp_bytes)
    lh = struct.pack(
        "<4sHHHHHIIIHH",
        b"PK\x03\x04", 20, flags, method, 0, 0x21, crc, csize, fsize, len(raw_name), 0,
    ) + raw_name + comp_bytes
    cd = struct.pack(
        "<4sHHHHHHIIIHHHHHII",
        b"PK\x01\x02", 20, 20, flags, method, 0, 0x21, crc, csize, fsize,
        len(raw_name), 0, 0, 0, 0, 0, 0,
    ) + raw_name
    eocd = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 1, 1, len(cd), len(lh), 0)
    return lh + cd + eocd


def _docx_bytes(
    body_xml: str,
    *,
    compress: bool = True,
    extra_members: Sequence[Tuple[str, bytes]] = (),
) -> bytes:
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
        f"<w:body>{body_xml}</w:body>"
        "</w:document>"
    ).encode("utf-8")
    return _zip_bytes([("word/document.xml", doc_xml), *extra_members], compress=compress)


def _xlsx_bytes(
    sheets: Sequence[Tuple[str, str]],
    *,
    shared_strings_xml: Optional[str] = None,
    with_rels: bool = True,
    compress: bool = True,
    extra_members: Sequence[Tuple[str, bytes]] = (),
) -> bytes:
    ns_main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns_pkg_rel = "http://schemas.openxmlformats.org/package/2006/relationships"

    sheet_tags = "".join(
        f'<sheet name="{name}" sheetId="{idx}" r:id="rId{idx}"/>'
        for idx, (name, _xml) in enumerate(sheets, start=1)
    )
    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{ns_main}" xmlns:r="{ns_r}">'
        f"<sheets>{sheet_tags}</sheets></workbook>"
    ).encode("utf-8")

    members: List[Tuple[str, bytes]] = [("xl/workbook.xml", wb_xml)]
    if with_rels:
        rel_tags = "".join(
            f'<Relationship Id="rId{idx}" '
            f'Type="{ns_r}/worksheet" Target="worksheets/sheet{idx}.xml"/>'
            for idx in range(1, len(sheets) + 1)
        )
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="{ns_pkg_rel}">{rel_tags}</Relationships>'
        ).encode("utf-8")
        members.append(("xl/_rels/workbook.xml.rels", rels_xml))

    if shared_strings_xml is not None:
        sst_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<sst xmlns="{ns_main}">{shared_strings_xml}</sst>'
        ).encode("utf-8")
        members.append(("xl/sharedStrings.xml", sst_xml))

    for idx, (_name, sheet_data_inner) in enumerate(sheets, start=1):
        ws_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<worksheet xmlns="{ns_main}"><sheetData>{sheet_data_inner}</sheetData></worksheet>'
        ).encode("utf-8")
        members.append((f"xl/worksheets/sheet{idx}.xml", ws_xml))

    members.extend(extra_members)
    return _zip_bytes(members, compress=compress)


def _pptx_bytes(
    slides_by_file: Sequence[Tuple[str, str]],
    *,
    order_targets: Optional[Sequence[str]] = None,
    extra_members: Sequence[Tuple[str, bytes]] = (),
) -> bytes:
    ns_p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    ns_a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns_pkg_rel = "http://schemas.openxmlformats.org/package/2006/relationships"

    targets = list(order_targets or [fname for fname, _xml in slides_by_file])
    sld_ids = "".join(
        f'<p:sldId id="{255 + idx}" r:id="rId{idx}"/>'
        for idx in range(1, len(targets) + 1)
    )
    pres_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<p:presentation xmlns:p="{ns_p}" xmlns:r="{ns_r}">'
        f"<p:sldIdLst>{sld_ids}</p:sldIdLst></p:presentation>"
    ).encode("utf-8")
    rel_tags = "".join(
        f'<Relationship Id="rId{idx}" Type="{ns_r}/slide" Target="{target}"/>'
        for idx, target in enumerate(targets, start=1)
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{ns_pkg_rel}">{rel_tags}</Relationships>'
    ).encode("utf-8")

    members: List[Tuple[str, bytes]] = [
        ("ppt/presentation.xml", pres_xml),
        ("ppt/_rels/presentation.xml.rels", rels_xml),
    ]
    for rel_target, inner_xml in slides_by_file:
        full_name = f"ppt/{rel_target.lstrip('/')}"
        slide_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<p:sld xmlns:p="{ns_p}" xmlns:a="{ns_a}">'
            f"<p:cSld><p:spTree>{inner_xml}</p:spTree></p:cSld></p:sld>"
        ).encode("utf-8")
        members.append((full_name, slide_xml))
    members.extend(extra_members)
    return _zip_bytes(members)


def _pdf_escape_ascii(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_minimal_pdf(pages: Sequence[Dict[str, object]]) -> bytes:
    """Build a valid PDF 1.4 byte stream with exact xref offsets."""
    unicode_chars: List[str] = []
    seen_chars: Dict[str, int] = {}
    raw_cmap_pairs: List[Tuple[bytes, bytes]] = []

    for spec in pages:
        utext = str(spec.get("unicode_text") or "")
        for ch in utext:
            if ch not in seen_chars:
                code = 0x21 + len(unicode_chars)
                assert code <= 0x7E, "Too many distinct glyphs for 1-byte test CMap"
                seen_chars[ch] = code
                unicode_chars.append(ch)
        for pair in spec.get("raw_cmap_pairs") or ():  # type: ignore[attr-defined]
            raw_cmap_pairs.append(pair)  # type: ignore[arg-type]

    bf_entries = [f"<{seen_chars[ch]:02X}> <{ord(ch):04X}>" for ch in unicode_chars]
    bf_entries.extend(
        f"<{a.decode('ascii')}> <{b.decode('ascii')}>" for a, b in raw_cmap_pairs
    )
    bf_section = (
        f"{len(bf_entries)} beginbfchar\n" + "\n".join(bf_entries) + "\nendbfchar\n"
        if bf_entries
        else ""
    )
    cmap_stream = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "/CMapName /Custom def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n"
        "<00> <FF>\n"
        "endcodespacerange\n"
        f"{bf_section}"
        "endcmap\n"
        "CMapName currentdict /CMap defineresource pop\n"
        "end\n"
        "end"
    ).encode("ascii")

    page_obj_nums: List[int] = []
    next_num = 6
    page_objects: List[Tuple[int, bytes]] = []

    for spec in pages:
        page_num = next_num
        content_num = next_num + 1
        next_num += 2
        page_obj_nums.append(page_num)

        if spec.get("corrupt_stream"):
            bad_bytes = b"corrupt-unparseable-page-stream"
            content_obj = (
                f"<< /Filter /UnknownCorruptFilter /Length {len(bad_bytes)} >>\nstream\n"
                .encode("ascii")
                + bad_bytes
                + b"\nendstream"
            )
        elif spec.get("raw_content") is not None:
            raw_content = bytes(spec["raw_content"])  # type: ignore[arg-type]
            comp = zlib.compress(raw_content, 9)
            content_obj = (
                f"<< /Filter /FlateDecode /Length {len(comp)} >>\nstream\n".encode("ascii")
                + comp
                + b"\nendstream"
            )
        else:
            ops: List[str] = ["BT", "72 750 Td"]
            ascii_lines = list(spec.get("ascii_lines") or ())  # type: ignore[arg-type]
            for idx, line in enumerate(ascii_lines):
                if idx > 0:
                    ops.append("0 -18 Td")
                ops.append(f"/F1 12 Tf ({_pdf_escape_ascii(str(line))}) Tj")
            utext = str(spec.get("unicode_text") or "")
            if utext:
                hex_codes = "".join(f"{seen_chars[ch]:02X}" for ch in utext)
                if ascii_lines:
                    ops.append("0 -18 Td")
                ops.append(f"/F2 12 Tf <{hex_codes}> Tj")
            ops.append("ET")
            stream_data = "\n".join(ops).encode("ascii")
            content_obj = (
                f"<< /Length {len(stream_data)} >>\nstream\n".encode("ascii")
                + stream_data
                + b"\nendstream"
            )

        page_dict = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {content_num} 0 R >>"
        ).encode("ascii")
        page_objects.append((page_num, page_dict))
        page_objects.append((content_num, content_obj))

    kids = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objects: List[Tuple[int, bytes]] = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii")),
        (3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
        (4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 5 0 R >>"),
        (
            5,
            f"<< /Length {len(cmap_stream)} >>\nstream\n".encode("ascii")
            + cmap_stream
            + b"\nendstream",
        ),
        *page_objects,
    ]
    objects.sort(key=lambda pair: pair[0])

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: Dict[int, int] = {}
    for obj_num, payload in objects:
        offsets[obj_num] = len(out)
        out.extend(f"{obj_num} 0 obj\n".encode("ascii"))
        out.extend(payload)
        out.extend(b"\nendobj\n")

    xref_offset = len(out)
    total_objs = len(objects) + 1
    out.extend(f"xref\n0 {total_objs}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for num in range(1, total_objs):
        out.extend(f"{offsets[num]:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {total_objs} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


# ---------------------------------------------------------------------------
# 1. Constants, classification & layout line conversion
# ---------------------------------------------------------------------------


def test_named_limit_constants():
    assert extract.MAX_EXTRACTED_CHARS == 5_000_000
    assert extract.MAX_CELL_CHARS == 2_000
    assert extract.MAX_CELL_REF_CHARS == 10
    assert extract.MAX_EML_HEADER_CHARS == 4_000
    assert extract.MAX_HEADER_ADDRESSES == 50
    assert extract.MAX_ARCHIVE_MEMBERS == 2_000
    assert extract.MAX_MEMBER_BYTES == 30 * 1024 * 1024
    assert extract.MAX_XML_PART_BYTES == 20 * 1024 * 1024
    assert extract.MAX_ARCHIVE_TOTAL_BYTES == 100 * 1024 * 1024
    assert extract.MAX_XML_DEPTH == 256
    assert extract.MAX_SHEET_ROWS == 5_000
    assert extract.MAX_SHEET_COLS == 50
    assert extract.MAX_PDF_PAGES == 500
    assert pdftext.MAX_LINE_CHARS == 5_000
    assert extract.MAX_ERROR_MESSAGE_CHARS == 300
    assert extract.MAX_NOTE_CHARS == 300
    assert extract.MAX_ECHOED_TEXT_CHARS == 80


@pytest.mark.parametrize(
    ("name", "ct", "expected"),
    [
        ("report.pdf", "application/octet-stream", "pdf"),
        ("blob", "application/pdf", "pdf"),
        ("memo.docx", None, "docx"),
        ("macro.docm", None, "docx"),
        ("budget.xlsx", None, "xlsx"),
        ("macro.xlsm", None, "xlsx"),
        ("deck.pptx", None, "pptx"),
        ("macro.pptm", None, "pptx"),
        ("forwarded.eml", "application/octet-stream", "eml"),
        ("part", "message/rfc822", "eml"),
        ("page.html", "text/html", "html"),
        ("page.htm", "application/octet-stream", "html"),
        ("notes.txt", "application/octet-stream", "text"),
        ("data.csv", "text/csv", "text"),
        ("readme.md", None, "text"),
        ("trace.log", None, "text"),
        ("config.json", "application/json", "text"),
        ("plain", "text/plain; charset=utf-8", "text"),
        ("outlook.msg", "application/vnd.ms-outlook", None),
        ("saved.msg", "text/plain", None),
        ("saved.MSG", "message/rfc822", None),
        ("diagram.png", "image/png", None),
        ("bundle.zip", "application/zip", None),
        ("legacy.doc", "application/msword", None),
    ],
)
def test_detect_kind(name, ct, expected):
    assert extract.detect_kind(name, ct) == expected


def test_pdf_rtl_layout_conversion_vectors_and_long_line_guard():
    pypdf_layout_lines = [
        " " * 55 + "يﻮﻨﺴﻟا ﺔﻌﺟاﺮﻤﻟا ﺮﻳﺮﻘﺗ",
        (
            " .ﺮﻄﺴﻟا ﻞﺧاد English ﺔﻤﻠﻛو %15 و 2026 "
            "ﻞﺜﻣ مﺎﻗرأ ﻰﻠﻋ يﻮﺘﺤﺗ ﺔﻴﺑﺮﻌﻟا ﺔﻐﻠﻟﺎﺑ ﺔﻴﺒﻳﺮﺠﺗ ةﺮﻘﻓ هﺬﻫ"
        ),
        (
            "            .ﻒﻠﻤﻟا ﻦﻣ ﻪﺟاﺮﺨﺘﺳا ﺪﻨﻋ ﺢﻴﺤﺼﻟا ﻲﻘﻄﻨﻤﻟا "
            "ﺐﻴﺗﺮﺘﻟﺎﺑ ﺺﻨﻟا ﺮﻬﻈﻳ نأ ﺐﺠﻳ :ﺔﻴﻧﺎﺜﻟا ةﺮﻘﻔﻟا"
        ),
        " " * 83 + "ﺔﻤﻴﻘﻟا       ﺪﻨﺒﻟا",
        " " * 87 + "500    ﺔﻴﻧاﺰﻴﻤﻟا",
        " " * 82 + "اﺮﻬﺷ 12        ةﺪﻤﻟا",
        "An English paragraph follows, left to right, with the number 42.",
    ]
    expected_lines = [
        "تقرير المراجعة السنوي",
        (
            "هذه فقرة تجريبية باللغة العربية تحتوي على أرقام مثل "
            "2026 و 15% وكلمة English داخل السطر."
        ),
        "الفقرة الثانية: يجب أن يظهر النص بالترتيب المنطقي الصحيح عند استخراجه من الملف.",
        "البند القيمة",
        "الميزانية 500",
        "المدة 12 شهرا",
        "An English paragraph follows, left to right, with the number 42.",
    ]
    converted, notes = pdftext.page_reading_order("\n".join(pypdf_layout_lines), None, word=False)
    assert notes == []
    assert converted == "\n".join(expected_lines)
    assert "\t" not in converted

    # Lines > 5,000 chars after collapsing spaces are returned unconverted with a note.
    huge_line = ("A " * 2_600) + "ب"
    out_huge, huge_notes = pdftext.page_reading_order(huge_line, None, word=False)
    assert out_huge == huge_line
    assert len(huge_notes) == 1 and "5000" in huge_notes[0]


def test_arabic_presentation_forms_normalized_only_for_arabic_ranges():
    sample = "Area 50 m² — \ufe97\ufed8\ufeae\ufef3\ufead \ufefb \ufef7 (x³ + ½)"
    assert (
        extract.normalize_arabic_presentation_forms(sample)
        == "Area 50 m² — تقرير لا لأ (x³ + ½)"
    )

    docx = _docx_bytes("<w:p><w:r><w:t>\ufe97\ufed8\ufeae\ufef3\ufead 10m²   </w:t></w:r></w:p>")
    assert extract.extract_docx(docx).text == "تقرير 10m²"

    xlsx = _xlsx_bytes(
        [(
            "\ufe91\ufef4\ufe8e\ufee7\ufe8e\ufe95",
            '<row r="1"><c r="A1" t="s"><v>0</v></c></row>',
        )],
        shared_strings_xml="<si><t>\ufefb 10m²  </t></si>",
    )
    assert extract.extract_xlsx(xlsx).text == "--- Sheet: بيانات ---\nلا 10m²"

    pptx_slide = (
        "<p:sp><p:txBody><a:p><a:r><a:t>\ufe8d\ufef9دارة 10m²</a:t></a:r></a:p></p:txBody></p:sp>"
    )
    pptx = _pptx_bytes([("slides/slide1.xml", pptx_slide)])
    assert extract.extract_pptx(pptx).text == "--- Slide 1 ---\nالإدارة 10m²"

    html = "<p>\ufe97\ufed8\ufeae\ufef3\ufead 10m²   </p>".encode("utf-8")
    assert extract.extract_html(html).text == "تقرير 10m²"


# ---------------------------------------------------------------------------
# 2. Each document type + Word mc:AlternateContent + Part E text-like files
# ---------------------------------------------------------------------------


def test_extract_docx_and_mc_alternate_content_reads_choice_only():
    body = (
        "<w:p><w:r><w:t>Quarterly review — ملخص الربع</w:t></w:r></w:p>"
        "<w:p><w:r>"
        "<mc:AlternateContent>"
        "<mc:Choice Requires=\"wps\">"
        "<w:drawing><w:txbxContent><w:p><w:r><w:t>Textbox once</w:t></w:r></w:p></w:txbxContent></w:drawing>"
        "</mc:Choice>"
        "<mc:Fallback>"
        "<w:pict><w:txbxContent><w:p><w:r><w:t>Textbox once</w:t></w:r></w:p></w:txbxContent></w:pict>"
        "</mc:Fallback>"
        "</mc:AlternateContent>"
        "</w:r></w:p>"
        "<w:tbl>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Item</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Count</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>الحالة</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Module A</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>12</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>معتمد</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        "</w:tbl>"
        "<w:p><w:r><w:t>Closing line.</w:t></w:r></w:p>"
    )
    payload = _docx_bytes(body, extra_members=[("word/vbaProject.bin", b"\x00MACRO\xff")])
    for filename in ("review.docx", "review.docm"):
        res = extract.extract_bytes(payload, name=filename)
        assert res.extracted_from == "docx"
        assert res.text.count("Textbox once") == 1
        assert res.text == (
            "Quarterly review — ملخص الربع\n"
            "Textbox once\n"
            "Item\tCount\tالحالة\n"
            "Module A\t12\tمعتمد\n"
            "Closing line."
        )


def test_extract_xlsx_and_xlsm_values_formulas_unicode_and_bounds():
    sst = (
        "<si><t>البند</t></si>"
        "<si><r><t>Total </t></r><r><t>Amount</t></r><rPh><t>IGN</t></rPh></si>"
    )
    sheet1 = (
        '<row r="1">'
        '<c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="s"><v>1</v></c>'
        '<c r="C1" t="inlineStr"><is><t>ملاحظات</t></is></c>'
        "</row>"
        '<row r="2">'
        '<c r="A2" t="inlineStr"><is><t> Licensing </t></is></c>'
        '<c r="B2"><v>1250.5</v></c>'
        '<c r="C2" t="str"><f>CONCAT("OK","-1")</f><v>OK-1</v></c>'
        "</row>"
        '<row r="3">'
        '<c r="A3" t="inlineStr"><is><t>Sum</t></is></c>'
        '<c r="XFD3"><v>99</v></c>'
        '<c r="C3"><f>SUM(B2:B2)</f><v>1250.5</v></c>'
        "</row>"
    )
    sheet2 = '<row r="1"><c r="A1" t="b"><v>1</v></c></row>'
    payload = _xlsx_bytes(
        [("Summary", sheet1), ("بيانات", sheet2)],
        shared_strings_xml=sst,
        extra_members=[("xl/vbaProject.bin", b"\x00VBA\xff")],
    )
    res = extract.extract_bytes(payload, name="ledger.xlsx")
    assert res.extracted_from == "xlsx"
    assert res.sheets == 2
    assert any("50 columns" in n for n in res.notes)
    assert "البند\tTotal Amount\tملاحظات" in res.text
    assert "Sum\t\t1250.5" in res.text
    assert "--- Sheet: بيانات ---\nTRUE" in res.text


def test_extract_pptx_and_pptm_respects_presentation_slide_order_and_tables():
    slide_file_1 = (
        "<p:sp><p:txBody>"
        "<a:p><a:r><a:t>Second in deck — الشريحة الثانية</a:t></a:r></a:p>"
        "</p:txBody></p:sp>"
    )
    slide_file_2 = (
        "<p:sp><p:txBody>"
        "<a:p><a:r><a:t>Opening slide — مقدمة</a:t></a:r></a:p>"
        "</p:txBody></p:sp>"
        "<p:graphicFrame><a:graphic><a:graphicData><a:tbl>"
        "<a:tr>"
        "<a:tc><a:txBody><a:p><a:r><a:t>Metric</a:t></a:r></a:p></a:txBody></a:tc>"
        "<a:tc><a:txBody><a:p><a:r><a:t>Value</a:t></a:r></a:p></a:txBody></a:tc>"
        "</a:tr>"
        "</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )
    payload = _pptx_bytes(
        [("slides/slide1.xml", slide_file_1), ("slides/slide2.xml", slide_file_2)],
        order_targets=["slides/slide2.xml", "slides/slide1.xml"],
    )
    res = extract.extract_bytes(payload, name="deck.pptx")
    assert res.extracted_from == "pptx"
    assert res.slides == 2
    assert res.text == (
        "--- Slide 1 ---\nOpening slide — مقدمة\nMetric\tValue\n\n"
        "--- Slide 2 ---\nSecond in deck — الشريحة الثانية"
    )


@pytest.mark.skipif(not extract.HAS_PYPDF, reason="pypdf not installed")
def test_extract_pdf_hand_written_text_stream_unicode_and_no_text_layer():
    pdf_bytes = _build_minimal_pdf([
        {
            "ascii_lines": ["Budget Report 2026", "Category   Amount", "Infra      500"],
            "unicode_text": "يﻮﻨﺴﻟا ﺔﻌﺟاﺮﻤﻟا ﺮﻳﺮﻘﺗ",
        },
        {
            "ascii_lines": ["Page two conclusion"],
        },
    ])
    res = extract.extract_bytes(pdf_bytes, name="report.pdf")
    assert res.extracted_from == "pdf"
    assert res.pages == 2
    assert res.notes == ()
    assert "Budget Report 2026" in res.text
    assert "Category Amount" in res.text
    assert "Infra 500" in res.text
    assert "تقرير المراجعة السنوي" in res.text
    assert "Page two conclusion" in res.text

    # A PDF with pages but no text gets a note that it has no text layer.
    no_text_pdf = _build_minimal_pdf([{"raw_content": b"q 1 0 0 1 0 0 cm Q"}])
    empty_res = extract.extract_pdf(no_text_pdf)
    assert empty_res.pages == 1
    assert any("no text layer" in n.lower() for n in empty_res.notes)
    assert "no text layer" in empty_res.text.lower()


def test_extract_eml_compat32_headers_raw8bit_address_cap_and_attachments():
    to_list = ", ".join(f"u{i}@example.com" for i in range(65)).encode("ascii")
    subj_b64 = base64.b64encode("تقرير الميزانية — Q2".encode("utf-8"))
    raw_overflow_eml = (
        b"From: sender@example.com\r\n"
        b"To: " + to_list + b"\r\n"
        b"Date: 1 Jan 999999999999 00:00:00 +0000\r\n"
        b"Subject: =?utf-8?b?" + subj_b64 + b"?=\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=BOUND1\r\n\r\n"
        b"--BOUND1\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        + "Plain body line — تم الاعتماد.\r\n".encode("utf-8")
        + b"--BOUND1\r\n"
        b"Content-Type: text/csv; charset=utf-8\r\n"
        b'Content-Disposition: attachment; filename="table.csv"\r\n\r\n'
        b"col1,col2\n1,2\n\r\n"
        b"--BOUND1--\r\n"
    )
    res = extract.extract_bytes(raw_overflow_eml, name="forwarded.eml")
    assert res.extracted_from == "eml"
    assert "From: sender@example.com" in res.text
    assert "u0@example.com" in res.text and "u49@example.com" in res.text
    assert "u50@example.com" not in res.text
    assert "(+15 more)" in res.text
    assert "Date: 1 Jan 999999999999 00:00:00 +0000" in res.text
    assert "Subject: تقرير الميزانية — Q2" in res.text
    assert "Plain body line — تم الاعتماد." in res.text
    assert "Attachments: table.csv" in res.text

    # Raw 8-bit UTF-8 headers and attachment filenames work under compat32.
    raw_eml = (
        b"From: " + "مكتب العمليات <ops@example.com>".encode("utf-8") + b"\r\n"
        b"To: b@example.com\r\n"
        b"Subject: " + "تقرير المراجعة".encode("utf-8") + b"\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nbody\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="'
        + "ملحق.pdf".encode("utf-8")
        + b'"\r\n\r\nxx\r\n--B--\r\n'
    )
    r8 = extract.extract_eml(raw_eml)
    assert "From: مكتب العمليات <ops@example.com>" in r8.text
    assert "Subject: تقرير المراجعة" in r8.text
    assert "Attachments: ملحق.pdf" in r8.text


def test_extract_message_item_mirrors_eml_layout_and_caps_recipients():
    attached_item = SimpleNamespace(
        sender=SimpleNamespace(name="Operations Desk", email_address="ops@example.com"),
        to_recipients=[
            SimpleNamespace(name=None, email_address=f"r{i}@example.com")
            for i in range(55)
        ],
        cc_recipients=[SimpleNamespace(name="Archive", email_address="archive@example.com")],
        datetime_sent=datetime(2026, 6, 10, 9, 30, tzinfo=timezone.utc),
        subject="Attached thread — متابعة",
        text_body="",
        body="<html><body><p>يرجى الاطلاع على الجدول:</p>"
             "<table><tr><td>A</td><td>10</td></tr></table></body></html>",
        attachments=[SimpleNamespace(name="schedule.xlsx"), SimpleNamespace(name="notes.pdf")],
    )
    res = extract.extract_message_item(attached_item, tz="UTC")
    assert res.extracted_from == "message"
    assert "From: Operations Desk <ops@example.com>" in res.text
    assert "(+5 more)" in res.text
    assert "Cc: Archive <archive@example.com>" in res.text
    assert "Date: 2026-06-10T09:30+00:00" in res.text
    assert "يرجى الاطلاع على الجدول:" in res.text
    assert "A 10" in res.text
    assert "Attachments: schedule.xlsx, notes.pdf" in res.text


def test_text_like_files_never_refused_for_leading_bytes_and_charsets_enforced():
    # Part E: CSV/TXT starting with MZ or PK (or containing NULs) must be read, not refused.
    for payload, fname, expected_sub in (
        (b"MZ-100,Widget,5\nPK-200,Bolt,9\n", "inventory.csv", "MZ-100,Widget,5"),
        (b"PK\x03\x04literal-prefix\nline2\n", "codes.csv", "PK\x03\x04literal-prefix"),
        (b"MZ header in log\x00with nul\n", "app.log", "MZ header in log"),
        ("\ufeffitem,qty\nوحدة,5\r\n".encode("utf-16"), "items.csv", "item,qty\nوحدة,5\r\n"),
    ):
        res = extract.extract_bytes(payload, name=fname)
        assert res.extracted_from == "text"
        assert expected_sub in res.text

    # Allowed charsets (e.g. windows-1256) work in <meta charset=...>, while prose
    # "charset=utf-16-le" inside body text is ignored, and hostile codecs
    # (unicode_escape, utf-7, idna, punycode, undefined) fall back to utf-8 safely.
    cp1256_html = (
        b'<html><head><meta charset="windows-1256"></head><body>'
        + "تقرير".encode("cp1256")
        + b"</body></html>"
    )
    assert extract.extract_html(cp1256_html).text == "تقرير"

    prose_html = (
        "<html><body><p>charset=utf-16-le in prose — نص</p></body></html>"
    ).encode("utf-8")
    assert "charset=utf-16-le in prose — نص" in extract.extract_html(prose_html).text

    for bad_charset, body in (
        (b"unicode_escape", b"lone \\ud800 surrogate"),
        (b"raw_unicode_escape", b"lone \\udfff surrogate"),
        (b"utf-7", b"lone +2AA- surrogate"),
        (b"idna", b"xn--mgbh0fb.example"),
        (b"undefined", b"hello"),
        (b"zlib_codec", b"hello"),
    ):
        raw = b'<meta charset="' + bad_charset + b'"><p>' + body + b"</p>"
        out = extract.extract_html(raw).text
        out.encode("utf-8")  # must never contain lone surrogates


# ---------------------------------------------------------------------------
# 3. Hostile & reviewer regression cases (speed, memory/char caps, error caps)
# ---------------------------------------------------------------------------


def test_reviewer_hostile_shared_string_amplification_and_cell_cap():
    # 100 KB shared string referenced by 5,000 x 50 = 250,000 cells (~31 KBdeflated).
    rnd = random.Random(1)
    big_str = "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(100_000))
    rows_xml = "".join(
        "<row>" + '<c t="s"><v>0</v></c>' * 50 + "</row>" for _ in range(5_000)
    )
    xlsx_data = _xlsx_bytes(
        [("S0", rows_xml)], shared_strings_xml=f"<si><t>{big_str}</t></si>"
    )
    t0 = time.perf_counter()
    res = extract.extract_xlsx(xlsx_data)
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0 * TIME_MARGIN
    assert len(res.text) <= extract.MAX_EXTRACTED_CHARS
    assert any(str(extract.MAX_EXTRACTED_CHARS) in n for n in res.notes)
    # Per-cell cap: first cell in first data row is at most 2,000 chars.
    first_cell = res.text.splitlines()[1].split("\t")[0]
    assert len(first_cell) <= extract.MAX_CELL_CHARS


def test_reviewer_hostile_long_cell_reference_rejected_instantly():
    # 400,000-letter cell reference previously took >12s in bigint base-26 math.
    long_ref = "A" * 400_000 + "1"
    xlsx_data = _xlsx_bytes([("S", f'<row><c r="{long_ref}"><v>1</v></c></row>')])
    t0 = time.perf_counter()
    with pytest.raises(ToolError) as exc:
        extract.extract_xlsx(xlsx_data)
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0 * TIME_MARGIN
    assert exc.value.code == "validation"
    assert len(exc.value.message) <= extract.MAX_ERROR_MESSAGE_CHARS

    # 11-character cell ref is also invalid (> 10 chars).
    ref_11 = _xlsx_bytes([("S", '<row><c r="AAAA1000000"><v>1</v></c></row>')])
    with pytest.raises(ToolError) as exc11:
        extract.extract_xlsx(ref_11)
    assert exc11.value.code == "validation"


def test_reviewer_hostile_deep_xml_and_huge_slide_number_are_fast_validation_errors():
    deep_runs = _docx_bytes(
        "<w:p>" + "<w:r>" * 2_000 + "<w:t>x</w:t>" + "</w:r>" * 2_000 + "</w:p>"
    )
    deep_tables = _docx_bytes(
        "<w:tbl><w:tr><w:tc>" * 400
        + "<w:p><w:r><w:t>x</w:t></w:r></w:p>"
        + "</w:tc></w:tr></w:tbl>" * 400
    )
    deep_sdt = _docx_bytes(
        "<w:sdt><w:sdtContent>" * 5_000
        + "<w:p><w:r><w:t>x</w:t></w:r></w:p>"
        + "</w:sdtContent></w:sdt>" * 5_000
    )
    deep_pptx = _pptx_bytes([(
        "slides/slide1.xml",
        "<p:grpSp>" * 3_000
        + "<p:sp><p:txBody><a:p><a:r><a:t>x</a:t></a:r></a:p></p:txBody></p:sp>"
        + "</p:grpSp>" * 3_000,
    )])
    deep_xlsx = _xlsx_bytes(
        [("S", '<row><c t="s"><v>0</v></c></row>')],
        shared_strings_xml="<si>" + "<r>" * 3_000 + "<t>x</t>" + "</r>" * 3_000 + "</si>",
    )

    for payload, name in (
        (deep_runs, "runs.docx"),
        (deep_tables, "tables.docx"),
        (deep_sdt, "sdt.docx"),
        (deep_pptx, "groups.pptx"),
        (deep_xlsx, "si.xlsx"),
    ):
        t0 = time.perf_counter()
        with pytest.raises(ToolError) as exc:
            extract.extract_bytes(payload, name=name)
        assert (time.perf_counter() - t0) < 1.0 * TIME_MARGIN
        assert exc.value.code == "validation"
        assert len(exc.value.message) <= extract.MAX_ERROR_MESSAGE_CHARS

    # 5,000-digit slide filename does not trigger int() 4300-digit ValueError.
    huge_slide = _zip_bytes([
        ("ppt/slides/slide" + "9" * 5_000 + ".xml", b"<sld/>"),
    ])
    with pytest.raises(ToolError) as s_exc:
        extract.extract_pptx(huge_slide)
    assert s_exc.value.code == "validation"


def test_reviewer_hostile_eml_encoded_words_dangling_quotes_and_quadratic_html():
    # 20,000 encoded words in Subject previously took 5.6 GB under default policy.
    subj = b" ".join([b"=?utf-8?b?2KrZgtix2YrYsQ==?="] * 20_000)
    eml_subj = b"From: a@example.com\r\nSubject: " + subj + b"\r\n\r\nbody\r\n"
    t0 = time.perf_counter()
    res_subj = extract.extract_eml(eml_subj)
    assert (time.perf_counter() - t0) < 1.0 * TIME_MARGIN
    assert "تقرير" in res_subj.text

    # Dangling quote with 500k backslashes + nested comments in To header.
    for hdr_val in (
        b'"' + b"\\" * 500_000 + b" <a@example.com>, b@example.com",
        b"(" * 10_000 + b"a@example.com",
    ):
        eml_to = b"From: a@example.com\r\nTo: " + hdr_val + b"\r\n\r\nbody\r\n"
        t0 = time.perf_counter()
        res_to = extract.extract_eml(eml_to)
        assert (time.perf_counter() - t0) < 1.0 * TIME_MARGIN
        assert "From: a@example.com" in res_to.text

    # Pathological unclosed HTML shapes that stalled stdlib HTMLParser for 40-60s+.
    for token in (b"<a b='", b"<a ", b"<a/", b"<!--", b"<![", b"<?", b"</"):
        html_bomb = b"<html><body>" + token * (200_000 // len(token))
        t0 = time.perf_counter()
        hres = extract.extract_html(html_bomb)
        assert (time.perf_counter() - t0) < 1.0 * TIME_MARGIN
        hres.text.encode("utf-8")


def test_zip_methods_member_xml_and_total_read_bounds_and_error_message_caps(monkeypatch):
    # Member count checked before reading any member.
    many = [("word/document.xml", b"<document><body/></document>")] + [
        (f"extra/f{i}.xml", b"<x/>") for i in range(extract.MAX_ARCHIVE_MEMBERS)
    ]
    with pytest.raises(ToolError) as mc_exc:
        extract.extract_docx(_zip_bytes(many, compress=False), max_bytes=None)
    assert mc_exc.value.code == "validation"

    # Only stored (0) and deflated (8) allowed: LZMA (14), BZIP2 (12), Deflate64 (9) refused.
    lzma_buf = io.BytesIO()
    with zipfile.ZipFile(lzma_buf, "w", zipfile.ZIP_LZMA) as zf:
        zf.writestr("word/document.xml", b"<document><body/></document>")
    with pytest.raises(ToolError) as lz_exc:
        extract.extract_docx(lzma_buf.getvalue())
    assert lz_exc.value.code == "validation"
    assert "stored and deflated" in lz_exc.value.message

    d64 = _raw_zip_single_member("word/document.xml", b"xx", method=9)
    with pytest.raises(ToolError) as d64_exc:
        extract.extract_docx(d64)
    assert d64_exc.value.code == "validation"

    enc_zip = _raw_zip_single_member("word/document.xml", b"xx", method=8, flags=1)
    with pytest.raises(ToolError) as ez_exc:
        extract.extract_docx(enc_zip)
    assert ez_exc.value.code == "validation"

    # Streamed XML part ceiling (MAX_XML_PART_BYTES) and total archive read ceiling.
    doc = _docx_bytes("<w:p><w:r><w:t>" + ("A" * 4096) + "</w:t></w:r></w:p>")
    monkeypatch.setattr(extract, "MAX_XML_PART_BYTES", 512)
    with pytest.raises(ToolError) as xml_cap_exc:
        extract.extract_docx(doc, max_bytes=None)
    assert xml_cap_exc.value.code == "validation"

    monkeypatch.setattr(extract, "MAX_XML_PART_BYTES", 20 * 1024 * 1024)
    monkeypatch.setattr(extract, "MAX_MEMBER_BYTES", 512)
    with pytest.raises(ToolError) as mem_cap_exc:
        extract.extract_docx(doc, max_bytes=None)
    assert mem_cap_exc.value.code == "validation"

    monkeypatch.setattr(extract, "MAX_MEMBER_BYTES", 30 * 1024 * 1024)
    xlsx = _xlsx_bytes(
        [("S1", '<row r="1"><c r="A1" t="s"><v>0</v></c></row>')],
        shared_strings_xml="<si><t>" + ("B" * 1500) + "</t></si>",
    )
    monkeypatch.setattr(extract, "MAX_ARCHIVE_TOTAL_BYTES", 1024)
    with pytest.raises(ToolError) as tot_exc:
        extract.extract_xlsx(xlsx, max_bytes=None)
    assert tot_exc.value.code == "validation"

    # Error messages and notes are capped at <=300 chars and never echo >80 chars of file text.
    long_file_text = "Z" * 500 + ".msg"
    err = extract.unsupported_type_error(long_file_text, "x-custom/" + "y" * 500)
    assert len(err.message) <= extract.MAX_ERROR_MESSAGE_CHARS
    assert "Z" * 81 not in err.message
    assert "y" * 81 not in err.message


def test_limit_xml_entity_bomb_and_mislabeled_document_formats():
    billion_laughs = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
        b'<document xmlns="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<body><p><r><t>&lol2;</t></r></p></body></document>"
    )
    with pytest.raises(ToolError) as xxe_exc:
        extract.extract_docx(_zip_bytes([("word/document.xml", billion_laughs)]))
    assert xxe_exc.value.code == "validation"

    for filename, payload in (
        ("fake.docx", b"plain text pretending to be docx"),
        ("fake.docx", _zip_bytes([("hello.txt", b"zipped text, not word")])),
        ("fake.xlsx", _zip_bytes([("hello.txt", b"zipped text, not excel")])),
        ("fake.pptx", _zip_bytes([("hello.txt", b"zipped text, not slides")])),
        ("fake.eml", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"),
        ("fake.eml", b"plain paragraph with no mail headers at all"),
    ):
        with pytest.raises(ToolError) as exc:
            extract.extract_bytes(payload, name=filename)
        assert exc.value.code == "validation"
        assert len(exc.value.message) <= extract.MAX_ERROR_MESSAGE_CHARS


@pytest.mark.skipif(not extract.HAS_PYPDF, reason="pypdf not installed")
def test_pdf_limits_lone_surrogates_padded_spaces_and_single_pass_fallback(monkeypatch):
    import pypdf

    with pytest.raises(ToolError) as fake_exc:
        extract.extract_pdf(b"not a pdf file at all")
    assert fake_exc.value.code == "validation"

    # Password-encrypted PDF refused; empty user password accepted.
    enc_buf = io.BytesIO()
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("non-empty-secret")
    writer.write(enc_buf)
    with pytest.raises(ToolError) as enc_exc:
        extract.extract_pdf(enc_buf.getvalue())
    assert enc_exc.value.code == "validation"

    empty_pw_buf = io.BytesIO()
    writer2 = pypdf.PdfWriter()
    writer2.add_blank_page(width=200, height=200)
    writer2.encrypt("", "owner-only-pw")
    writer2.write(empty_pw_buf)
    empty_res = extract.extract_pdf(empty_pw_buf.getvalue())
    assert empty_res.pages == 1
    assert any("no text layer" in n.lower() for n in empty_res.notes)

    # Corrupt page stream isolated to a note while sibling pages succeed.
    broken_pdf = _build_minimal_pdf([
        {"ascii_lines": ["First page readable"]},
        {"corrupt_stream": True},
        {"ascii_lines": ["Third page readable"]},
    ])
    broken_res = extract.extract_pdf(broken_pdf)
    assert broken_res.pages == 3
    assert "First page readable" in broken_res.text
    assert "Third page readable" in broken_res.text
    assert any("Page 2 failed to parse" in n for n in broken_res.notes)

    # Lone surrogate via /ToUnicode (<42> -> <D800>) is replaced with U+FFFD.
    surr_pdf = _build_minimal_pdf([{
        "raw_content": b"BT /F2 12 Tf 72 700 Td <42> Tj ET",
        "raw_cmap_pairs": [(b"42", b"D800")],
    }])
    surr_res = extract.extract_pdf(surr_pdf)
    assert "\ufffd" in surr_res.text
    surr_res.text.encode("utf-8")

    # Padded-space PDF (5,000 wide horizontal gaps with RTL glyph) collapses quickly.
    gap_ops = (
        b"BT /F1 10 Tf 1 0 0 1 10 700 Tm (A) Tj "
        + b"9000000 0 Td (A) Tj " * 2_000
        + b"9000000 0 Td /F2 10 Tf <42> Tj ET"
    )
    gap_pdf = _build_minimal_pdf([{
        "raw_content": gap_ops,
        "raw_cmap_pairs": [(b"42", b"0627")],
    }])
    t0 = time.perf_counter()
    gap_res = extract.extract_pdf(gap_pdf)
    assert (time.perf_counter() - t0) < 10.0 * TIME_MARGIN
    assert "A" in gap_res.text

    # Non-RTL pages call layout mode ONLY (never calling plain extract_text() a second time);
    # RTL pages also fetch plain text for word repair; fallback to plain mode happens ONLY
    # when layout raises and returns plain text unconverted with a note.
    calls: List[Optional[str]] = []

    def spy_non_rtl(self, *args, **kwargs):
        calls.append(kwargs.get("extraction_mode"))
        return "English only line"

    monkeypatch.setattr(pypdf.PageObject, "extract_text", spy_non_rtl)
    extract.extract_pdf(_build_minimal_pdf([{"ascii_lines": ["x"]}]))
    assert calls == ["layout"]

    calls_rtl: List[Optional[str]] = []

    def spy_rtl(self, *args, **kwargs):
        mode = kwargs.get("extraction_mode")
        calls_rtl.append(mode)
        return "يونسلا ريرقت" if mode == "layout" else "تقرير السنوي"

    monkeypatch.setattr(pypdf.PageObject, "extract_text", spy_rtl)
    rtl_res = extract.extract_pdf(_build_minimal_pdf([{"ascii_lines": ["x"]}]))
    assert calls_rtl == ["layout", None]
    assert rtl_res.text == "تقرير السنوي"

    def spy_raises(self, *args, **kwargs):
        if kwargs.get("extraction_mode") == "layout":
            raise RuntimeError("layout boom")
        return "plain-unconverted \ufe97\ufed8\ufeae\ufef3\ufead"

    monkeypatch.setattr(pypdf.PageObject, "extract_text", spy_raises)
    fb_res = extract.extract_pdf(_build_minimal_pdf([{"ascii_lines": ["x"]}]))
    assert fb_res.text.startswith("plain-unconverted \ufe97\ufed8\ufeae\ufef3\ufead")
    assert any("fell back to plain mode" in n for n in fb_res.notes)


def test_pdf_without_pypdf_installed_names_the_extra(monkeypatch):
    monkeypatch.setattr(extract, "_pypdf", None)
    assert extract.pypdf_available() is False
    with pytest.raises(ToolError) as exc:
        extract.extract_bytes(b"%PDF-1.4\n%%EOF\n", name="doc.pdf")
    assert exc.value.code == "validation"
    assert exc.value.hint is not None
    assert "ews-mcp[extract]" in exc.value.hint


def test_raw_8bit_cp1256_headers_and_decode_before_4000_char_cut():
    from ewsmcp import errors, toolerror

    assert toolerror.ToolError is errors.ToolError
    assert toolerror.HTTP_BY_CODE is errors.HTTP_BY_CODE

    # 1. Raw Windows-1256 (cp1256) headers and attachment filename under compat32.
    cp1256_eml = (
        b"From: " + "مكتب العمليات <ops@example.com>".encode("cp1256") + b"\r\n"
        b"To: b@example.com\r\n"
        b"Subject: " + "تقرير المراجعة".encode("cp1256") + b"\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain; charset=windows-1256\r\n\r\n"
        + "تم الاعتماد".encode("cp1256")
        + b"\r\n--B\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="'
        + "ملحق.pdf".encode("cp1256")
        + b'"\r\n\r\nxx\r\n--B--\r\n'
    )
    res_cp = extract.extract_eml(cp1256_eml)
    assert "From: مكتب العمليات <ops@example.com>" in res_cp.text
    assert "Subject: تقرير المراجعة" in res_cp.text
    assert "تم الاعتماد" in res_cp.text
    assert "Attachments: ملحق.pdf" in res_cp.text

    # 2. >4,000-character raw UTF-8 Subject (9,000 UTF-8 bytes = 4,500 Arabic letters)
    # is decoded BEFORE the 4,000-character cut, so 4,000 letters survive cleanly
    # with zero U+FFFD replacement characters.
    long_utf8_subj = ("أ" * 4_500).encode("utf-8")
    long_eml = (
        b"From: a@example.com\r\nSubject: " + long_utf8_subj + b"\r\n\r\nbody\r\n"
    )
    res_long = extract.extract_eml(long_eml)
    subj_line = next(
        ln for ln in res_long.text.splitlines() if ln.startswith("Subject: ")
    )
    subj_val = subj_line[len("Subject: ") :]
    assert len(subj_val) == extract.MAX_EML_HEADER_CHARS
    assert "\ufffd" not in subj_val
    assert subj_val == "أ" * extract.MAX_EML_HEADER_CHARS


def test_soft_deadline_returns_partial_units_with_note():
    expired = time.monotonic() - 1.0

    # Slides: stops after slide 1 of 4 when soft_deadline is already reached.
    pptx = _pptx_bytes([
        (f"slides/slide{i}.xml", f"<p:sp><p:txBody><a:p><a:r><a:t>SlideBody{i}</a:t></a:r></a:p></p:txBody></p:sp>")
        for i in range(1, 5)
    ])
    r_pptx = extract.extract_pptx(pptx, soft_deadline=expired)
    assert "SlideBody1" in r_pptx.text
    assert "SlideBody2" not in r_pptx.text
    assert any("Stopped after slide 1 of 4 (time limit)" in n for n in r_pptx.notes)

    # Sheets: stops after sheet 1 of 3.
    xlsx = _xlsx_bytes([
        (f"S{i}", f'<row r="1"><c r="A1" t="inlineStr"><is><t>Val{i}</t></is></c></row>')
        for i in range(1, 4)
    ])
    r_xlsx = extract.extract_xlsx(xlsx, soft_deadline=expired)
    assert "Val1" in r_xlsx.text
    assert "Val2" not in r_xlsx.text
    assert any("Stopped after sheet 1 of 3 (time limit)" in n for n in r_xlsx.notes)

    # Message parts: stops after message part 1 of 3.
    eml = (
        b"From: a@example.com\r\nSubject: Parts\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=P\r\n\r\n"
        b"--P\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nFirstBody\r\n"
        b'--P\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename="two.pdf"\r\n\r\nxx\r\n'
        b'--P\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename="three.pdf"\r\n\r\nxx\r\n'
        b"--P--\r\n"
    )
    r_eml = extract.extract_eml(eml, soft_deadline=expired)
    assert "FirstBody" in r_eml.text
    assert "two.pdf" not in r_eml.text
    assert any("Stopped after message part 1 of 3 (time limit)" in n for n in r_eml.notes)

    if extract.HAS_PYPDF:
        pdf = _build_minimal_pdf([
            {"ascii_lines": ["PageOneText"]},
            {"ascii_lines": ["PageTwoText"]},
            {"ascii_lines": ["PageThreeText"]},
        ])
        r_pdf = extract.extract_pdf(pdf, soft_deadline=expired)
        assert "PageOneText" in r_pdf.text
        assert "PageTwoText" not in r_pdf.text
        assert any("Stopped after page 1 of 3 (time limit)" in n for n in r_pdf.notes)


def test_empty_documents_carry_no_readable_text_note():
    empty_docx = extract.extract_docx(_docx_bytes(""))
    assert empty_docx.text == ""
    assert any("no readable text" in n.lower() for n in empty_docx.notes)

    empty_pptx_zero = extract.extract_pptx(_pptx_bytes([]))
    assert empty_pptx_zero.text == ""
    assert empty_pptx_zero.slides == 0
    assert any("no readable text" in n.lower() for n in empty_pptx_zero.notes)

    empty_pptx_blank = extract.extract_pptx(_pptx_bytes([("slides/slide1.xml", "")]))
    assert empty_pptx_blank.text == ""
    assert empty_pptx_blank.slides == 1
    assert any("no readable text" in n.lower() for n in empty_pptx_blank.notes)

    empty_xlsx_zero = extract.extract_xlsx(_xlsx_bytes([]))
    assert empty_xlsx_zero.text == ""
    assert empty_xlsx_zero.sheets == 0
    assert any("no readable text" in n.lower() for n in empty_xlsx_zero.notes)

    empty_xlsx_blank = extract.extract_xlsx(_xlsx_bytes([("Blank", '<row r="1"><c r="A1"/></row>')]))
    assert empty_xlsx_blank.text == ""
    assert empty_xlsx_blank.sheets == 1
    assert any("no readable text" in n.lower() for n in empty_xlsx_blank.notes)

    empty_html = extract.extract_html(b"<html><head><title>x</title></head><body>   </body></html>")
    assert empty_html.text == ""
    assert any("no readable text" in n.lower() for n in empty_html.notes)

    empty_eml = extract.extract_eml(b"From: \r\nSubject: \r\n\r\n   \r\n")
    assert empty_eml.text == ""
    assert any("no readable text" in n.lower() for n in empty_eml.notes)

    empty_msg = extract.extract_serialized_message_item({})
    assert empty_msg.text == ""
    assert any("no readable text" in n.lower() for n in empty_msg.notes)

    if extract.HAS_PYPDF:
        no_layer = extract.extract_pdf(_build_minimal_pdf([{"raw_content": b"q Q"}]))
        assert no_layer.notes == ("PDF has no text layer (pages contain no extractable text).",)


# ---------------------------------------------------------------------------
# 4. Round 7 Part 1: PDF invisible-layout text & Excel styles + streaming
# ---------------------------------------------------------------------------


def _custom_pdf_with_xobjects(
    content_stream: bytes,
    *,
    xobjects_res: bytes = b"",
    extra_objs: Optional[Dict[int, bytes]] = None,
) -> bytes:
    def _stream(raw: bytes, extra_dict: bytes = b"") -> bytes:
        comp = zlib.compress(raw)
        hdr = f"<< /Filter /FlateDecode /Length {len(comp)} ".encode("ascii")
        if extra_dict:
            hdr += extra_dict + b" "
        hdr += b">>\nstream\n"
        return hdr + comp + b"\nendstream"

    objs: Dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> "
            + xobjects_res
            + b" >> /Contents 5 0 R >>"
        ),
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        5: _stream(content_stream),
    }
    if extra_objs:
        objs.update(extra_objs)
    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: Dict[int, int] = {}
    for idx in sorted(objs):
        offsets[idx] = len(buf)
        buf.extend(f"{idx} 0 obj\n".encode("ascii") + objs[idx] + b"\nendobj\n")
    xref = len(buf)
    size = max(objs) + 1
    buf.extend(f"xref\n0 {size}\n0000000000 65535 f \n".encode("ascii"))
    for idx in range(1, size):
        buf.extend(f"{offsets[idx]:010d} 00000 n \n".encode("ascii"))
    buf.extend(
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(buf)


@pytest.mark.skipif(not extract.HAS_PYPDF, reason="pypdf not installed")
def test_pdf_rotated_skewed_and_form_xobject_text_recovered_in_file_order():
    def _form_stream(raw: bytes) -> bytes:
        comp = zlib.compress(raw)
        return (
            f"<< /Filter /FlateDecode /Length {len(comp)} "
            "/Type /XObject /Subtype /Form /BBox [0 0 612 792] "
            "/Resources << /Font << /F1 4 0 R >> >> >>\nstream\n".encode("ascii")
            + comp
            + b"\nendstream"
        )

    c1 = math.cos(math.radians(1.0))
    s1 = math.sin(math.radians(1.0))
    c01 = math.cos(math.radians(0.1))
    s01 = math.sin(math.radians(0.1))

    form_obj = _form_stream(
        b"BT /F1 12 Tf 72 650 Td (Invoice total USD 142500 in Form XObject) Tj ET"
    )

    invisible_cases = [
        (
            "text_matrix_rot90",
            _custom_pdf_with_xobjects(
                b"BT /F1 12 Tf 0 1 -1 0 100 200 Tm "
                b"(Vertical margin text invisible to layout mode) Tj ET"
            ),
            ["Vertical margin text invisible to layout mode"],
        ),
        (
            "cm_rot90",
            _custom_pdf_with_xobjects(
                b"q 0 1 -1 0 200 200 cm BT /F1 12 Tf 0 0 Td "
                b"(Rotated via cm operator) Tj ET Q"
            ),
            ["Rotated via cm operator"],
        ),
        (
            "skew_1_degree_ocr_layer",
            _custom_pdf_with_xobjects(
                f"BT /F1 12 Tf {c1:.6f} {s1:.6f} {-s1:.6f} {c1:.6f} 72 700 Tm "
                "(Slightly skewed OCR text layer 1 deg) Tj ET".encode("ascii")
            ),
            ["Slightly skewed OCR text layer 1 deg"],
        ),
        (
            "rot180_short_14_nonspace",
            _custom_pdf_with_xobjects(
                b"BT /F1 12 Tf -1 0 0 -1 300 500 Tm (Upside down text) Tj ET"
            ),
            ["Upside down text"],
        ),
        (
            "form_xobject_only",
            _custom_pdf_with_xobjects(
                b"/Fm1 Do",
                xobjects_res=b"/XObject << /Fm1 6 0 R >>",
                extra_objs={6: form_obj},
            ),
            ["Invoice total USD 142500 in Form XObject"],
        ),
        (
            "mixed_header_form_rotated",
            _custom_pdf_with_xobjects(
                b"BT /F1 12 Tf 72 740 Td (Header in page stream) Tj ET "
                b"/Fm1 Do "
                b"BT /F1 10 Tf 0 1 -1 0 560 200 Tm (Sideways stamp) Tj ET",
                xobjects_res=b"/XObject << /Fm1 6 0 R >>",
                extra_objs={6: form_obj},
            ),
            [
                "Header in page stream",
                "Invoice total USD 142500 in Form XObject",
                "Sideways stamp",
            ],
        ),
    ]

    import io as _io

    import pypdf as _pypdf

    needed_fallback = 0
    for label, pdf_data, expected_parts in invisible_cases:
        res = extract.extract_pdf(pdf_data)
        for part in expected_parts:
            assert part in res.text, f"{label}: missing {part!r} in {res.text!r}"
        assert not any("no text layer" in n.lower() for n in res.notes)
        # The file-order note is owed exactly when the layout pass alone misses text
        # (pypdf reads a slight skew in layout mode, so that case needs no note).
        layout_alone = (
            _pypdf.PdfReader(_io.BytesIO(pdf_data)).pages[0].extract_text(extraction_mode="layout")
            or ""
        )
        if all(part in layout_alone for part in expected_parts):
            assert not any("file order" in n.lower() for n in res.notes), label
        else:
            needed_fallback += 1
            assert any("file order" in n.lower() for n in res.notes), (
                f"{label}: expected file-order note, got {res.notes!r}"
            )
    assert needed_fallback >= 4

    # Upright control and tiny 0.1-degree skew are read by layout mode with no note.
    for ctrl_pdf, expected in (
        (
            _custom_pdf_with_xobjects(
                b"BT /F1 12 Tf 72 700 Td (Upright control line) Tj ET"
            ),
            "Upright control line",
        ),
        (
            _custom_pdf_with_xobjects(
                f"BT /F1 12 Tf {c01:.6f} {s01:.6f} {-s01:.6f} {c01:.6f} 72 700 Tm "
                "(Tiny skew 0.1 degree text) Tj ET".encode("ascii")
            ),
            "Tiny skew 0.1 degree text",
        ),
    ):
        ctrl_res = extract.extract_pdf(ctrl_pdf)
        assert expected in ctrl_res.text
        assert ctrl_res.notes == ()

    # Image-only page (both layout and plain passes empty) still reports "no text layer".
    img_only = _custom_pdf_with_xobjects(b"q 100 0 0 100 50 600 cm Q")
    img_res = extract.extract_pdf(img_only)
    assert any("no text layer" in n.lower() for n in img_res.notes)


def test_xlsx_dates_times_percentages_broken_styles_note_and_streamed_30000_rows():
    # 1. Built-in and custom date/time/percent number formats (1900 date system).
    styles_xml = (
        b'<?xml version="1.0"?>'
        b'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<numFmts count="5">'
        b'<numFmt numFmtId="164" formatCode="yyyy/mm/dd"/>'
        b'<numFmt numFmtId="165" formatCode="dd-mmm-yyyy hh:mm:ss"/>'
        b'<numFmt numFmtId="166" formatCode="0.0%"/>'
        b'<numFmt numFmtId="167" formatCode="[Red]#,##0"/>'
        b'<numFmt numFmtId="168" formatCode="[h]:mm:ss"/>'
        b"</numFmts>"
        b'<cellXfs count="9">'
        b'<xf numFmtId="0"/>'
        b'<xf numFmtId="14" applyNumberFormat="1"/>'
        b'<xf numFmtId="10" applyNumberFormat="1"/>'
        b'<xf numFmtId="9" applyNumberFormat="1"/>'
        b'<xf numFmtId="164" applyNumberFormat="1"/>'
        b'<xf numFmtId="165" applyNumberFormat="1"/>'
        b'<xf numFmtId="166" applyNumberFormat="1"/>'
        b'<xf numFmtId="167" applyNumberFormat="1"/>'
        b'<xf numFmtId="168" applyNumberFormat="1"/>'
        b"</cellXfs></styleSheet>"
    )
    sheet_row = (
        '<row r="1">'
        '<c r="A1" t="inlineStr"><is><t>Due date</t></is></c>'
        '<c r="B1" s="1"><v>46357</v></c>'
        '<c r="C1" s="2"><v>0.125</v></c>'
        '<c r="D1" s="1"><v>46357.625</v></c>'
        '<c r="E1" s="3"><v>0.125</v></c>'
        '<c r="F1" s="4"><v>46357</v></c>'
        '<c r="G1" s="5"><v>46357.5</v></c>'
        '<c r="H1" s="6"><v>0.5</v></c>'
        '<c r="I1" s="7"><v>46357</v></c>'
        '<c r="J1" s="8"><v>1.5</v></c>'
        "</row>"
    )
    xlsx_ok = _xlsx_bytes(
        [("Plan", sheet_row)], extra_members=[("xl/styles.xml", styles_xml)]
    )
    res_ok = extract.extract_xlsx(xlsx_ok)
    assert res_ok.notes == ()
    cells = res_ok.text.splitlines()[1].split("\t")
    assert cells == [
        "Due date",
        "2026-12-01",
        "12.50%",
        "2026-12-01 15:00",
        "13%",
        "2026-12-01",
        "2026-12-01 12:00:00",
        "50.0%",
        "46357",  # [Red]#,##0 is a color-coded integer, not a date!
        "36:00:00",
    ]

    # 2. 1904 date system (<workbookPr date1904="1"/>).
    ns_main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    wb_1904 = (
        f'<?xml version="1.0"?><workbook xmlns="{ns_main}">'
        '<workbookPr date1904="1"/><sheets><sheet name="S1" sheetId="1"/></sheets>'
        "</workbook>"
    ).encode("utf-8")
    ws_1904 = (
        f'<?xml version="1.0"?><worksheet xmlns="{ns_main}"><sheetData>'
        '<row r="1"><c r="A1" s="1"><v>0</v></c><c r="B1" s="1"><v>1</v></c></row>'
        "</sheetData></worksheet>"
    ).encode("utf-8")
    res_1904 = extract.extract_xlsx(
        _zip_bytes([
            ("xl/workbook.xml", wb_1904),
            ("xl/worksheets/sheet1.xml", ws_1904),
            ("xl/styles.xml", styles_xml),
        ])
    )
    assert "1904-01-01\t1904-01-02" in res_1904.text

    # 3. Corrupt xl/styles.xml falls back to raw numbers and emits a note.
    xlsx_bad_styles = _xlsx_bytes(
        [("Plan", '<row r="1"><c r="A1" s="1"><v>46357</v></c><c r="B1" s="2"><v>0.125</v></c></row>')],
        extra_members=[("xl/styles.xml", b"<styleSheet><unclosed")],
    )
    res_bad = extract.extract_xlsx(xlsx_bad_styles)
    assert "46357\t0.125" in res_bad.text
    assert any("styles could not be read" in n.lower() for n in res_bad.notes)

    # 4. A 30,000-row worksheet whose uncompressed XML exceeds MAX_XML_PART_BYTES (20 MiB)
    # streams cleanly, stops at 5,000 rows, and returns the first 5,000 rows + cut note.
    filler = "X" * 700
    big_rows_inner = "".join(
        f'<row r="{r}"><c r="A{r}" t="inlineStr"><is><t>R{r}-{filler}</t></is></c></row>'
        for r in range(1, 30_001)
    )
    assert len(big_rows_inner) > extract.MAX_XML_PART_BYTES
    big_sheet_xlsx = _xlsx_bytes([("Huge30k", big_rows_inner)])
    t0 = time.perf_counter()
    res_stream = extract.extract_xlsx(big_sheet_xlsx)
    assert (time.perf_counter() - t0) < 5.0 * TIME_MARGIN
    assert "R1-" in res_stream.text
    assert "R5000-" in res_stream.text
    assert "R5001-" not in res_stream.text
    assert any("5000 rows" in n for n in res_stream.notes)
    assert res_stream.partial is True
    assert res_stream.sheets == 1 and res_stream.sheets_read == 1


def test_partial_flag_read_counts_and_folded_cut_first_notes():
    assert extract.MAX_RETURNED_NOTES == 8

    # Whole document: partial is False and sheets_read == sheets.
    whole_xlsx = _xlsx_bytes([
        ("S1", '<row r="1"><c r="A1"><v>1</v></c></row>'),
        ("S2", '<row r="1"><c r="A1"><v>2</v></c></row>'),
    ])
    whole_res = extract.extract_xlsx(whole_xlsx)
    assert whole_res.partial is False
    assert whole_res.sheets == 2 and whole_res.sheets_read == 2

    # 14-sheet workbook with >5,000 rows per sheet stopped by soft_deadline:
    # `time limit` note comes FIRST, repeated 5000-row notes fold into one with count,
    # `partial` is True, and `sheets_read < sheets`.
    rows_5200 = "".join(
        f'<row r="{r}"><c r="A{r}"><v>{r}</v></c><c r="B{r}"><v>{r * 2}</v></c></row>'
        for r in range(1, 5_202)
    )
    fourteen = _xlsx_bytes([
        (f"Sales detail FY2025 region {i:02d} central province", rows_5200)
        for i in range(1, 15)
    ])
    t0 = time.monotonic()
    full_14 = extract.extract_xlsx(fourteen)
    full_dt = time.monotonic() - t0
    assert full_14.partial is True
    assert full_14.sheets == 14 and full_14.sheets_read == 14
    assert any("14 sheets were cut at 5000 rows" in n for n in full_14.notes)

    # Force soft_deadline stop mid-workbook (or after sheet 1 if instantaneous).
    part_14 = extract.extract_xlsx(
        fourteen, soft_deadline=time.monotonic() + max(0.001, full_dt * 0.45)
    )
    if part_14.sheets_read == 14:
        part_14 = extract.extract_xlsx(fourteen, soft_deadline=time.monotonic() - 1.0)
    assert part_14.partial is True
    assert part_14.sheets == 14
    assert 1 <= (part_14.sheets_read or 0) < 14
    assert "time limit" in part_14.notes[0]
    assert len(part_14.notes) <= extract.MAX_RETURNED_NOTES
    assert all(len(n) <= extract.MAX_NOTE_CHARS for n in part_14.notes)


def test_plain_text_25mb_reviewer_shapes_decode_and_slice_fast():
    import asyncio

    size_25mb = 25 * 1024 * 1024
    log_line = b"2026-11-16 04:29:11 INFO worker-3 processed batch 18842 rows=5000 ok\n"
    shapes = {
        "honest_log": log_line * (size_25mb // len(log_line)),
        "newlines": b"\n" * size_25mb,
        "space_newline": b" \n" * (size_25mb // 2),
        "cr_plus_one_astral": b"\r" * (size_25mb - 4) + "\U0001F600".encode("utf-8"),
        "invalid_bytes_plus_astral": b"\xff" * (size_25mb - 4) + "\U0001F600".encode("utf-8"),
        "utf16_newlines": b"\xff\xfe" + ("\n".encode("utf-16-le")) * ((size_25mb - 2) // 2),
    }

    for label, payload in shapes.items():
        t0 = time.perf_counter()
        res = extract.extract_plain_text(payload, max_bytes=size_25mb)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.3 * TIME_MARGIN, f"{label} took {elapsed:.3f}s (expected < 0.3s)"
        assert len(res.text) == extract.MAX_EXTRACTED_CHARS
        assert res.partial is True
        assert len(res.notes) == 1 and str(extract.MAX_EXTRACTED_CHARS) in res.notes[0]
        if label == "cr_plus_one_astral":
            # Line endings are NOT normalized (`\r` stays `\r`).
            assert res.text[:32] == "\r" * 32
        if label == "space_newline":
            # Trailing spaces before `\n` are NOT stripped in plain text.
            assert res.text[:6] == " \n \n \n"

    # Four concurrent 25 MB calls finish quickly without starving the event loop.
    nl_payload = shapes["cr_plus_one_astral"]

    async def _run_four():
        t_start = time.perf_counter()
        outs = await asyncio.gather(
            *[
                asyncio.to_thread(extract.extract_plain_text, nl_payload, max_bytes=size_25mb)
                for _ in range(4)
            ]
        )
        return time.perf_counter() - t_start, outs

    wall_4, outs_4 = asyncio.run(_run_four())
    assert wall_4 < 0.6 * TIME_MARGIN, f"4 concurrent 25 MB plain-text calls took {wall_4:.3f}s"
    assert all(len(o.text) == extract.MAX_EXTRACTED_CHARS and o.partial for o in outs_4)

    # Multi-byte UTF-8 sequences straddling the initial byte slice decode without U+FFFD.
    for ch in ("أ", "—", "\U0001F600"):
        multi_bytes = ("a" + ch * 120).encode("utf-8")
        res_mb = extract.extract_plain_text(multi_bytes, max_chars=50)
        assert len(res_mb.text) == 50
        assert "\ufffd" not in res_mb.text
        assert res_mb.text == "a" + ch * 49
        assert res_mb.partial is True


def test_relationship_targets_cannot_leave_the_archive():
    """A relationship target is a member name inside the archive: it may go up within the
    archive, never above its root, and an absolute form is read from the root."""
    resolve = extract._resolve_rel_target
    assert resolve("xl", "worksheets/sheet1.xml") == "xl/worksheets/sheet1.xml"
    assert resolve("xl", "../docProps/core.xml") == "docProps/core.xml"
    assert resolve("xl", "/xl/worksheets/sheet1.xml") == "xl/worksheets/sheet1.xml"
    for climbing in ("../../outside.xml", "../../../etc/hosts", "..\\..\\outside.xml", "worksheets/../../../x"):
        assert resolve("xl", climbing) is None, climbing
    assert resolve("", "../outside.xml") is None
    assert resolve("xl", "") is None
    assert resolve("xl", "   ") is None

