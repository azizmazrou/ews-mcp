"""Gateway probe/reset behavior — the false-warm fix (Phase B critical #4).

``inbox.total_count`` is a cached property after its first read, so probing
it reported warm forever (the /readyz lie). The probe must round-trip every
call, and reset() must evict exchangelib's protocol cache so a wedged
session actually renegotiates auth.
"""

from unittest.mock import MagicMock

from conftest import make_settings

from ewsmcp.gateway import client as client_mod
from ewsmcp.gateway.client import EWSGateway


def _gateway_with_mock_account():
    gw = EWSGateway(make_settings())
    account = MagicMock(name="account")
    gw._account = account
    return gw, account


def test_probe_round_trips_on_every_call():
    gw, account = _gateway_with_mock_account()
    assert gw.test_connection() is True
    assert gw.test_connection() is True
    assert account.root.refresh.call_count == 2  # not a cached-property read


def test_probe_failure_is_reported_not_cached():
    gw, account = _gateway_with_mock_account()
    account.root.refresh.side_effect = ConnectionError("front door down")
    assert gw.test_connection() is False
    assert "front door down" in gw.last_connection_error
    account.root.refresh.side_effect = None
    assert gw.test_connection() is True  # recovery observed immediately
    assert gw.last_connection_error is None


def test_reset_evicts_protocol_cache_and_folder_cache(monkeypatch):
    cleared = []
    monkeypatch.setattr(client_mod.CachingProtocol, "clear_cache",
                        lambda: cleared.append(True))
    gw, account = _gateway_with_mock_account()
    gw._folder_cache = {"path": object()}
    gw._folder_cache_ts = 123.0
    gw.reset()
    assert gw._account is None
    account.protocol.close.assert_called_once()
    assert cleared == [True]  # without this, the wedged Protocol comes back
    assert gw._folder_cache == {}
    assert gw._folder_cache_ts == 0.0


def test_reset_survives_close_and_clear_failures(monkeypatch):
    monkeypatch.setattr(
        client_mod.CachingProtocol, "clear_cache",
        lambda: (_ for _ in ()).throw(RuntimeError("cache locked")))
    gw, account = _gateway_with_mock_account()
    account.protocol.close.side_effect = OSError("socket gone")
    gw.reset()  # must not raise
    assert gw._account is None


def _response(status, content=b""):
    from types import SimpleNamespace as NS
    return NS(status_code=status, content=content, url="https://mail.corp.example/EWS/Exchange.asmx",
              headers={}, text=content.decode())


def test_multi_user_reports_401_at_once_instead_of_backing_off():
    import pytest
    from exchangelib.errors import ErrorInternalServerTransientError, UnauthorizedError
    from ewsmcp.errors import map_exception

    gw = EWSGateway(make_settings(ews_multi_user=True, ews_email=None, mcp_transport="http"))
    policy = gw._retry_policy()
    assert isinstance(policy, client_mod.RejectingFaultTolerance)
    with pytest.raises(UnauthorizedError) as exc:
        policy.raise_response_errors(_response(401))
    assert map_exception(exc.value).code == "auth_failed"
    with pytest.raises(UnauthorizedError, match="locked out"):
        policy.raise_response_errors(_response(401, b"The referenced account is currently locked out"))
    with pytest.raises(ErrorInternalServerTransientError):
        policy.raise_response_errors(_response(503))


def test_single_mailbox_keeps_retrying_401():
    import pytest
    from exchangelib.errors import ErrorServerBusy

    policy = EWSGateway(make_settings())._retry_policy()
    assert not isinstance(policy, client_mod.RejectingFaultTolerance)
    with pytest.raises(ErrorServerBusy):
        policy.raise_response_errors(_response(401))
