"""End-to-end loopback HTTP 401 regression test for the lockout guard.

Spins up a loopback ``http.server`` on port 0 that always answers HTTP 401 and
counts POST requests carrying an ``Authorization`` header, exercising the real
``EWSGateway``, real ``exchangelib`` stack, forced Basic auth, injected clock,
and the two-gateway ``CachingProtocol`` case under ``ConnectionManager``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List

import pytest
from exchangelib.protocol import CachingProtocol

from ewsmcp.config import Settings
from ewsmcp.errors import ToolError
from ewsmcp.gateway.client import EWSGateway
from ewsmcp.gateway.connection import STATE_AUTH_FAILED, ConnectionManager


@dataclass
class _LoopbackRejectServer:
    url: str
    submissions: List[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_submission(self) -> None:
        with self._lock:
            self.submissions.append(time.monotonic())

    def count(self) -> int:
        with self._lock:
            return len(self.submissions)

    def clear(self) -> None:
        with self._lock:
            self.submissions.clear()


@pytest.fixture
def rejecting_ews_server(monkeypatch):
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)

    CachingProtocol.clear_cache()
    tracker = _LoopbackRejectServer(url="")

    class _RejectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                self.rfile.read(length)
            if self.headers.get("Authorization"):
                tracker.record_submission()
            body = b"<html>401 Unauthorized</html>"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="ews.example.com"')
            self.send_header("WWW-Authenticate", "NTLM")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RejectHandler)
    port = httpd.server_address[1]
    tracker.url = f"http://127.0.0.1:{port}/EWS/Exchange.asmx"
    thread = threading.Thread(
        target=httpd.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    thread.start()
    try:
        yield tracker
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        CachingProtocol.clear_cache()


def test_e2e_lockout_guard_single_gateway_and_second_cached_protocol_gateway(
    tmp_path, rejecting_ews_server
):
    cooldown = 900
    max_rejections = 3
    settings = Settings(
        ews_server_url=rejecting_ews_server.url,
        ews_email="user@example.com",
        ews_password="wrong-password",
        ews_auth_type_force="basic",
        ews_retry_max_wait_seconds=300,
        ews_auth_failure_cooldown_seconds=cooldown,
        ews_auth_max_rejections=max_rejections,
        data_dir=str(tmp_path / "data"),
        request_timeout=5,
    )

    clock1 = {"t": 1_000_000.0}
    gw1 = EWSGateway(settings, clock=lambda: clock1["t"])
    gw2 = None
    try:
        # 1. First probe submits credentials once and opens the breaker.
        with pytest.raises(ToolError) as first_err:
            gw1.test_connection()
        assert first_err.value.code == "auth_failed"
        assert first_err.value.retry_after_s == cooldown
        assert rejecting_ews_server.count() == 1

        # 2. Repeated probes and tool calls inside the cooldown submit nothing.
        for _ in range(5):
            clock1["t"] += 60
            with pytest.raises(ToolError) as err:
                gw1.test_connection()
            assert err.value.code == "auth_failed"
        for _ in range(5):
            with pytest.raises(ToolError) as err:
                asyncio.run(gw1.call(lambda account: account.root.refresh()))
            assert err.value.code == "auth_failed"
        assert rejecting_ews_server.count() == 1

        # 3. One more submission after the first cooldown, none immediately after.
        clock1["t"] = 1_000_000.0 + cooldown + 1
        with pytest.raises(ToolError) as second_err:
            gw1.test_connection()
        assert second_err.value.code == "auth_failed"
        assert rejecting_ews_server.count() == 2

        with pytest.raises(ToolError):
            gw1.test_connection()
        with pytest.raises(ToolError):
            asyncio.run(gw1.call(lambda account: account.root.refresh()))
        assert rejecting_ews_server.count() == 2

        # 4. One more submission after the second cooldown reaches the cap (3).
        clock1["t"] += cooldown + 1
        with pytest.raises(ToolError) as third_err:
            gw1.test_connection()
        assert third_err.value.code == "auth_failed"
        assert third_err.value.retry_after_s is None
        assert rejecting_ews_server.count() == max_rejections

        # 5. Long after the cap, probes and tool calls never submit again.
        clock1["t"] += 100_000
        for _ in range(3):
            with pytest.raises(ToolError) as cap_err:
                gw1.test_connection()
            assert cap_err.value.code == "auth_failed"
            assert cap_err.value.retry_after_s is None
        with pytest.raises(ToolError) as call_cap_err:
            asyncio.run(gw1.call(lambda account: account.root.refresh()))
        assert call_cap_err.value.code == "auth_failed"
        assert call_cap_err.value.retry_after_s is None
        assert rejecting_ews_server.count() == max_rejections

        # 6. Second EWSGateway in the same process (CachingProtocol still warm
        #    with gw1's entry) driven by ConnectionManager + fake clock/sleep.
        rejecting_ews_server.clear()
        clock2 = {"t": 2_000_000.0}
        gw2 = EWSGateway(settings, clock=lambda: clock2["t"])
        sleeps: List[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            clock2["t"] += delay
            await asyncio.sleep(0)

        async def run_manager():
            mgr = ConnectionManager(gw2, clock=lambda: clock2["t"], sleep=fake_sleep)
            await mgr.start()
            assert mgr._task is not None
            await asyncio.wait_for(mgr._task, timeout=10)
            return mgr, mgr.status()

        mgr, status = asyncio.run(run_manager())
        assert mgr.state == STATE_AUTH_FAILED
        assert status["state"] == STATE_AUTH_FAILED
        assert status["auth_permanent"] is True
        assert status["auth_rejections"] == max_rejections
        assert status["next_retry_in_s"] is None
        assert rejecting_ews_server.count() == max_rejections
        assert len(sleeps) == max_rejections - 1
        assert all(s >= cooldown for s in sleeps)
    finally:
        gw1._pool.shutdown(wait=False, cancel_futures=True)
        if gw2 is not None:
            gw2._pool.shutdown(wait=False, cancel_futures=True)


def test_e2e_interleaved_gateways_sharing_cached_protocol_never_spin(
    tmp_path, rejecting_ews_server
):
    cooldown = 600
    max_rejections = 3
    settings = Settings(
        ews_server_url=rejecting_ews_server.url,
        ews_email="user@example.com",
        ews_password="wrong-password",
        ews_auth_type_force="basic",
        ews_retry_max_wait_seconds=300,
        ews_auth_failure_cooldown_seconds=cooldown,
        ews_auth_max_rejections=max_rejections,
        data_dir=str(tmp_path / "data"),
        request_timeout=5,
    )

    clock_a = {"t": 3_000_000.0}
    clock_b = {"t": 3_000_000.0}
    gw_a = EWSGateway(settings, clock=lambda: clock_a["t"])
    gw_b = EWSGateway(settings, clock=lambda: clock_b["t"])
    try:
        # gw_b goes first: exchangelib now caches a Protocol for this endpoint
        # and these credentials, carrying gw_b's retry policy and callback.
        with pytest.raises(ToolError) as primed:
            gw_b.test_connection()
        assert primed.value.code == "auth_failed"
        primed_submissions = rejecting_ews_server.count()
        assert primed_submissions == 1
        assert gw_a.auth_status()["rejections"] == 0

        sleeps: List[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            clock_a["t"] += delay
            await asyncio.sleep(0)

        async def run_manager_a():
            mgr = ConnectionManager(gw_a, clock=lambda: clock_a["t"], sleep=fake_sleep)
            await mgr.start()
            assert mgr._task is not None
            await asyncio.wait_for(mgr._task, timeout=10)
            return mgr.status()

        # gw_a is handed the cached Protocol. Its rejections must be counted on
        # its OWN breaker, and the loop must stop by itself at the cap.
        status_a = asyncio.run(run_manager_a())
        assert status_a["state"] == STATE_AUTH_FAILED
        assert status_a["auth_permanent"] is True
        assert status_a["auth_rejections"] == max_rejections
        assert rejecting_ews_server.count() - primed_submissions == max_rejections
        assert len(sleeps) == max_rejections - 1
        assert all(s >= cooldown for s in sleeps)
    finally:
        gw_a._pool.shutdown(wait=False, cancel_futures=True)
        gw_b._pool.shutdown(wait=False, cancel_futures=True)
