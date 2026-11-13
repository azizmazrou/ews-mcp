"""Offline tests for HTTP 401 fail-fast policy, gateway auth breaker,
ConnectionManager auth_failed lifecycle, dispatcher gate, and diagnostics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
import requests
from conftest import make_settings
from exchangelib import Account, Configuration, Credentials, DELEGATE, EWSTimeZone
from exchangelib.errors import UnauthorizedError
from exchangelib.protocol import BaseProtocol, CachingProtocol, FaultTolerance
from exchangelib.version import EXCHANGE_2016, Version
from requests.adapters import HTTPAdapter

from ewsmcp.audit import AuditLog
from ewsmcp.cache.store import CacheStore
from ewsmcp.cache.sync import SyncEngine
from ewsmcp.errors import ToolError
from ewsmcp.gateway.client import AuthBreaker, EWSGateway, RejectingFaultTolerance
from ewsmcp.gateway.connection import STATE_AUTH_FAILED, STATE_WARM, ConnectionManager
from ewsmcp.http import build_app
from ewsmcp.ids import get_aliaser
from ewsmcp.tools import build_registry
from ewsmcp.tools.base import Context, dispatch

ENDPOINT = "https://ews.example.com/EWS/Exchange.asmx"
MAILBOX = "mailbox@example.com"
USERNAME = "svc-mailbox"
SECRET = "wrong-secret-value"

SOAP_SERVER_BUSY_GETFOLDER = b"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"
            xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
  <s:Body>
    <m:GetFolderResponse>
      <m:ResponseMessages>
        <m:GetFolderResponseMessage ResponseClass="Error">
          <m:MessageText>The server cannot service this request right now.</m:MessageText>
          <m:ResponseCode>ErrorServerBusy</m:ResponseCode>
          <m:DescriptiveLinkKey>0</m:DescriptiveLinkKey>
          <m:MessageXml>
            <t:Value Name="BackOffMilliseconds">5000</t:Value>
          </m:MessageXml>
        </m:GetFolderResponseMessage>
      </m:ResponseMessages>
    </m:GetFolderResponse>
  </s:Body>
</s:Envelope>"""


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.current = start
        self.sleeps: List[float] = []

    def now(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds

    async def async_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds
        await asyncio.sleep(0)


def _install_fake_http(monkeypatch, queued_responses: List[Dict[str, Any]]):
    """Mount an offline HTTPAdapter on BaseProtocol and trap any sleep calls."""
    queue = list(queued_responses)
    sent_requests: List[Any] = []
    sleeps: List[float] = []

    class _Adapter(HTTPAdapter):
        def send(self, request, stream=False, timeout=None, verify=True,
                 cert=None, proxies=None):
            sent_requests.append(request)
            spec = queue.pop(0)
            resp = requests.Response()
            resp.status_code = spec["status"]
            resp._content = spec.get("body", b"")
            resp.url = request.url
            resp.request = request
            for k, v in (spec.get("headers") or {}).items():
                resp.headers[k] = v
            return resp

    def fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    CachingProtocol.clear_cache()
    monkeypatch.setattr(BaseProtocol, "HTTP_ADAPTER_CLS", _Adapter)
    real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", fake_sleep)
    for mod in list(sys.modules.values()):
        if (
            mod is not None
            and getattr(mod, "__name__", "").startswith("exchangelib")
            and getattr(mod, "sleep", None) is real_sleep
        ):
            monkeypatch.setattr(mod, "sleep", fake_sleep)
    return sent_requests, sleeps


def _real_account(policy: RejectingFaultTolerance) -> Account:
    config = Configuration(
        service_endpoint=ENDPOINT,
        credentials=Credentials(USERNAME, SECRET),
        auth_type="basic",
        version=Version(build=EXCHANGE_2016),
        retry_policy=policy,
    )
    return Account(
        primary_smtp_address=MAILBOX,
        config=config,
        autodiscover=False,
        access_type=DELEGATE,
        default_timezone=EWSTimeZone("Asia/Riyadh"),
    )


# ---------------------------------------------------------------------------
# (a) RejectingFaultTolerance + (7) Diagnostics
# ---------------------------------------------------------------------------


def test_policy_401_raises_unauthorized_immediately_without_sleep_or_retry(
    monkeypatch, caplog
):
    sent, sleeps = _install_fake_http(monkeypatch, [
        {
            "status": 401,
            "body": b"401 Unauthorized",
            "headers": {
                "WWW-Authenticate": 'Negotiate, NTLM, Basic realm="ews.example.com"'
            },
        },
        {"status": 200, "body": b"unreachable second response"},
    ])
    try:
        account = _real_account(
            RejectingFaultTolerance(max_wait=60, configured_auth_type="basic")
        )
        with caplog.at_level(logging.WARNING, logger="ewsmcp.gateway.client"):
            with pytest.raises(UnauthorizedError):
                account.root.refresh()
        assert len(sent) == 1
        assert sleeps == []
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "Negotiate, NTLM, Basic" in warnings[0]
        assert "ews_auth_type_force=basic" in warnings[0]
        assert SECRET not in caplog.text
        assert USERNAME not in caplog.text
        assert MAILBOX not in caplog.text
    finally:
        CachingProtocol.clear_cache()


def test_policy_503_still_follows_fault_tolerance(monkeypatch):
    sent, sleeps = _install_fake_http(monkeypatch, [
        {"status": 503, "body": b"Service Unavailable"},
        {"status": 401, "body": b"stop after retry"},
    ])
    try:
        account = _real_account(RejectingFaultTolerance(max_wait=60))
        with pytest.raises(UnauthorizedError):
            account.root.refresh()
        assert len(sent) == 2
        assert len(sleeps) >= 1 and all(s > 0 for s in sleeps)
    finally:
        CachingProtocol.clear_cache()


def test_policy_soap_throttling_still_follows_fault_tolerance(monkeypatch):
    sent, sleeps = _install_fake_http(monkeypatch, [
        {
            "status": 200,
            "body": SOAP_SERVER_BUSY_GETFOLDER,
            "headers": {"Content-Type": "text/xml; charset=utf-8"},
        },
        {"status": 401, "body": b"stop after throttle backoff"},
    ])
    try:
        account = _real_account(RejectingFaultTolerance(max_wait=60))
        with pytest.raises(UnauthorizedError):
            account.root.refresh()
        assert len(sent) == 2
        assert len(sleeps) >= 1 and all(s > 0 for s in sleeps)
    finally:
        CachingProtocol.clear_cache()


def test_policy_401_locked_out_body_keeps_exchangelib_handling(monkeypatch):
    resp = requests.Response()
    resp.status_code = 401
    resp._content = b"The referenced account is currently locked out"
    resp.url = ENDPOINT

    with pytest.raises(
        UnauthorizedError, match="The referenced account is currently locked out"
    ) as ft_exc:
        FaultTolerance(max_wait=60).raise_response_errors(resp)
    with pytest.raises(
        UnauthorizedError, match="The referenced account is currently locked out"
    ) as rft_exc:
        RejectingFaultTolerance(max_wait=60).raise_response_errors(resp)
    assert str(rft_exc.value) == str(ft_exc.value)

    sent, sleeps = _install_fake_http(monkeypatch, [
        {"status": 401, "body": b"The referenced account is currently locked out"},
        {"status": 200, "body": b"unreachable second response"},
    ])
    try:
        account = _real_account(RejectingFaultTolerance(max_wait=60))
        with pytest.raises(
            UnauthorizedError,
            match="The referenced account is currently locked out",
        ):
            account.root.refresh()
        assert len(sent) == 1
        assert sleeps == []
    finally:
        CachingProtocol.clear_cache()


def test_policy_diagnostics_auto_negotiated_and_strips_challenge_blobs(caplog):
    resp = requests.Response()
    resp.status_code = 401
    resp._content = b""
    resp.url = ENDPOINT
    resp.headers["WWW-Authenticate"] = (
        'Digest realm="example.org, internal", qop="auth", nonce="sensitive-nonce", '
        "NTLM TlRMTVNTUAACAAAADAAMADgAAAA="
    )
    policy = RejectingFaultTolerance(max_wait=60, configured_auth_type=None)
    with caplog.at_level(logging.WARNING, logger="ewsmcp.gateway.client"):
        with pytest.raises(UnauthorizedError):
            policy.raise_response_errors(resp)
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Digest, NTLM" in warnings[0]
    assert "ews_auth_type_force=auto-negotiated" in warnings[0]
    assert "sensitive-nonce" not in caplog.text
    assert "TlRMTVNTUAAC" not in caplog.text


# ---------------------------------------------------------------------------
# (b) Gateway auth circuit breaker
# ---------------------------------------------------------------------------


def _settings(**overrides):
    base = dict(
        ews_server_url=ENDPOINT,
        ews_email=MAILBOX,
        ews_username=USERNAME,
        ews_password=SECRET,
        ews_auth_failure_cooldown_seconds=900,
        ews_auth_max_rejections=3,
    )
    base.update(overrides)
    return make_settings(**base)


def test_gateway_breaker_cooldown_single_probe_reset_and_permanent_cap():
    clock = FakeClock()
    gw = EWSGateway(_settings(), clock=clock.now)
    exchange_calls = 0
    build_calls = 0
    should_reject = True

    def fake_build() -> Any:
        nonlocal build_calls
        build_calls += 1
        acc = MagicMock(name="account")

        def refresh():
            nonlocal exchange_calls
            exchange_calls += 1
            if should_reject:
                raise UnauthorizedError("HTTP 401")

        acc.root.refresh.side_effect = refresh
        return acc

    gw._build_account = fake_build  # type: ignore[method-assign]

    async def probe_via_call():
        return await gw.call(lambda acc: acc.root.refresh())

    # 1st rejection trips the breaker for 900s.
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(probe_via_call())
    assert exc_info.value.code == "auth_failed"
    assert exc_info.value.retry_after_s == 900
    assert exchange_calls == 1
    assert build_calls == 1

    # Drop cached account to prove open breaker does NOT even build an Account.
    gw._account = None
    for delta in (0, 300, 599):
        clock.advance(delta)
        with pytest.raises(ToolError) as blocked_call:
            asyncio.run(probe_via_call())
        assert blocked_call.value.code == "auth_failed"
        with pytest.raises(ToolError):
            gw.test_connection()
        with pytest.raises(ToolError):
            _ = gw.account
    assert exchange_calls == 1
    assert build_calls == 1

    # Advance past the 900s cooldown (already advanced 899s above -> +1s = 900s).
    clock.advance(1)
    with pytest.raises(ToolError) as second_rej:
        asyncio.run(probe_via_call())
    assert second_rej.value.code == "auth_failed"
    assert second_rej.value.retry_after_s == 900
    assert exchange_calls == 2
    assert build_calls == 2
    assert gw.auth_status()["rejections"] == 2
    assert gw.auth_status()["permanent"] is False

    # Still zero calls during the second cooldown.
    clock.advance(450)
    with pytest.raises(ToolError):
        asyncio.run(probe_via_call())
    assert exchange_calls == 2

    # Cooldown ends; a successful round trip resets the rejection count to 0.
    clock.advance(450)
    should_reject = False
    asyncio.run(probe_via_call())
    assert exchange_calls == 3
    assert gw.auth_status()["rejections"] == 0
    assert gw.auth_status()["permanent"] is False

    # Now accumulate ews_auth_max_rejections (3) consecutive rejections.
    should_reject = True
    for expected_count in (1, 2, 3):
        with pytest.raises(ToolError) as rej:
            asyncio.run(probe_via_call())
        assert rej.value.code == "auth_failed"
        assert gw.auth_status()["rejections"] == expected_count
        if expected_count < 3:
            assert rej.value.retry_after_s == 900
            assert gw.auth_status()["permanent"] is False
            clock.advance(900)
        else:
            assert rej.value.retry_after_s is None
            assert gw.auth_status()["permanent"] is True

    calls_at_cap = exchange_calls
    assert calls_at_cap == 6

    # Permanently open: never tries again regardless of elapsed time.
    should_reject = False
    clock.advance(10_000_000)
    with pytest.raises(ToolError) as perm_err:
        asyncio.run(probe_via_call())
    assert perm_err.value.code == "auth_failed"
    assert perm_err.value.retry_after_s is None
    with pytest.raises(ToolError):
        gw.test_connection()
    with pytest.raises(ToolError):
        _ = gw.account
    assert exchange_calls == calls_at_cap


def test_gateway_half_open_allows_only_one_concurrent_probe():
    clock = FakeClock()
    gw = EWSGateway(_settings(), clock=clock.now)
    entered = threading.Event()
    release = threading.Event()
    exchange_calls = 0

    account = MagicMock(name="account")
    gw._account = account

    def slow_reject(_acc):
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 2:
            entered.set()
            release.wait(timeout=5)
        raise UnauthorizedError("HTTP 401")

    with pytest.raises(ToolError):
        asyncio.run(gw.call(slow_reject))
    assert exchange_calls == 1

    clock.advance(900)

    async def run_two():
        t1 = asyncio.create_task(gw.call(slow_reject))
        await asyncio.to_thread(entered.wait, 5)
        # While t1's post-cooldown probe is in flight, t2 must fail fast.
        with pytest.raises(ToolError) as t2_err:
            await gw.call(slow_reject)
        assert t2_err.value.code == "auth_failed"
        release.set()
        with pytest.raises(ToolError):
            await t1

    asyncio.run(run_two())
    assert exchange_calls == 2


def test_gateway_locked_out_response_opens_breaker_like_any_rejection(monkeypatch):
    locked_body = b"The referenced account is currently locked out"
    sent, sleeps = _install_fake_http(monkeypatch, [
        {"status": 401, "body": locked_body},
        {"status": 401, "body": locked_body},
        {"status": 200, "body": b"unreachable while breaker open"},
    ])
    try:
        clock = FakeClock()
        gw = EWSGateway(
            _settings(
                ews_auth_type_force="basic",
                ews_auth_failure_cooldown_seconds=900,
                ews_auth_max_rejections=2,
            ),
            clock=clock.now,
        )
        gw._account = _real_account(
            RejectingFaultTolerance(
                max_wait=60,
                configured_auth_type="basic",
                on_rejection=lambda: gw._breaker.record_rejection(),
            )
        )

        with pytest.raises(ToolError) as first_err:
            gw.test_connection()
        assert first_err.value.code == "auth_failed"
        assert first_err.value.retry_after_s == 900
        assert len(sent) == 1
        assert sleeps == []
        st1 = gw.auth_status()
        assert st1["rejections"] == 1
        assert st1["open"] is True
        assert st1["permanent"] is False

        clock.advance(300)
        with pytest.raises(ToolError):
            gw.test_connection()
        with pytest.raises(ToolError):
            asyncio.run(gw.call(lambda acc: acc.root.refresh()))
        assert len(sent) == 1

        clock.advance(600)
        with pytest.raises(ToolError) as second_err:
            asyncio.run(gw.call(lambda acc: acc.root.refresh()))
        assert second_err.value.code == "auth_failed"
        assert second_err.value.retry_after_s is None
        assert len(sent) == 2
        assert sleeps == []
        st2 = gw.auth_status()
        assert st2["rejections"] == 2
        assert st2["permanent"] is True

        clock.advance(10_000_000)
        with pytest.raises(ToolError):
            gw.test_connection()
        assert len(sent) == 2
    finally:
        CachingProtocol.clear_cache()


def test_gateway_records_rejection_on_own_breaker_even_with_foreign_policy_callback(
    monkeypatch,
):
    sent, sleeps = _install_fake_http(monkeypatch, [
        {"status": 401, "body": b"401 Unauthorized"},
        {"status": 401, "body": b"401 Unauthorized"},
    ])
    try:
        clock = FakeClock()
        other_gw = EWSGateway(_settings(), clock=clock.now)
        gw = EWSGateway(
            _settings(ews_auth_failure_cooldown_seconds=900, ews_auth_max_rejections=2),
            clock=clock.now,
        )
        # Deliberately attach a policy whose callback points at other_gw,
        # simulating a shared/foreign retry_policy callback.
        gw._account = _real_account(
            RejectingFaultTolerance(
                max_wait=60,
                configured_auth_type="basic",
                on_rejection=lambda: other_gw._breaker.record_rejection(),
            )
        )

        with pytest.raises(ToolError) as err1:
            gw.test_connection()
        assert err1.value.code == "auth_failed"
        assert err1.value.retry_after_s == 900
        assert gw.auth_status()["rejections"] == 1
        assert gw.auth_status()["open"] is True
        assert len(sent) == 1
        assert sleeps == []

        clock.advance(900)
        with pytest.raises(ToolError) as err2:
            asyncio.run(gw.call(lambda acc: acc.root.refresh()))
        assert err2.value.code == "auth_failed"
        assert err2.value.retry_after_s is None
        assert gw.auth_status()["rejections"] == 2
        assert gw.auth_status()["permanent"] is True
        assert len(sent) == 2
    finally:
        CachingProtocol.clear_cache()


# ---------------------------------------------------------------------------
# (c) ConnectionManager warm-up loop with fake clock
# ---------------------------------------------------------------------------


class FakeBreakerGateway:
    """Gateway test double built on the public AuthBreaker."""

    def __init__(
        self,
        outcomes: List[str],
        *,
        cooldown_seconds: int = 900,
        max_rejections: int = 3,
        clock: Any = None,
    ):
        self._outcomes = list(outcomes)
        self._clock = clock
        self.settings = _settings(
            ews_auth_failure_cooldown_seconds=max(60, cooldown_seconds),
            ews_auth_max_rejections=max_rejections,
        )
        self._breaker = AuthBreaker(
            cooldown_seconds=cooldown_seconds,
            max_rejections=max_rejections,
            clock=clock,
        )
        self.probes = 0
        self.resets = 0
        self.last_connection_error: str | None = None

    def auth_status(self) -> Dict[str, Any]:
        return self._breaker.status()

    def reset(self) -> None:
        self.resets += 1

    def test_connection(self) -> bool:
        _seq, claimed = self._breaker.before_call()
        try:
            self.probes += 1
            step = self._outcomes.pop(0) if self._outcomes else "auth"
            if step == "ok":
                self._breaker.record_success()
                self.last_connection_error = None
                return True
            if step == "transient":
                self.last_connection_error = "ConnectionError: transient fault"
                return False
            err = self._breaker.record_rejection()
            self.last_connection_error = f"ToolError: {err.message}"
            raise err
        finally:
            if claimed:
                self._breaker.release_half_open()


def test_warmup_loop_with_auth_breaker_fake_stops_at_cap_and_supports_recovery():
    clock = FakeClock()
    fake = FakeBreakerGateway(
        ["auth", "auth", "auth"],
        cooldown_seconds=600,
        max_rejections=3,
        clock=clock.now,
    )

    async def drive_to_cap():
        mgr = ConnectionManager(fake, clock=clock.now, sleep=clock.async_sleep)
        await mgr.start()
        assert mgr._task is not None
        await mgr._task
        return mgr

    mgr = asyncio.run(drive_to_cap())
    assert fake.probes == 3
    assert fake.resets == 0
    assert clock.sleeps == [600.0, 600.0]
    assert mgr.state == STATE_AUTH_FAILED
    st = mgr.status()
    assert st["state"] == STATE_AUTH_FAILED
    assert st["auth_rejections"] == 3
    assert st["auth_permanent"] is True
    assert st["next_retry_in_s"] is None

    # Recovery path: one rejection, wait out cooldown, then succeed.
    clock2 = FakeClock()
    recovering = FakeBreakerGateway(
        ["auth", "ok"],
        cooldown_seconds=120,
        max_rejections=3,
        clock=clock2.now,
    )

    async def drive_recovery():
        mgr2 = ConnectionManager(
            recovering,
            heartbeat_seconds=0,
            clock=clock2.now,
            sleep=clock2.async_sleep,
        )
        await mgr2.start()
        assert mgr2._task is not None
        await mgr2._task
        return mgr2

    mgr2 = asyncio.run(drive_recovery())
    assert recovering.probes == 2
    assert recovering.resets == 0
    assert clock2.sleeps == [120.0]
    assert mgr2.state == STATE_WARM
    assert mgr2.status()["auth_rejections"] == 0


def test_warmup_loop_enforces_cooldown_floor_even_when_breaker_reports_zero_or_none():
    clock = FakeClock()

    class _ZeroCooldownGateway:
        """Pathological gateway whose auth_status reports 0/None cooldowns."""

        def __init__(self) -> None:
            self.settings = _settings(
                ews_auth_failure_cooldown_seconds=300, ews_auth_max_rejections=2
            )
            self.probes = 0
            self._pre_open_once = True

        def auth_status(self) -> Dict[str, Any]:
            if self._pre_open_once:
                self._pre_open_once = False
                return {
                    "rejections": 1,
                    "max_rejections": 2,
                    "permanent": False,
                    "open": True,
                    "next_retry_in_s": 0,
                    "cooldown_remaining_s": 0.0,
                }
            return {
                "rejections": self.probes,
                "max_rejections": 2,
                "permanent": self.probes >= 2,
                "open": False,
                "next_retry_in_s": None,
                "cooldown_remaining_s": None if self.probes == 1 else 0.0,
            }

        def reset(self) -> None:
            raise AssertionError("reset must never run on auth_failed")

        def test_connection(self) -> bool:
            self.probes += 1
            raise ToolError("auth_failed", "simulated rejection", retry_after_s=0)

    gw = _ZeroCooldownGateway()

    async def drive():
        mgr = ConnectionManager(gw, clock=clock.now, sleep=clock.async_sleep)
        await mgr.start()
        assert mgr._task is not None
        await mgr._task
        return mgr

    mgr = asyncio.run(drive())
    assert gw.probes == 2
    # 1st sleep at top of loop (open=True, cooldown_remaining_s=0.0 -> 300.0 floor);
    # 2nd sleep after 1st probe (cooldown_remaining_s=None -> 300.0 floor);
    # 2nd probe hits permanent=True and exits.
    assert clock.sleeps == [300.0, 300.0]
    assert mgr.state == STATE_AUTH_FAILED
    assert mgr.status()["auth_permanent"] is True


def test_warmup_loop_stops_after_max_rejections_without_session_reset():
    clock = FakeClock()
    gw = EWSGateway(
        _settings(ews_auth_failure_cooldown_seconds=900, ews_auth_max_rejections=3),
        clock=clock.now,
    )
    account = MagicMock(name="account")
    probes = 0

    def rejecting_refresh():
        nonlocal probes
        probes += 1
        raise UnauthorizedError("HTTP 401")

    account.root.refresh.side_effect = rejecting_refresh
    gw._account = account
    reset_spy = MagicMock(name="reset")
    gw.reset = reset_spy  # type: ignore[method-assign]

    async def drive():
        mgr = ConnectionManager(gw, clock=clock.now, sleep=clock.async_sleep)
        await mgr.start()
        assert mgr._task is not None
        await mgr._task
        return mgr

    mgr = asyncio.run(drive())
    assert probes == 3
    reset_spy.assert_not_called()
    assert clock.sleeps == [900.0, 900.0]
    assert mgr.state == STATE_AUTH_FAILED
    st = mgr.status()
    assert st["state"] == STATE_AUTH_FAILED
    assert st["auth_rejections"] == 3
    assert st["auth_permanent"] is True
    assert st["next_retry_in_s"] is None


# ---------------------------------------------------------------------------
# (d) Heartbeat rejection -> auth_failed + dispatcher gate + /readyz
# ---------------------------------------------------------------------------


def test_heartbeat_rejection_enters_auth_failed_and_blocks_tools_without_gateway(
    tmp_path,
):
    clock = FakeClock()
    gw = EWSGateway(
        _settings(ews_auth_failure_cooldown_seconds=900, ews_auth_max_rejections=3),
        clock=clock.now,
    )
    account = MagicMock(name="account")
    reject_now = False
    probe_calls = 0

    def refresh():
        nonlocal probe_calls
        probe_calls += 1
        if reject_now:
            raise UnauthorizedError("HTTP 401 after password rotation")

    account.root.refresh.side_effect = refresh
    gw._account = account

    heartbeat_ready = asyncio.Event()
    proceed_heartbeat = asyncio.Event()
    warmup_paused = asyncio.Event()

    async def controlled_sleep(delay: float) -> None:
        if delay == 600.0:
            heartbeat_ready.set()
            await proceed_heartbeat.wait()
            clock.advance(delay)
            return
        # Re-armed warmup waiting on the 900s breaker cooldown: park here so we
        # can inspect state/readyz/tool dispatch mid-cooldown.
        warmup_paused.set()
        await asyncio.Future()

    async def scenario():
        mgr = ConnectionManager(
            gw,
            heartbeat_seconds=600,
            clock=clock.now,
            sleep=controlled_sleep,
        )
        await mgr.start()
        await heartbeat_ready.wait()
        assert mgr.state == STATE_WARM
        assert probe_calls == 1

        # Next heartbeat hits a 401 rejection.
        nonlocal reject_now
        reject_now = True
        proceed_heartbeat.set()
        assert mgr._heartbeat_task is not None
        await mgr._heartbeat_task
        await warmup_paused.wait()

        assert mgr.state == STATE_AUTH_FAILED
        st = mgr.status()
        assert st["state"] == STATE_AUTH_FAILED
        assert st["auth_rejections"] == 1
        assert st["auth_permanent"] is False
        assert st["next_retry_in_s"] == 900

        # Wire Context; forbid any gateway.call from a tool execution.
        async def forbidden_call(_fn):
            raise AssertionError("dispatcher must block before gateway.call")

        gw.call = forbidden_call  # type: ignore[method-assign]
        ctx = Context(
            settings=gw.settings,
            gateway=gw,
            manager=mgr,
            aliaser=get_aliaser(str(tmp_path / "alias")),
            audit=AuditLog(str(tmp_path / "audit")),
        )
        build_registry(ctx)

        tool_res = await dispatch(
            ctx, ctx.registry["search_messages"], {"fresh": True}
        )
        assert tool_res["ok"] is False
        assert tool_res["error"]["code"] == "auth_failed"
        assert tool_res["error"]["retry_after_s"] == 900
        assert "retrying will not help" in tool_res["error"]["hint"].lower()

        # Local status tool still works in state auth_failed.
        status_res = await dispatch(ctx, ctx.registry["get_server_status"], {})
        assert status_res["ok"] is True
        assert status_res["connection"]["state"] == STATE_AUTH_FAILED
        assert status_res["connection"]["auth_rejections"] == 1

        # /readyz reports 503 with status=auth_failed and the connection block.
        app = build_app(ctx, ctx.settings)
        sent_http: List[Dict[str, Any]] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent_http.append(msg)

        await app(
            {"type": "http", "path": "/readyz", "method": "GET", "headers": []},
            receive,
            send,
        )
        assert sent_http[0]["status"] == 503
        body = json.loads(sent_http[1]["body"].decode("utf-8"))
        assert body["status"] == "auth_failed"
        assert body["connection"]["state"] == STATE_AUTH_FAILED
        assert body["connection"]["auth_rejections"] == 1

        await mgr.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Background SyncEngine + thread safety
# ---------------------------------------------------------------------------


def test_sync_engine_slow_lane_propagates_unauthorized_and_trips_breaker(tmp_path):
    clock = FakeClock()
    gw = EWSGateway(_settings(), clock=clock.now)
    account = MagicMock(name="account")
    inbox = MagicMock(name="inbox")
    inbox.sync_items.return_value = []
    inbox.item_sync_state = "S1"
    account.inbox = inbox

    class _RejectingFolder:
        @property
        def children(self):
            raise UnauthorizedError("HTTP 401")

    account.msg_folder_root = _RejectingFolder()
    gw._account = account

    store = CacheStore(str(tmp_path / "mirror.db"))
    try:
        sync = SyncEngine(
            _settings(ews_cache_folders="inbox"),
            gw,
            store,
        )

        with pytest.raises(ToolError) as exc_info:
            asyncio.run(sync._cycle())
        assert exc_info.value.code == "auth_failed"
        assert gw.auth_status()["rejections"] == 1
        # Calendar and tasks were never attempted after the 401.
        account.calendar.view.assert_not_called()

        # Next sync cycle is blocked immediately by the open breaker with zero EWS I/O.
        inbox.sync_items.reset_mock()
        with pytest.raises(ToolError) as exc_info2:
            asyncio.run(sync._cycle())
        assert exc_info2.value.code == "auth_failed"
        inbox.sync_items.assert_not_called()
    finally:
        store.close()


def test_breaker_thread_safety_across_readers_and_pool_writers():
    clock = FakeClock()
    gw = EWSGateway(
        _settings(ews_auth_max_rejections=5, ews_auth_failure_cooldown_seconds=60),
        clock=clock.now,
    )
    errors: List[BaseException] = []

    def writer():
        try:
            for _ in range(25):
                gw._breaker.record_rejection()
                clock.advance(60)
                gw._breaker.record_success()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def reader():
        try:
            for _ in range(100):
                st = gw.auth_status()
                assert 0 <= st["rejections"] <= 5
                assert isinstance(st["permanent"], bool)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(3)] + [
        threading.Thread(target=reader) for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert errors == []
