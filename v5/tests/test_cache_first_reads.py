"""Cache-first read contract: mirror answers with provenance, fresh=true
forces live, cache errors fall back live, writes patch the mirror.

The gateway in these tests RAISES on contact — proving that a cache-served
read never touches Exchange.
"""

import asyncio
import time

from conftest import make_settings

from ewsmcp.audit import AuditLog
from ewsmcp.cache.store import CacheStore
from ewsmcp.ids import get_aliaser
from ewsmcp.tools import build_registry
from ewsmcp.tools.base import Context, dispatch

from test_cache_store import make_row


class NoTouchGateway:
    """Raises if any tool actually reaches for Exchange."""

    async def call(self, fn):
        raise AssertionError("EWS was contacted — cache path failed")

    def resolve_folder(self, account, ref, aliaser):
        raise AssertionError("EWS folder resolution — cache path failed")


class RecordingGateway:
    def __init__(self, account=None):
        self.account = account
        self.calls = 0

    async def call(self, fn):
        self.calls += 1
        return fn(self.account)


def seeded_store(tmp_path):
    store = CacheStore(tmp_path / "mirror.db")
    now = int(time.time())
    store.upsert_messages([
        make_row("RAW-1", subject="Budget review", sender_email="a@corp.example",
                 body="please review the numbers", date_ts=now - 300,
                 conv="C1", is_read=0),
        make_row("RAW-2", subject="Re: Budget review", folder="sent",
                 sender_email="exec@corp.example", body="looks good",
                 date_ts=now - 200, conv="C1"),
        make_row("RAW-3", subject="Lunch", sender_email="b@corp.example",
                 body="see you at noon", date_ts=now - 100, conv="C2"),
    ])
    store.set_sync_state("item:inbox", "TOK", now)
    store.set_sync_state("item:sent", "TOK", now)
    store.set_sync_state("events", None, now)
    store.replace_events([{
        "ews_id": "EV1", "changekey": None, "subject": "Standup",
        "start_ts": now - 60, "start_iso": "2026-07-10T09:00+03:00",
        "end_ts": now + 3600, "end_iso": "2026-07-10T10:00+03:00",
        "location": None, "organizer": None, "is_recurring": 0,
        "my_response": None,
    }])
    store.replace_folders([{
        "ews_id": "F-IN", "name": "Inbox", "path": "Inbox", "wk": "f:inbox",
        "total": 3, "unread": 1, "children": 0, "folder_class": "IPF.Note",
    }, {
        # no folder_class key at all: Exchange did not report one
        "ews_id": "F-AR", "name": "Archive", "path": "Archive", "wk": None,
        "total": 2, "unread": 0, "children": 0,
    }])
    return store


def _ctx(tmp_path, gateway, **overrides) -> Context:
    ctx = Context(
        settings=make_settings(**overrides),
        gateway=gateway,
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "alias")),
        audit=AuditLog(str(tmp_path / "audit")),
        cache=seeded_store(tmp_path),
    )
    build_registry(ctx)
    return ctx


def _run(ctx, name, **kwargs):
    return asyncio.run(dispatch(ctx, ctx.registry[name], dict(kwargs)))


def test_search_served_from_mirror_with_provenance(tmp_path):
    ctx = _ctx(tmp_path, NoTouchGateway())
    res = _run(ctx, "search_messages", query="budget")
    assert res["source"] == "cache" and res["as_of"]
    assert res["count"] == 1
    assert res["total_available"] == 1  # exact — COUNT(*) is free locally
    assert res["items"][0]["subject"] == "Budget review"
    assert res["items"][0]["id"].startswith("m")  # alias, never a raw id


def test_search_sender_filter_from_mirror(tmp_path):
    ctx = _ctx(tmp_path, NoTouchGateway())
    res = _run(ctx, "search_messages", sender="a@corp", limit=1)
    assert res["source"] == "cache"
    assert res["count"] == 1
    assert res["items"][0]["unread"] is True


def test_get_message_from_mirror(tmp_path):
    ctx = _ctx(tmp_path, NoTouchGateway())
    res = _run(ctx, "get_message", id="RAW-1")
    assert res["source"] == "cache"
    assert res["message"]["body"].startswith("please review")
    assert res["message"]["subject"] == "Budget review"


def test_get_thread_local_conversation_join(tmp_path):
    ctx = _ctx(tmp_path, NoTouchGateway())
    res = _run(ctx, "get_thread", id="RAW-1")
    assert res["source"] == "cache"
    assert res["count"] == 2  # inbox + sent halves of C1
    assert [e["from"] for e in res["items"]] == ["a@corp.example",
                                                 "exec@corp.example"]


def test_overview_pure_mirror(tmp_path):
    ctx = _ctx(tmp_path, NoTouchGateway())
    res = _run(ctx, "get_mailbox_overview")
    assert res["source"] == "cache" and res["as_of"]
    assert res["unread_total"] == 1
    assert res["recent_unread"][0]["subject"] == "Budget review"
    assert res["today_events"][0]["subject"] == "Standup"


def test_list_folders_from_mirror(tmp_path):
    ctx = _ctx(tmp_path, NoTouchGateway())
    res = _run(ctx, "list_folders")
    assert res["source"] == "cache"
    by_path = {r["path"]: r for r in res["items"]}
    assert by_path["Inbox"]["wk"] == "f:inbox"
    assert by_path["Inbox"]["unread"] == 1
    assert by_path["Inbox"]["folder_class"] == "IPF.Note"
    assert "folder_class" not in by_path["Archive"]


def test_fresh_true_forces_live(tmp_path):
    ctx = _ctx(tmp_path, NoTouchGateway())
    res = _run(ctx, "get_message", id="RAW-1", fresh=True)
    # NoTouchGateway raises AssertionError → mapped upstream error — which
    # is exactly the proof that fresh=true went to Exchange.
    assert res["ok"] is False


def test_uncached_folder_goes_live(tmp_path):
    ctx = _ctx(tmp_path, NoTouchGateway())
    res = _run(ctx, "search_messages", folder="f:junk")
    assert res["ok"] is False  # gateway raised → live path was chosen


def test_cache_error_falls_back_to_live(tmp_path):
    account = None
    gateway = RecordingGateway()

    class _Query(list):
        total_count = 0

        def filter(self, *a, **k):
            return self

        def only(self, *a):
            return self

        def order_by(self, *a):
            return self

        def refresh(self):
            pass

    from unittest.mock import MagicMock
    account = MagicMock()
    account.inbox = _Query()
    gateway.account = account
    gateway.resolve_folder = lambda acc, ref, aliaser: account.inbox
    ctx = _ctx(tmp_path, gateway)
    ctx.cache.close()

    def boom(**kwargs):
        raise RuntimeError("mirror unavailable")

    ctx.cache.search_messages = boom
    res = _run(ctx, "search_messages")
    assert res["ok"] is True
    assert res["source"] == "live"
    assert gateway.calls == 1


def test_write_through_update_and_delete(tmp_path):
    from types import SimpleNamespace

    class Item:
        def __init__(self, raw_id):
            self.id = raw_id
            self.is_read = False
            self.categories = None

        def save(self, **kwargs):
            return None

        def move_to_trash(self):
            return None

    items = {"RAW-1": Item("RAW-1")}
    account = SimpleNamespace()
    account.fetch = lambda pairs, only_fields=None: [items[i] for i, _ in pairs]
    gateway = RecordingGateway(account)
    ctx = _ctx(tmp_path, gateway, ews_capability_tier="full")

    res = _run(ctx, "update_messages", ids=["RAW-1"], set_read=True)
    assert res["updated"] == 1
    assert ctx.cache.get_message("RAW-1")["is_read"] == 1  # mirror patched

    res = _run(ctx, "delete_messages", ids=["RAW-1"])
    assert res["deleted"] == 1
    assert ctx.cache.get_message("RAW-1") is None  # tombstoned
