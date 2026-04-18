"""Regression tests for Bug 8 — semantic search ranked automated mail highly.

Observed: "Dataset Rejected Status Notification" ranked 0.64 for the
query "data migration". Automated senders (no-reply, notifications@,
mailer-daemon, cancelled-invite subjects) crowded out real
correspondence.

Fix: ``is_automated_sender`` helper in utils + ``exclude_automated``
param on SemanticSearchEmailsTool (default true for semantic search).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.utils import is_automated_sender


# --- Helper -------------------------------------------------------------


@pytest.mark.parametrize(
    "sender,subject",
    [
        ("noreply@service.com", "any"),
        ("no-reply@domain.com", "any"),
        ("notifications@github.com", "any"),
        ("notification@example.com", "any"),
        ("admin@ops.com", "any"),
        ("mailer-daemon@corp.com", "Delivery failure"),
        ("user@notify.something.com", "any"),
        ("bounce@mailgun.net", "any"),
        ("amanda@corp.com", "Accepted: Meeting tomorrow"),
        ("bob@corp.com", "Canceled: Kickoff"),
        ("bob@corp.com", "Cancelled: Kickoff"),
        ("bob@corp.com", "Declined: Review"),
        ("bob@corp.com", "Automatic reply: Out of office"),
        ("bob@corp.com", "Undeliverable: Project plan"),
    ],
)
def test_is_automated_sender_positive(sender, subject):
    assert is_automated_sender(sender, subject) is True


@pytest.mark.parametrize(
    "sender,subject",
    [
        ("alice@example.com", "Project plan review"),
        ("bob@example.com", "Re: budget"),
        ("admin@not-a-match-host.com", "Some business subject"),  # admin@ matches, though
        ("user@example.com", "Accepted lol for kids"),  # no colon after Accepted
    ],
)
def test_is_automated_sender_negative(sender, subject):
    # Some edge cases intentionally tricky — we don't claim perfection,
    # only that the patterns don't produce runaway false positives.
    # Note: admin@ DOES match the allow-list, so we filter it out here.
    if sender.startswith("admin@"):
        assert is_automated_sender(sender, subject) is True
    else:
        assert is_automated_sender(sender, subject) is False


# --- Integration --------------------------------------------------------


@pytest.fixture
def _enable_ai(mock_ews_client):
    mock_ews_client.config.enable_ai = True
    mock_ews_client.config.enable_semantic_search = True
    mock_ews_client.config.ai_provider = "local"
    mock_ews_client.config.ai_api_key = "x"
    mock_ews_client.config.ai_model = "ignored"
    mock_ews_client.config.ai_embedding_model = "text-embedding-3-small"
    mock_ews_client.config.ai_base_url = "http://fake/v1"
    return mock_ews_client


def _fake(id_, sender, subject):
    m = MagicMock()
    m.subject = subject
    m.text_body = "body"
    m.id = id_
    m.sender = MagicMock(email_address=sender)
    m.to_recipients = []
    m.datetime_received = "2026-04-18T10:00:00"
    m.is_read = False
    m.has_attachments = False
    return m


@pytest.mark.asyncio
async def test_semantic_search_excludes_automated_by_default(_enable_ai):
    """Default exclude_automated=true filters out no-reply senders and
    meeting-response subject prefixes before embedding."""
    from src.tools.ai_tools import SemanticSearchEmailsTool

    fakes = [
        _fake("A", "alice@corp.com", "Data migration plan"),
        _fake("B", "no-reply@service.com", "Dataset Rejected"),
        _fake("C", "notifications@github.com", "Push event"),
        _fake("D", "bob@corp.com", "Accepted: Monday kick-off"),
        _fake("E", "carol@corp.com", "Re: migration status"),
    ]
    ordered = MagicMock()
    ordered.__getitem__ = lambda _self, _slc: fakes
    _enable_ai.account.inbox.all.return_value.order_by.return_value = ordered

    passed_documents: list = []

    class _Service:
        def __init__(self, *_a, **_kw):
            pass

        async def search_similar(self, *, query, documents, text_key, top_k, threshold):
            passed_documents.extend(documents)
            return [(d, 0.9) for d in documents]

    tool = SemanticSearchEmailsTool(_enable_ai)
    with patch("src.tools.ai_tools.EmbeddingService", _Service), \
         patch("src.tools.ai_tools.get_embedding_provider", return_value=object()):
        result = await tool.execute(query="migration")

    # Only A and E should have survived the exclude_automated filter.
    ids_in = sorted(d["id"] for d in passed_documents)
    assert ids_in == ["A", "E"]
    # Response should surface how many were filtered.
    meta = result.get("meta") or {}
    assert meta.get("filtered_automated") == 3


@pytest.mark.asyncio
async def test_semantic_search_keeps_everything_when_disabled(_enable_ai):
    """exclude_automated=false -> every candidate reaches the embedder."""
    from src.tools.ai_tools import SemanticSearchEmailsTool

    fakes = [
        _fake("A", "alice@corp.com", "Data migration"),
        _fake("B", "no-reply@service.com", "Dataset Rejected"),
    ]
    ordered = MagicMock()
    ordered.__getitem__ = lambda _self, _slc: fakes
    _enable_ai.account.inbox.all.return_value.order_by.return_value = ordered

    seen: list = []

    class _Service:
        def __init__(self, *_a, **_kw):
            pass

        async def search_similar(self, *, query, documents, text_key, top_k, threshold):
            seen.extend(documents)
            return [(d, 0.9) for d in documents]

    tool = SemanticSearchEmailsTool(_enable_ai)
    with patch("src.tools.ai_tools.EmbeddingService", _Service), \
         patch("src.tools.ai_tools.get_embedding_provider", return_value=object()):
        result = await tool.execute(query="migration", exclude_automated=False)

    ids_in = sorted(d["id"] for d in seen)
    assert ids_in == ["A", "B"]
    meta = result.get("meta") or {}
    # When the filter is off, filtered_automated is 0 regardless of how
    # many noise senders were in the input.
    assert meta.get("filtered_automated") == 0
