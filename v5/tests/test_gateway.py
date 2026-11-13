"""Gateway probe/reset behavior — the false-warm fix (Phase B critical #4).

``inbox.total_count`` is a cached property after its first read, so probing
it reported warm forever (the /readyz lie). The probe must round-trip every
call, and reset() must evict exchangelib's protocol cache so a wedged
session actually renegotiates auth.
"""

import logging
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
import requests
from conftest import make_settings
from exchangelib.errors import UnauthorizedError
from exchangelib.protocol import CachingProtocol
from exchangelib.version import EXCHANGE_2016, Version

from ewsmcp.errors import ToolError
from ewsmcp.gateway import client as client_mod
from ewsmcp.gateway.client import EWSGateway, RejectingFaultTolerance
from ewsmcp.ids import get_aliaser


def _gateway_with_mock_account():
    gw = EWSGateway(make_settings())
    account = MagicMock(name="account")
    gw._account = account
    return gw, account


def test_mail_folders_skips_other_classes_and_excluded_trees(monkeypatch):
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


def test_resolve_folder_distinguished_names_and_unknown_not_found(tmp_path, monkeypatch):
    gw, account = _gateway_with_mock_account()
    account.sent = NS(id="F-SENT", name="Sent Items")
    account.trash = NS(id="F-TRASH", name="Deleted Items")
    account.junk = NS(id="F-JUNK", name="Junk Email")
    monkeypatch.setattr(gw, "_folder_map", lambda _a: {})
    aliaser = get_aliaser(str(tmp_path / "aliases"))

    for ref, expected in (
        ("sentitems", account.sent),
        ("SentItems", account.sent),
        ("f:sentitems", account.sent),
        ("deleteditems", account.trash),
        ("DeletedItems", account.trash),
        ("f:deleteditems", account.trash),
        ("junkemail", account.junk),
        ("JunkEmail", account.junk),
        ("f:junkemail", account.junk),
    ):
        assert gw.resolve_folder(account, ref, aliaser) is expected

    for unknown in ("nonexistent_folder", "msgfolderroot"):
        with pytest.raises(ToolError) as excinfo:
            gw.resolve_folder(account, unknown, aliaser)
        assert excinfo.value.code == "not_found"
        assert "list_folders" in (excinfo.value.hint or "")


def test_mail_folders_handles_missing_well_known_folders_and_none_ids(monkeypatch):
    gw = EWSGateway(make_settings())
    root = NS(id=None, name="root")
    top = NS(id=None, name="Top of Information Store")
    inbox = NS(id="in", name="Inbox", folder_class="IPF.Note")
    inbox.parts = [root, top, inbox]
    drafts = NS(id="dr", name="Drafts", folder_class="IPF.Note")
    drafts.parts = [root, top, drafts]

    class _PartialAccount:
        trash = None
        outbox = NS(id=None)

        def __init__(self):
            self.drafts = drafts

        @property
        def junk(self):
            raise RuntimeError("ErrorFolderNotFound")

    account = _PartialAccount()
    monkeypatch.setattr(gw, "_folder_map", lambda _a: {"in": inbox, "dr": drafts})
    assert [(p, f.id) for p, f in gw.mail_folders(account)] == [("Inbox", "in")]


def test_build_account_uses_rejecting_fault_tolerance_in_every_mode(monkeypatch, caplog):
    captured = []
    monkeypatch.setattr(client_mod, "Account", lambda **kw: captured.append(kw) or kw)
    for forced in (None, "basic"):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ewsmcp.gateway.client"):
            gw = EWSGateway(
                make_settings(
                    ews_server_url="https://ews.example.com/EWS/Exchange.asmx",
                    ews_email="user@example.com",
                    ews_auth_type_force=forced,
                    ews_retry_max_wait_seconds=180,
                )
            )
            gw._build_account()
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        if forced is None:
            assert infos == []
        else:
            assert len(infos) == 1
            assert "basic" in infos[0]
            assert "user@example.com" not in infos[0]
            assert "ews.example.com" not in infos[0]
    assert len(captured) == 2
    for built, expected_auth in zip(captured, (None, "basic")):
        policy = built["config"].retry_policy
        assert isinstance(policy, RejectingFaultTolerance)
        assert policy.max_wait == 180
        assert policy.configured_auth_type == expected_auth


def test_second_gateway_replaces_cached_protocol_policy_on_real_objects(monkeypatch):
    # Account() asks the server for its version; answer locally so the real
    # Account/Protocol objects are built without touching the network.
    monkeypatch.setattr(
        Version, "guess",
        classmethod(lambda cls, protocol, api_version_hint=None: Version(build=EXCHANGE_2016)),
    )
    CachingProtocol.clear_cache()
    try:
        settings = make_settings(
            ews_server_url="https://ews.example.com/EWS/Exchange.asmx",
            ews_email="user@example.com",
            ews_username="svc-user",
            ews_password="secret",
            ews_auth_type_force="basic",
        )
        gw1 = EWSGateway(settings)
        acc1 = gw1._build_account()
        policy1 = acc1.protocol.retry_policy
        assert isinstance(policy1, RejectingFaultTolerance)

        gw2 = EWSGateway(settings)
        acc2 = gw2._build_account()
        policy2 = acc2.protocol.retry_policy
        assert acc2.protocol is acc1.protocol
        assert isinstance(policy2, RejectingFaultTolerance)
        assert policy2 is not policy1

        resp = requests.Response()
        resp.status_code = 401
        resp._content = b"401 Unauthorized"
        resp.url = settings.ews_server_url
        with pytest.raises(UnauthorizedError) as exc_info:
            policy2.raise_response_errors(resp)
        assert not hasattr(exc_info.value, "_auth_rejection_recorded")
        assert gw1.auth_status()["rejections"] == 0
        assert gw2.auth_status()["rejections"] == 1
    finally:
        CachingProtocol.clear_cache()
