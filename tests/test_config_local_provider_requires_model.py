"""When AI_PROVIDER=local, the OpenAI-compat upstream (Ollama, vLLM, LM Studio,
llama.cpp server) all require an explicit `model` field on
/chat/completions. Without AI_MODEL the previous behaviour was to leave
`config.ai_model` as None — every chat call (build_voice_profile,
extract_commitments, etc.) then sent `model=None` and the upstream
rejected with HTTP 400 "model is required".

This test pins the loud-failure behaviour: a local provider with
enable_ai=true and no AI_MODEL must refuse to validate so the operator
sees the misconfiguration at startup rather than on the first AI call.
"""

from __future__ import annotations

import pytest


def _base_local_config(**overrides):
    from src.config import Settings as Config

    kwargs = dict(
        ews_email="user@example.invalid",
        ews_username="user@example.invalid",
        ews_password="x",
        ews_auth_type="basic",
        enable_ai=True,
        ai_provider="local",
        ai_api_key="ollama",
        ai_base_url="http://localhost:11434/v1",
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def test_local_provider_without_ai_model_raises():
    """No AI_MODEL + AI_PROVIDER=local must fail validation explicitly."""
    with pytest.raises(ValueError) as exc_info:
        _base_local_config(ai_model=None)
    msg = str(exc_info.value)
    assert "AI_MODEL is required" in msg
    assert "AI_PROVIDER=local" in msg
    # The error must point at the actual base_url so operators know which
    # endpoint they need to load a model into.
    assert "http://localhost:11434/v1" in msg


def test_local_provider_with_ai_model_validates():
    """Setting AI_MODEL satisfies the requirement."""
    cfg = _base_local_config(ai_model="llama3.2:latest")
    assert cfg.ai_model == "llama3.2:latest"
    assert cfg.ai_provider == "local"


def test_openai_provider_still_defaults_when_unset():
    """The default-on-unset path for OpenAI is unchanged — only `local`
    needs the explicit error."""
    from src.config import Settings as Config

    cfg = Config(
        ews_email="user@example.invalid",
        ews_username="user@example.invalid",
        ews_password="x",
        ews_auth_type="basic",
        enable_ai=True,
        ai_provider="openai",
        ai_api_key="sk-fake",
        ai_model=None,
    )
    assert cfg.ai_model == "gpt-4o-mini"
