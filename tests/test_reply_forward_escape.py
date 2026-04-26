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
