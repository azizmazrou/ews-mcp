"""v5 test fixtures: import path + per-test alias-store isolation."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ewsmcp.confirm import reset_consumed_tokens  # noqa: E402
from ewsmcp.ids import reset_aliaser_cache  # noqa: E402
from ewsmcp.tools.base import reset_send_rate_window  # noqa: E402
from ewsmcp.tools.writes import reset_idempotency_store  # noqa: E402

# Fixed UTC+3 IANA zone without daylight saving (Etc/GMT-3 means +03:00).
FIXED_OFFSET_TZ = "Etc/GMT-3"


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_aliaser_cache()
    reset_send_rate_window()
    reset_consumed_tokens()
    reset_idempotency_store()
    yield
    reset_aliaser_cache()
    reset_send_rate_window()
    reset_consumed_tokens()
    reset_idempotency_store()


def make_settings(**overrides):
    """Settings with synthetic Exchange endpoints.

    data_dir intentionally comes from the DATA_DIR env var that
    ``_isolate_stores`` points at tmp_path — the synced-folder guard
    would (correctly) refuse a relative path resolved inside the repo.
    """
    from ewsmcp.config import Settings
    base = dict(
        ews_server_url="https://mail.corp.example/EWS/Exchange.asmx",
        ews_email="exec@corp.example",
        ews_username="svc",
        ews_password="pw",
        mcp_transport="stdio",
    )
    base.update(overrides)
    return Settings(**base)
