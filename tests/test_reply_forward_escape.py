"""Regression tests for the reply/forward HTML double-escape bug.

The original bug: ``format_forward_header`` returned recipient strings with
``&lt;`` / ``&gt;`` already in place. The reply / forward callers then ran the
result through ``escape_html`` again, producing ``&amp;lt;`` / ``&amp;gt;``.
Because each subsequent reply quotes the previous body, the entities
compound on every cycle: ``&amp;amp;lt;``, ``&amp;amp;amp;lt;``, etc. — the
"multiple `&` between the contact name in the thread" symptom users see.

Fix: ``format_forward_header`` now returns plain text with literal ``<`` and
``>``. Callers continue to ``escape_html`` once, exactly once.
"""
from __future__ import annotations

from html import escape
from unittest.mock import MagicMock

from src.tools.email_tools import format_forward_header


def _mailbox(name: str | None, email: str | None) -> MagicMock:
    m = MagicMock()
    m.name = name
    m.email_address = email
    return m


def _message(*, sender_name=None, sender_email=None,
             to=None, cc=None, subject="", sent=None):
    msg = MagicMock()
    if sender_name is not None or sender_email is not None:
        msg.sender = _mailbox(sender_name, sender_email)
    else:
        msg.sender = None
    msg.author = None
    msg.from_ = None
    msg.headers = None
    msg.internet_message_headers = None
    msg.to_recipients = list(to or [])
    msg.cc_recipients = list(cc or [])
    msg.subject = subject
    msg.datetime_sent = sent
    return msg


def test_format_forward_header_returns_plain_text_no_html_entities():
    """The helper must return raw 'Name <email>' so the single escape_html()
    in the caller produces correct &lt;/&gt; (one level, not two)."""
    msg = _message(
        sender_name="John Smith", sender_email="john@example.com",
        to=[_mailbox("Alice", "alice@x.com"), _mailbox("Bob", "bob@x.com")],
        cc=[_mailbox("Carol", "carol@x.com")],
        subject="Q4 plan",
    )
    h = format_forward_header(msg)

    # No HTML entities should be in the helper's output. They're applied
    # exactly once by the caller (via escape_html) right before HTML
    # interpolation.
    for field in ("from", "to", "cc"):
        value = h[field]
        assert "&lt;" not in value, f"{field!r} contains &lt; — should be raw '<'"
        assert "&gt;" not in value, f"{field!r} contains &gt; — should be raw '>'"
        assert "&amp;" not in value, f"{field!r} contains &amp; — double-escape leaked in"

    assert h["from"] == "John Smith <john@example.com>"
    assert h["to"]   == "Alice <alice@x.com>; Bob <bob@x.com>"
    assert h["cc"]   == "Carol <carol@x.com>"


def test_caller_single_escape_yields_one_level_of_entities():
    """Simulate exactly what ReplyEmailTool / ForwardEmailTool do — one
    escape_html() pass on the helper's output. Result should have exactly
    one level of &lt;/&gt; (not &amp;lt; or &amp;amp;lt;)."""
    def caller_escape(s):
        # Same as escape_html(s) used in the source (html.escape with quote=False)
        return escape(s, quote=False) if s else ""

    msg = _message(
        sender_name="John Smith", sender_email="john@example.com",
        to=[_mailbox("Alice", "alice@x.com")],
    )
    h = format_forward_header(msg)
    safe_to = caller_escape(h["to"])

    assert "&lt;" in safe_to, "single escape pass should produce &lt;"
    assert "&gt;" in safe_to, "single escape pass should produce &gt;"
    assert "&amp;" not in safe_to, (
        "&amp; would mean we double-escaped — the visible thread bug"
    )
    # And after a hypothetical reply-to-the-reply, the body would be re-rendered
    # but the recipient line itself isn't run through escape_html again — it's
    # pulled fresh from the new message's recipients via format_forward_header.
    # So the second cycle's safe_to is still single-level:
    safe_to_2nd_cycle = caller_escape(format_forward_header(msg)["to"])
    assert safe_to_2nd_cycle == safe_to


def test_format_forward_header_handles_email_only_recipients():
    """Recipients without a display name should fall back to bare email."""
    msg = _message(
        sender_email="anon@example.com",
        to=[_mailbox(None, "noname@example.com"),
            _mailbox("Pat", "pat@example.com")],
    )
    h = format_forward_header(msg)
    assert h["to"] == "noname@example.com; Pat <pat@example.com>"
    assert "&lt;" not in h["to"]


def test_format_forward_header_with_ampersand_in_name_does_not_compound():
    """A recipient name containing '&' must not get pre-escaped here either —
    the caller's escape_html will turn it into '&amp;' once. If we escape it
    here too we'd get '&amp;amp;'."""
    msg = _message(
        sender_email="x@example.com",
        to=[_mailbox("Smith & Sons Ltd", "smith@example.com")],
    )
    h = format_forward_header(msg)
    assert h["to"] == "Smith & Sons Ltd <smith@example.com>"
    assert "&amp;" not in h["to"]


# ----------------------------------------------------------------------
# Integration tests — drive the full draft creation flow end-to-end and
# inspect the HTML body that would be saved to the Drafts folder. These
# catch the regression at the layer the user actually sees.
# ----------------------------------------------------------------------

from datetime import datetime
from unittest.mock import patch

import pytest

from src.tools.email_tools_draft import CreateReplyDraftTool, CreateForwardDraftTool


def _make_original(*, body_html: str = "<p>Original</p>"):
    """Build a mock 'original message' that the draft tools will quote from."""
    original = MagicMock()
    original.subject = "Q4 plan"
    original.sender.name = "John Smith"
    original.sender.email_address = "john@example.com"
    original.to_recipients = [_mailbox("Alice", "alice@example.com")]
    original.cc_recipients = [_mailbox("Bob", "bob@example.com")]
    original.datetime_sent = datetime(2026, 1, 1, 10, 0, 0)
    original.body = MagicMock()
    original.body.body = body_html
    original.attachments = []
    return original


async def _capture_draft_body(tool, *, original, **execute_kwargs) -> str:
    """Run a draft tool against a mocked original and return the HTML body
    string that the tool tried to save to Drafts."""
    with patch("src.tools.email_tools_draft.find_message_for_account",
               return_value=original):
        with patch("src.tools.email_tools_draft.Message") as mock_message:
            mock_msg = MagicMock()
            mock_msg.id = "draft-id"
            mock_message.return_value = mock_msg

            await tool.execute(**execute_kwargs)

            body_arg = mock_message.call_args.kwargs["body"]
            return str(body_arg)


@pytest.mark.asyncio
async def test_create_reply_draft_body_has_single_escape_only(mock_ews_client):
    """Drive CreateReplyDraftTool end-to-end and verify the saved body has
    exactly one level of HTML entities — &lt; present, &amp;lt; absent."""
    mock_ews_client.account.primary_smtp_address = "me@example.com"
    tool = CreateReplyDraftTool(mock_ews_client)
    original = _make_original()

    body = await _capture_draft_body(
        tool, original=original, message_id="orig-id", body="Reply text"
    )

    # The header line "From: John Smith <john@example.com>" should appear in
    # the draft as "From: John Smith &lt;john@example.com&gt;" — single pass.
    assert "&lt;john@example.com&gt;" in body
    assert "&amp;lt;" not in body, (
        "&amp;lt; means the helper output got escaped twice — "
        "this is the visible thread bug."
    )
    assert "&amp;gt;" not in body
    assert "&amp;amp;" not in body


@pytest.mark.asyncio
async def test_create_forward_draft_body_has_single_escape_only(mock_ews_client):
    """Same single-escape invariant for CreateForwardDraftTool."""
    tool = CreateForwardDraftTool(mock_ews_client)
    original = _make_original()

    body = await _capture_draft_body(
        tool, original=original,
        message_id="orig-id",
        to=["target@example.com"],
        body="FYI",
    )

    assert "&lt;john@example.com&gt;" in body
    assert "&amp;lt;" not in body
    assert "&amp;gt;" not in body
    assert "&amp;amp;" not in body


@pytest.mark.asyncio
async def test_two_reply_cycles_do_not_compound_ampersands(mock_ews_client):
    """The original symptom: each reply quotes the previous body, so any
    double-escape compounds (`&lt;` → `&amp;lt;` → `&amp;amp;lt;`...).
    Simulate a 2-cycle reply chain and verify the entities never compound."""
    mock_ews_client.account.primary_smtp_address = "me@example.com"
    tool = CreateReplyDraftTool(mock_ews_client)

    cycle1_original = _make_original()
    cycle1_body = await _capture_draft_body(
        tool, original=cycle1_original,
        message_id="orig-id", body="First reply",
    )
    assert "&lt;" in cycle1_body
    assert "&amp;lt;" not in cycle1_body

    # Cycle 2: the previous reply's HTML body becomes the new "original" body.
    cycle2_original = _make_original(body_html=cycle1_body)
    cycle2_body = await _capture_draft_body(
        tool, original=cycle2_original,
        message_id="cycle2-id", body="Second reply",
    )

    assert "&lt;" in cycle2_body
    assert "&amp;lt;" not in cycle2_body, (
        "After two reply cycles, &amp;lt; would mean the quoted body got "
        "escaped a second time — the exact compounding the user reported."
    )
    assert "&amp;amp;" not in cycle2_body
    assert cycle2_body.count("&amp;lt;") == 0
    assert cycle2_body.count("&amp;gt;") == 0
