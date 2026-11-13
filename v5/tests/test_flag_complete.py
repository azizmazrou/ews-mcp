"""Offline tests for follow-up flag completion across search, detail, and write tools."""

import asyncio
import importlib
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from conftest import make_settings
from exchangelib import Message

from ewsmcp import flag_property
from ewsmcp.audit import AuditLog
from ewsmcp.cache.store import CacheStore
from ewsmcp.errors import ToolError
from ewsmcp.ids import get_aliaser
from ewsmcp.tools import mail_read, writes
from ewsmcp.tools.base import Context, dispatch

TZ = ZoneInfo("Asia/Riyadh")
READ_SPECS = {spec.name: spec for spec in mail_read.TOOLS}
WRITE_SPECS = {spec.name: spec for spec in writes.TOOLS}


class _FolderQuery(list):
    """Folder / QuerySet stand-in that honours flag_status and categories filters."""

    def __init__(self, items=(), *, _root=None):
        super().__init__(items)
        self._root = _root if _root is not None else self
        if _root is None:
            self.filter_calls = []
            self.exclude_calls = []
            self.count_calls = 0
            self.refresh_calls = 0
            self.total_count = len(items)

    def filter(self, *args, **kwargs):
        self._root.filter_calls.append((args, kwargs))
        items = list(self)
        if "categories__contains" in kwargs:
            wanted = set(kwargs["categories__contains"])
            items = [
                it for it in items
                if wanted.issubset(set(getattr(it, "categories", None) or []))
            ]
        if "flag_status" in kwargs:
            items = [
                it for it in items
                if getattr(it, "flag_status", None) == kwargs["flag_status"]
            ]
        return _FolderQuery(items, _root=self._root)

    def exclude(self, *args, **kwargs):
        self._root.exclude_calls.append((args, kwargs))
        items = list(self)
        if "flag_status" in kwargs:
            items = [
                it for it in items
                if getattr(it, "flag_status", None) != kwargs["flag_status"]
            ]
        return _FolderQuery(items, _root=self._root)

    def order_by(self, field):
        reverse = str(field).startswith("-")
        attr = str(field).lstrip("-")
        items = sorted(self, key=lambda it: getattr(it, attr), reverse=reverse)
        return _FolderQuery(items, _root=self._root)

    def only(self, *_fields):
        return self

    def count(self):
        self._root.count_calls += 1
        return len(self)

    def refresh(self):
        self._root.refresh_calls += 1


class _FakeGateway:
    def __init__(self, account):
        self.account = account

    async def call(self, fn):
        return fn(self.account)

    def resolve_folder(self, account, ref, aliaser):
        if not ref or ref.strip().lower() in ("f:inbox", "inbox"):
            return account.inbox
        key = aliaser.resolve(ref.strip()).strip("/").lower()
        for path, folder in account.mail_folders:
            if path.rstrip("/").lower() == key:
                return folder
        raise ToolError("not_found", f"No folder matches {ref!r}.")

    def mail_folders(self, account):
        return account.mail_folders


def _item(raw_id, subject, *, day=10, flag_status=None, categories=None):
    return SimpleNamespace(
        id=raw_id,
        changekey=f"CK-{raw_id}",
        subject=subject,
        sender=SimpleNamespace(name="Ops", email_address="ops@example.com"),
        datetime_received=datetime(2026, 6, day, 9, 0, tzinfo=TZ),
        is_read=True,
        has_attachments=False,
        text_body=f"Body for {subject}",
        message_id=f"<{raw_id}@example.com>",
        conversation_id=SimpleNamespace(id="CONV-1="),
        to_recipients=[SimpleNamespace(email_address="team@example.com")],
        cc_recipients=[],
        attachments=[],
        importance="Normal",
        body=None,
        categories=list(categories) if categories is not None else None,
        flag_status=flag_status,
        save=MagicMock(name=f"save-{raw_id}"),
    )


def _make_env(tmp_path, folders, *, cache=None):
    account = SimpleNamespace()
    account.mail_folders = list(folders.items())
    account.inbox = folders.get("Inbox", _FolderQuery())
    account.by_id = {
        it.id: it for folder in folders.values() for it in folder
    }
    account.fetch_projections = []

    def fetch(ids, only_fields=None):
        account.fetch_projections.append(only_fields)
        return [account.by_id[raw_id] for raw_id, _ck in ids]

    account.fetch = fetch
    ctx = Context(
        settings=make_settings(data_dir=str(tmp_path / "data")),
        gateway=_FakeGateway(account),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "alias")),
        audit=AuditLog(str(tmp_path / "audit")),
        cache=cache,
    )
    return ctx, account


def _read(ctx, name, **kwargs):
    return asyncio.run(dispatch(ctx, READ_SPECS[name], dict(kwargs)))


def _write(ctx, name, **kwargs):
    return asyncio.run(dispatch(ctx, WRITE_SPECS[name], dict(kwargs)))


def test_flag_complete_false_excludes_completed_in_single_recursive_and_all(tmp_path):
    inbox = _FolderQuery([
        _item("IN-DONE=", "inbox-done", day=9, flag_status=1),
        _item("IN-OPEN=", "inbox-open", day=8, flag_status=2),
        _item("IN-NONE=", "inbox-unflagged", day=2, flag_status=None),
    ])
    proj = _FolderQuery([
        _item("PR-DONE=", "proj-done", day=7, flag_status=1),
        _item("PR-NONE=", "proj-unflagged", day=6, flag_status=None),
    ])
    proj_sub = _FolderQuery([
        _item("PS-DONE=", "sub-done", day=10, flag_status=1),
        _item("PS-OPEN=", "sub-open", day=5, flag_status=2),
    ])
    ctx, _ = _make_env(tmp_path, {
        "Inbox": inbox,
        "Projects": proj,
        "Projects/Sub": proj_sub,
    })

    # 1. Single folder
    single = _read(ctx, "search_messages", folder="f:inbox", flag_complete=False)
    assert single["ok"] is True
    assert [i["subject"] for i in single["items"]] == ["inbox-open", "inbox-unflagged"]
    assert inbox.exclude_calls == [((), {"flag_status": 1})]
    assert inbox.count_calls == 0
    assert single["total_available"] is None

    # 2. Recursive subtree (Projects + Projects/Sub; Inbox excluded)
    rec = _read(ctx, "search_messages", folder="Projects",
                recursive=True, flag_complete=False)
    assert rec["ok"] is True
    assert [i["subject"] for i in rec["items"]] == ["proj-unflagged", "sub-open"]
    assert [i["folder"] for i in rec["items"]] == ["Projects", "Projects/Sub"]
    assert rec["folders_searched"] == 2
    assert proj.exclude_calls == [((), {"flag_status": 1})]
    assert proj_sub.exclude_calls == [((), {"flag_status": 1})]
    assert proj.count_calls == 0 and proj_sub.count_calls == 0

    # 3. Whole mailbox (f:all)
    whole = _read(ctx, "search_messages", folder="f:all", flag_complete=False)
    assert whole["ok"] is True
    assert [i["subject"] for i in whole["items"]] == [
        "inbox-open", "proj-unflagged", "sub-open", "inbox-unflagged",
    ]
    assert whole["folders_searched"] == 3
    assert len(inbox.exclude_calls) == 2  # once for single, once for f:all


def test_flag_complete_true_filters_in_single_recursive_and_all(tmp_path):
    inbox = _FolderQuery([
        _item("IN-DONE=", "inbox-done", day=4, flag_status=1),
        _item("IN-OPEN=", "inbox-open", day=8, flag_status=2),
        _item("IN-NONE=", "inbox-unflagged", day=2, flag_status=None),
    ])
    proj = _FolderQuery([
        _item("PR-DONE=", "proj-done", day=6, flag_status=1),
        _item("PR-NONE=", "proj-unflagged", day=5, flag_status=None),
    ])
    proj_sub = _FolderQuery([
        _item("PS-DONE=", "sub-done", day=9, flag_status=1),
        _item("PS-OPEN=", "sub-open", day=7, flag_status=2),
    ])
    ctx, _ = _make_env(tmp_path, {
        "Inbox": inbox,
        "Projects": proj,
        "Projects/Sub": proj_sub,
    })

    # 1. Single folder (count() is allowed because completed set is small)
    single = _read(ctx, "search_messages", folder="f:inbox", flag_complete=True)
    assert single["ok"] is True
    assert [i["subject"] for i in single["items"]] == ["inbox-done"]
    assert single["total_available"] == 1
    assert inbox.count_calls == 1
    assert inbox.filter_calls == [((), {"flag_status": 1})]

    # 2. Recursive subtree
    rec = _read(ctx, "search_messages", folder="Projects",
                recursive=True, flag_complete=True)
    assert rec["ok"] is True
    assert [i["subject"] for i in rec["items"]] == ["sub-done", "proj-done"]
    assert [i["folder"] for i in rec["items"]] == ["Projects/Sub", "Projects"]
    assert rec["folders_searched"] == 2
    assert proj.count_calls == 0 and proj_sub.count_calls == 0

    # 3. Whole mailbox (f:all)
    whole = _read(ctx, "search_messages", folder="f:all", flag_complete=True)
    assert whole["ok"] is True
    assert [i["subject"] for i in whole["items"]] == [
        "sub-done", "proj-done", "inbox-done",
    ]
    assert whole["folders_searched"] == 3


def test_flag_complete_bypasses_warm_mirror_and_rejects_aqs_query(tmp_path):
    store = CacheStore(tmp_path / "mirror.db")
    try:
        now = int(time.time())
        store.upsert_messages([{
            "ews_id": "MIRROR-ONLY=",
            "changekey": "CK",
            "folder": "inbox",
            "conversation_id": "CONV-1=",
            "sender_name": "Ops",
            "sender_email": "ops@example.com",
            "to_json": "[]",
            "subject": "mirror-row",
            "date_ts": now,
            "date_iso": "2026-06-10T09:00+03:00",
            "is_read": 1,
            "has_attachments": 0,
            "importance": None,
            "categories_json": "[]",
            "body_clean": "body",
            "internet_message_id": "<mirror@example.com>",
            "norm_text": CacheStore.norm_for_row("mirror-row", "Ops", "ops@example.com", "body"),
        }])
        store.set_sync_state("item:inbox", "TOK", now)

        inbox = _FolderQuery([_item("LIVE-1=", "live-done", flag_status=1)])
        ctx, _ = _make_env(tmp_path, {"Inbox": inbox}, cache=store)

        res = _read(ctx, "search_messages", flag_complete=True)
        assert res["source"] == "live"
        assert [i["subject"] for i in res["items"]] == ["live-done"]

        bad = _read(ctx, "search_messages", query="urgent", flag_complete=False)
        assert bad["ok"] is False and bad["error"]["code"] == "validation"
    finally:
        store.close()


def test_update_messages_sets_and_clears_flag_and_get_message_reflects_it(tmp_path):
    unflagged = _item("MSG-1=", "Follow up item", flag_status=None)
    open_flag = _item("MSG-2=", "Open flagged item", flag_status=2)
    inbox = _FolderQuery([unflagged, open_flag])
    ctx, account = _make_env(tmp_path, {"Inbox": inbox})

    alias_1 = ctx.aliaser.alias_for("MSG-1=", "m")
    alias_2 = ctx.aliaser.alias_for("MSG-2=", "m")

    # Neither unflagged (None) nor open-flagged (2) surfaces flag_complete.
    before = _read(ctx, "get_message", id=alias_1)
    assert before["ok"] is True
    assert "flag_complete" not in before["message"]
    still_open = _read(ctx, "get_message", id=alias_2)
    assert "flag_complete" not in still_open["message"]

    # Mark complete via update_messages -> flag_status=1 saved, get_message shows True.
    marked = _write(ctx, "update_messages", ids=[alias_1], flag_complete=True)
    assert marked == {"ok": True, "updated": 1, "failed": []}
    assert "flag_status" in account.fetch_projections[-1]
    assert unflagged.flag_status == 1
    unflagged.save.assert_called_once_with(update_fields=["flag_status"])

    after_set = _read(ctx, "get_message", id=alias_1)
    assert after_set["message"]["flag_complete"] is True

    # Concise card format stays compact and omits flag_complete.
    concise = _read(ctx, "get_message", id=alias_1, format="concise")
    assert "flag_complete" not in concise["message"]

    # Clear completion via update_messages(flag_complete=False) -> flag_status=None.
    unflagged.save.reset_mock()
    cleared = _write(ctx, "update_messages", ids=[alias_1], flag_complete=False)
    assert cleared == {"ok": True, "updated": 1, "failed": []}
    assert unflagged.flag_status is None
    unflagged.save.assert_called_once_with(update_fields=["flag_status"])

    after_clear = _read(ctx, "get_message", id=alias_1)
    assert "flag_complete" not in after_clear["message"]


def test_ensure_registered_is_idempotent_across_repeated_calls_and_reloads():
    flag_property.ensure_registered()
    flag_property.ensure_registered()
    assert [f.name for f in Message.FIELDS].count("flag_status") == 1

    # Even if the module is reloaded, Message.FIELDS already holds flag_status.
    reloaded = importlib.reload(flag_property)
    reloaded.ensure_registered()
    assert [f.name for f in Message.FIELDS].count("flag_status") == 1

    # And if deregistered, ensure_registered restores it cleanly.
    try:
        Message.deregister("flag_status")
        assert "flag_status" not in [f.name for f in Message.FIELDS]
        reloaded.ensure_registered()
        reloaded.ensure_registered()
        assert [f.name for f in Message.FIELDS].count("flag_status") == 1
    finally:
        reloaded.ensure_registered()


def test_get_message_flag_complete_documented_and_pinned_live_only(tmp_path):
    desc = READ_SPECS["get_message"].description
    assert "flag_complete" in desc
    assert "only reported when read live, i.e. with fresh=true" in desc

    store = CacheStore(tmp_path / "mirror.db")
    try:
        now = int(time.time())
        store.upsert_messages([{
            "ews_id": "DONE-BOTH=",
            "changekey": "CK",
            "folder": "inbox",
            "conversation_id": "CONV-1=",
            "sender_name": "Ops",
            "sender_email": "ops@example.com",
            "to_json": '["team@example.com"]',
            "subject": "Completed item",
            "date_ts": now,
            "date_iso": "2026-06-10T09:00+03:00",
            "is_read": 1,
            "has_attachments": 0,
            "importance": None,
            "categories_json": "[]",
            "body_clean": "Done body",
            "internet_message_id": "<done-both@example.com>",
            "norm_text": CacheStore.norm_for_row(
                "Completed item", "Ops", "ops@example.com", "Done body",
            ),
        }])
        store.set_sync_state("item:inbox", "TOK", now)

        live_item = _item("DONE-BOTH=", "Completed item", flag_status=1)
        ctx, account = _make_env(
            tmp_path, {"Inbox": _FolderQuery([live_item])}, cache=store,
        )
        alias = ctx.aliaser.alias_for("DONE-BOTH=", "m")

        # Mirror lane: omits flag_complete without touching live EWS.
        cached = _read(ctx, "get_message", id=alias)
        assert cached["ok"] is True and cached["source"] == "cache"
        assert "flag_complete" not in cached["message"]
        assert account.fetch_projections == []

        # Live lane (fresh=True): reports flag_complete=True.
        live = _read(ctx, "get_message", id=alias, fresh=True)
        assert live["ok"] is True and live["source"] == "live"
        assert live["message"]["flag_complete"] is True
    finally:
        store.close()
