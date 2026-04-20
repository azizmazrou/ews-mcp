"""Tests for the SSE keepalive / long-running-tool resilience stack.

Scope:
  * SSE comment-frame heartbeat emits through a serialised send lock.
  * Keepalive loop cancels cleanly on peer disconnect.
  * API-key auth uses hmac.compare_digest for both Bearer and X-API-Key.
  * Settings clamp out-of-range transport intervals.
  * Active-SSE-connection counter increments/decrements.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

from src.main import (
    ASGISend,
    _authorized_request,
    _enable_tcp_keepalive,
    _keepalive_loop,
    _merge_sse_headers,
    _peer_gone,
    _sse_active_count,
    _wrap_send_with_sse_headers,
)


# ---------------------------------------------------------------------------
# Keepalive frames
# ---------------------------------------------------------------------------


class _FakeASGI:
    """Captures every ASGI event plus the order of sends."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self.raise_after: int = 0
        self.raise_exc: BaseException = ConnectionResetError("peer closed")

    async def __call__(self, message: Dict[str, Any]) -> None:
        self.events.append(message)
        if self.raise_after and len(self.events) >= self.raise_after:
            raise self.raise_exc


@pytest.mark.asyncio
async def test_keepalive_loop_emits_comment_frames_after_headers():
    sink = _FakeASGI()
    wrapped, headers_sent, lock = _wrap_send_with_sse_headers(sink)

    # Simulate the SDK flushing its response headers first.
    await wrapped({
        "type": "http.response.start", "status": 200, "headers": []
    })
    assert headers_sent.is_set()

    # Tight interval so the test finishes fast.
    task = asyncio.create_task(_keepalive_loop(
        wrapped, lock, headers_sent,
        interval_seconds=1, logger=logging.getLogger("test"),
    ))
    # Need > 3 * interval to observe 3 frames.
    await asyncio.sleep(3.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # First event is the headers. Subsequent comment frames are bodies
    # carrying exactly ``: keepalive <epoch>\n\n``.
    body_events = [e for e in sink.events if e.get("type") == "http.response.body"]
    assert len(body_events) >= 3, sink.events
    for ev in body_events:
        payload = ev["body"].decode("ascii")
        assert payload.startswith(": keepalive "), payload
        assert payload.endswith("\n\n")
        # No user-controlled content: last token is an integer epoch.
        int(payload[len(": keepalive "):].strip())
        # Non-final chunk so HTTP/1.1 chunked framing stays open.
        assert ev.get("more_body") is True


@pytest.mark.asyncio
async def test_keepalive_waits_for_headers_before_first_frame():
    """Sending a body before ``http.response.start`` would 500 the request;
    the loop must block until ``headers_sent`` is set."""
    sink = _FakeASGI()
    wrapped, headers_sent, lock = _wrap_send_with_sse_headers(sink)

    task = asyncio.create_task(_keepalive_loop(
        wrapped, lock, headers_sent, interval_seconds=1, logger=logging.getLogger("test"),
    ))
    # Leave headers_sent unset briefly; no body should appear.
    await asyncio.sleep(1.5)
    assert not any(e.get("type") == "http.response.body" for e in sink.events)

    # Now flush headers and let the loop catch up.
    await wrapped({"type": "http.response.start", "status": 200, "headers": []})
    await asyncio.sleep(1.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    body_events = [e for e in sink.events if e.get("type") == "http.response.body"]
    assert body_events, sink.events


@pytest.mark.asyncio
async def test_keepalive_serialises_through_lock_with_concurrent_writer():
    """Simulate a concurrent fake MCP writer while the keepalive runs.
    The lock must ensure no two writes interleave inside one SSE event."""
    sink = _FakeASGI()
    wrapped, headers_sent, lock = _wrap_send_with_sse_headers(sink)
    await wrapped({"type": "http.response.start", "status": 200, "headers": []})

    alive = asyncio.create_task(_keepalive_loop(
        wrapped, lock, headers_sent, interval_seconds=1, logger=logging.getLogger("test"),
    ))

    async def _sdk_writer() -> None:
        # Simulate the SDK writing 10 "event:" style chunks.
        for i in range(10):
            await wrapped({
                "type": "http.response.body",
                "body": f"event: data-{i}\n\n".encode(),
                "more_body": True,
            })
            await asyncio.sleep(0.05)

    await _sdk_writer()
    # Give the keepalive one more tick.
    await asyncio.sleep(1.1)
    alive.cancel()
    try:
        await alive
    except asyncio.CancelledError:
        pass

    # Every body event starts cleanly with either "event:" or ": keepalive";
    # no interleaved bytes from two writers sharing a frame.
    for e in (ev for ev in sink.events if ev.get("type") == "http.response.body"):
        body = e["body"].decode("ascii")
        assert body.startswith("event:") or body.startswith(": keepalive"), body


@pytest.mark.asyncio
async def test_keepalive_skips_tick_when_lock_contended():
    """If the SDK holds the lock when the tick fires, the loop must
    skip rather than queue — prevents memory pressure from a slow client."""
    sink = _FakeASGI()
    wrapped, headers_sent, lock = _wrap_send_with_sse_headers(sink)
    await wrapped({"type": "http.response.start", "status": 200, "headers": []})

    async def _hold_lock_long() -> None:
        async with lock:
            # Hold for longer than our observation window so the
            # keepalive never sees the release.
            await asyncio.sleep(5.0)

    holder = asyncio.create_task(_hold_lock_long())
    task = asyncio.create_task(_keepalive_loop(
        wrapped, lock, headers_sent, interval_seconds=1, logger=logging.getLogger("test"),
    ))
    # Observe for 3s — spans 3 would-be ticks while the holder is still
    # firmly holding the lock. Stop measuring before the holder releases.
    await asyncio.sleep(3.0)

    # Snapshot BEFORE the holder releases.
    snapshot = list(sink.events)

    # Now wind down the holder + task so we don't leak.
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    holder.cancel()
    try:
        await holder
    except asyncio.CancelledError:
        pass

    # With the lock held for the full observation window, zero
    # keepalive bodies should have landed (the loop skipped every tick).
    bodies = [
        e for e in snapshot
        if e.get("type") == "http.response.body"
        and e.get("body", b"").startswith(b": keepalive")
    ]
    debug = [(e.get("type"), e.get("body", b"")[:40]) for e in snapshot]
    assert bodies == [], debug


@pytest.mark.asyncio
async def test_keepalive_exits_cleanly_on_peer_disconnect(caplog):
    """ClosedResourceError / BrokenResourceError / ConnectionResetError
    must stop the loop at DEBUG without propagating."""
    sink = _FakeASGI()
    sink.raise_after = 2  # first body write -> 2nd event total (start=1)
    sink.raise_exc = ConnectionResetError("peer")

    wrapped, headers_sent, lock = _wrap_send_with_sse_headers(sink)
    await wrapped({"type": "http.response.start", "status": 200, "headers": []})

    with caplog.at_level(logging.DEBUG, logger="test"):
        task = asyncio.create_task(_keepalive_loop(
            wrapped, lock, headers_sent, interval_seconds=1,
            logger=logging.getLogger("test"),
        ))
        await asyncio.sleep(2.1)
        # Loop must have exited on its own.
        assert task.done()
        # Did not raise.
        assert task.exception() is None

    # At least one DEBUG line about peer closing.
    assert any(
        "peer closed" in r.message and r.levelno == logging.DEBUG
        for r in caplog.records
    ), caplog.records


@pytest.mark.asyncio
async def test_keepalive_no_user_input_in_frame():
    """Defence: no user-controlled data reaches the keepalive frame."""
    sink = _FakeASGI()
    wrapped, headers_sent, lock = _wrap_send_with_sse_headers(sink)
    await wrapped({"type": "http.response.start", "status": 200, "headers": []})

    task = asyncio.create_task(_keepalive_loop(
        wrapped, lock, headers_sent, interval_seconds=1,
        logger=logging.getLogger("test"),
    ))
    await asyncio.sleep(2.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    for e in sink.events:
        if e.get("type") == "http.response.body":
            body = e["body"]
            # No CR/LF outside the framing \n\n suffix. Enforces "static
            # prefix + monotonic epoch" contract.
            assert body.startswith(b": keepalive ")
            assert body.endswith(b"\n\n")
            assert body.count(b"\n") == 2  # exactly the framing pair
            assert b"\r" not in body


def test_merge_sse_headers_injects_proxy_busters_without_duplicate():
    sdk_headers: List[Tuple[bytes, bytes]] = [
        (b"content-type", b"text/event-stream"),
        (b"cache-control", b"max-age=0"),  # SDK already set; must NOT duplicate
    ]
    merged = _merge_sse_headers(sdk_headers)
    names = [h[0] for h in merged]
    # Existing SDK values preserved.
    assert names.count(b"cache-control") == 1
    assert [h for h in merged if h[0] == b"cache-control"][0][1] == b"max-age=0"
    # We ALWAYS add X-Accel-Buffering: no.
    assert [b"x-accel-buffering", b"no"] in merged
    # Connection: keep-alive always present.
    assert [b"connection", b"keep-alive"] in merged


# ---------------------------------------------------------------------------
# Auth: constant-time compare
# ---------------------------------------------------------------------------


def test_authorized_returns_true_when_no_api_key_configured():
    assert _authorized_request([], api_key=None) is True
    assert _authorized_request([(b"x-foo", b"bar")], api_key=None) is True


def test_authorized_bearer_uses_compare_digest(monkeypatch):
    calls: List[Tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def _spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr("src.main.hmac.compare_digest", _spy)
    ok = _authorized_request(
        [(b"authorization", b"Bearer secret-key-1234567890")],
        api_key="secret-key-1234567890",
    )
    assert ok is True
    assert calls, "compare_digest was never consulted"


def test_authorized_x_api_key_uses_compare_digest(monkeypatch):
    calls: List[Tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def _spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)  # bind the real one, not the patched symbol

    monkeypatch.setattr("src.main.hmac.compare_digest", _spy)
    ok = _authorized_request(
        [(b"x-api-key", b"secret-key-1234567890")],
        api_key="secret-key-1234567890",
    )
    assert ok is True
    assert calls, "compare_digest was never consulted"


def test_authorized_rejects_same_length_different_last_byte():
    assert _authorized_request(
        [(b"authorization", b"Bearer secret-key-1234567890")],
        api_key="secret-key-1234567891",  # last byte differs
    ) is False


def test_authorized_rejects_missing_bearer_prefix():
    assert _authorized_request(
        [(b"authorization", b"secret-key")],
        api_key="secret-key",
    ) is False


# ---------------------------------------------------------------------------
# Config clamping
# ---------------------------------------------------------------------------


def _make_settings(**overrides):
    """Construct a Settings with auth requirements satisfied."""
    import os
    for key in (
        "EWS_EMAIL", "EWS_AUTH_TYPE", "EWS_USERNAME", "EWS_PASSWORD",
        "EWS_AUTODISCOVER", "EWS_SERVER_URL",
    ):
        os.environ.pop(key, None)
    from src.config import Settings
    kwargs = dict(
        ews_email="dev@example.com",
        ews_auth_type="basic",
        ews_username="dev",
        ews_password="x",
        ews_autodiscover=False,
        ews_server_url="https://mail.example.com/EWS/Exchange.asmx",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def test_settings_clamps_sse_interval_low(caplog):
    with caplog.at_level(logging.WARNING, logger="src.config"):
        s = _make_settings(sse_keepalive_interval_seconds=0)
    assert s.sse_keepalive_interval_seconds == 5
    assert any("below minimum" in r.message for r in caplog.records)


def test_settings_clamps_sse_interval_high(caplog):
    with caplog.at_level(logging.WARNING, logger="src.config"):
        s = _make_settings(sse_keepalive_interval_seconds=3600)
    assert s.sse_keepalive_interval_seconds == 60


def test_settings_clamps_http_keep_alive(caplog):
    s = _make_settings(http_keep_alive_timeout_seconds=5)
    assert s.http_keep_alive_timeout_seconds == 30
    s = _make_settings(http_keep_alive_timeout_seconds=10_000)
    assert s.http_keep_alive_timeout_seconds == 900


def test_settings_clamps_progress_interval(caplog):
    s = _make_settings(progress_notification_interval_seconds=1)
    assert s.progress_notification_interval_seconds == 5
    s = _make_settings(progress_notification_interval_seconds=999)
    assert s.progress_notification_interval_seconds == 60


def test_settings_valid_values_pass_through():
    s = _make_settings(
        sse_keepalive_interval_seconds=15,
        http_keep_alive_timeout_seconds=300,
        tcp_keepalive_idle_seconds=60,
        progress_notification_interval_seconds=10,
    )
    assert s.sse_keepalive_interval_seconds == 15
    assert s.http_keep_alive_timeout_seconds == 300
    assert s.tcp_keepalive_idle_seconds == 60
    assert s.progress_notification_interval_seconds == 10


# ---------------------------------------------------------------------------
# Peer-gone classifier
# ---------------------------------------------------------------------------


def test_peer_gone_matches_known_types():
    class ClosedResourceError(Exception):
        pass

    class BrokenResourceError(Exception):
        pass

    assert _peer_gone(ClosedResourceError()) is True
    assert _peer_gone(BrokenResourceError()) is True
    assert _peer_gone(ConnectionResetError()) is True
    assert _peer_gone(BrokenPipeError()) is True
    assert _peer_gone(RuntimeError("Broken pipe: sent 0")) is True
    assert _peer_gone(RuntimeError("connection aborted by peer")) is False  # case-sensitive check; message must have 'Connection aborted'
    assert _peer_gone(RuntimeError("Connection aborted by peer")) is True
    assert _peer_gone(ValueError("totally unrelated")) is False


# ---------------------------------------------------------------------------
# Active-connection counter
# ---------------------------------------------------------------------------


def test_sse_counter_starts_at_zero_or_stable():
    # Ensures the counter API exists and is safe to read concurrently.
    baseline = _sse_active_count()
    assert baseline >= 0


# ---------------------------------------------------------------------------
# TCP keepalive setup
# ---------------------------------------------------------------------------


def test_enable_tcp_keepalive_sets_so_keepalive(monkeypatch):
    """Basic plumbing: SO_KEEPALIVE is always set on a supported socket."""
    import socket as _socket

    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        _enable_tcp_keepalive(sock, idle_seconds=60, logger=logging.getLogger("t"))
        # Read back SO_KEEPALIVE; kernel may report 0 or 1.
        val = sock.getsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE)
        assert val == 1
    finally:
        sock.close()


def test_enable_tcp_keepalive_swallows_oserror():
    """A closed socket must not crash the helper — best-effort only."""
    import socket as _socket
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.close()
    # Should not raise even though the socket is closed.
    _enable_tcp_keepalive(sock, idle_seconds=60, logger=logging.getLogger("t"))
