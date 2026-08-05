"""Settings guards: absolute data_dir + the synced-folder refusal.

The data dir holds mail-at-rest (aliases, audit chain, cache mirror) —
booting with it inside OneDrive/Dropbox/… replicates a mailbox to every
synced device, so the default posture is refusal with an explicit escape
hatch.
"""

import pytest

from conftest import make_settings


def test_data_dir_is_always_absolute(tmp_path):
    s = make_settings(data_dir=str(tmp_path / "d"))
    import os
    assert os.path.isabs(s.data_dir)


def test_default_data_dir_is_home_scoped_absolute(monkeypatch, tmp_path):
    monkeypatch.delenv("DATA_DIR", raising=False)
    s = make_settings()
    assert s.data_dir.endswith(".ewsmcp")


@pytest.mark.parametrize("marker", ["OneDrive", "Dropbox", "Google Drive"])
def test_synced_paths_are_refused(tmp_path, marker):
    with pytest.raises(Exception, match="synced"):
        make_settings(data_dir=str(tmp_path / marker / "data"))


def test_synced_path_escape_hatch(tmp_path):
    s = make_settings(data_dir=str(tmp_path / "OneDrive" / "data"),
                      data_dir_allow_synced=True)
    assert "OneDrive" in s.data_dir


def test_confirm_ttl_default_matches_confirm_module():
    from ewsmcp import confirm
    assert make_settings().confirm_ttl_seconds == confirm.DEFAULT_TTL_SECONDS == 600


# --- AUTH_MODE guards (DESIGN.md §Transports) --------------------------------
#
# Every one of these fails CLOSED. A half-configured oidc server that booted
# anyway would either reject everyone or — far worse — fall back to the single
# static mailbox and serve it to unauthenticated callers.

def _oidc(**overrides):
    """The identity half of an oidc config, without the upstream half."""
    base = dict(
        auth_mode="oidc",
        auth_issuer="https://idp.corp.example/",
        auth_audience="api://ews-mcp",
        auth_jwks_url="https://idp.corp.example/keys",
        data_dir_namespace_salt="test-salt",
        mcp_transport="http",
    )
    base.update(overrides)
    return make_settings(**base)


def test_static_is_the_default_and_needs_no_auth_config():
    """The regression canary: existing deployments must be untouched."""
    assert make_settings().auth_mode == "static"


def test_complete_oidc_config_boots_with_a_per_caller_upstream():
    settings = _obo()
    assert settings.auth_mode == "oidc"
    assert settings.per_caller_upstream is True


@pytest.mark.parametrize(
    "missing", ["auth_issuer", "auth_audience", "auth_jwks_url",
                "data_dir_namespace_salt"])
def test_oidc_requires_its_identity_settings(missing):
    with pytest.raises(Exception, match=missing.upper()):
        _oidc(**{missing: None})


def test_oidc_refuses_stdio():
    """stdio carries no bearer token, so no caller could be identified."""
    with pytest.raises(Exception, match="MCP_TRANSPORT=http"):
        _oidc(mcp_transport="stdio")


def _obo(**overrides):
    base = dict(
        ews_cache_enabled=False,
        auth_obo_token_url="https://idp.corp.example/token",
        auth_obo_client_id="ews-mcp",
        auth_obo_client_secret="secret",
        auth_ews_scope="https://mail.corp.example/.default",
    )
    base.update(overrides)
    return _oidc(**base)


@pytest.mark.parametrize(
    "missing", ["auth_obo_token_url", "auth_obo_client_id",
                "auth_obo_client_secret", "auth_ews_scope"])
def test_obo_upstream_requires_its_client_settings(missing):
    with pytest.raises(Exception, match=missing.upper()):
        _obo(**{missing: None})


def test_passthrough_needs_an_explicit_opt_in():
    """Forwarding our own audience token to Exchange is a confused deputy."""
    with pytest.raises(Exception, match="confused deputy"):
        _obo(auth_upstream_mode="passthrough")


def test_passthrough_opt_in_gets_past_the_guard():
    settings = _obo(auth_upstream_mode="passthrough", auth_allow_token_passthrough=True)
    assert settings.auth_upstream_mode == "passthrough"


def test_per_caller_upstream_refuses_the_semantic_tier():
    """No tenant column on the embeddings table yet."""
    with pytest.raises(Exception, match="EWS_SEMANTIC_INDEX"):
        _obo(ews_semantic_index="pgvector", ews_semantic_pg_dsn="postgresql:///x")


def test_per_caller_upstream_allows_the_cache_mirror():
    """The mirror is per caller now, warmed from inside their own requests
    rather than by a background loop that would need a credential we
    deliberately do not hold."""
    assert _obo(ews_cache_enabled=True).ews_cache_enabled is True


@pytest.mark.parametrize("algs", ["HS256", "RS256,HS512", "hs256"])
def test_symmetric_algorithms_are_refused(algs):
    """An HS* allowlist entry turns the public JWKS key into the HMAC secret:
    anyone holding the published key could mint valid tokens."""
    with pytest.raises(Exception, match="HS"):
        make_settings(auth_allowed_algs=algs)


def test_symmetric_algorithm_guard_applies_in_static_mode_too():
    """The guard is not behind the mode switch — a misconfigured allowlist
    should be caught the moment it is written, not when oidc is switched on."""
    with pytest.raises(Exception, match="HS"):
        make_settings(auth_mode="static", auth_allowed_algs="RS256,HS256")
