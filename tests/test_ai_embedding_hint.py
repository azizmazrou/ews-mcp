"""Regression tests for the embedding-error hint dispatcher.

The live NAS surfaced a ConnectError when AI_BASE_URL was set to the host's
LAN IP from inside a Docker bridge network. The original hint pointed
exclusively at AI_EMBEDDING_MODEL, which sent the user on a wrong-trail
debug. The dispatcher now branches on the underlying error message and
returns either a networking hint or a model-name hint — these tests pin
that branching so future refactors can't silently merge the two.
"""
from __future__ import annotations

import pytest

from src.tools.ai_tools import _embedding_error_hint


# --- Networking-style failures should produce the docker hint ----------

@pytest.mark.parametrize(
    "msg",
    [
        # The exact shape of the error we saw against a real bridge-network
        # deployment (IP redacted to a documentation range):
        "Embedding endpoint unreachable at http://10.0.0.10:11434/v1/embeddings: "
        "ConnectError: All connection attempts failed",
        # httpx variants — these are *connection* failures, not slow upstream:
        "ConnectError: connection refused",
        "ConnectTimeout: timed out",
        # Bare TCP refusals:
        "[Errno 111] Connection refused",
    ],
)
def test_unreachable_hint_points_at_docker_networking(msg):
    hint = _embedding_error_hint(msg)
    assert "host.docker.internal" in hint, hint
    assert "extra_hosts" in hint, hint
    assert "AI_BASE_URL" in hint, hint


def test_read_timeout_is_not_classified_as_unreachable():
    """ReadTimeout means the TCP connection succeeded but the upstream was
    slow to respond — that's a model/load issue, not a docker-networking
    issue. Keep it on the model-name hint path so we don't mislead operators
    whose routing is fine but whose Ollama is just under-provisioned."""
    hint = _embedding_error_hint("ReadTimeout: timeout")
    assert "host.docker.internal" not in hint
    assert "AI_EMBEDDING_MODEL" in hint


# --- Model-name / API-shape failures should keep the original hint -----

@pytest.mark.parametrize(
    "msg",
    [
        "OpenAI API error 404: model not found",
        "Embedding API error 400: invalid model 'ollama'",
        "model 'foo' is not installed",
    ],
)
def test_model_hint_points_at_embedding_model(msg):
    hint = _embedding_error_hint(msg)
    assert "AI_EMBEDDING_MODEL" in hint, hint
    assert "text-embedding-3-small" in hint, hint
    assert "nomic-embed-text" in hint, hint
    # And it must NOT recommend swapping the URL — that wastes a debug
    # cycle on a fully-routable endpoint that just doesn't have the model.
    assert "host.docker.internal" not in hint


# --- Empty/None safety -------------------------------------------------

def test_empty_message_falls_back_to_model_hint():
    """An EmbeddingError with no message shouldn't crash the dispatcher;
    the safest default is the model-name hint."""
    assert "AI_EMBEDDING_MODEL" in _embedding_error_hint("")
    assert "AI_EMBEDDING_MODEL" in _embedding_error_hint(None)  # type: ignore[arg-type]
