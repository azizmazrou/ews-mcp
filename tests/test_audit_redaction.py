"""Pin ``middleware.logging.redact_sensitive`` against the field-name list.

History (S5 in CHANGELOG): the audit log used to write request payloads
verbatim, so passwords, tokens, message bodies, and base64 attachment
blobs ended up on disk under ``logs/audit.log``. The fix introduced
``redact_sensitive()`` which replaces matching keys with ``[redacted]``.

Risk: the field list lives in ``_SENSITIVE_KEY_PATTERNS`` and is
substring-matched. Adding a new request param like ``mfa_secret`` or
``oauth_token`` without re-checking this list silently leaks. These
parametrized tests pin every pattern so a subtraction (or a typo) fails
loudly.
"""
from __future__ import annotations

import pytest

from src.middleware.logging import redact_sensitive, _SENSITIVE_KEY_PATTERNS


# Each row: (key_in_payload, value, must_be_redacted)
_FIELD_CASES = [
    # Direct matches.
    ("password", "hunter2", True),
    ("token", "abc.def.ghi", True),
    ("secret", "shh", True),
    ("api_key", "sk-test", True),
    ("apikey", "sk-test", True),
    ("authorization", "Bearer xxx", True),
    ("body", "Hello world", True),
    ("html_body", "<p>x</p>", True),
    ("text_body", "x", True),
    ("file_content", "BASE64DATA", True),
    ("content_base64", "BASE64DATA", True),
    ("mime_content", "...", True),
    ("mime_content_base64", "...", True),
    ("inline_attachments", [{"file_content": "x"}], True),
    # Substring matches — these are the silent-leak hazards. Pin them.
    ("client_secret", "shh", True),
    ("access_token", "tok", True),
    ("auth_token", "tok", True),
    ("user_password", "hunter2", True),
    ("X-API-Key", "sk", True),
    # Negative: NOT redacted.
    ("subject", "Hello", False),
    ("from_address", "x@y.com", False),
    ("message_id", "AAMk-xxx", False),
    ("count", 42, False),
    ("recipient", "y@z.com", False),
    ("folder", "Inbox", False),
]


@pytest.mark.parametrize("key,value,redact", _FIELD_CASES)
def test_redact_sensitive_field_contract(key, value, redact):
    out = redact_sensitive({key: value})
    assert key in out
    if redact:
        assert out[key] != value, (
            f"Expected {key!r} to be redacted but it survived as {out[key]!r}"
        )
        # The replacement is a string ("[redacted...]") regardless of input type.
        assert isinstance(out[key], str)
        assert "[redacted" in out[key]
    else:
        assert out[key] == value, (
            f"Expected {key!r} to pass through unchanged but it was redacted"
        )


def test_sensitive_key_patterns_inventory_is_minimal_complete():
    """Document and pin the exact list of patterns. If a future PR adds
    or removes a pattern, this test fails — forcing an intentional
    review of what's in scope."""
    expected = (
        "password", "token", "secret", "api_key", "apikey", "authorization",
        "body", "html_body", "text_body",
        "file_content", "content_base64", "mime_content", "mime_content_base64",
        "inline_attachments",
    )
    assert tuple(_SENSITIVE_KEY_PATTERNS) == expected, (
        "_SENSITIVE_KEY_PATTERNS changed. Audit-log redaction scope was "
        "modified — make sure both this test and operations are aligned."
    )


def test_redact_sensitive_walks_nested_dict():
    payload = {
        "subject": "OK",
        "creds": {"password": "hunter2", "username": "u"},
    }
    out = redact_sensitive(payload)
    assert out["subject"] == "OK"
    assert "[redacted" in out["creds"]["password"]
    assert out["creds"]["username"] == "u"


def test_redact_sensitive_walks_list_of_dicts():
    payload = {
        "attachments": [
            {"name": "a.pdf", "file_content": "BASE64A"},
            {"name": "b.pdf", "file_content": "BASE64B"},
        ],
    }
    out = redact_sensitive(payload)
    assert out["attachments"][0]["name"] == "a.pdf"
    assert "[redacted" in out["attachments"][0]["file_content"]
    assert out["attachments"][1]["name"] == "b.pdf"
    assert "[redacted" in out["attachments"][1]["file_content"]


def test_redact_sensitive_truncates_long_strings_outside_sensitive_keys():
    long = "x" * 1000
    out = redact_sensitive({"subject": long}, max_str=100)
    # Truncated to max_str.
    assert len(out["subject"]) == 100
    assert out["subject"].endswith("...")


def test_redact_sensitive_handles_none_and_primitives():
    assert redact_sensitive(None) is None
    assert redact_sensitive(42) == 42
    assert redact_sensitive(True) is True
    assert redact_sensitive("plain") == "plain"


def test_redact_sensitive_inline_attachments_list_announces_count():
    """Don't silently swallow — the redacted form should hint at how
    many items were stripped, so audit reviewers can spot anomalies."""
    out = redact_sensitive({"inline_attachments": [1, 2, 3]})
    assert "3" in out["inline_attachments"]
    assert "redacted" in out["inline_attachments"]
