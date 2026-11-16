"""Acceptance, rule, ligature-map, and linear-cost tests for ewsmcp.pdftext."""

from __future__ import annotations

import collections
import io
import json
import re
import time
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Sequence, Tuple

import pytest

from ewsmcp import extract, pdftext
from test_extract import _build_minimal_pdf

from conftest import TIME_MARGIN  # noqa: E402


VECTORS_PATH = Path(__file__).resolve().parent / "data" / "pdf_reading_order_vectors.json"


def _load_vector_pages() -> List[Tuple[str, int, bool, str, List[Dict[str, str]]]]:
    payload = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    cases: List[Tuple[str, int, bool, str, List[Dict[str, str]]]] = []
    for file_entry in payload["files"]:
        fname = file_entry["name"]
        by_word = bool(file_entry["laid_out_by_word"])
        for page_idx, page in enumerate(file_entry["pages"], start=1):
            cases.append((fname, page_idx, by_word, page["plain"], page["lines"]))
    return cases


VECTOR_PAGES = _load_vector_pages()


# ---------------------------------------------------------------------------
# 1. Full 14-file / 532-line real-program vector suite
# ---------------------------------------------------------------------------


def test_vector_dataset_completeness():
    assert len(VECTOR_PAGES) > 0
    total_lines = sum(len(lines) for *_rest, lines in VECTOR_PAGES)
    assert total_lines == 532


@pytest.mark.parametrize(
    ("file_name", "page_idx", "by_word", "plain_text", "line_specs"),
    VECTOR_PAGES,
    ids=[f"{fn}-p{pi}" for fn, pi, *_ in VECTOR_PAGES],
)
def test_real_pdf_reading_order_vectors(
    file_name: str,
    page_idx: int,
    by_word: bool,
    plain_text: str,
    line_specs: List[Dict[str, str]],
):
    visual_page = "\n".join(spec["visual"] for spec in line_specs)
    converted_page, notes = pdftext.page_reading_order(
        visual_page, plain_text, word=by_word
    )
    assert notes == [], f"Unexpected notes on {file_name} p{page_idx}: {notes}"

    got_lines = converted_page.split("\n")
    assert len(got_lines) == len(line_specs)

    for line_no, (got, spec) in enumerate(zip(got_lines, line_specs), start=1):
        expected = spec["expected"]
        mode = spec["match"]
        if mode == "exact":
            assert " ".join(got.split()) == " ".join(expected.split()), (
                f"{file_name} p{page_idx} L{line_no} (exact): "
                f"got={got!r} expected={expected!r}"
            )
        elif mode == "letters":
            assert "".join(got.split()) == "".join(expected.split()), (
                f"{file_name} p{page_idx} L{line_no} (letters): "
                f"got={got!r} expected={expected!r}"
            )
        else:
            pytest.fail(f"Unknown match mode {mode!r}")


# ---------------------------------------------------------------------------
# 2. Unit tests per rule (numbers, Latin islands, brackets, twins, repair)
# ---------------------------------------------------------------------------


def test_numbers_at_start_and_end_under_unicode_and_word_rules():
    # Unicode/browser rules (word=False): dates with `/` or `-` stay joined in visual
    # digit order, and `%` attaches to EN at line start.
    unicode_cases = [
        ("15-03-2026 :ﺦﻳرﺎﺘﻟا", "التاريخ: 2026-03-15"),
        ("15/03/2026 :ﺦﻳرﺎﺘﻟا", "التاريخ: 15/03/2026"),
        ("%85.5 زﺎﺠﻧﻹا ﺔﺒﺴﻧ", "نسبة الإنجاز 85.5%"),
        ("%15 ﺔﺒﻳﺮﻀﻟا", "الضريبة 15%"),
        ("ﻢﯿﻠﺴﺘﻟا ﺦﻳرﺎﺗ 2026-03-15", "2026-03-15 تاريخ التسليم"),
        ("ﻲﻟﺎﻤﺟﻹا ﻦﻣ 15%", "15% من الإجمالي"),
        ("ﻒﺗﺎﮫﻟا ﻢﻗر 100-456-7890", "100-456-7890 رقم الهاتف"),
        ("مﺎﻳأ 5", "5 أيام"),
        ("﷼ نﻮﯿﻠﻣ 3.5", "3.5 مليون ريال"),
    ]
    vis_u = "\n".join(v for v, _ in unicode_cases)
    exp_u = [e for _, e in unicode_cases]
    got_u, notes_u = pdftext.page_reading_order(vis_u, None, word=False)
    assert notes_u == []
    assert got_u.split("\n") == exp_u

    # Word rules (word=True): Word never joins digit groups across `-` or `/` and
    # never attaches `%` to a number, plus removes spaces around `-`/`/` between digits.
    word_cases = [
        ("15-03-2026 :خيراتلا", "التاريخ: 2026-03-15"),
        ("2026 / 03 / 15 :خيراتلا", "التاريخ: 15/03/2026"),
        ("014 / 2026 :عجرملا مقر", "رقم المرجع: 2026/014"),
        ("%85.5 زاجنإلا ةبسن", "نسبة الإنجاز 85.5%"),
        ("ميلستلا خيرات 15 - 03 - 2026", "2026-03-15 تاريخ التسليم"),
        ("يلامجإلا نم %15", "15% من الإجمالي"),
        ("فتاهلا مقر 7890-456-100", "100-456-7890 رقم الهاتف"),
    ]
    vis_w = "\n".join(v for v, _ in word_cases)
    exp_w = [e for _, e in word_cases]
    got_w, notes_w = pdftext.page_reading_order(vis_w, None, word=True)
    assert notes_w == []
    assert got_w.split("\n") == exp_w


def test_latin_islands_with_numbers_and_word_arabic_comma():
    cases_unicode = [
        ("ﺮﻳرﺎﻘﺘﻟا ﻲﻓ Power BI مﺪﺨﺘﺴﻧ", "نستخدم Power BI في التقارير"),
        ("ﺪﻤﺘﻌﻤﻟا ISO 27001 رﺎﯿﻌﻣ ﻖﻓو", "وفق معيار ISO 27001 المعتمد"),
        ("ﺎﯿﻟﺎﺣ Windows Server 2022 ﻰﻠﻋ ﻞﻤﻌﻳ", "يعمل على Windows Server 2022 حاليا"),
        ("ةﺪﻤﺘﻌﻤﻟا Incoterms 2020 (DAP) ﻖﻓو", "وفق Incoterms 2020 (DAP) المعتمدة"),
        ("تﺮﺛأ COVID   - 19 ﺔﺤﺋﺎﺟ", "جائحة COVID-19 أثرت"),
        ("ﻂﻘﻓ GB 5 ﺔﺣﺎﺴﻤﻟا", "المساحة GB 5 فقط"),
        ("ﺎﻳﺮﮫﺷ USD 500 ﺮﻌﺴﻟا", "السعر USD 500 شهريا"),
    ]
    plain = "COVID-19"
    got, _ = pdftext.page_reading_order(
        "\n".join(v for v, _ in cases_unicode), plain, word=False
    )
    assert got.split("\n") == [e for _, e in cases_unicode]

    # In Word, Arabic comma `،` does not merge surrounding Latin words into one island.
    # The line has more Latin than Arabic letters, so it is read right-to-left only on a
    # page where Arabic dominates: give it one (a first line of Arabic text).
    got_w, _ = pdftext.page_reading_order(
        "يونسلا ةعجارملا ريرقت\nناسفانتت Google ،Microsoft اتكرش", None, word=True
    )
    assert got_w.split("\n") == [
        "تقرير المراجعة السنوي",
        "شركتا Microsoft، Google تتنافسان",
    ]
    # Alone on a page, Latin letters dominate: the Arabic words are converted in place.
    alone, _ = pdftext.page_reading_order("ناسفانتت Google ،Microsoft اتكرش", None, word=True)
    assert alone == "تتنافسان Google ،Microsoft شركتا"


def test_both_bracket_conventions_lone_brackets_and_arabic_indic_digits():
    # Browser drawn bracket shapes (mirrored after reversal):
    b_vis = "\n".join([
        "{أ} مﺎﻈﻨﻟا ﻦﻣ [3] ةدﺎﻤﻟا",
        "ﺔﺑﻮﻠﻄﻣ (Data Governance) تﺎﻧﺎﯿﺒﻟا ﺔﻤﻛﻮﺣ",
        "(2026)",
        "(لوأ",  # lone closing bracket drawn at visual left after RTL word
        "ةيادب)",  # lone opening bracket drawn at visual right before RTL word
    ])
    b_got, _ = pdftext.page_reading_order(b_vis, None, word=False)
    assert b_got.split("\n") == [
        "المادة [3] من النظام {أ}",
        "حوكمة البيانات (Data Governance) مطلوبة",
        "(2026)",
        "أول)",
        "(بداية",
    ]

    # Word logical bracket characters (not mirrored after reversal):
    w_vis = "\n".join([
        "}أ{ ماظنلا نم ]3[ ةداملا",
        "ةبولطم )Data Governance( تانايبلا ةمكوح",
        ")2026(",
        ")لوأ",
        "ةيادب(",
    ])
    w_got, _ = pdftext.page_reading_order(w_vis, None, word=True)
    assert w_got.split("\n") == [
        "المادة [3] من النظام {أ}",
        "حوكمة البيانات (Data Governance) مطلوبة",
        "(2026)",
        "أول)",
        "(بداية",
    ]

    # Arabic-Indic digits:
    indic_vis = "\n".join([
        "١٥-٠٣-٢٠٢٦ :خيراتلا",
        "لاير ١٬٢٥٠٫٥٠ غلبملا",
        "طقف ٪١٥ ةبسن",
    ])
    indic_got, _ = pdftext.page_reading_order(indic_vis, None, word=True)
    assert indic_got.split("\n") == [
        "التاريخ: ٢٠٢٦-٠٣-١٥",
        "المبلغ ١٬٢٥٠٫٥٠ ريال",
        "نسبة ١٥٪ فقط",
    ]


def test_persian_twins_with_and_without_persian_letter_on_page():
    # Arabic page (no Persian/Urdu-only letters): U+06CC, U+06BE, U+06A9 and their
    # presentation forms fold to Arabic ي / ى, ه, ك.
    arabic_visual = "\n".join([
        "ﯽﻠﻋ ﻰﻟإ بﺎﺘک ﻞﺳرأ",  # ک (06A9) and ﯽ (FBFD final Farsi yeh -> ى)
        "ﺮﯾﺮﻘﺗ و ﮫﻠﻟا",  # ﯾ (FBFF medial Farsi yeh -> ي) and ﮫ (FBAB Heh -> ه)
        "یﺪھ ﺮیبک کﻼﮭﺘﺳا",  # base ی medial (-> ي) & final (-> ى), ھ (-> ه), ک (-> ك)
    ])
    ar_got, _ = pdftext.page_reading_order(arabic_visual, None, word=False)
    assert ar_got.split("\n") == [
        "أرسل كتاب إلى على",
        "الله و تقرير",
        "استهلاك كبير هدى",
    ]

    # Page containing a Persian-only letter (`پ` U+067E or `گ` U+06AF): twins are
    # genuine Persian letters and must NOT be folded to Arabic.
    persian_visual = "شرازگ بﺎﺘک پﺎچ"  # چاپ کتاب گزارش
    fa_got, _ = pdftext.page_reading_order(persian_visual, None, word=False)
    assert "پ" in fa_got and "گ" in fa_got
    assert "ک" in fa_got and "ك" not in fa_got


def test_word_repair_joins_split_words_without_joining_two_real_words():
    # 1. Joins split pieces when the joined word is in plain_text and pieces are not all words.
    visual = "م ي ل س ت ل ا   م ت   خ ي ر ا ت ب"
    plain = "بتاريخ تم التسليم"
    got, _ = pdftext.page_reading_order(visual, plain, word=True)
    assert got == "بتاريخ تم التسليم"

    # 2. Never joins two real standalone words even if their concatenation also appears in plain.
    visual_two_words = "لخاد نم عورشملا"
    plain_two_words = "المشروع من داخل منداخل"
    got_two, _ = pdftext.page_reading_order(visual_two_words, plain_two_words, word=True)
    assert got_two == "المشروع من داخل"

    # 3. Word repair joins at most MAX_WORD_REPAIR_PIECES (12) pieces.
    assert pdftext.MAX_WORD_REPAIR_PIECES == 12
    twelve_word = "استمراريةعمل"  # 12 Arabic letters
    assert len(twelve_word) == 12
    split_12 = " ".join(twelve_word)
    assert pdftext.repair_words(split_12, {twelve_word}) == twelve_word

    thirteen_word = "استمراريةعملن"  # 13 Arabic letters
    assert len(thirteen_word) == 13
    split_13 = " ".join(thirteen_word)
    assert pdftext.repair_words(split_13, {thirteen_word}) == split_13


def test_laid_out_by_word_metadata_detection():
    w_prod = SimpleNamespace(metadata={"/Producer": "Microsoft® Word 2019", "/Creator": ""})
    w_crea = SimpleNamespace(metadata={"/Producer": "macOS Quartz", "/Creator": "Word"})
    non_w = SimpleNamespace(metadata={"/Producer": "Skia/PDF m128", "/Creator": "Chromium"})

    class _BrokenReader:
        @property
        def metadata(self):
            raise RuntimeError("corrupt info ref")

    assert pdftext.laid_out_by_word(w_prod) is True
    assert pdftext.laid_out_by_word(w_crea) is True
    assert pdftext.laid_out_by_word(non_w) is False
    assert pdftext.laid_out_by_word(None) is False
    assert pdftext.laid_out_by_word(_BrokenReader()) is False

    # Whole-word matching: Word producers match; substring lookalikes do not.
    for word_meta in (
        "Microsoft® Word for Microsoft 365",
        "Microsoft Word",
        "Acrobat PDFMaker 24 for Word",
    ):
        assert pdftext.laid_out_by_word(
            SimpleNamespace(metadata={"/Producer": word_meta, "/Creator": ""})
        ) is True
        assert pdftext.laid_out_by_word(
            SimpleNamespace(metadata={"/Producer": "", "/Creator": word_meta})
        ) is True

    for non_word_meta in ("WordPress", "Password Depot", "Swordfish"):
        assert pdftext.laid_out_by_word(
            SimpleNamespace(metadata={"/Producer": non_word_meta, "/Creator": ""})
        ) is False
        assert pdftext.laid_out_by_word(
            SimpleNamespace(metadata={"/Producer": "", "/Creator": non_word_meta})
        ) is False


# ---------------------------------------------------------------------------
# 3. fix_ligature_maps (/ToUnicode multi-letter RTL ligature repair)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not extract.HAS_PYPDF, reason="pypdf not installed")
def test_fix_ligature_maps_repairs_salam_lam_alef_and_ignores_oversized_or_malformed(
    monkeypatch,
):
    import pypdf

    # Glyph 0x21 -> س (0633), Glyph 0x22 -> لا (<0644 0627> in reading order!), Glyph 0x23 -> م (0645).
    # Visually on the page left-to-right: م (0x23), لا (0x22), س (0x21).
    salam_pdf = _build_minimal_pdf([{
        "raw_content": b"BT /F2 12 Tf 72 700 Td <232221> Tj ET",
        "raw_cmap_pairs": [
            (b"21", b"0633"),
            (b"22", b"0644 0627"),
            (b"23", b"0645"),
        ],
    }])

    # Without fix_ligature_maps, reversing the visual line reverses لا into ال -> "سالم".
    raw_reader = pypdf.PdfReader(io.BytesIO(salam_pdf))
    unfixed_layout = raw_reader.pages[0].extract_text(extraction_mode="layout")
    unfixed_out, _ = pdftext.page_reading_order(unfixed_layout, None, word=True)
    assert unfixed_out.strip() == "سالم"

    # With fix_ligature_maps (and through extract.extract_pdf), "سلام" is recovered cleanly.
    fixed_reader = pypdf.PdfReader(io.BytesIO(salam_pdf))
    patched = pdftext.fix_ligature_maps(fixed_reader)
    assert patched == 1
    fixed_layout = fixed_reader.pages[0].extract_text(extraction_mode="layout")
    fixed_out, _ = pdftext.page_reading_order(fixed_layout, None, word=True)
    assert fixed_out.strip() == "سلام"

    full_res = extract.extract_pdf(salam_pdf)
    assert full_res.text.strip() == "سلام"

    # Oversized /ToUnicode map (> MAX_TOUNICODE_BYTES) is skipped untouched.
    assert pdftext.MAX_TOUNICODE_BYTES == 2_000_000
    with monkeypatch.context() as mp:
        mp.setattr(pdftext, "MAX_TOUNICODE_BYTES", 40)
        big_reader = pypdf.PdfReader(io.BytesIO(salam_pdf))
        assert pdftext.fix_ligature_maps(big_reader) == 0

    # Codespace wider than 2 bytes (<000000> <FFFFFF>) is skipped untouched.
    wide_cs_pdf = salam_pdf.replace(b"<00> <FF>", b"<000000> <FFFFFF>")
    wide_reader = pypdf.PdfReader(io.BytesIO(wide_cs_pdf))
    assert pdftext.fix_ligature_maps(wide_reader) == 0

    # Malformed / broken reader or font objects never raise.
    class _BrokenFont:
        def get(self, _k):
            raise ValueError("corrupt font dict")

    fake_page = {"/Resources": {"/Font": {"/F1": _BrokenFont()}}}
    assert pdftext.fix_ligature_maps(SimpleNamespace(pages=[fake_page])) == 0
    assert pdftext.fix_ligature_maps(None) == 0


@pytest.mark.skipif(not extract.HAS_PYPDF, reason="pypdf not installed")
def test_word_repair_with_rewritten_ligature_map_uses_unpatched_plain_text(monkeypatch):
    import pypdf

    # Hand-built PDF for "للاستفسار": the glyphs are written left to right as the page
    # shows them (visual order), and glyph 0x22 is the lam-alef ligature, mapped to
    # <0644 0627> in reading order as Word does.
    ligature_pdf = _build_minimal_pdf([{
        "raw_content": (
            b"BT /F2 12 Tf 1 0 0 1 320 700 Tm "
            b"<2928232624232221> Tj ET"
        ),
        "raw_cmap_pairs": [
            (b"21", b"0644"),       # ل
            (b"22", b"0644 0627"),  # لا (multi-letter glyph)
            (b"23", b"0633"),       # س
            (b"24", b"062A"),       # ت
            (b"26", b"0641"),       # ف
            (b"28", b"0627"),       # ا
            (b"29", b"0631"),       # ر
        ],
    }])

    # Plain mode keeps the ligature as one unit: right with the file's own map, wrong
    # once the map has been rewritten for the layout pass.
    untouched = pypdf.PdfReader(io.BytesIO(ligature_pdf))
    assert (untouched.pages[0].extract_text() or "").strip() == "للاستفسار"
    rewritten = pypdf.PdfReader(io.BytesIO(ligature_pdf))
    assert pdftext.fix_ligature_maps(rewritten) == 1
    assert (rewritten.pages[0].extract_text() or "").strip() != "للاستفسار"

    # The extractor must hand the word repair the plain text of the untouched file.
    seen = {}
    real = extract.page_reading_order

    def spy(layout_text, plain_text=None, **kw):
        seen["plain"] = plain_text
        return real(layout_text, plain_text, **kw)

    monkeypatch.setattr(extract, "page_reading_order", spy)
    res = extract.extract_bytes(ligature_pdf, name="ligature.pdf")
    assert (seen["plain"] or "").strip() == "للاستفسار"
    assert res.text.strip() == "للاستفسار"


# ---------------------------------------------------------------------------
# 4. Non-RTL pages are byte-identical to layout mode
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not extract.HAS_PYPDF, reason="pypdf not installed")
def test_page_with_no_rtl_letter_is_byte_identical_to_layout_mode():
    layout_sample = (
        "    Quarterly Financial Summary   2026\n\n"
        "  Item          Q1        Q2\n"
        "  Revenue       1,200     1,450   \n"
        "  Question: What is this؟\n"
    )
    out, notes = pdftext.page_reading_order(
        layout_sample, "Quarterly Financial Summary", word=True
    )
    assert notes == []
    assert out == layout_sample
    assert out.encode("utf-8") == layout_sample.encode("utf-8")

    # Also verify end-to-end through extract_pdf against pypdf layout mode directly.
    import pypdf

    eng_pdf = _build_minimal_pdf([
        {"ascii_lines": ["  Header    ColumnA    ColumnB", "  Row 1     100        200"]}
    ])
    expected_layout = pypdf.PdfReader(io.BytesIO(eng_pdf)).pages[0].extract_text(
        extraction_mode="layout"
    )
    extracted = extract.extract_pdf(eng_pdf)
    assert extracted.notes == ()
    assert extracted.text.encode("utf-8") == expected_layout.encode("utf-8")


# ---------------------------------------------------------------------------
# 5. Cost, line-length bound (5,000), 200k-char page (<2s), and never-raises
# ---------------------------------------------------------------------------


def test_line_limit_linear_hostile_shapes_200k_page_under_2s_and_error_fallback(
    monkeypatch,
):
    assert pdftext.MAX_LINE_CHARS == 5_000
    assert pdftext.MAX_CODESPACE_HEX_CHARS == 4

    # 1. Line > 5,000 chars after collapsing spaces is returned unconverted with a note,
    # while normal lines on the same page still convert.
    long_line = ("بجد " * 1_260).strip()  # 5,039 chars after collapsing spaces
    assert len(long_line) > pdftext.MAX_LINE_CHARS
    mixed_page = f"يونسلا ةعجارملا ريرقت\n{long_line}"
    out_mixed, notes_mixed = pdftext.page_reading_order(mixed_page, None, word=True)
    out_lines = out_mixed.split("\n")
    assert out_lines[0] == "تقرير المراجعة السنوي"
    assert out_lines[1] == long_line
    assert len(notes_mixed) == 1 and "5000" in notes_mixed[0]

    # A line with 100,000 spaces that collapses below 5,000 chars DOES convert quickly.
    padded_short = "يونسلا" + (" " * 100_000) + "ريرقت"
    t0 = time.perf_counter()
    out_padded, notes_padded = pdftext.page_reading_order(padded_short, None, word=True)
    assert (time.perf_counter() - t0) < 0.5 * TIME_MARGIN
    assert notes_padded == []
    assert out_padded == "تقرير السنوي"

    # 2. Hostile shapes that were quadratic in the prototype (chained digit groups before
    # Latin, long neutral runs in neighbour(), deeply nested brackets) run in < 0.5s.
    hostile_lines: Sequence[str] = (
        ("1 " * 2_400) + "abc ب",
        "1" + ("." * 4_500) + "a ب",
        (")" * 2_000) + "abc" + ("(" * 2_000) + " ب",
    )
    for hline in hostile_lines:
        assert len(hline) <= pdftext.MAX_LINE_CHARS
        t0 = time.perf_counter()
        _out, hnotes = pdftext.page_reading_order(hline, None, word=False)
        assert (time.perf_counter() - t0) < 0.5 * TIME_MARGIN
        assert hnotes == []

    # 3. A 200,000-character mixed Arabic/Latin/numeric page converts in under 2 seconds.
    sample_vis = (
        "ةبولطم (Data Governance) تانايبلا ةمكوح — 15-03-2026 :خيراتلا — %85.5 زاجنإلا ةبسن"
    )
    repeats = (200_000 // (len(sample_vis) + 1)) + 2
    big_layout = "\n".join([sample_vis] * repeats)
    big_plain = "نسبة الإنجاز 85.5% التاريخ 2026-03-15 حوكمة البيانات Data Governance مطلوبة"
    assert len(big_layout) >= 200_000

    t0 = time.perf_counter()
    big_out, big_notes = pdftext.page_reading_order(big_layout, big_plain, word=False)
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0 * TIME_MARGIN, f"200k-char page took {elapsed:.3f}s (limit 2.0s)"
    assert big_notes == []
    assert big_out.startswith("نسبة الإنجاز 85.5%")

    # 4. Internal error never raises: returns original layout_text with a note.
    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated internal failure")

    monkeypatch.setattr(pdftext, "_convert_single_line", _boom)
    fb_text, fb_notes = pdftext.page_reading_order("يونسلا ريرقت", None, word=False)
    assert fb_text == "يونسلا ريرقت"
    assert len(fb_notes) == 1 and "RuntimeError" in fb_notes[0]


# ---------------------------------------------------------------------------
# 6. Round 7: bfrange start-string expansion, linear word repair & regex stress
# ---------------------------------------------------------------------------


def _pdf_with_custom_cmap(content_stream: bytes, cmap_body: bytes) -> bytes:
    cmap = (
        b"/CIDInit /ProcSet findresource begin\n"
        b"12 dict begin\n"
        b"begincmap\n"
        b"/CMapName /Adobe-Identity-UCS def\n"
        b"/CMapType 2 def\n"
        b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        + cmap_body
        + b"\n1 beginbfchar\n<44> <0628>\nendbfchar\n"
        b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    )

    def _stream(raw: bytes) -> bytes:
        comp = zlib.compress(raw)
        return (
            f"<< /Filter /FlateDecode /Length {len(comp)} >>\nstream\n".encode("ascii")
            + comp
            + b"\nendstream"
        )

    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>",
        5: _stream(content_stream),
        6: _stream(cmap),
    }
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
def test_bfrange_start_string_expanded_and_over_256_left_untouched():
    import pypdf

    assert pdftext.MAX_BFRANGE_EXPAND == 256

    variants = {
        "range_start_string": b"1 beginbfrange\n<41> <43> <06440627>\nendbfrange",
        "range_array": (
            b"1 beginbfrange\n<41> <43> [<06440627> <06440628> <06440629>]\nendbfrange"
        ),
        "three_bfchar": (
            b"3 beginbfchar\n<41> <06440627>\n<42> <06440628>\n<43> <06440629>\nendbfchar"
        ),
    }
    extracted_texts: Dict[str, str] = {}
    for label, block in variants.items():
        pdf_bytes = _pdf_with_custom_cmap(b"BT /F1 12 Tf 72 700 Td (ABCD) Tj ET", block)
        plain = pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text() or ""
        res = extract.extract_pdf(pdf_bytes)
        plain_letters = collections.Counter("".join(plain.split()))
        tool_letters = collections.Counter("".join(res.text.split()))
        assert tool_letters == plain_letters, (
            f"{label}: invented={tool_letters - plain_letters}, "
            f"missing={plain_letters - tool_letters}"
        )
        extracted_texts[label] = res.text.strip()

    assert extracted_texts["range_start_string"] == extracted_texts["range_array"]
    assert extracted_texts["range_start_string"] == extracted_texts["three_bfchar"]

    # Ranges of > 256 codes are left completely untouched (start string not reversed).
    over_256_cmap = (
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        "1 beginbfrange\n<0100> <0200> <06440627>\nendbfrange\n"
    )
    assert pdftext._rewrite_cmap_ligatures(over_256_cmap) == over_256_cmap

    # Exactly 256 codes (<0100> <01FF>) IS expanded into array form.
    exact_256_cmap = (
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        "1 beginbfrange\n<0100> <01FF> <06440627>\nendbfrange\n"
    )
    rewritten_256 = pdftext._rewrite_cmap_ligatures(exact_256_cmap)
    assert "[<06270644> <06280644>" in rewritten_256


def test_repair_words_linear_without_split_word_regex():
    assert not hasattr(pdftext, "_SPLIT_WORD_RE")

    # 24,000 Arabic letters with no spaces previously took ~9.5s in _SPLIT_WORD_RE;
    # linear scan completes in milliseconds.
    unbroken_ar = ("ا" * 24_000) + " ب"
    t0 = time.perf_counter()
    repaired = pdftext.repair_words(unbroken_ar, {"سلام", "تقرير"})
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.2 * TIME_MARGIN, f"repair_words took {elapsed:.3f}s on 24k Arabic chars"
    assert repaired == unbroken_ar


def test_compiled_regexes_in_extract_and_pdftext_are_linear_at_20000_chars():
    # Exact single-character and pair alphabet from .gemini-pair/reference/regex_stress.py.
    chars = [
        " ", "\t", "\n", ".", "&", "#", ";", "/", "\\", "@", "a", "A", "1", "-",
        "_", ":", "%", "=", "\"", "'", "`", "<", ">", "(", ")", ",", "x", "́",
        "\u00a0", "\u200b", "ا",
    ]
    pairs = [
        " .", ". ", "a.", ".a", "a ", " a", "&#", "&a", "a&", "1.", "/\\", "a@",
        "@a", "a-", "-a", "a:", ":a", "%2", "=\"", "a\t", "\n ", " \n", "á",
        ". \t", "&amp", "&#x2", "x.y",
    ]

    for mod in (extract, pdftext):
        patterns = [
            (name, val)
            for name, val in vars(mod).items()
            if isinstance(val, re.Pattern)
        ]
        assert patterns, f"Expected compiled regex patterns in {mod.__name__}"
        for name, rx in patterns:
            is_bytes = isinstance(rx.pattern, bytes)
            for unit in chars + pairs:
                base_str = unit * (20_000 // len(unit))
                for suffix in ("", "!"):
                    sample_str = base_str + suffix
                    if is_bytes:
                        sample_b = sample_str.encode("utf-8")
                        for op_name, fn in (
                            ("finditer", lambda s=sample_b, r=rx: sum(1 for _ in r.finditer(s))),
                            ("sub", lambda s=sample_b, r=rx: r.sub(b"", s)),
                            ("search", lambda s=sample_b, r=rx: r.search(s)),
                            ("match", lambda s=sample_b, r=rx: r.match(s)),
                        ):
                            t0 = time.perf_counter()
                            fn()
                            dt = time.perf_counter() - t0
                            assert dt <= 0.5 * TIME_MARGIN, (
                                f"{mod.__name__}.{name}.{op_name} took {dt:.3f}s "
                                f"(>0.5s) on unit={unit!r} suffix={suffix!r}"
                            )
                    else:
                        for op_name, fn in (
                            ("finditer", lambda s=sample_str, r=rx: sum(1 for _ in r.finditer(s))),
                            ("sub", lambda s=sample_str, r=rx: r.sub("", s)),
                            ("search", lambda s=sample_str, r=rx: r.search(s)),
                            ("match", lambda s=sample_str, r=rx: r.match(s)),
                        ):
                            t0 = time.perf_counter()
                            fn()
                            dt = time.perf_counter() - t0
                            assert dt <= 0.5 * TIME_MARGIN, (
                                f"{mod.__name__}.{name}.{op_name} took {dt:.3f}s "
                                f"(>0.5s) on unit={unit!r} suffix={suffix!r}"
                            )
