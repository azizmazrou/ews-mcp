"""Cross-process sync lease + leader/follower lifecycle tests."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from conftest import make_settings
from ewsmcp.cache import CacheStore, SyncLease
from ewsmcp.cache.lease import LOCK_FILENAME
from ewsmcp.gateway.client import EWSGateway
from ewsmcp.server import (
    build_context,
    start_connection_manager,
    stop_connection_manager,
)
from ewsmcp.tools.base import dispatch

CHILD_HOLD_SCRIPT = """
import os
import sys
from ewsmcp.cache.lease import SyncLease

lease = SyncLease(sys.argv[1])
if not lease.try_acquire():
    sys.stdout.write("BUSY\\n")
    sys.stdout.flush()
    sys.exit(2)
# The holder reports its own pid: with a virtual environment on Windows,
# sys.executable is a launcher and Popen.pid is the launcher's pid.
sys.stdout.write(f"LOCKED {os.getpid()}\\n")
sys.stdout.flush()
sys.stdin.readline()
"""


def _make_message_row(ews_id: str, *, sender_email: str = "peer@example.com") -> dict:
    subject = "Quarterly review"
    sender_name = "Peer"
    body = "Shared mirror body content."
    return {
        "ews_id": ews_id,
        "changekey": "CK-1",
        "folder": "inbox",
        "conversation_id": "CONV-1",
        "sender_name": sender_name,
        "sender_email": sender_email,
        "to_json": json.dumps(["exec@example.com"]),
        "subject": subject,
        "date_ts": int(time.time()),
        "date_iso": "2026-07-01T09:00:00+00:00",
        "is_read": 1,
        "has_attachments": 0,
        "importance": None,
        "categories_json": "[]",
        "body_clean": body,
        "internet_message_id": f"<{ews_id}@example.com>",
        "norm_text": CacheStore.norm_for_row(subject, sender_name, sender_email, body),
    }


class FakeSyncEngine:
    """In-memory SyncEngine stand-in with zero network or sleep delay."""

    instances: list["FakeSyncEngine"] = []

    def __init__(self, settings, gateway, store, *, semantic=None) -> None:
        self.settings = settings
        self.gateway = gateway
        self.store = store
        self.semantic = semantic
        self.started = False
        self.stopped = False
        FakeSyncEngine.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def status(self) -> dict:
        return {"cycles": 3, "last_cycle_age_s": 1, "last_error": None}


async def _wait_until(predicate, *, timeout: float = 2.0, step: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    assert predicate(), "condition was not met before timeout"


def _close_ctx_cache(ctx) -> None:
    if ctx.cache is not None:
        ctx.cache.close()


def _child_env() -> dict[str, str]:
    v5_root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{v5_root}{os.pathsep}{existing}" if existing else v5_root
    return env


def test_same_process_two_leases_contend_and_hand_off(tmp_path):
    lock_path = tmp_path / "cache" / LOCK_FILENAME
    first = SyncLease(lock_path, retry_seconds=0.05)
    second = SyncLease(lock_path, retry_seconds=0.05)
    try:
        assert first.held is False
        assert first.try_acquire() is True
        assert first.held is True
        # Idempotent when already held by the same SyncLease instance.
        assert first.try_acquire() is True
        assert lock_path.read_text(encoding="ascii").strip() == str(os.getpid())

        assert second.try_acquire() is False
        assert second.held is False

        first.release()
        assert first.held is False
        first.release()  # idempotent release

        assert second.try_acquire() is True
        assert second.held is True
    finally:
        first.release()
        second.release()
    assert lock_path.exists()


def test_cross_process_lease_blocks_parent_until_child_exits(tmp_path):
    lock_path = tmp_path / "cache" / LOCK_FILENAME
    env = _child_env()

    for kill_child in (False, True):
        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD_HOLD_SCRIPT, str(lock_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        parent = SyncLease(lock_path)
        try:
            assert proc.stdout is not None
            banner = proc.stdout.readline().split()
            assert banner[:1] == ["LOCKED"], (
                f"child failed to lock: {proc.stderr.read() if proc.stderr else ''}"
            )
            holder_pid = banner[1]
            assert holder_pid != str(os.getpid())
            assert lock_path.read_text(encoding="ascii").strip() == holder_pid
            assert parent.try_acquire() is False
            assert parent.held is False

            if kill_child:
                proc.kill()
            else:
                assert proc.stdin is not None
                proc.stdin.close()
            proc.wait(timeout=5)

            # The OS frees the lock when the holder is gone. Through a
            # virtual-environment launcher, Popen.wait() returns for the
            # launcher while the interpreter it started is still being torn
            # down, so allow a moment instead of asserting at once.
            deadline = time.monotonic() + 5
            while not parent.try_acquire() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert parent.held is True
        finally:
            parent.release()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def test_leader_starts_sync_and_follower_takes_over_on_release(monkeypatch):
    FakeSyncEngine.instances.clear()
    monkeypatch.setattr(EWSGateway, "test_connection", lambda self: True)
    monkeypatch.setattr("ewsmcp.server.SyncEngine", FakeSyncEngine)
    settings = make_settings(ews_tz="UTC", ews_heartbeat_seconds=0)

    async def scenario() -> None:
        ctx1 = build_context(settings, lease_retry_seconds=0.02)
        ctx2 = build_context(settings, lease_retry_seconds=0.02)
        try:
            assert ctx1.lease is not None and ctx1.lease.held is True
            assert ctx2.lease is not None and ctx2.lease.held is False

            await start_connection_manager(ctx1)
            await start_connection_manager(ctx2)
            await _wait_until(lambda: ctx1.sync is not None and ctx1.sync.started)

            # Let several follower retry intervals pass while leader holds the lease.
            await asyncio.sleep(0.06)
            assert ctx2.sync is None
            assert ctx2.lease.held is False
            assert len(FakeSyncEngine.instances) == 1

            # Leader shuts down and releases its lease; follower takes over on retry.
            await stop_connection_manager(ctx1)
            assert ctx1.lease.held is False
            assert ctx1.sync.stopped is True

            await _wait_until(
                lambda: ctx2.lease.held and ctx2.sync is not None and ctx2.sync.started
            )
            assert len(FakeSyncEngine.instances) == 2
        finally:
            await stop_connection_manager(ctx2)
            await stop_connection_manager(ctx1)
            _close_ctx_cache(ctx2)
            _close_ctx_cache(ctx1)

    asyncio.run(scenario())


def test_follower_takeover_waits_until_exchange_is_warm(monkeypatch):
    FakeSyncEngine.instances.clear()
    probe_entered = threading.Event()
    allow_warm = threading.Event()

    def gated_probe(self) -> bool:
        probe_entered.set()
        allow_warm.wait(timeout=2.0)
        return True

    monkeypatch.setattr(EWSGateway, "test_connection", gated_probe)
    monkeypatch.setattr("ewsmcp.server.SyncEngine", FakeSyncEngine)
    settings = make_settings(ews_tz="UTC", ews_heartbeat_seconds=0)

    async def scenario() -> None:
        ctx1 = build_context(settings, lease_retry_seconds=0.02)
        ctx2 = build_context(settings, lease_retry_seconds=0.02)
        try:
            await start_connection_manager(ctx2)
            await _wait_until(probe_entered.is_set)

            # Leader releases while follower is still cold (Exchange probe blocked).
            ctx1.lease.release()
            await _wait_until(lambda: ctx2.lease is not None and ctx2.lease.held)
            assert ctx2.sync is None

            # Once Exchange answers, the promoted follower starts SyncEngine.
            allow_warm.set()
            await _wait_until(lambda: ctx2.sync is not None and ctx2.sync.started)
        finally:
            allow_warm.set()
            await stop_connection_manager(ctx2)
            await stop_connection_manager(ctx1)
            _close_ctx_cache(ctx2)
            _close_ctx_cache(ctx1)

    asyncio.run(scenario())


def test_purge_on_boot_runs_for_leader_and_is_skipped_by_follower(caplog):
    base_settings = make_settings(ews_tz="UTC")
    seed = CacheStore(f"{base_settings.data_dir}/cache/mirror.db")
    seed.upsert_messages([_make_message_row("M-OLD")])
    assert seed.stats()["rows"]["messages"] == 1
    seed.close()

    purge_settings = make_settings(ews_tz="UTC", ews_cache_purge_on_boot=True)
    leader = build_context(purge_settings)
    follower = None
    try:
        assert leader.lease is not None and leader.lease.held is True
        assert leader.cache is not None
        assert leader.cache.stats()["rows"]["messages"] == 0

        now = int(time.time())
        leader.cache.upsert_messages([_make_message_row("M-SHARED")])
        leader.cache.set_sync_state("item:inbox", "TOK-INBOX", now)
        leader.cache.set_sync_state("item:sent", "TOK-SENT", now)

        with caplog.at_level(logging.WARNING):
            follower = build_context(purge_settings)

        assert follower.lease is not None and follower.lease.held is False
        assert follower.cache is not None
        assert follower.cache.stats()["rows"]["messages"] == 1
        assert any(
            rec.levelname == "WARNING" and "purge" in rec.message.lower()
            for rec in caplog.records
        )

        # Follower still serves cache-first reads from the shared mirror.
        res = asyncio.run(
            dispatch(
                follower,
                follower.registry["search_messages"],
                {"sender": "peer@example.com", "limit": 5},
            )
        )
        assert res["ok"] is True
        assert res["source"] == "cache"
        assert res["count"] == 1
    finally:
        if follower is not None:
            if follower.lease is not None:
                follower.lease.release()
            _close_ctx_cache(follower)
        if leader.lease is not None:
            leader.lease.release()
        _close_ctx_cache(leader)


def test_get_server_status_reports_leader_and_follower_roles(monkeypatch):
    FakeSyncEngine.instances.clear()
    monkeypatch.setattr(EWSGateway, "test_connection", lambda self: True)
    monkeypatch.setattr("ewsmcp.server.SyncEngine", FakeSyncEngine)
    settings = make_settings(ews_tz="UTC", ews_heartbeat_seconds=0)

    async def scenario() -> None:
        leader = build_context(settings, lease_retry_seconds=0.02)
        follower = build_context(settings, lease_retry_seconds=0.02)
        try:
            cold_leader = await dispatch(
                leader, leader.registry["get_server_status"], {}
            )
            cold_follower = await dispatch(
                follower, follower.registry["get_server_status"], {}
            )
            assert cold_leader["cache"]["sync"] == {"role": "leader"}
            assert cold_follower["cache"]["sync"] == {"role": "follower"}

            await start_connection_manager(leader)
            await start_connection_manager(follower)
            await _wait_until(lambda: leader.sync is not None and leader.sync.started)

            warm_leader = await dispatch(
                leader, leader.registry["get_server_status"], {}
            )
            warm_follower = await dispatch(
                follower, follower.registry["get_server_status"], {}
            )
            assert warm_leader["cache"]["sync"] == {
                "cycles": 3,
                "last_cycle_age_s": 1,
                "last_error": None,
                "role": "leader",
            }
            assert warm_follower["cache"]["sync"] == {"role": "follower"}

            await stop_connection_manager(leader)
            await _wait_until(
                lambda: follower.sync is not None and follower.sync.started
            )
            promoted = await dispatch(
                follower, follower.registry["get_server_status"], {}
            )
            assert promoted["cache"]["sync"] == {
                "cycles": 3,
                "last_cycle_age_s": 1,
                "last_error": None,
                "role": "leader",
            }
        finally:
            await stop_connection_manager(follower)
            await stop_connection_manager(leader)
            _close_ctx_cache(follower)
            _close_ctx_cache(leader)

    asyncio.run(scenario())


def test_unopenable_lock_file_logs_once_and_degrades_to_running_sync(
    monkeypatch, caplog
):
    FakeSyncEngine.instances.clear()
    monkeypatch.setattr(EWSGateway, "test_connection", lambda self: True)
    monkeypatch.setattr("ewsmcp.server.SyncEngine", FakeSyncEngine)
    settings = make_settings(ews_tz="UTC", ews_heartbeat_seconds=0)

    # Making sync.lock a directory forces os.open(..., O_RDWR) to fail with
    # OSError on both Windows and POSIX (even as root), while mirror.db works.
    bad_lock = Path(settings.data_dir) / "cache" / LOCK_FILENAME
    bad_lock.mkdir(parents=True, exist_ok=True)

    async def scenario() -> None:
        with caplog.at_level(logging.ERROR):
            ctx = build_context(settings)
        try:
            lease_errors = [
                rec
                for rec in caplog.records
                if rec.levelname == "ERROR" and "lease" in rec.message.lower()
            ]
            assert len(lease_errors) == 1
            assert ctx.cache is not None
            assert ctx.lease is None

            await start_connection_manager(ctx)
            await _wait_until(lambda: ctx.sync is not None and ctx.sync.started)
            status = await dispatch(ctx, ctx.registry["get_server_status"], {})
            assert status["cache"]["sync"]["role"] == "leader"
            assert status["cache"]["sync"]["cycles"] == 3
        finally:
            await stop_connection_manager(ctx)
            _close_ctx_cache(ctx)

    asyncio.run(scenario())


def test_cache_disabled_creates_no_lock_file(monkeypatch):
    monkeypatch.setattr(EWSGateway, "test_connection", lambda self: True)
    settings = make_settings(
        ews_tz="UTC",
        ews_cache_enabled=False,
        ews_heartbeat_seconds=0,
    )
    cache_dir = Path(settings.data_dir) / "cache"
    lock_path = cache_dir / LOCK_FILENAME

    async def scenario() -> None:
        ctx = build_context(settings)
        try:
            assert ctx.cache is None
            assert ctx.lease is None
            await start_connection_manager(ctx)
            await _wait_until(
                lambda: ctx.manager is not None and ctx.manager.is_warm
            )
            assert ctx.sync is None
            assert not lock_path.exists()
            assert not cache_dir.exists()

            status = await dispatch(ctx, ctx.registry["get_server_status"], {})
            assert status["cache"] == {"enabled": False, "ready": False}
        finally:
            await stop_connection_manager(ctx)

    asyncio.run(scenario())
