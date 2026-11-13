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
