"""Pin ``utils.format_body_for_html`` against the cascading-escape and
plain-text-with-angle-brackets classes of bug.

History:
* The S4 hardening introduced ``format_body_for_html`` to route
  user-supplied bodies through either ``sanitize_html`` (when HTML) or
  ``escape_html + <br/>`` (when plain text).
* The original heuristic was ``<[^>]+>``, which matches plain text like
  ``"x < y < z"`` and silently bypassed escaping — the ``<`` ended up in
  the rendered HTML as raw markup.
* Reply/forward thread headers were independently double-escaping; pinned
  separately in ``test_reply_forward_escape.py``.

This file pins both the *heuristic* and the *escape contract* so the next
refactor of either path fails here.
"""
from __future__ import annotations

import pytest

from src.utils import format_body_for_html, escape_html, sanitize_html


@pytest.mark.parametrize("plain_text", [
    "x < y",
    "x < y < z",
    "if a < b then c",
    "5 < 10 and 10 > 5",
])
def test_plain_text_with_angle_brackets_is_escaped_not_sanitised(plain_text):
    """The misclassification bug. ``"x < y"`` has ``<`` but is not HTML.
    Old heuristic: ``<[^>]+>`` matched ``< y`` and routed through
    ``sanitize_html`` which does not escape stray ``<``. Result: raw
    markup in the rendered email.

    Note: a string like ``"use <pre> for monospace"`` IS still classified
    as HTML by the heuristic — this is unavoidable without a caller-supplied
    ``is_html`` flag. Tracked as a round-2 follow-up."""
    out = format_body_for_html(plain_text)
    # The raw `<` must not appear; it must be `&lt;`.
    assert "&lt;" in out
    assert "<" not in out.replace("<br/>", "")


def test_real_html_is_sanitised_not_double_escaped():
    """Conversely, real HTML must NOT be HTML-escaped — that would render
    visible ``&lt;p&gt;`` text in the user's email."""
    body = "<p>Hello <b>world</b></p>"
    out = format_body_for_html(body)
    # `<p>` survives intact because it's real HTML.
    assert "<p>" in out
    assert "</p>" in out
    # No double-escape into &lt;p&gt;.
    assert "&lt;p&gt;" not in out


def test_html_with_script_is_neutralised():
    """Pin the ``sanitize_html`` contract on the most common XSS shape."""
    body = "<p>hi</p><script>alert(1)</script>"
    out = format_body_for_html(body)
    assert "<script>" not in out.lower()
    assert "alert(1)" not in out


def test_html_with_event_handler_is_neutralised():
    body = '<a href="x" onclick="bad()">click</a>'
    out = format_body_for_html(body)
    assert "onclick" not in out.lower()


def test_html_with_javascript_uri_is_neutralised():
    body = '<a href="javascript:bad()">x</a>'
    out = format_body_for_html(body)
    assert "javascript:" not in out.lower()


def test_empty_body_returns_empty_string():
    assert format_body_for_html("") == ""
    assert format_body_for_html(None) == ""
    assert format_body_for_html("   ") == ""


def test_plain_newlines_become_br_tags():
    out = format_body_for_html("line one\nline two")
    assert "line one" in out
    assert "<br/>" in out
    assert "line two" in out
    # Raw newline must not survive — the renderer wouldn't keep the break.
    assert "\n" not in out


def test_format_body_for_html_escapes_ampersand_once():
    """Single-pass plain text with `&` becomes `&amp;` exactly once.
    The reply/forward double-escape outage was a *caller* applying the
    transform twice; the function itself escapes once correctly."""
    out = format_body_for_html("Hello & welcome")
    assert out.count("&amp;") == 1
    assert "&amp;amp;" not in out


def test_format_body_for_html_is_a_one_shot_transform():
    """Document the known non-idempotence so callers don't accumulate
    entities by running the function twice. Each caller must call
    `format_body_for_html` at most once per body."""
    body = "Hello & welcome"
    once = format_body_for_html(body)
    twice = format_body_for_html(once)
    # If a caller re-runs the function, entities compound. This is by
    # design — `&amp;` parses as plain text on the second pass and is
    # re-escaped. Pin the property so a future "make it idempotent"
    # refactor must update the callers too.
    assert twice == "Hello &amp;amp; welcome"


def test_escape_html_round_trip_does_not_compound():
    """Direct contract test on the underlying escape — the property the
    reply/forward double-escape bug violated. ``escape(escape(x))`` of an
    already-escaped string must keep ``&amp;`` to one level when the
    caller knows the input is already escaped (i.e. don't escape twice)."""
    raw = "Smith <john@x.com>"
    once = escape_html(raw)
    # `escape_html` itself is non-idempotent (it's a low-level primitive),
    # but the caller in format_forward_header / reply paths must call it
    # at most once. Pin the primitive so we know the building block.
    assert once == "Smith &lt;john@x.com&gt;"
    twice = escape_html(once)
    # If a caller ever does escape twice, the bug looks like this:
    assert twice == "Smith &amp;lt;john@x.com&amp;gt;"


def test_sanitize_html_preserves_safe_text():
    """Property: ``sanitize_html`` of a string with NO tags should be
    identical (we only strip script/style/handlers, not text)."""
    assert sanitize_html("just plain text") == "just plain text"
