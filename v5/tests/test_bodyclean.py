"""Golden-style tests for src.body_clean — deterministic email body cleaning.

All fixtures are realistic inline constants; assertions pin exact cleaned
outputs (not just lengths) so any behavior drift fails loudly.
"""

from ewsmcp.bodyclean import (
    clean_body,
    html_to_text,
    strip_quoted_history,
    strip_signature,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

EN_OUTLOOK = (
    "Hi team,\n"
    "\n"
    "The Q2 numbers are attached. Please review the variance on slide 4\n"
    "before Thursday's meeting.\n"
    "\n"
    "From: Alice <alice@example.com>\n"
    "Sent: Monday, June 1, 2026 9:14 AM\n"
    "To: Bob <bob@bank.example>\n"
    "Subject: RE: Q2 numbers\n"
    "\n"
    "Earlier text that should be stripped.\n"
)
EN_OUTLOOK_KEPT = (
    "Hi team,\n"
    "\n"
    "The Q2 numbers are attached. Please review the variance on slide 4\n"
    "before Thursday's meeting."
)

AR_OUTLOOK = (
    "وعليكم السلام،\n"
    "\n"
    "تم استلام التقرير وسنوافيكم بالملاحظات قبل نهاية الأسبوع.\n"
    "\n"
    "من: المرسل <alice@example.com>\n"
    "تاريخ الإرسال: الأحد 31 مايو 2026 2:10 م\n"
    "إلى: المستلم\n"
    "الموضوع: التقرير الربعي\n"
    "\n"
    "نص قديم.\n"
)
AR_OUTLOOK_KEPT = (
    "وعليكم السلام،\n"
    "\n"
    "تم استلام التقرير وسنوافيكم بالملاحظات قبل نهاية الأسبوع."
)

GMAIL_EN = (
    "Sounds good — see you at 10.\n"
    "I will bring the printed deck.\n"
    "\n"
    "On Mon, Jun 1, 2026 at 9:14 AM Alice <alice@example.com> wrote:\n"
    "> Can we move the sync to 10?\n"
    "> The room is taken at 9.\n"
)
GMAIL_EN_KEPT = "Sounds good — see you at 10.\nI will bring the printed deck."

GMAIL_AR = (
    "تمام، أراك غداً إن شاء الله.\n"
    "سأرسل جدول الأعمال قبل الاجتماع.\n"
    "\n"
    "في الأحد، 31 مايو 2026 في 9:14 ص، كتب المرسل <alice@example.com>:\n"
    "النص السابق هنا.\n"
)
GMAIL_AR_KEPT = "تمام، أراك غداً إن شاء الله.\nسأرسل جدول الأعمال قبل الاجتماع."

MIXED_AR_EN = (
    "Dear Team,\n"
    "\n"
    "نوافق على المقترح بصيغته الحالية.\n"
    "We will sign tomorrow.\n"
    "\n"
    "-----Original Message-----\n"
    "From: Procurement <procure@example.com>\n"
    "Sent: Sunday, May 31, 2026 1:00 PM\n"
    "\n"
    "Old content.\n"
)
MIXED_AR_EN_KEPT = (
    "Dear Team,\n"
    "\n"
    "نوافق على المقترح بصيغته الحالية.\n"
    "We will sign tomorrow."
)

QUOTED_RUN = (
    "Thanks for the update.\n"
    "Looks good to me.\n"
    "\n"
    "> Status: deployment finished at 22:00.\n"
    "> All checks passed.\n"
    "> Rollback plan was not needed.\n"
)
QUOTED_RUN_KEPT = "Thanks for the update.\nLooks good to me."

# Quoted Arabic Outlook header with RTL marks (U+200F) — must still cut.
BIDI_QUOTED = (
    "شكراً جزيلاً.\n"
    "سيتم التنفيذ كما تفضلتم.\n"
    "\n"
    "> ‏من: المرسل <alice@example.com>\n"
    "> ‏التاريخ: 31 مايو 2026\n"
    "> الموضوع: العرض\n"
)
BIDI_QUOTED_KEPT = "شكراً جزيلاً.\nسيتم التنفيذ كما تفضلتم."

# Body that STARTS with a From:/Sent:-looking pair — must NOT be cut.
FROM_START = (
    "From: my point of view, the proposal is solid.\n"
    "Sent: I mean, submitted, last night for review.\n"
    "Let me know your thoughts."
)

# Mid-paragraph "From:" with no Sent:/Date: partner — must NOT be cut.
FROM_MIDPARA = (
    "The committee reviewed the file.\n"
    "All members agreed.\n"
    "From: a governance standpoint this is fine.\n"
    "We can proceed."
)

PLAIN = (
    "Quick note — the dashboard refresh moved to 6 AM.\n"
    "No action needed from your side."
)

SIG_EN = (
    "The contract is signed and archived.\n"
    "Finance has been notified.\n"
    "\n"
    "Best regards,\n"
    "Alice\n"
    "Director, Legal\n"
)
SIG_EN_KEPT = "The contract is signed and archived.\nFinance has been notified."

SIG_AR = (
    "تم اعتماد المسودة النهائية.\n"
    "سيتم الرفع للإدارة غداً صباحاً.\n"
    "\n"
    "تحياتي\n"
    "فريق العمل\n"
)
SIG_AR_KEPT = "تم اعتماد المسودة النهائية.\nسيتم الرفع للإدارة غداً صباحاً."

SIG_AR_FORMAL = (
    "نشكر لكم تعاونكم المستمر.\n"
    "تم رفع المحضر للاعتماد.\n"
    "\n"
    "وتفضلوا بقبول فائق الاحترام والتقدير،\n"
    "إدارة العمليات\n"
    "مدير إدارة البيانات\n"
)
SIG_AR_FORMAL_KEPT = "نشكر لكم تعاونكم المستمر.\nتم رفع المحضر للاعتماد."

SIG_DELIM = (
    "Numbers confirmed.\n"
    "Invoice goes out today.\n"
    "\n"
    "-- \n"
    "Alice\n"
    "Example Ltd\n"
)
SIG_DELIM_KEPT = "Numbers confirmed.\nInvoice goes out today."

FULL_EMAIL = (
    "Hi Alice,\n"
    "\n"
    "Approved — please proceed with phase two.\n"
    "Loop in finance on the PO.\n"
    "\n"
    "\n"
    "\n"
    "Best regards,\n"
    "Bob\n"
    "\n"
    "From: Alice <alice@example.com>\n"
    "Sent: Monday, June 1, 2026 8:02 AM\n"
    "To: Bob\n"
    "Subject: Phase two\n"
    "\n"
    "May we proceed?\n"
)
FULL_EMAIL_CLEAN = (
    "Hi Alice,\n"
    "\n"
    "Approved — please proceed with phase two.\n"
    "Loop in finance on the PO."
)

TRUNC_SUFFIX = "… [truncated]"

HTML_DOC = (
    "<html><head><title>x</title><style>p {color: red}</style></head><body>"
    "<p>مرحباً <b>بالفريق</b>،</p>"
    "<table><tr><td>البند</td><td>القيمة</td></tr>"
    "<tr><td>Total</td><td>500 &amp; up</td></tr></table>"
    '<p>See <a href="https://example.com/report">report</a> first.</p>'
    '<p>Track: <a href="https://example.com/r?token=' + "x" * 90 + '">'
    "this link</a></p>"
    '<p><a href="https://example.com/">example.com</a></p>'
    "</body></html>"
)


# ---------------------------------------------------------------------------
# strip_quoted_history
# ---------------------------------------------------------------------------

def test_outlook_en_chain_cut_at_from_sent_pair():
    assert strip_quoted_history(EN_OUTLOOK) == (EN_OUTLOOK_KEPT, 1)


def test_outlook_ar_chain_cut_at_arabic_header():
    assert strip_quoted_history(AR_OUTLOOK) == (AR_OUTLOOK_KEPT, 1)


def test_gmail_en_attribution_cut():
    assert strip_quoted_history(GMAIL_EN) == (GMAIL_EN_KEPT, 1)


def test_gmail_ar_attribution_cut():
    assert strip_quoted_history(GMAIL_AR) == (GMAIL_AR_KEPT, 1)


def test_mixed_ar_en_original_message_counts_inner_header_too():
    # "-----Original Message-----" plus the From:/Sent: pair inside the
    # stripped tail -> 2 markers counted.
    assert strip_quoted_history(MIXED_AR_EN) == (MIXED_AR_EN_KEPT, 2)


def test_quoted_run_of_three_lines_cut_as_one_block():
    assert strip_quoted_history(QUOTED_RUN) == (QUOTED_RUN_KEPT, 1)


def test_bidi_marks_and_quote_prefix_tolerated():
    # The quoted Arabic header is both a '>' run and a من/التاريخ pair —
    # absorbed into a single counted block.
    assert strip_quoted_history(BIDI_QUOTED) == (BIDI_QUOTED_KEPT, 1)


def test_body_starting_with_from_like_line_survives():
    assert strip_quoted_history(FROM_START) == (FROM_START, 0)


def test_midparagraph_from_without_pair_not_cut():
    assert strip_quoted_history(FROM_MIDPARA) == (FROM_MIDPARA, 0)


def test_plain_body_unchanged_count_zero():
    assert strip_quoted_history(PLAIN) == (PLAIN, 0)


def test_original_message_may_cut_even_on_first_line():
    text = (
        "-----Original Message-----\n"
        "From: X <x@y.example>\n"
        "Sent: Monday\n"
        "\n"
        "Old."
    )
    assert strip_quoted_history(text) == ("", 2)


def test_strip_quoted_history_empty_inputs():
    assert strip_quoted_history("") == ("", 0)
    assert strip_quoted_history(None) == ("", 0)


# ---------------------------------------------------------------------------
# strip_signature
# ---------------------------------------------------------------------------

def test_signature_en_closer_block_stripped():
    assert strip_signature(SIG_EN) == SIG_EN_KEPT


def test_signature_ar_short_closer_stripped():
    assert strip_signature(SIG_AR) == SIG_AR_KEPT


def test_signature_ar_formal_closer_stripped():
    assert strip_signature(SIG_AR_FORMAL) == SIG_AR_FORMAL_KEPT


def test_signature_dash_dash_delimiter_stripped():
    assert strip_signature(SIG_DELIM) == SIG_DELIM_KEPT


def test_signature_sent_from_my_stripped():
    text = "On it.\nWill confirm by noon.\n\nSent from my iPhone"
    assert strip_signature(text) == "On it.\nWill confirm by noon."


def test_signature_not_stripped_when_body_too_short():
    text = "Approved.\n\nBest regards,\nBob"
    assert strip_signature(text) == text


def test_closer_like_sentence_in_body_not_stripped():
    # "Thanks," followed by real prose must not be treated as a closer.
    text = (
        "Hi team\n"
        "The report is ready for review.\n"
        "Thanks, that works for me as well."
    )
    assert strip_signature(text) == text


def test_strip_signature_empty_inputs():
    assert strip_signature("") == ""
    assert strip_signature(None) == ""


# ---------------------------------------------------------------------------
# clean_body pipeline
# ---------------------------------------------------------------------------

def test_clean_body_full_email_quote_and_signature():
    assert clean_body(FULL_EMAIL) == {
        "text": FULL_EMAIL_CLEAN,
        "quoted_blocks_stripped": 1,
        "truncated": False,
        "original_chars": len(FULL_EMAIL),
    }


def test_clean_body_collapses_three_plus_blank_lines():
    text = "Line one.\nLine two.\n\n\n\nLine three."
    result = clean_body(text)
    assert result["text"] == "Line one.\nLine two.\n\nLine three."
    assert result["quoted_blocks_stripped"] == 0
    assert result["truncated"] is False


def test_clean_body_normalizes_crlf():
    result = clean_body("A line.\r\nSecond line.\r\n")
    assert result["text"] == "A line.\nSecond line."
    assert result["original_chars"] == len("A line.\r\nSecond line.\r\n")


def test_clean_body_truncates_at_word_boundary():
    text = "alpha bravo charlie " * 40  # 800 chars, no markers
    result = clean_body(text, max_chars=100)
    assert result["truncated"] is True
    assert result["original_chars"] == 800
    assert len(result["text"]) <= 100
    assert result["text"].endswith(TRUNC_SUFFIX)
    body = result["text"][: -len(TRUNC_SUFFIX)]
    # No word may be cut in half.
    assert set(body.split()) <= {"alpha", "bravo", "charlie"}


def test_clean_body_no_truncation_under_limit():
    result = clean_body(PLAIN, max_chars=4000)
    assert result["text"] == PLAIN
    assert result["truncated"] is False
    assert TRUNC_SUFFIX not in result["text"]


def test_clean_body_empty_and_none():
    expected = {
        "text": "",
        "quoted_blocks_stripped": 0,
        "truncated": False,
        "original_chars": 0,
    }
    assert clean_body("") == expected
    assert clean_body(None) == expected


# ---------------------------------------------------------------------------
# html_to_text
# ---------------------------------------------------------------------------

def test_html_to_text_table_links_and_arabic():
    out = html_to_text(HTML_DOC)
    nonempty = [ln for ln in out.split("\n") if ln]
    assert nonempty == [
        "مرحباً بالفريق،",
        "البند القيمة",
        "Total 500 & up",
        "See report (https://example.com/report) first.",
        "Track: this link",
        "example.com",
    ]
    # <style>/<head> content and the long tracking href are dropped.
    assert "color" not in out
    assert "token=" not in out


def test_html_to_text_br_and_entities():
    assert html_to_text("<div>One<br>Two &amp; Three</div>") == "One\nTwo & Three"


def test_html_to_text_link_label_equals_href():
    html = '<p>Visit <a href="https://example.com/">https://example.com/</a> today</p>'
    assert html_to_text(html) == "Visit https://example.com/ today"


def test_html_to_text_empty_inputs():
    assert html_to_text("") == ""
    assert html_to_text(None) == ""


# ---------------------------------------------------------------------------
# Linear HTML -> text equivalence, hostile-input cost, and linear growth
# ---------------------------------------------------------------------------

import ast  # noqa: E402
import gc  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from conftest import TIME_MARGIN  # noqa: E402

from ewsmcp import bodyclean as _bodyclean_mod  # noqa: E402
from ewsmcp import extract as _extract_mod  # noqa: E402
from ewsmcp.bodyclean import html_to_text_bounded  # noqa: E402

_REFERENCE_VECTORS_PATH = (
    Path(__file__).resolve().parent / "data" / "bodyclean_reference_vectors.json"
)


def test_bodyclean_does_not_import_html_parser():
    for path in (Path(_bodyclean_mod.__file__), Path(__file__)):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "html.parser", f"{path} imports html.parser"
                    assert not alias.name.startswith("html.parser."), path
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod != "html.parser", f"{path} imports from html.parser"
                assert not mod.startswith("html.parser."), path
                if mod == "html":
                    assert all(a.name != "parser" for a in node.names), path
    assert not hasattr(_bodyclean_mod, "_HTMLToText")


def test_frozen_reference_vectors_match():
    payload = json.loads(_REFERENCE_VECTORS_PATH.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert len(cases) >= 900
    for idx, (fragment, expected) in enumerate(cases):
        actual = html_to_text(fragment)
        assert actual == expected, (
            f"mismatch at case {idx}:\nHTML: {fragment!r}\n"
            f"expected: {expected!r}\nactual:   {actual!r}"
        )


def _make_hostile_body(shape: str, size: int) -> str:
    """Build a hostile HTML input of exact length ``size`` with visible text."""
    if shape == "lt_a_space":
        # Table shape: "<a " * N + "x", prefixed with visible text so a browser
        # has text before the unterminated start tag.
        prefix = "x "
        rem = size - len(prefix) - 1
        reps = rem // 3
        pad = " " * (rem - reps * 3)
        body = prefix + ("<a " * reps) + pad + "x"
    elif shape == "lt_a_single_quote":
        # Table shape: "<a b='" * N + "x", prefixed with visible text.
        prefix = "x "
        rem = size - len(prefix) - 1
        reps = rem // 6
        pad = "b" * (rem - reps * 6)
        body = prefix + ("<a b='" * reps) + pad + "x"
    elif shape == "unclosed_comments":
        # Table shape: "x" + "<!--" * N.
        rem = size - 1
        reps = rem // 4
        pad = "-" * (rem - reps * 4)
        body = "x" + ("<!--" * reps) + pad
    elif shape == "ordinary_html":
        unit = '<p>Project update &amp; <a href="https://example.com/r">note</a>.</p>'
        reps = size // len(unit)
        tail_len = size - reps * len(unit)
        body = (unit * reps) + ("x" * tail_len)
    elif shape == "marked_section_open":
        # Shape: "<![" * N with leading text.
        prefix = "x "
        rem = size - len(prefix)
        reps = rem // 3
        pad = "[" * (rem - reps * 3)
        body = prefix + ("<![" * reps) + pad
    elif shape == "ampersands":
        body = "&" * size
    elif shape == "less_thans":
        body = "<" * size
    elif shape == "unclosed_end_tags":
        # Shape: "</a " * N with leading text.
        prefix = "x "
        rem = size - len(prefix)
        reps = rem // 4
        pad = " " * (rem - reps * 4)
        body = prefix + ("</a " * reps) + pad
    elif shape == "unclosed_href_eq":
        # Shape: "<a href=" * N with leading text.
        prefix = "x "
        rem = size - len(prefix)
        reps = rem // 8
        pad = "a" * (rem - reps * 8)
        body = prefix + ("<a href=" * reps) + pad
    elif shape == "head_meta_flood":
        # Shape: "<head>" + "<meta>" * N with leading text.
        prefix = "x <head>"
        rem = size - len(prefix)
        reps = rem // 6
        pad = " " * (rem - reps * 6)
        body = prefix + ("<meta>" * reps) + pad
    elif shape == "head_unclosed_title":
        # Shape: "<head><title>" + "x" * N with leading text.
        prefix = "x <head><title>"
        body = prefix + ("x" * (size - len(prefix)))
    elif shape == "head_open_flood":
        # Shape: "<head>" * N with leading text.
        prefix = "x "
        rem = size - len(prefix)
        reps = rem // 6
        pad = " " * (rem - reps * 6)
        body = prefix + ("<head>" * reps) + pad
    elif shape == "title_open_flood":
        # Shape: "<title>" * N with leading text.
        prefix = "x "
        rem = size - len(prefix)
        reps = rem // 7
        pad = " " * (rem - reps * 7)
        body = prefix + ("<title>" * reps) + pad
    elif shape == "x_title_unclosed":
        # Shape: "x<title>" + "y" * N.
        prefix = "x<title>"
        body = prefix + ("y" * (size - len(prefix)))
    elif shape == "cdata_open_flood":
        # Shape: "<![CDATA[" * N with leading text.
        prefix = "x "
        rem = size - len(prefix)
        reps = rem // 9
        pad = "[" * (rem - reps * 9)
        body = prefix + ("<![CDATA[" * reps) + pad
    elif shape == "svg_cdata_open_flood":
        # Shape: "<svg>" + "<![CDATA[" * N.
        prefix = "<svg>"
        rem = size - len(prefix)
        reps = rem // 9
        pad = "[" * (rem - reps * 9)
        body = prefix + ("<![CDATA[" * reps) + pad
    elif shape == "svg_cdata_unclosed_text":
        # Shape: "<svg><![CDATA[" + "x" * N.
        prefix = "<svg><![CDATA["
        body = prefix + ("x" * (size - len(prefix)))
    elif shape == "cdata_gt_flood":
        # Shape: "<![CDATA[>" * N with leading text.
        prefix = "x "
        rem = size - len(prefix)
        reps = rem // 10
        pad = " " * (rem - reps * 10)
        body = prefix + ("<![CDATA[>" * reps) + pad
    elif shape == "a_href_in_attr_spaces":
        # Shape: '<a x="href"' + ' ' * N + '>t</a>'.
        head = '<a x="href"'
        tail = ">t</a>"
        body = head + (" " * (size - len(head) - len(tail))) + tail
    elif shape == "a_href_in_attr_slashes":
        # Shape: '<a title="see href"' + '/' * N + '>t</a>'.
        head = '<a title="see href"'
        tail = ">t</a>"
        body = head + ("/" * (size - len(head) - len(tail))) + tail
    elif shape == "script_double_escape_open_flood":
        # Shape: "<script><!--<script>" * N with leading text.
        prefix = "x "
        rem = size - len(prefix)
        reps = rem // 20
        pad = " " * (rem - reps * 20)
        body = prefix + ("<script><!--<script>" * reps) + pad
    elif shape == "script_escaped_pair_flood":
        # Shape: "<script><!--" + "<script></script>" * N with leading text.
        prefix = "x <script><!--"
        rem = size - len(prefix)
        reps = rem // 17
        pad = " " * (rem - reps * 17)
        body = prefix + ("<script></script>" * reps) + pad
    elif shape == "svg_open_math_close_flood":
        # Shape: "<svg>" * N + "</math>" * N with leading text.
        prefix = "x "
        rem = size - len(prefix)
        reps = rem // 12
        pad = " " * (rem - reps * 12)
        body = prefix + ("<svg>" * reps) + ("</math>" * reps) + pad
    elif shape == "svg_open_p_breakout_flood":
        # Shape: "<svg>" * N + "<p>" * N with leading text.
        prefix = "x "
        rem = size - len(prefix)
        reps = rem // 8
        pad = " " * (rem - reps * 8)
        body = prefix + ("<svg>" * reps) + ("<p>" * reps) + pad
    elif shape == "textarea_open_flood":
        # Shape: "<textarea>" * N.
        reps = size // 10
        pad = "x" * (size - reps * 10)
        body = ("<textarea>" * reps) + pad
    elif shape == "xmp_lt_flood":
        # Shape: "<xmp>" + "<" * N.
        prefix = "<xmp>"
        body = prefix + ("<" * (size - len(prefix)))
    elif shape == "iframe_pair_flood":
        # Shape: "<iframe></iframe>" * N with leading text.
        prefix = "x "
        rem = size - len(prefix)
        reps = rem // 17
        pad = " " * (rem - reps * 17)
        body = prefix + ("<iframe></iframe>" * reps) + pad
    elif shape == "nul_lt_flood":
        # Shape: "\x00<" * N.
        reps = size // 2
        pad = "x" * (size - reps * 2)
        body = ("\x00<" * reps) + pad
    else:
        raise ValueError(f"unknown shape {shape!r}")
    assert len(body) == size
    return body


_HOSTILE_SHAPES = (
    "lt_a_space",
    "lt_a_single_quote",
    "unclosed_comments",
    "ordinary_html",
    "marked_section_open",
    "ampersands",
    "less_thans",
    "unclosed_end_tags",
    "unclosed_href_eq",
    "head_meta_flood",
    "head_unclosed_title",
    "head_open_flood",
    "title_open_flood",
    "x_title_unclosed",
    "cdata_open_flood",
    "svg_cdata_open_flood",
    "svg_cdata_unclosed_text",
    "cdata_gt_flood",
    "a_href_in_attr_spaces",
    "a_href_in_attr_slashes",
    "script_double_escape_open_flood",
    "script_escaped_pair_flood",
    "svg_open_math_close_flood",
    "svg_open_p_breakout_flood",
    "textarea_open_flood",
    "xmp_lt_flood",
    "iframe_pair_flood",
    "nul_lt_flood",
)


@pytest.mark.parametrize("shape", _HOSTILE_SHAPES)
def test_hostile_shapes_1m_cost_and_non_empty_text(shape: str):
    payload = _make_hostile_body(shape, 1_000_000)
    t0 = time.perf_counter()
    out = html_to_text(payload)
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0 * TIME_MARGIN, (
        f"{shape} at 1,000,000 chars took {elapsed:.3f}s "
        f"(limit {1.0 * TIME_MARGIN:.3f}s)"
    )
    assert out != ""

    # Also verify exact bare unterminated expressions (no leading text) finish
    # within the same bound and follow browser eof-in-tag / head rules.
    bare_builders = {
        "lt_a_space": ("<a " * (999_999 // 3)) + "x",
        "lt_a_single_quote": ("<a b='" * (999_996 // 6)) + "x" * 4,
        "marked_section_open": "<![" * (999_999 // 3) + "x",
        "unclosed_end_tags": "</a " * (1_000_000 // 4),
        "unclosed_href_eq": "<a href=" * (1_000_000 // 8),
        "head_meta_flood": "<head>" + ("<meta>" * 166_665) + (" " * 4),
        "head_unclosed_title": "<head><title>" + ("x" * (1_000_000 - 13)),
        "head_open_flood": ("<head>" * 166_666) + (" " * 4),
        "title_open_flood": ("<title>" * (1_000_000 // 7)) + (" " * (1_000_000 % 7)),
        "cdata_open_flood": ("<![CDATA[" * (1_000_000 // 9)) + ("[" * (1_000_000 % 9)),
        "cdata_gt_flood": "<![CDATA[>" * (1_000_000 // 10),
        "script_double_escape_open_flood": "<script><!--<script>" * (1_000_000 // 20),
        "script_escaped_pair_flood": (
            "<script><!--"
            + ("<script></script>" * ((1_000_000 - 12) // 17))
            + (" " * ((1_000_000 - 12) % 17))
        ),
        "svg_open_math_close_flood": (
            ("<svg>" * (1_000_000 // 12))
            + ("</math>" * (1_000_000 // 12))
            + (" " * (1_000_000 % 12))
        ),
        "svg_open_p_breakout_flood": ("<svg>" * 125_000) + ("<p>" * 125_000),
        "iframe_pair_flood": (
            ("<iframe></iframe>" * (1_000_000 // 17))
            + (" " * (1_000_000 % 17))
        ),
    }
    if shape in bare_builders:
        bare = bare_builders[shape]
        assert len(bare) == 1_000_000
        tb = time.perf_counter()
        bare_out = html_to_text(bare)
        bare_elapsed = time.perf_counter() - tb
        assert bare_elapsed < 1.0 * TIME_MARGIN
        assert bare_out == ""


def _measure_batch(payload: str, reps: int) -> float:
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        t0 = time.perf_counter()
        for _ in range(reps):
            html_to_text(payload)
        return time.perf_counter() - t0
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("shape", _HOSTILE_SHAPES)
def test_hostile_shapes_linear_growth_400k_vs_100k(shape: str):
    s_100k = _make_hostile_body(shape, 100_000)
    s_400k = _make_hostile_body(shape, 400_000)

    # Warm regex/codec paths and pick enough repetitions so 100k is >= ~15 ms,
    # avoiding sub-millisecond OS scheduling noise.
    t_warm = _measure_batch(s_100k, 1)
    _measure_batch(s_400k, 1)
    reps = max(1, min(500, int(0.015 / max(t_warm, 1e-6)) + 1))

    samples_100k: list[float] = []
    samples_400k: list[float] = []
    for _ in range(5):
        samples_100k.append(_measure_batch(s_100k, reps))
        samples_400k.append(_measure_batch(s_400k, reps))

    best_100k = min(samples_100k)
    best_400k = min(samples_400k)
    ratio = best_400k / max(best_100k, 1e-9)
    assert best_400k < 6.0 * best_100k, (
        f"{shape}: 400k took {best_400k:.4f}s vs 100k {best_100k:.4f}s "
        f"(ratio {ratio:.2f}x >= 6x)"
    )


def test_malformed_inputs_follow_browser_and_shared_extractor_core():
    # Browser keeps bare '<' that do not open tags, and drops unclosed tags /
    # comments / declarations / processing instructions at EOF.
    assert html_to_text("2 < 3 and x <3 y and <> and trailing <") == (
        "2 < 3 and x <3 y and <> and trailing <"
    )
    assert html_to_text("trailing end open </") == "trailing end open </"
    assert html_to_text("Shown <!-- unclosed comment") == "Shown"
    assert html_to_text("Shown <![unclosed section") == "Shown"
    assert html_to_text("Shown <?unclosed pi") == "Shown"
    assert html_to_text("Shown <a href='https://example.com") == "Shown"
    assert html_to_text("Shown </div unclosed") == "Shown"

    # Abruptly closed HTML5 comments and quotes inside non-value positions.
    assert html_to_text("<!-->First<!---><p>Second</p>") == "First\nSecond"
    assert html_to_text('<p data=foo\'bar>Kept</p><p>Next</p >') == "Kept\n\nNext"
    assert html_to_text('<p>One</p "><p>Two</p>') == "One\n\nTwo"

    # Non-void <script/> and unclosed <head> closed by <body>; stray </style>
    # inside <head> does not leak <title>.
    assert html_to_text("<script/>alert(1)</script><p>Safe</p>") == "Safe"
    assert (
        html_to_text("<html><head><title>Hidden</title><body><p>Body</p></body></html>")
        == "Body"
    )
    assert (
        html_to_text("<head></style><title>Hidden</title></head><p>Body</p>")
        == "Body"
    )

    # Overlong numeric character references never raise ValueError.
    assert html_to_text("&#" + "0" * 6_000 + "65;") == "A"
    assert html_to_text("&#" + "9" * 6_000 + ";") == "\ufffd"

    # Shared core with extract.py: bounded truncation + presentation forms in
    # extract_html, while bodyclean.html_to_text leaves Arabic untouched.
    pres_html = "<p>\ufe97\ufed8\ufeae\ufef3\ufead</p>"
    assert html_to_text(pres_html) == "\ufe97\ufed8\ufeae\ufef3\ufead"
    assert _extract_mod.extract_html(pres_html.encode("utf-8")).text == "تقرير"

    long_html = "<p>" + ("word " * 500) + "</p>"
    bounded_text, was_trunc = html_to_text_bounded(long_html, max_chars=40)
    assert was_trunc is True
    assert len(bounded_text) == 40

    # End-to-end HTML -> html_to_text -> clean_body / strip_quoted_history.
    mail_html = (
        "<html><body>"
        "<p>Approved — please proceed with phase two.</p>"
        "<p>Loop in operations on the schedule.</p>"
        "<p>Best regards,<br>Alice</p>"
        "<p>From: Bob &lt;bob@example.com&gt;<br>"
        "Sent: Monday, June 1, 2026 8:02 AM<br>"
        "Subject: Phase two</p>"
        "<p>Earlier message.</p>"
        "</body></html>"
    )
    plain = html_to_text(mail_html)
    stripped, count = strip_quoted_history(plain)
    assert count == 1
    assert "Approved — please proceed with phase two." in stripped
    assert "Earlier message." not in stripped
    cleaned = clean_body(plain)
    assert cleaned["text"] == (
        "Approved — please proceed with phase two.\n\n"
        "Loop in operations on the schedule."
    )
    assert cleaned["quoted_blocks_stripped"] == 1


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ("<head>text", "text"),
        ("<head><p>Body</p>", "Body"),
        (
            '<head><meta charset="utf-8"><title>T</title><style>p{}</style><div>Body</div>',
            "Body",
        ),
        ("<head><title>T</title></head><p>Body</p>", "Body"),
        ("<head><head><title>T</title></head>Body", "Body"),
        ('<head>  \n <link rel="x" href="y">\n</head>Body', "Body"),
        ("<p>One</p><head><title>T</title></head><p>Two</p>", "One\n\nTwo"),
        ("<head><title>T", ""),
        ("<head>&nbsp;Body", "Body"),
        ("<head></p>Body", "Body"),
        ("<head><svg><title>x</title></svg>Body", "xBody"),
        ("<title>Outside</title>", ""),
        ("<head></body><title>After</title>", ""),
        ("<head></html><title>After</title>", ""),
    ],
)
def test_head_mode_html5_reduced_rule(fragment: str, expected: str):
    assert html_to_text(fragment) == expected


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ("<html><title>T</title><body>Body</body></html>", "Body"),
        ("<title>T</title>Body", "Body"),
        ("<p>One</p><title>T</title><p>Two</p>", "One\n\nTwo"),
        ("<TITLE>T</TITLE >Body", "Body"),
        ("<title>T", ""),
        ("Shown<title>T", "Shown"),
        ("<title>a<p>b</p>c</title>Body", "Body"),
        ('<head><noscript><img src="x"></noscript><title>T</title></head>Body', "Body"),
        ("<svg><title>x</title></svg>Body", "xBody"),
    ],
)
def test_title_content_never_shown_outside_foreign_content(fragment: str, expected: str):
    assert html_to_text(fragment) == expected


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ("<![CDATA[>Body]]>", "Body]]>"),
        ("<p>a</p><![CDATA[x>Body]]>", "a\nBody]]>"),
        ("<p>a<![CDATA[ <b>bold</b> ]]>c</p>", "abold ]]>c"),
        ("<![cdata[>Body]]>", "Body]]>"),
        ("<![CDATA[x]]>Body", "Body"),
        ("<![CDATA[>Body", "Body"),
        ("<![CDATA[Body", ""),
        ("<svg><![CDATA[Body]]></svg>", "Body"),
        ("<math><![CDATA[a &amp; <b>]]></math>", "a &amp; <b>"),
        ("<svg><![CDATA[Body", "Body"),
        ("<svg><![cdata[x>Body]]></svg>", "Body]]>"),
        ("<head><![CDATA[x><title>T</title></head>Body", "Body"),
    ],
)
def test_cdata_html_bogus_comment_and_foreign_section(fragment: str, expected: str):
    assert html_to_text(fragment) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<p>Lunch</p><svg><style><p>MARK</p></style></svg><p>TAIL</p>", "LunchMARKTAIL"),
        ("alpha<svg><style></svg><p>MARK</p>", "alphaMARK"),
        ("alpha<svg><script></svg>MARK<i></script></i>TAIL", "alphaMARKTAIL"),
        ("alpha<math><style><b>MARK</b></style></math>TAIL", "alphaMARKTAIL"),
        ("alpha<style></ſtyle><!--</style>MARK<!-- x -->TAIL", "alphaMARKTAIL"),
        ("alpha<script></SCRİPT><style></script>MARK<i></style></i>TAIL", "alphaMARKTAIL"),
        ("<head><title></tıtle><!--</title></head>MARK<!-- x -->TAIL", "MARKTAIL"),
        ("alpha<script><!--<script></script><!--</script>MARK<!-- x -->TAIL", "alphaMARKTAIL"),
        ("alpha<script><!--<script></script><!--</script>MARK", "alphaMARK"),
        ("alpha<script><!-- x --></script>MARK", "alphaMARK"),
        ("alpha<script><!--<script>x</script>y-->z</script>MARK", "alphaMARK"),
        ("alpha<script>a</script>MARK<script>b", "alphaMARK"),
        ("alpha<svg></math><title></svg>MARK<i></title></i>TAIL", "alphaMARKTAIL"),
        ("alpha<math></svg><title></math>MARK<i></title></i>TAIL", "alphaMARKTAIL"),
        ("alpha<svg><p><title><!--</title>MARK<!-- x -->TAIL", "alphaMARKTAIL"),
        ('alpha<svg><div><style/><a title="</style>MARK<i title="">TAIL', "alphaMARKTAIL"),
        ("alpha<textarea><MARK beta gamma></textarea>TAIL", "alpha<MARKbetagamma>TAIL"),
        ("alpha<textarea>a &amp; b</textarea>TAIL", "alphaa&bTAIL"),
        ("alpha<xmp><!-- MARK --></xmp>TAIL", "alpha<!--MARK-->TAIL"),
        ("alpha<xmp>a &amp; b</xmp>TAIL", "alphaa&amp;bTAIL"),
        (
            "alpha<plaintext><MARK beta gamma></plaintext>TAIL",
            "alpha<MARKbetagamma></plaintext>TAIL",
        ),
        ("alpha<iframe><!--</iframe>MARK<!-- x -->TAIL", "alpha<!--MARKTAIL"),
        ("alpha<noframes><!--</noframes>MARK<!-- x -->TAIL", "alpha<!--MARKTAIL"),
        ('alpha<noembed><a title="</noembed>MARK<i title="">TAIL', 'alpha<atitle="MARKTAIL'),
        ("alpha<textarea><!--</textarea>MARK", "alpha<!--MARK"),
        ("alpha<textarea><title></textarea>MARK", "alpha<title>MARK"),
        ("alpha<TEXTAREA>x</TextArea >TAIL", "alphaxTAIL"),
        ("alpha<scr\x00ipt>MARK</scr\x00ipt>TAIL", "alphaMARKTAIL"),
        ("alpha<\x00!-- MARK -->TAIL", "alpha<!--MARK-->TAIL"),
        ('alpha<p title=\x00"x>MARK">TAIL', 'alphaMARK">TAIL'),
        ("a\x00b", "ab"),
        ("<svg><title>x</title></svg>Body", "xBody"),
        ("<svg><![CDATA[Body]]></svg>", "Body"),
        ("<head><noframes>x</noframes><title>T</title></head>Body", "Body"),
    ],
)
def test_round5_html5_tokenizer_rules(html: str, expected: str):
    assert "".join(html_to_text(html).split()) == expected


def test_foreign_breakout_font_attrs_and_p_br_end_tags():
    assert html_to_text('<svg><font color="red"><title>H</title>Shown') == "Shown"
    assert html_to_text('<svg><font FACE="serif"><title>H</title>Shown') == "Shown"
    assert html_to_text('<svg><font size="3"><title>H</title>Shown') == "Shown"
    assert html_to_text('<svg><font class="x"><title>InSvg</title></svg>') == "InSvg"
    assert html_to_text('<svg><font ſize="3"><title>InSvg</title></svg>') == "InSvg"
    assert html_to_text("<svg></p><title>H</title>Shown") == "Shown"
    assert html_to_text("<svg></br><title>H</title>Shown") == "Shown"


def test_outlook_written_head_prints_no_stray_markup():
    """Exchange returns Outlook-written mail with a style inside a conditional comment whose
    content is itself wrapped in <!-- -->. The comment therefore ends inside the style, and
    '</style><![endif]-->' follows as stray markup in the head. The converter built on
    html.parser lost count of the head there: depending on the interpreter build it printed
    '<![endif]-->' and the title at the top of the text."""
    head = (
        '<head>\n<meta http-equiv="Content-Type" content="text/html; charset=utf-8">\n'
        '<meta name="Generator" content="Microsoft Word 15 (filtered medium)">\n'
        "<!--[if !mso]><style>\n<!--\nv\\:* {behavior:url(#default#VML);}\n-->\n</style><![endif]-->"
        "<style>\n<!--\np.MsoNormal {margin:0cm;}\n-->\n</style>"
        '<!--[if gte mso 9]><xml>\n<o:shapedefaults v:ext="edit" spidmax="1026" />\n</xml><![endif]-->'
        "<title>Hidden title</title>\n</head>"
    )
    body = (
        '<body lang="EN-US"><div class="WordSection1">'
        '<p class="MsoNormal">Body text<o:p></o:p></p>'
        '<p class="MsoNormal">Second line<o:p></o:p></p></div></body>'
    )
    assert html_to_text(f"<html>{head}\n{body}</html>") == "Body text\n\nSecond line"
    assert html_to_text("<head><!--[if !mso]><style><!-- x{} --></style><![endif]--></head>Body text") == "Body text"

