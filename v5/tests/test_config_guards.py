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


def test_attachment_settings_defaults_and_bounds():
    s = make_settings()
    assert s.ews_attachment_dir == ""
    assert s.ews_max_attachment_mb == 25
    assert make_settings(ews_max_attachment_mb=1).ews_max_attachment_mb == 1
    assert make_settings(ews_max_attachment_mb=50).ews_max_attachment_mb == 50
    for out_of_range in (0, -1, 51, 100):
        with pytest.raises(Exception):
            make_settings(ews_max_attachment_mb=out_of_range)
