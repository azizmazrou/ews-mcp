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


def test_mail_folders_skips_other_classes_and_excluded_trees(monkeypatch):
    from types import SimpleNamespace as NS

    gw, account = _gateway_with_mock_account()
    root, top = NS(id="root", name="root"), NS(id="top", name="Top")

    def folder(raw_id, name, parents=(), cls="IPF.Note"):
        made = NS(id=raw_id, name=name, folder_class=cls)
        made.parts = [root, top, *parents, made]
        return made

    inbox = folder("in", "Inbox")
    child = folder("ch", "Projects", [inbox])
    trash = folder("tr", "Deleted")
    below_trash = folder("bt", "Old", [trash])
    calendar = folder("ca", "Calendar", cls="IPF.Appointment")
    account.trash, account.junk = trash, NS(id="ju")
    account.drafts, account.outbox = NS(id="dr"), NS(id="ou")
    monkeypatch.setattr(gw, "_folder_map", lambda _a: {
        "in": inbox, "inbox": inbox, "ch": child, "tr": trash, "bt": below_trash, "ca": calendar,
    })
    assert [(p, f.id) for p, f in gw.mail_folders(account)] == [
        ("Inbox", "in"), ("Inbox/Projects", "ch"),
    ]


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
