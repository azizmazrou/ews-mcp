"""Settings guards: absolute data_dir + the synced-folder refusal.

The data dir holds mail-at-rest (aliases, audit chain, cache mirror) —
booting with it inside OneDrive/Dropbox/… replicates a mailbox to every
synced device, so the default posture is refusal with an explicit escape
hatch.
"""

import pytest
from pydantic import ValidationError

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


def test_empty_and_whitespace_auth_type_force_loads_as_unset(monkeypatch):
    monkeypatch.setenv("EWS_AUTH_TYPE_FORCE", "")
    assert make_settings().ews_auth_type_force is None
    monkeypatch.setenv("EWS_AUTH_TYPE_FORCE", "   \t  ")
    assert make_settings().ews_auth_type_force is None
    assert make_settings(ews_auth_type_force="").ews_auth_type_force is None
    assert make_settings(ews_auth_type_force="  ").ews_auth_type_force is None
    assert make_settings(ews_auth_type_force=" NTLM ").ews_auth_type_force == "ntlm"
    with pytest.raises(Exception):
        make_settings(ews_auth_type_force="kerberos")


def test_auth_breaker_setting_defaults():
    s = make_settings()
    assert s.ews_auth_failure_cooldown_seconds == 900
    assert s.ews_auth_max_rejections == 3


@pytest.mark.parametrize("valid", [60, 900, 3600])
def test_auth_failure_cooldown_seconds_accepts_at_or_above_60(monkeypatch, valid):
    assert (
        make_settings(ews_auth_failure_cooldown_seconds=valid).ews_auth_failure_cooldown_seconds
        == valid
    )
    monkeypatch.setenv("EWS_AUTH_FAILURE_COOLDOWN_SECONDS", str(valid))
    assert make_settings().ews_auth_failure_cooldown_seconds == valid


@pytest.mark.parametrize("invalid", [-60, -1, 0, 1, 59])
def test_auth_failure_cooldown_seconds_rejects_below_60(monkeypatch, invalid):
    with pytest.raises(ValidationError):
        make_settings(ews_auth_failure_cooldown_seconds=invalid)
    monkeypatch.setenv("EWS_AUTH_FAILURE_COOLDOWN_SECONDS", str(invalid))
    with pytest.raises(ValidationError):
        make_settings()


@pytest.mark.parametrize("valid", [1, 3, 10])
def test_auth_max_rejections_accepts_1_to_10(monkeypatch, valid):
    assert make_settings(ews_auth_max_rejections=valid).ews_auth_max_rejections == valid
    monkeypatch.setenv("EWS_AUTH_MAX_REJECTIONS", str(valid))
    assert make_settings().ews_auth_max_rejections == valid


@pytest.mark.parametrize("invalid", [-1, 0, 11, 100])
def test_auth_max_rejections_rejects_outside_1_to_10(monkeypatch, invalid):
    with pytest.raises(ValidationError):
        make_settings(ews_auth_max_rejections=invalid)
    monkeypatch.setenv("EWS_AUTH_MAX_REJECTIONS", str(invalid))
    with pytest.raises(ValidationError):
        make_settings()
