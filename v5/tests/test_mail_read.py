"""mail_read pack tests — all eight read tools driven through dispatch().

No network, no exchangelib objects: a MagicMock account behind a fake
gateway, list-subclass query doubles, and SimpleNamespace messages with
tz-aware datetimes (FIXED_OFFSET_TZ, UTC+3).
"""

import ast
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import jsonschema
import pytest
from conftest import FIXED_OFFSET_TZ, TIME_MARGIN, make_settings
from exchangelib import Message
from exchangelib.attachments import ItemAttachment
from exchangelib.properties import Mailbox

from ewsmcp import dto, extract, extract_child, extract_worker
from ewsmcp.audit import AuditLog
from ewsmcp.cache.store import CacheStore
from ewsmcp.errors import ToolError
from ewsmcp.gateway.client import EWSGateway
from ewsmcp.ids import NullAliaser, get_aliaser
from ewsmcp.tools import build_registry, mail_read
from ewsmcp.tools.base import Context, dispatch
from test_extract import _build_minimal_pdf, _docx_bytes, _pptx_bytes, _xlsx_bytes

TZ = ZoneInfo(FIXED_OFFSET_TZ)
SPECS = {spec.name: spec for spec in mail_read.TOOLS}

QUOTED_TAIL = (
    "\n\nFrom: Exec <exec@corp.example>\nSent: Monday, June 8, 2026\n"
    "To: Alice\nSubject: Re: RFP\n\nOLD QUOTED LINE ONE\nOLD QUOTED LINE TWO"
)


# --- doubles ----------------------------------------------------------------


class _Query(list):
    """List-like stand-in for a Folder/QuerySet: chainable, records calls."""

    def __init__(self, items=(), *, _root=None):
        super().__init__(items)
        self._root = _root if _root is not None else self
        if _root is None:
            self.filter_calls = []
            self.exclude_calls = []
            self.only_calls = []
            self.order_calls = []
            self.count_calls = 0
            self.refresh_calls = 0
            self.total_count = len(items)

    def filter(self, *args, **kwargs):
        self._root.filter_calls.append((args, kwargs))
        items = list(self)
        narrowed = False
        if "categories__contains" in kwargs:
            wanted = set(kwargs["categories__contains"])
            items = [
                it for it in items
                if wanted.issubset(set(getattr(it, "categories", None) or []))
            ]
            narrowed = True
        if "flag_status" in kwargs:
            items = [
                it for it in items
                if getattr(it, "flag_status", None) == kwargs["flag_status"]
            ]
            narrowed = True
        if "importance" in kwargs:
            items = [
                it for it in items
                if (getattr(it, "importance", None) or "Normal") == kwargs["importance"]
            ]
            narrowed = True
        return _Query(items, _root=self._root) if narrowed else self

    def exclude(self, *args, **kwargs):
        self._root.exclude_calls.append((args, kwargs))
        if "flag_status" in kwargs:
            items = [
                it for it in self
                if getattr(it, "flag_status", None) != kwargs["flag_status"]
            ]
            return _Query(items, _root=self._root)
        return self

    def only(self, *args):
        self._root.only_calls.append(args)
        return self

    def order_by(self, *args):
        self._root.order_calls.append(args)
        return self

    def count(self):
        self._root.count_calls += 1
        return len(self)

    def refresh(self):
        self._root.refresh_calls += 1


class _FakeGateway:
    def __init__(self, account):
        self.account = account
        self.in_worker = 0

    async def call(self, fn):
        self.in_worker += 1
        try:
            return fn(self.account)
        finally:
            self.in_worker -= 1

    def resolve_folder(self, account, ref, aliaser):
        if ref and isinstance(getattr(account, "mail_folders", None), list):
            key = ref.strip()
            wk = (mail_read.WELL_KNOWN.get(key.lower())
                  or mail_read.WELL_KNOWN.get(f"f:{key.lower()}"))
            try:
                resolved = wk or aliaser.resolve(key)
            except KeyError as exc:
                raise ToolError("validation", str(exc.args[0] if exc.args else exc))
            needle = resolved.strip("/").lower()
            for path, folder in account.mail_folders:
                if (getattr(folder, "id", None) == resolved
                        or path.rstrip("/").lower() == needle):
                    return folder
            raise ToolError("not_found", f"No folder matches {ref!r}.")
        return self.account.inbox

    def mail_folders(self, account):
        return self.account.mail_folders


def _msg(raw_id, subject="Subj", sender="alice@corp.example", *, sender_name=None,
         dt=None, is_read=True, has_attachments=False, text_body="",
         conv="CONV-RAW-1=", message_id=None, attachments=None, to=None,
         categories=None, flag_status=None):
    return SimpleNamespace(
        id=raw_id,
        subject=subject,
        sender=SimpleNamespace(name=sender_name or sender, email_address=sender),
        datetime_received=dt or datetime(2026, 6, 10, 9, 0, tzinfo=TZ),
        is_read=is_read,
        has_attachments=has_attachments,
        text_body=text_body,
        message_id=message_id or f"<{raw_id}@corp.example>",
        conversation_id=SimpleNamespace(id=conv),
        to_recipients=to or [],
        cc_recipients=[],
        attachments=list(attachments or []),
        importance="Normal",
        body=None,
        categories=list(categories) if categories is not None else None,
        flag_status=flag_status,
    )


def _account(inbox=None, sent=None):
    account = MagicMock(name="account")
    account.inbox = inbox if inbox is not None else _Query()
    account.sent = sent if sent is not None else _Query()
    return account


def _ctx(tmp_path, account, cache=None, **overrides):
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    overrides.setdefault("ews_tz", FIXED_OFFSET_TZ)
    return Context(
        settings=make_settings(**overrides),
        gateway=_FakeGateway(account),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "alias")),
        audit=AuditLog(str(tmp_path / "audit")),
        cache=cache,
    )


def _run(ctx, name, **kwargs):
    return asyncio.run(dispatch(ctx, SPECS[name], dict(kwargs)))


# --- pack shape ---------------------------------------------------------------


def test_pack_exports_eight_read_specs():
    assert len(mail_read.TOOLS) == 8
    assert set(SPECS) == {
        "list_folders", "search_messages", "check_messages", "get_message",
        "get_messages", "get_thread", "get_attachment", "get_mailbox_overview",
    }
    for spec in mail_read.TOOLS:
        assert spec.side_effect_class == "read"
        assert spec.requires_ews is True
        assert spec.input_schema["additionalProperties"] is False


# --- search_messages -----------------------------------------------------------


def _whole_mailbox(tmp_path, folders):
    account = _account()
    account.mail_folders = list(folders.items())
    return _ctx(tmp_path, account)


def test_search_whole_mailbox_merges_folders_newest_first(tmp_path):
    inbox = _Query([_msg("A=", subject="older", dt=datetime(2026, 6, 1, tzinfo=TZ))])
    archive = _Query([_msg("B=", subject="newest", dt=datetime(2026, 6, 9, tzinfo=TZ)),
                      _msg("C=", subject="oldest", dt=datetime(2026, 5, 1, tzinfo=TZ))])
    ctx = _whole_mailbox(tmp_path, {"Inbox": inbox, "Inbox/Archive": archive})
    res = _run(ctx, "search_messages", folder="f:all", query="from:alice@example.com")
    assert [i["subject"] for i in res["items"]] == ["newest", "older", "oldest"]
    assert [i["folder"] for i in res["items"]] == ["Inbox/Archive", "Inbox", "Inbox/Archive"]
    assert res["total_available"] == 3 and res["next_offset"] is None
    assert res["folders_searched"] == 2 and "folders_failed" not in res
    assert inbox.filter_calls == [(("from:alice@example.com",), {})]


def test_search_whole_mailbox_pages_over_the_merged_list(tmp_path):
    days = [datetime(2026, 6, d, tzinfo=TZ) for d in range(1, 7)]
    one = _Query([_msg(f"A{d.day}=", subject=str(d.day), dt=d) for d in reversed(days[::2])])
    two = _Query([_msg(f"B{d.day}=", subject=str(d.day), dt=d) for d in reversed(days[1::2])])
    ctx = _whole_mailbox(tmp_path, {"One": one, "Two": two})
    first = _run(ctx, "search_messages", folder="f:all", subject="x", limit=2)
    assert [i["subject"] for i in first["items"]] == ["6", "5"]
    assert first["next_offset"] == 2 and first["total_available"] is None
    second = _run(ctx, "search_messages", folder="f:all", subject="x", limit=2, offset=2)
    assert [i["subject"] for i in second["items"]] == ["4", "3"]
    assert second["next_offset"] == 4


def test_search_whole_mailbox_reports_a_failed_folder(tmp_path):
    broken = MagicMock(name="broken")
    broken.filter.side_effect = RuntimeError("no access")
    ctx = _whole_mailbox(tmp_path, {"Inbox": _Query([_msg("A=")]), "Secret": broken})
    res = _run(ctx, "search_messages", folder="f:all", subject="x")
    assert res["count"] == 1 and res["total_available"] is None
    assert res["folders_failed"] == ["Secret"] and res["folders_searched"] == 1


def test_search_whole_mailbox_fails_if_every_folder_fails(tmp_path):
    broken = MagicMock(name="broken")
    broken.filter.side_effect = RuntimeError("no access")
    ctx = _whole_mailbox(tmp_path, {"Secret": broken})
    res = _run(ctx, "search_messages", folder="f:all", subject="x")
    assert res["ok"] is False


def test_search_envelope_alias_ids_and_clean_snippet(tmp_path):
    inbox = _Query([
        _msg("RAW-1=", subject="RFP timeline",
             text_body="Confirming the new deadline works." + QUOTED_TAIL),
        _msg("RAW-2=", subject="Budget", text_body="Numbers attached."),
    ])
    ctx = _ctx(tmp_path, _account(inbox=inbox))
    res = _run(ctx, "search_messages")
    assert res["ok"] is True
    assert res["count"] == 2 and res["total_available"] == 2
    assert res["next_offset"] is None
    assert res["items"][0]["id"] == "m1"
    assert res["items"][1]["id"] == "m2"
    assert res["items"][0]["snippet"] == "Confirming the new deadline works."
    assert "OLD QUOTED" not in res["items"][0]["snippet"]


def test_search_engine_conflict_is_validation_error(tmp_path):
    ctx = _ctx(tmp_path, _account())
    res = _run(ctx, "search_messages", query="from:alice@example.com", subject="rfp")
    assert res["ok"] is False
    assert res["error"]["code"] == "validation"
    assert "hint" in res["error"]


def test_search_aqs_passes_query_string_to_filter(tmp_path):
    inbox = _Query([_msg("RAW-1=")])
    ctx = _ctx(tmp_path, _account(inbox=inbox))
    res = _run(ctx, "search_messages", query='subject:"RFP" hasattachment:yes')
    assert res["ok"] is True
    assert inbox.filter_calls[0] == (('subject:"RFP" hasattachment:yes',), {})


def test_search_structured_filters_become_lookups(tmp_path):
    inbox = _Query([_msg("RAW-1=")])
    ctx = _ctx(tmp_path, _account(inbox=inbox))
    res = _run(ctx, "search_messages", subject="rfp", since="2026-06-01",
               until="2026-06-12T18:30+03:00", is_unread=True, has_attachments=True)
    assert res["ok"] is True
    args, kwargs = inbox.filter_calls[0]
    assert args == ()
    assert kwargs["subject__icontains"] == "rfp"
    assert kwargs["datetime_received__gte"] == datetime(2026, 6, 1, 0, 0, tzinfo=TZ)
    assert kwargs["datetime_received__lte"] == datetime(2026, 6, 12, 18, 30, tzinfo=TZ)
    assert kwargs["is_read"] is False
    assert kwargs["has_attachments"] is True
    assert inbox.order_calls and inbox.only_calls  # projection applied


def test_search_from_is_a_client_side_postfilter(tmp_path):
    inbox = _Query([
        _msg("RAW-1=", sender="alice@corp.example"),
        _msg("RAW-2=", sender="bob@corp.example"),
    ])
    ctx = _ctx(tmp_path, _account(inbox=inbox))
    res = _run(ctx, "search_messages", from_="alice")
    assert res["ok"] is True
    assert res["count"] == 1
    assert res["items"][0]["from"] == "alice@corp.example"
    assert res["total_available"] is None  # post-filter voids the upstream count


def test_search_pagination_next_offset(tmp_path):
    inbox = _Query([_msg(f"RAW-{n}=") for n in range(3)])
    ctx = _ctx(tmp_path, _account(inbox=inbox))
    res = _run(ctx, "search_messages", limit=2)
    assert res["count"] == 2
    # Unfiltered listing: the exact total comes from the REFRESHED folder
    # property, never from a count() full-folder scan.
    assert res["total_available"] == 3
    assert res["next_offset"] == 2
    assert inbox.refresh_calls == 1
    assert inbox.count_calls == 0


def test_search_never_calls_queryset_count(tmp_path):
    """count() iterates every matching id server-side — banned on all
    search paths (the v3 latency root cause)."""
    inbox = _Query([_msg(f"RAW-{n}=") for n in range(5)])
    ctx = _ctx(tmp_path, _account(inbox=inbox))
    _run(ctx, "search_messages", subject="x", limit=2)
    _run(ctx, "search_messages", limit=2)
    assert inbox.count_calls == 0


def test_search_filtered_has_lookahead_next_offset_but_no_total(tmp_path):
    inbox = _Query([_msg(f"RAW-{n}=") for n in range(4)])
    ctx = _ctx(tmp_path, _account(inbox=inbox))
    res = _run(ctx, "search_messages", subject="Subj", limit=2)
    assert res["total_available"] is None  # filtered: total would need a scan
    assert res["next_offset"] == 2  # lookahead saw a third item
    assert inbox.refresh_calls == 0  # refresh only pays off unfiltered


def test_search_sender_param_and_deprecated_alias(tmp_path):
    inbox = _Query([
        _msg("RAW-1=", sender="alice@corp.example"),
        _msg("RAW-2=", sender="bob@corp.example"),
    ])
    ctx = _ctx(tmp_path, _account(inbox=inbox))
    res = _run(ctx, "search_messages", sender="alice")
    assert res["count"] == 1
    both = _run(ctx, "search_messages", sender="alice", from_="alice")
    assert both["error"]["code"] == "validation"


def _neutral_msg(raw_id, subject, *, dt=None, categories=None, flag_status=None):
    return _msg(
        raw_id,
        subject=subject,
        sender="ops@example.com",
        sender_name="Ops",
        dt=dt,
        message_id=f"<{raw_id}@example.com>",
        categories=categories,
        flag_status=flag_status,
    )


def _cache_row(ews_id, *, folder="inbox", subject="Item", categories=(), date_ts=1700000000):
    body = "message body"
    return {
        "ews_id": ews_id,
        "changekey": "CK",
        "folder": folder,
        "conversation_id": "CONV-1",
        "sender_name": "Ops",
        "sender_email": "ops@example.com",
        "to_json": "[]",
        "subject": subject,
        "date_ts": date_ts,
        "date_iso": "2026-06-10T09:00+03:00",
        "is_read": 1,
        "has_attachments": 0,
        "importance": None,
        "categories_json": json.dumps(list(categories)),
        "body_clean": body,
        "internet_message_id": f"<{ews_id}@example.com>",
        "norm_text": CacheStore.norm_for_row(subject, "Ops", "ops@example.com", body),
    }


def test_search_recursive_merges_subtree_newest_first_and_pages(tmp_path):
    root = _Query([
        _neutral_msg("R4=", "root-4", dt=datetime(2026, 6, 4, tzinfo=TZ)),
    ])
    alpha = _Query([
        _neutral_msg("A6=", "alpha-6", dt=datetime(2026, 6, 6, tzinfo=TZ)),
        _neutral_msg("A2=", "alpha-2", dt=datetime(2026, 6, 2, tzinfo=TZ)),
    ])
    beta = _Query([
        _neutral_msg("B5=", "beta-5", dt=datetime(2026, 6, 5, tzinfo=TZ)),
        _neutral_msg("B1=", "beta-1", dt=datetime(2026, 6, 1, tzinfo=TZ)),
    ])
    # Prefix-collision sibling and unrelated folder must stay out of the subtree.
    sibling = _Query([
        _neutral_msg("S9=", "sibling-9", dt=datetime(2026, 6, 9, tzinfo=TZ)),
    ])
    outside = _Query([
        _neutral_msg("O8=", "outside-8", dt=datetime(2026, 6, 8, tzinfo=TZ)),
    ])
    ctx = _whole_mailbox(tmp_path, {
        "Projects": root,
        "Projects/Alpha": alpha,
        "Projects/Beta": beta,
        "Projects-Archive": sibling,
        "Inbox": outside,
    })

    full = _run(ctx, "search_messages", folder="Projects", recursive=True, subject="x")
    assert full["ok"] is True
    assert full["source"] == "live"
    assert [i["subject"] for i in full["items"]] == [
        "alpha-6", "beta-5", "root-4", "alpha-2", "beta-1",
    ]
    assert [i["folder"] for i in full["items"]] == [
        "Projects/Alpha", "Projects/Beta", "Projects",
        "Projects/Alpha", "Projects/Beta",
    ]
    assert full["total_available"] == 5 and full["next_offset"] is None
    assert full["folders_searched"] == 3 and "folders_failed" not in full
    assert sibling.filter_calls == [] and outside.filter_calls == []

    p1 = _run(ctx, "search_messages", folder="Projects", recursive=True,
              subject="x", offset=0, limit=2)
    assert [i["subject"] for i in p1["items"]] == ["alpha-6", "beta-5"]
    assert [i["folder"] for i in p1["items"]] == ["Projects/Alpha", "Projects/Beta"]
    assert p1["next_offset"] == 2 and p1["total_available"] == 5
    assert p1["folders_searched"] == 3

    trunc = _run(ctx, "search_messages", folder="Projects", recursive=True,
                 subject="x", offset=0, limit=1)
    assert [i["subject"] for i in trunc["items"]] == ["alpha-6"]
    assert trunc["next_offset"] == 1 and trunc["total_available"] is None

    p2 = _run(ctx, "search_messages", folder="Projects", recursive=True,
              subject="x", offset=2, limit=2)
    assert [i["subject"] for i in p2["items"]] == ["root-4", "alpha-2"]
    assert [i["folder"] for i in p2["items"]] == ["Projects", "Projects/Alpha"]
    assert p2["next_offset"] == 4

    p3 = _run(ctx, "search_messages", folder="Projects", recursive=True,
              subject="x", offset=4, limit=2)
    assert [i["subject"] for i in p3["items"]] == ["beta-1"]
    assert [i["folder"] for i in p3["items"]] == ["Projects/Beta"]
    assert p3["next_offset"] is None


def test_search_recursive_bypasses_mirror_and_f_all_with_recursive_is_f_all(tmp_path):
    store = CacheStore(tmp_path / "mirror.db")
    try:
        now = int(time.time())
        store.upsert_messages([_cache_row("CACHED-1", subject="from-mirror", date_ts=now)])
        store.set_sync_state("item:inbox", "TOK", now)

        inbox = _Query([_neutral_msg("L1=", "live-root", dt=datetime(2026, 6, 3, tzinfo=TZ))])
        sub = _Query([_neutral_msg("L2=", "live-sub", dt=datetime(2026, 6, 5, tzinfo=TZ))])
        archive = _Query([_neutral_msg("L3=", "live-arch", dt=datetime(2026, 6, 7, tzinfo=TZ))])
        account = _account(inbox=inbox)
        account.mail_folders = [("Inbox", inbox), ("Inbox/Sub", sub), ("Archive", archive)]
        ctx = _ctx(tmp_path, account, cache=store)

        rec = _run(ctx, "search_messages", folder="f:inbox", recursive=True)
        assert rec["source"] == "live"
        assert [i["subject"] for i in rec["items"]] == ["live-sub", "live-root"]
        assert rec["folders_searched"] == 2

        all_res = _run(ctx, "search_messages", folder="f:all", recursive=True)
        assert all_res["source"] == "live"
        assert [i["subject"] for i in all_res["items"]] == [
            "live-arch", "live-sub", "live-root",
        ]
        assert all_res["folders_searched"] == 3
    finally:
        store.close()


def test_search_recursive_reports_failed_subfolder(tmp_path):
    broken = MagicMock(name="broken")
    broken.filter.side_effect = RuntimeError("denied")
    ctx = _whole_mailbox(tmp_path, {
        "Projects": _Query([_neutral_msg("P1=", "ok-root")]),
        "Projects/Broken": broken,
        "Other": _Query([_neutral_msg("O1=", "outside")]),
    })
    res = _run(ctx, "search_messages", folder="Projects", recursive=True, subject="x")
    assert res["ok"] is True
    assert [i["subject"] for i in res["items"]] == ["ok-root"]
    assert res["folders_searched"] == 1
    assert res["folders_failed"] == ["Projects/Broken"]
    assert res["total_available"] is None


def test_search_categories_reaches_mirror_and_live_lanes(tmp_path):
    store = CacheStore(tmp_path / "mirror.db")
    try:
        now = int(time.time())
        store.upsert_messages([
            _cache_row("M1", subject="both-cats", categories=["Finance", "Review"], date_ts=now),
            _cache_row("M2", subject="one-cat", categories=["Finance"], date_ts=now - 10),
            _cache_row("M3", subject="no-cat", categories=[], date_ts=now - 20),
        ])
        store.set_sync_state("item:inbox", "TOK", now)

        inbox = _Query([
            _neutral_msg("L1=", "live-both", categories=["Finance", "Review"]),
            _neutral_msg("L2=", "live-one", categories=["Finance"]),
            _neutral_msg("L3=", "live-none"),
        ])
        ctx = _ctx(tmp_path, _account(inbox=inbox), cache=store)

        cached_one = _run(ctx, "search_messages", categories=["Finance"])
        assert cached_one["source"] == "cache"
        assert [i["subject"] for i in cached_one["items"]] == ["both-cats", "one-cat"]
        assert cached_one["total_available"] == 2
        assert inbox.filter_calls == []

        cached_both = _run(ctx, "search_messages", categories=["Finance", "Review"])
        assert cached_both["source"] == "cache"
        assert [i["subject"] for i in cached_both["items"]] == ["both-cats"]
        assert cached_both["total_available"] == 1

        live = _run(ctx, "search_messages", categories=["Finance", "Review"], fresh=True)
        assert live["source"] == "live"
        assert [i["subject"] for i in live["items"]] == ["live-both"]
        assert inbox.filter_calls == [((), {"categories__contains": ["Finance", "Review"]})]
        assert inbox.count_calls == 1
        assert inbox.refresh_calls == 0
        assert live["total_available"] == 1

        conflict = _run(ctx, "search_messages", query="budget", categories=["Finance"])
        assert conflict["ok"] is False and conflict["error"]["code"] == "validation"
    finally:
        store.close()


def test_search_count_called_for_categories_not_for_flag_complete_false(tmp_path):
    inbox = _Query([
        _neutral_msg("M1=", "done", flag_status=1, categories=["Finance"]),
        _neutral_msg("M2=", "open-1", flag_status=2),
        _neutral_msg("M3=", "open-2", flag_status=None),
        _neutral_msg("M4=", "open-3", flag_status=None),
    ])
    ctx = _ctx(tmp_path, _account(inbox=inbox))

    incomplete = _run(ctx, "search_messages", flag_complete=False, limit=2)
    assert incomplete["ok"] is True
    assert incomplete["count"] == 2
    assert incomplete["total_available"] is None
    assert incomplete["next_offset"] == 2
    assert inbox.count_calls == 0

    by_cat = _run(ctx, "search_messages", categories=["Finance"], fresh=True)
    assert by_cat["ok"] is True
    assert by_cat["total_available"] == 1
    assert inbox.count_calls == 1


# --- get_message -----------------------------------------------------------------


def test_get_message_full_shape_and_alias_roundtrip(tmp_path):
    item = _msg("RAW-1=", subject="RFP timeline",
                text_body="Body line.\n\nRegards", has_attachments=True,
                attachments=[SimpleNamespace(name="contract.pdf", size=9,
                                             content_type="application/pdf")])
    inbox = _Query([item])
    account = _account(inbox=inbox)
    ctx = _ctx(tmp_path, account)
    assert _run(ctx, "search_messages")["items"][0]["id"] == "m1"  # mints m1

    account.fetch = MagicMock(return_value=[item])
    res = _run(ctx, "get_message", id="m1")  # dispatcher resolves m1 -> RAW-1=
    assert res["ok"] is True
    assert account.fetch.call_args.kwargs["ids"] == [("RAW-1=", None)]
    message = res["message"]
    assert message["id"] == "m1"  # same alias back out
    assert message["internet_message_id"] == "<RAW-1=@corp.example>"
    assert message["body"].startswith("Body line.")
    assert message["to"] == []
    assert message["attachments"][0]["name"] == "contract.pdf"
    assert "smime_type" not in message["attachments"][0]
    assert "snippet" not in message


def test_get_message_flags_smime_signed_attachment_in_inventory(tmp_path):
    item = _msg("RAW-2=", subject="Signed mail", has_attachments=True,
                attachments=[_att("smime.p7m", _P7M_SIGNED_HEAD,
                                  "application/pkcs7-mime")])
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_message", id="RAW-2=")
    assert res["message"]["attachments"][0]["smime_type"] == "signed"


def test_get_message_concise_returns_card(tmp_path):
    item = _msg("RAW-1=", text_body="Hello.")
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_message", id="RAW-1=", format="concise")
    assert res["ok"] is True
    assert res["message"]["snippet"] == "Hello."
    assert "body" not in res["message"]


def test_get_message_stale_id_maps_to_not_found(tmp_path):
    class ErrorItemNotFound(Exception):
        pass

    account = _account()
    account.fetch = MagicMock(return_value=[ErrorItemNotFound("gone")])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_message", id="RAW-STALE=")
    assert res["ok"] is False
    assert res["error"]["code"] == "not_found"
    assert "search" in res["error"]["hint"].lower()


# --- get_thread --------------------------------------------------------------------


def test_get_thread_merges_inbox_and_sent_chronologically(tmp_path):
    msg_a = _msg("RAW-A=", subject="RFP", sender="alice@corp.example",
                 dt=datetime(2026, 6, 10, 9, 0, tzinfo=TZ),
                 text_body="Opening question?")
    msg_b = _msg("RAW-B=", subject="Re: RFP", sender="exec@corp.example",
                 dt=datetime(2026, 6, 10, 10, 0, tzinfo=TZ),
                 text_body="Agreed, Sunday 10am." + QUOTED_TAIL)
    msg_c = _msg("RAW-C=", subject="Re: RFP", sender="alice@corp.example",
                 dt=datetime(2026, 6, 10, 11, 0, tzinfo=TZ),
                 text_body="Confirmed, thanks.", has_attachments=True)
    account = _account(inbox=_Query([msg_c, msg_a]), sent=_Query([msg_b]))
    account.fetch = MagicMock(return_value=[msg_a])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_thread", id="RAW-A=")
    assert res["ok"] is True
    assert res["thread_id"].startswith("t")
    assert res["subject"] == "RFP"
    assert res["count"] == 3 and res["total_available"] == 3
    dates = [entry["date"] for entry in res["items"]]
    assert dates == sorted(dates)
    assert res["items"][1]["body"] == "Agreed, Sunday 10am."
    assert "OLD QUOTED" not in res["items"][1]["body"]
    assert res["items"][2].get("attach") is True
    assert sum(p["msgs"] for p in res["participants"]) == 3
    assert res["participants"][0]["name_or_email"] == "alice@corp.example"
    # conversation_id must be passed as the ConversationId OBJECT — 5.0.3
    # raises TypeError on a plain string (verified against the installed
    # library; the old pin here asserted the broken behavior).
    for folder in (account.inbox, account.sent):
        passed = folder.filter_calls[0][1]["conversation_id"]
        assert passed is msg_a.conversation_id
        assert passed.id == "CONV-RAW-1="


def test_get_thread_caps_at_limit_keeping_latest(tmp_path):
    msgs = [
        _msg(f"RAW-{n}=", dt=datetime(2026, 6, 10, 8 + n, 0, tzinfo=TZ),
             text_body=f"entry {n}")
        for n in range(4)
    ]
    account = _account(inbox=_Query(msgs), sent=_Query())
    account.fetch = MagicMock(return_value=[msgs[0]])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_thread", id="RAW-0=", limit=2)
    assert res["count"] == 2 and res["total_available"] == 4
    assert [entry["body"] for entry in res["items"]] == ["entry 2", "entry 3"]
    assert res["next_offset"] == 2  # older history exists


def test_get_thread_offset_pages_older_history(tmp_path):
    msgs = [
        _msg(f"RAW-{n}=", dt=datetime(2026, 6, 10, 8 + n, 0, tzinfo=TZ),
             text_body=f"entry {n}")
        for n in range(5)
    ]
    account = _account(inbox=_Query(msgs), sent=_Query())
    account.fetch = MagicMock(return_value=[msgs[0]])
    ctx = _ctx(tmp_path, account)
    older = _run(ctx, "get_thread", id="RAW-0=", limit=2, offset=2)
    assert [e["body"] for e in older["items"]] == ["entry 1", "entry 2"]
    assert older["next_offset"] == 4
    oldest = _run(ctx, "get_thread", id="RAW-0=", limit=2, offset=4)
    assert [e["body"] for e in oldest["items"]] == ["entry 0"]
    assert oldest["next_offset"] is None
    # participants always count the WHOLE thread, not the returned page
    assert sum(p["msgs"] for p in oldest["participants"]) == 5


# --- get_attachment --------------------------------------------------------------


def _att(name="notes.txt", content=b"hello world", content_type="text/plain"):
    return SimpleNamespace(name=name, size=len(content),
                           content_type=content_type, content=content)


def test_get_attachment_text_mode(tmp_path):
    item = _msg("RAW-A=", attachments=[_att()])
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_attachment", message_id="RAW-A=", mode="text")
    assert res["ok"] is True
    assert res["mode"] == "text"
    assert res["text"] == "hello world"
    assert res["name"] == "notes.txt"
    assert res["size_bytes"] == 11
    assert "truncated" not in res


def test_get_attachment_multiple_without_selector_is_validation(tmp_path):
    item = _msg("RAW-A=", attachments=[_att("a.txt"), _att("b.csv")])
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_attachment", message_id="RAW-A=")
    assert res["ok"] is False
    assert res["error"]["code"] == "validation"
    assert "a.txt" in res["error"]["message"] and "b.csv" in res["error"]["message"]


def test_get_attachment_index_selector_string(tmp_path):
    item = _msg("RAW-A=", attachments=[_att("a.txt", b"first"),
                                       _att("b.txt", b"second")])
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_attachment", message_id="RAW-A=", attachment="1",
               mode="text")
    assert res["ok"] is True
    assert res["text"] == "second"


def test_get_attachment_auto_on_binary_returns_info_with_hint(tmp_path):
    item = _msg("RAW-A=", attachments=[
        _att("diagram.png", b"\x89PNG\r\n\x1a\n", "image/png"),
    ])
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_attachment", message_id="RAW-A=")
    assert res["ok"] is True
    assert res["mode"] == "info"
    assert "text" not in res
    assert "save" in res["hint"]


def test_get_attachment_save_writes_sanitized_file(tmp_path):
    item = _msg("RAW-A=", attachments=[
        _att("quarterly report?.csv", b"a,b\n1,2\n", "text/csv"),
    ])
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_attachment", message_id="RAW-A=", mode="save")
    assert res["ok"] is True
    saved = res["saved_path"]
    assert str(tmp_path / "data") in saved
    assert "?" not in saved and " " not in saved.rsplit("attachments", 1)[1]
    with open(saved, "rb") as fh:
        assert fh.read() == b"a,b\n1,2\n"


# --- get_attachment: S/MIME smime.p7m sniff ---------------------------------

# Minimal DER ContentInfo prefixes: SEQUENCE { OID contentType, ... } — real
# messages carry certs/signature bytes after this, but the sniff only scans
# the head, so a short synthetic prefix is enough to exercise it.
_P7M_SIGNED_HEAD = bytes.fromhex("3082") + bytes.fromhex("06092a864886f70d010702") + b"REST-OF-SIGNED-DATA-including-original-mime"
_P7M_ENVELOPED_HEAD = bytes.fromhex("3082") + bytes.fromhex("06092a864886f70d010703") + b"opaque-encrypted-bytes"


def test_get_attachment_smime_signed_is_auto_decoded_as_text(tmp_path):
    item = _msg("RAW-A=", attachments=[
        _att("smime.p7m", _P7M_SIGNED_HEAD, "application/pkcs7-mime"),
    ])
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_attachment", message_id="RAW-A=")
    assert res["ok"] is True
    assert res["smime_type"] == "signed"
    assert res["mode"] == "text"
    assert "original-mime" in res["text"]
    assert "not encrypted" in res["hint"]


def test_get_attachment_smime_enveloped_stays_info_with_hint(tmp_path):
    item = _msg("RAW-A=", attachments=[
        _att("smime.p7m", _P7M_ENVELOPED_HEAD, "application/pkcs7-mime"),
    ])
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_attachment", message_id="RAW-A=")
    assert res["ok"] is True
    assert res["smime_type"] == "enveloped"
    assert res["mode"] == "info"
    assert "text" not in res
    assert "private key" in res["hint"]


def test_get_attachment_smime_undetermined_falls_back_to_binary_info(tmp_path):
    item = _msg("RAW-A=", attachments=[
        _att("smime.p7m", b"not-a-recognizable-der-prefix", "application/pkcs7-mime"),
    ])
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_attachment", message_id="RAW-A=")
    assert res["ok"] is True
    assert "smime_type" not in res
    assert res["mode"] == "info"


# --- get_mailbox_overview ----------------------------------------------------------


def test_overview_shape(tmp_path):
    inbox = _Query([
        _msg("RAW-1=", is_read=False, text_body="Urgent one."),
        _msg("RAW-2=", is_read=False, text_body="Urgent two."),
    ])
    inbox.unread_count = 2
    account = _account(inbox=inbox)
    event = SimpleNamespace(id="EV-RAW-1=", subject="Standup",
                            start=datetime(2026, 6, 13, 9, 0, tzinfo=TZ),
                            end=datetime(2026, 6, 13, 9, 30, tzinfo=TZ))
    account.calendar.view.return_value = [event]
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_mailbox_overview")
    assert res["ok"] is True
    assert res["unread_total"] == 2
    assert res["connection"] == "unmanaged"
    assert "generated_at" in res
    assert [card["unread"] for card in res["recent_unread"]] == [True, True]
    assert res["recent_unread"][0]["id"].startswith("m")
    assert res["today_events"][0]["id"].startswith("e")
    assert res["today_events"][0]["subject"] == "Standup"
    view_kwargs = account.calendar.view.call_args.kwargs
    assert view_kwargs["start"].hour == 0 and view_kwargs["start"].minute == 0
    assert (view_kwargs["end"] - view_kwargs["start"]).days == 1
    assert inbox.filter_calls[0][1] == {"is_read": False}


# --- list_folders --------------------------------------------------------------------


def _folder(raw_id, name, total=1, unread=0, children=(), folder_class=None):
    return SimpleNamespace(id=raw_id, name=name, total_count=total,
                           unread_count=unread, children=list(children),
                           folder_class=folder_class)


def test_list_folders_walk_with_paths_and_wk(tmp_path):
    sub = _folder("FLD-SUB=", "2026", total=4)
    inbox_folder = _folder("FLD-INBOX=", "Inbox", total=5, unread=2,
                           children=[sub], folder_class="IPF.Note")
    empty = _folder("FLD-EMPTY=", "Archive Old", total=0)
    account = _account()
    account.msg_folder_root = _folder("FLD-ROOT=", "root",
                                      children=[inbox_folder, empty])
    account.inbox = inbox_folder  # lets the wk map identify f:inbox
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "list_folders")
    assert res["ok"] is True
    by_path = {row["path"]: row for row in res["items"]}
    assert set(by_path) == {"Inbox", "Inbox/2026", "Archive Old"}
    assert by_path["Inbox"]["wk"] == "f:inbox"
    assert by_path["Inbox"]["unread"] == 2
    assert by_path["Inbox"]["children"] == 1
    assert by_path["Inbox"]["id"].startswith("f")
    assert by_path["Inbox"]["folder_class"] == "IPF.Note"
    assert "wk" not in by_path["Inbox/2026"]
    assert "folder_class" not in by_path["Inbox/2026"]
    # alias roundtrip: the f-alias resolves back to the raw folder id
    assert ctx.aliaser.resolve(by_path["Inbox"]["id"]) == "FLD-INBOX="


def test_list_folders_depth_and_include_empty(tmp_path):
    sub = _folder("FLD-SUB=", "2026", total=4)
    inbox_folder = _folder("FLD-INBOX=", "Inbox", total=5, children=[sub])
    empty = _folder("FLD-EMPTY=", "Archive Old", total=0)
    account = _account()
    account.msg_folder_root = _folder("FLD-ROOT=", "root",
                                      children=[inbox_folder, empty])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "list_folders", depth=1, include_empty=False)
    assert [row["name"] for row in res["items"]] == ["Inbox"]


def test_check_messages_whole_word_and_missing(tmp_path):
    inbox = _Query([
        _neutral_msg("RAW-0=", "Review RFP-123 draft"),
        _neutral_msg("RAW-1=", "RFP-12 signed"),
        _neutral_msg("RAW-2=", "Lunch"),
    ])
    ctx = _ctx(tmp_path, _account(inbox=inbox))
    out = _run(ctx, "check_messages", values="RFP-12\nrfp-12, Budget")
    assert out["ok"] is True
    assert out["values_checked"] == 2
    assert out["missing"] == ["Budget"]
    assert [c["subject"] for c in out["found"]["RFP-12"]] == ["RFP-12 signed"]
    assert out["found"]["RFP-12"][0]["id"].startswith("m")
    only = _run(ctx, "check_messages", values='["Budget", "Lunch"]', only_missing=True)
    assert only["ok"] is True
    assert only["missing"] == ["Budget"] and "found" not in only


# --- r2: lazy attachment reads, check_messages live sender, fan-out guards ---


class _CountingAttachment:
    """Attachment double whose lazy .content property counts reads and records
    whether each read happened inside the gateway worker."""

    def __init__(self, name, content, content_type, *, size=None, gateway=None):
        self.name = name
        self._content = content
        self.content_type = content_type
        self.size = len(content) if size is None and content is not None else size
        self._gateway = gateway
        self.reads = 0
        self.read_in_worker = []

    @property
    def content(self):
        self.reads += 1
        if self._gateway is not None:
            self.read_in_worker.append(self._gateway.in_worker > 0)
        return self._content


def test_get_message_never_downloads_ordinary_or_oversized_attachments(tmp_path):
    account = _account()
    ctx = _ctx(tmp_path, account)
    gw = ctx.gateway

    pdf = _CountingAttachment("contract.pdf", b"%PDF-1.7-body", "application/pdf", gateway=gw)
    txt = _CountingAttachment("notes.txt", b"hello world", "text/plain", gateway=gw)
    small_p7m = _CountingAttachment(
        "smime.p7m", _P7M_SIGNED_HEAD, "application/pkcs7-mime", gateway=gw,
    )
    huge_p7m = _CountingAttachment(
        "huge.p7m", _P7M_SIGNED_HEAD, "application/pkcs7-mime",
        size=5 * 1024 * 1024 + 1, gateway=gw,
    )
    item = _neutral_msg("RAW-LAZY=", "Attachments")
    item.has_attachments = True
    item.attachments = [pdf, txt, small_p7m, huge_p7m]

    # dto.msg_full itself never touches .content even on .p7m items.
    direct = dto.msg_full(item, ctx.aliaser, FIXED_OFFSET_TZ, 4000)
    assert [a.reads for a in (pdf, txt, small_p7m, huge_p7m)] == [0, 0, 0, 0]
    assert all("smime_type" not in entry for entry in direct["attachments"])

    # Live get_message(format="full") sniffs only PKCS#7 candidates <= 5 MiB,
    # inside the worker function, and never downloads ordinary or >5 MiB files.
    account.fetch = MagicMock(return_value=[item])
    res = _run(ctx, "get_message", id="RAW-LAZY=")
    assert res["ok"] is True and res["source"] == "live"
    assert pdf.reads == 0
    assert txt.reads == 0
    assert small_p7m.reads == 1
    assert small_p7m.read_in_worker == [True]
    assert huge_p7m.reads == 0
    listed = res["message"]["attachments"]
    assert "smime_type" not in listed[0]
    assert "smime_type" not in listed[1]
    assert listed[2]["smime_type"] == "signed"
    assert "smime_type" not in listed[3]


def test_get_attachment_reads_content_only_when_needed(tmp_path):
    account = _account()
    ctx = _ctx(tmp_path, account)
    gw = ctx.gateway

    pdf = _CountingAttachment("contract.pdf", b"%PDF-binary", "application/pdf", gateway=gw)
    png = _CountingAttachment("diagram.png", b"\x89PNG-binary", "image/png", gateway=gw)
    txt = _CountingAttachment("notes.txt", b"plain notes", "text/plain", gateway=gw)
    signed_p7m = _CountingAttachment(
        "smime.p7m", _P7M_SIGNED_HEAD, "application/pkcs7-mime", gateway=gw,
    )
    enveloped_p7m = _CountingAttachment(
        "enc.p7m", _P7M_ENVELOPED_HEAD, "application/pkcs7-mime", gateway=gw,
    )
    item = _neutral_msg("RAW-ATT=", "Files")
    item.attachments = [pdf, png, txt, signed_p7m, enveloped_p7m]
    account.fetch = MagicMock(return_value=[item])

    # mode="info" on ordinary attachments: zero downloads.
    info_pdf = _run(ctx, "get_attachment", message_id="RAW-ATT=",
                    attachment="contract.pdf", mode="info")
    info_txt = _run(ctx, "get_attachment", message_id="RAW-ATT=",
                    attachment="notes.txt", mode="info")
    assert info_pdf["mode"] == "info" and info_txt["mode"] == "info"
    assert pdf.reads == 0 and txt.reads == 0

    # mode="auto" on unsupported binary non-PKCS#7: stays info with zero downloads.
    auto_png = _run(ctx, "get_attachment", message_id="RAW-ATT=",
                    attachment="diagram.png", mode="auto")
    assert auto_png["mode"] == "info" and png.reads == 0

    # .p7m candidates read .content exactly ONCE (inside the worker), even when
    # auto-mode decodes signed data as text.
    auto_signed = _run(ctx, "get_attachment", message_id="RAW-ATT=",
                       attachment="smime.p7m", mode="auto")
    assert auto_signed["mode"] == "text" and auto_signed["smime_type"] == "signed"
    assert signed_p7m.reads == 1 and signed_p7m.read_in_worker == [True]

    auto_env = _run(ctx, "get_attachment", message_id="RAW-ATT=",
                    attachment="enc.p7m", mode="auto")
    assert auto_env["mode"] == "info" and auto_env["smime_type"] == "enveloped"
    assert enveloped_p7m.reads == 1 and enveloped_p7m.read_in_worker == [True]

    # mode="save" and mode="text" read .content once.
    saved = _run(ctx, "get_attachment", message_id="RAW-ATT=",
                 attachment="contract.pdf", mode="save")
    assert saved["mode"] == "save" and pdf.reads == 1
    assert pdf.read_in_worker == [True]


class _AqsSenderFolder(_Query):
    """Simulates Exchange's server-side AQS ``from:"..."`` index over a folder."""

    def filter(self, *args, **kwargs):
        self._root.filter_calls.append((args, kwargs))
        if args and isinstance(args[0], str) and args[0].startswith('from:"'):
            aqs = args[0]
            end_idx = len(aqs) - 1
            # Extract unescaped term inside from:"..." (before any received>= clause).
            buf = []
            i = len('from:"')
            while i <= end_idx:
                ch = aqs[i]
                if ch == "\\" and i + 1 <= end_idx:
                    buf.append(aqs[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    break
                buf.append(ch)
                i += 1
            needle = "".join(buf).lower()
            matched = [
                it for it in self
                if needle in (getattr(it.sender, "name", "") or "").lower()
                or needle in (getattr(it.sender, "email_address", "") or "").lower()
            ]
            return _AqsSenderFolder(matched, _root=self._root)
        return super().filter(*args, **kwargs)


def test_check_messages_sender_live_uses_aqs_and_whole_word_display_name(tmp_path):
    # 60 newer messages from someone else push target senders past page 1 (50).
    items = [
        _msg(
            f"NEW-{n}=", subject=f"Noise {n}",
            sender="bulk@example.com", sender_name="Bulk Bot",
            dt=datetime(2026, 6, 20, 12, 0, tzinfo=TZ),
            message_id=f"<NEW-{n}@example.com>",
        )
        for n in range(60)
    ]
    # Prefix trap: "SupportDesk" matches AQS from:"Support" but NOT whole-word "Support".
    items.append(_msg(
        "OLD-PREFIX=", subject="From SupportDesk",
        sender="sdesk@example.com", sender_name="SupportDesk Ops",
        dt=datetime(2026, 6, 2, 9, 0, tzinfo=TZ),
        message_id="<OLD-PREFIX@example.com>",
    ))
    items.append(_msg(
        "OLD-EXACT=", subject="From Support",
        sender="desk@example.com", sender_name="Support Ops",
        dt=datetime(2026, 6, 1, 9, 0, tzinfo=TZ),
        message_id="<OLD-EXACT@example.com>",
    ))
    items.append(_msg(
        "OLD-QUOTE=", subject="Quoted display",
        sender="quoted@example.com", sender_name='Ops "West"\\Team',
        dt=datetime(2026, 6, 1, 8, 0, tzinfo=TZ),
        message_id="<OLD-QUOTE@example.com>",
    ))
    inbox = _AqsSenderFolder(items)
    ctx = _ctx(tmp_path, _account(inbox=inbox))

    res = _run(
        ctx, "check_messages",
        values=["Support", 'Ops "West"\\Team', "missing@example.com"],
        field="sender",
    )
    assert res["ok"] is True
    assert res["source"] == "live" and "as_of" not in res
    assert res["values_checked"] == 3
    assert res["found_count"] == 2
    assert res["missing"] == ["missing@example.com"]
    assert [c["subject"] for c in res["found"]["Support"]] == ["From Support"]
    assert [c["subject"] for c in res["found"]['Ops "West"\\Team']] == ["Quoted display"]

    # Verify escaped AQS queries hit Exchange directly rather than a bare page scan.
    aqs_queries = [call_args[0] for call_args, _kw in inbox.filter_calls if call_args]
    assert 'from:"Support"' in aqs_queries
    assert 'from:"Ops \\"West\\"\\\\Team"' in aqs_queries
    assert 'from:"missing@example.com"' in aqs_queries


def test_check_messages_mirror_provenance_fresh_and_values_schema(tmp_path):
    # Public JSON schema accepts both a string and an array of strings for `values`.
    schema = SPECS["check_messages"].public_schema()["inputSchema"]
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate({"values": "Support, Ops"})
    validator.validate({"values": ["Support", "Ops"], "fresh": True})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"values": [123]})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"values": 123})

    store = CacheStore(tmp_path / "mirror.db")
    try:
        now = int(time.time())
        row_prefix = _cache_row("M-PREFIX", subject="SupportDesk mail", date_ts=now)
        row_prefix["sender_name"] = "SupportDesk Ops"
        row_prefix["sender_email"] = "sdesk@example.com"
        row_exact = _cache_row("M-EXACT", subject="Support mail", date_ts=now - 10)
        row_exact["sender_name"] = "Support Ops"
        row_exact["sender_email"] = "desk@example.com"
        store.upsert_messages([row_prefix, row_exact])
        store.set_sync_state("item:inbox", "TOK", now)

        live_inbox = _AqsSenderFolder([
            _msg("L-EXACT=", subject="Live Support", sender="desk@example.com",
                 sender_name="Support Ops", message_id="<L-EXACT@example.com>"),
        ])
        ctx = _ctx(tmp_path, _account(inbox=live_inbox), cache=store)

        cached = _run(ctx, "check_messages", values=["Support", "Nobody"], field="sender")
        assert cached["ok"] is True
        assert cached["source"] == "cache" and cached["as_of"]
        assert [c["subject"] for c in cached["found"]["Support"]] == ["Support mail"]
        assert cached["missing"] == ["Nobody"]
        assert live_inbox.filter_calls == []

        live = _run(ctx, "check_messages", values=["Support"], field="sender", fresh=True)
        assert live["ok"] is True
        assert live["source"] == "live" and "as_of" not in live
        assert [c["subject"] for c in live["found"]["Support"]] == ["Live Support"]
        assert live_inbox.filter_calls == [(('from:"Support"',), {})]
    finally:
        store.close()


def test_search_fanout_caps_at_200_folders_and_500_window(tmp_path):
    folders = {}
    for idx in range(205):
        msgs = (
            [_neutral_msg(f"M-{idx}=", f"hit-{idx}", dt=datetime(2026, 6, 10, tzinfo=TZ))]
            if idx in (0, 199, 200) else []
        )
        folders[f"F{idx:03d}"] = _Query(msgs)
    ctx = _whole_mailbox(tmp_path, folders)

    res = _run(ctx, "search_messages", folder="f:all", subject="x")
    assert res["ok"] is True
    assert res["folders_searched"] == 200
    assert res["folders_skipped"] == 5
    assert res["total_available"] is None
    assert {i["folder"] for i in res["items"]} == {"F000", "F199"}
    assert folders["F199"].filter_calls != []
    assert folders["F200"].filter_calls == []
    assert folders["F204"].filter_calls == []

    too_deep = _run(ctx, "search_messages", folder="f:all", offset=460, limit=50)
    assert too_deep["ok"] is False
    assert too_deep["error"]["code"] == "validation"
    assert "500" in too_deep["error"]["message"]

    rec_deep = _run(ctx, "search_messages", folder="F000", recursive=True,
                    offset=480, limit=21)
    assert rec_deep["ok"] is False
    assert rec_deep["error"]["code"] == "validation"
    assert "500" in rec_deep["error"]["message"]

    at_cap = _run(ctx, "search_messages", folder="F000", recursive=True,
                  offset=450, limit=50)
    assert at_cap["ok"] is True


def test_search_fanout_propagates_cancelled_and_fails_on_throttle(tmp_path):
    class ErrorServerBusy(Exception):
        pass

    cancelled_folder = MagicMock(name="cancelled")
    cancelled_folder.filter.side_effect = asyncio.CancelledError()
    ctx_cancel = _whole_mailbox(tmp_path / "cancel", {
        "Inbox": _Query([_neutral_msg("A=", "ok")]),
        "Archive": cancelled_folder,
    })
    with pytest.raises(asyncio.CancelledError):
        _run(ctx_cancel, "search_messages", folder="f:all", subject="x")

    throttled_folder = MagicMock(name="throttled")
    throttled_folder.filter.side_effect = ErrorServerBusy("Server busy, back off")
    ctx_throttle = _whole_mailbox(tmp_path / "throttle", {
        "Inbox": _Query([_neutral_msg("A=", "ok")]),
        "Archive": throttled_folder,
    })
    res = _run(ctx_throttle, "search_messages", folder="f:all", subject="x")
    assert res["ok"] is False
    assert res["error"]["code"] == "throttled"
    assert "folders_failed" not in res


def test_search_fanout_slices_page_before_sender_filter_and_matches_single_folder(tmp_path):
    props = SPECS["search_messages"].input_schema["properties"]
    assert (
        "matched within each page; keep paging while next_offset is set"
        in props["sender"]["description"]
    )
    assert "contains" in props["categories"]["description"]
    assert "exact" in props["categories"]["description"]

    def make_mail(day, role):
        return _msg(
            f"ID-{day}=",
            subject=f"{role}-{day}",
            sender=f"{role}@example.com",
            sender_name=role.title(),
            dt=datetime(2026, 6, day, tzinfo=TZ),
            message_id=f"<{day}@example.com>",
        )

    inbox_items = [
        make_mail(20, "noise"),
        make_mail(19, "noise"),
        make_mail(18, "noise"),
        make_mail(17, "noise"),
        make_mail(16, "target"),
        make_mail(15, "target"),
        make_mail(14, "target"),
    ]
    ctx_single = _ctx(tmp_path / "single", _account(inbox=_Query(inbox_items)))
    ctx_fanout = _whole_mailbox(tmp_path / "fanout", {
        "Inbox": _Query(inbox_items),
        "Inbox/Sub": _Query([make_mail(10, "noise")]),
        "Archive": _Query([make_mail(9, "noise")]),
    })

    def collect_pages(ctx_under_test, **kwargs):
        pages = []
        seen = []
        off = 0
        for _ in range(10):
            res = _run(ctx_under_test, "search_messages", offset=off, limit=2, **kwargs)
            assert res["ok"] is True
            assert res["total_available"] is None
            pages.append(res)
            seen.extend(item["subject"] for item in res["items"])
            if res["next_offset"] is None:
                break
            off = res["next_offset"]
        assert pages[-1]["next_offset"] is None
        return pages, seen

    _single_pages, single_seen = collect_pages(
        ctx_single, folder="f:inbox", sender="target", fresh=True,
    )
    assert single_seen == ["target-16", "target-15", "target-14"]

    fanout_pages, fanout_seen = collect_pages(
        ctx_fanout, folder="f:all", sender="target",
    )
    # Pages 0 and 1 hold only newer noise items, yet next_offset keeps advancing.
    assert fanout_pages[0]["items"] == [] and fanout_pages[0]["next_offset"] == 2
    assert fanout_pages[1]["items"] == [] and fanout_pages[1]["next_offset"] == 4
    assert fanout_seen == single_seen

    rec_pages, rec_seen = collect_pages(
        ctx_fanout, folder="Inbox", recursive=True, sender="target",
    )
    assert rec_pages[0]["items"] == [] and rec_pages[0]["next_offset"] == 2
    assert rec_seen == single_seen


def test_check_messages_rejects_f_all_and_recursive_scopes(tmp_path):
    class _CountingGateway(_FakeGateway):
        def __init__(self, account):
            super().__init__(account)
            self.calls = 0

        async def call(self, fn):
            self.calls += 1
            return await super().call(fn)

    account = _account()
    account.mail_folders = [("Inbox", _Query([_neutral_msg("M1=", "RFP-1")]))]
    ctx = _ctx(tmp_path, account)
    ctx.gateway = _CountingGateway(account)

    for bad_folder in ("f:all", "F:ALL", "Inbox/*"):
        res = _run(ctx, "check_messages", values=["RFP-1"], folder=bad_folder)
        assert res["ok"] is False
        assert res["error"]["code"] == "validation"
        assert "single folder" in res["error"]["message"].lower()

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            mail_read._check_messages(
                ctx, ["RFP-1"], folder="f:inbox", recursive=True,
            )
        )
    assert exc_info.value.code == "validation"
    assert "single folder" in exc_info.value.message.lower()
    assert ctx.gateway.calls == 0


def test_check_messages_cancels_siblings_on_first_failure(tmp_path):
    class ErrorServerBusy(Exception):
        pass

    class _FailFirstGateway(_FakeGateway):
        def __init__(self, account):
            super().__init__(account)
            self.calls = 0

        async def call(self, fn):
            self.calls += 1
            call_num = self.calls
            await asyncio.sleep(0.02)
            if call_num == 1:
                raise ErrorServerBusy("Server busy, back off")
            return fn(self.account)

    account = _account(inbox=_Query([_neutral_msg("M1=", "RFP-1 update")]))
    ctx = _ctx(tmp_path, account)
    gw = _FailFirstGateway(account)
    ctx.gateway = gw
    values = [f"RFP-{idx}" for idx in range(50)]

    async def exercise():
        res = await dispatch(
            ctx,
            SPECS["check_messages"],
            {"values": values, "folder": "f:inbox", "fresh": True},
        )
        at_return = gw.calls
        await asyncio.sleep(0.1)
        return res, at_return, gw.calls

    res, calls_at_return, calls_after_wait = asyncio.run(exercise())
    assert res["ok"] is False
    assert res["error"]["code"] == "throttled"
    # Four searches run at once; the ones already started when the first
    # fails may each have let one more begin before cancellation lands.
    assert 1 <= calls_at_return <= 8
    assert calls_after_wait == calls_at_return


def test_check_messages_uses_slim_projection_without_touching_body_or_attachments(tmp_path):
    class _StrictSlimItem:
        def __init__(self, raw_id, subject, sender_email, sender_name, dt):
            self.id = raw_id
            self.changekey = "CK"
            self.subject = subject
            self.sender = SimpleNamespace(
                name=sender_name, email_address=sender_email,
            )
            self.datetime_received = dt

        @property
        def text_body(self):
            raise AssertionError("check_messages must not read text_body")

        @property
        def body(self):
            raise AssertionError("check_messages must not read body")

        @property
        def attachments(self):
            raise AssertionError("check_messages must not read attachments")

    class _SliceRecordingFolder(_AqsSenderFolder):
        def __init__(self, items=(), *, _root=None):
            super().__init__(items, _root=_root)
            if _root is None:
                self.slices = []

        def __getitem__(self, key):
            if isinstance(key, slice):
                self._root.slices.append((key.start, key.stop))
            return super().__getitem__(key)

    items = [
        _StrictSlimItem(
            f"SLIM-{idx}=",
            f"RFP-{idx} update",
            "desk@example.com",
            "Support Ops",
            datetime(2026, 6, 20, 12, 0, tzinfo=TZ),
        )
        for idx in range(55)
    ]
    inbox = _SliceRecordingFolder(items)
    ctx = _ctx(tmp_path, _account(inbox=inbox))

    by_subj = _run(ctx, "check_messages", values=["RFP-0"], field="subject", fresh=True)
    assert by_subj["ok"] is True
    assert inbox.only_calls == [("id", "subject", "sender", "datetime_received")]
    assert inbox.slices == [(0, 50)]
    hit = by_subj["found"]["RFP-0"][0]
    assert set(hit.keys()) == {"id", "subject", "from", "date"}
    assert hit["id"].startswith("m")

    by_sender = _run(
        ctx, "check_messages", values=["Support"], field="sender", fresh=True,
    )
    assert by_sender["ok"] is True
    assert len(by_sender["found"]["Support"]) == 5
    assert inbox.only_calls[-1] == ("id", "subject", "sender", "datetime_received")
    assert inbox.slices[-1] == (0, 50)


def test_get_attachment_caps_pkcs7_sniff_at_5_mib_for_info_and_auto(tmp_path):
    account = _account()
    ctx = _ctx(tmp_path, account)
    gw = ctx.gateway
    cap = 5 * 1024 * 1024

    over_signed = _CountingAttachment(
        "smime.p7m", _P7M_SIGNED_HEAD, "application/pkcs7-mime",
        size=cap + 1, gateway=gw,
    )
    over_enveloped = _CountingAttachment(
        "enc.p7m", _P7M_ENVELOPED_HEAD, "application/x-pkcs7-mime",
        size=cap + 1, gateway=gw,
    )
    at_cap = _CountingAttachment(
        "exact.p7m", _P7M_SIGNED_HEAD, "application/pkcs7-mime",
        size=cap, gateway=gw,
    )
    item = _neutral_msg("RAW-CAP=", "Oversized PKCS7")
    item.attachments = [over_signed, over_enveloped, at_cap]
    account.fetch = MagicMock(return_value=[item])

    info_over = _run(
        ctx, "get_attachment", message_id="RAW-CAP=",
        attachment="smime.p7m", mode="info",
    )
    assert info_over["ok"] is True and info_over["mode"] == "info"
    assert "smime_type" not in info_over
    assert "hint" in info_over
    assert over_signed.reads == 0

    auto_signed = _run(
        ctx, "get_attachment", message_id="RAW-CAP=",
        attachment="smime.p7m", mode="auto",
    )
    assert auto_signed["ok"] is True and auto_signed["mode"] == "info"
    assert "smime_type" not in auto_signed
    assert "hint" in auto_signed
    assert over_signed.reads == 0

    auto_env = _run(
        ctx, "get_attachment", message_id="RAW-CAP=",
        attachment="enc.p7m", mode="auto",
    )
    assert auto_env["ok"] is True and auto_env["mode"] == "info"
    assert "smime_type" not in auto_env
    assert "hint" in auto_env
    assert over_enveloped.reads == 0

    info_at_cap = _run(
        ctx, "get_attachment", message_id="RAW-CAP=",
        attachment="exact.p7m", mode="info",
    )
    assert info_at_cap["mode"] == "info"
    assert info_at_cap["smime_type"] == "signed"
    assert at_cap.reads == 1

    # Explicit byte modes still read the payload when requested by the caller.
    text_over = _run(
        ctx, "get_attachment", message_id="RAW-CAP=",
        attachment="smime.p7m", mode="text",
    )
    assert text_over["mode"] == "text"
    assert over_signed.reads == 1

    save_over = _run(
        ctx, "get_attachment", message_id="RAW-CAP=",
        attachment="enc.p7m", mode="save",
    )
    assert save_over["mode"] == "save"
    assert over_enveloped.reads == 1


def test_search_recursive_walks_subtrees_of_excluded_well_known_folders(tmp_path):
    root_node = SimpleNamespace(id="FLD-ROOT=", name="root")
    top_node = SimpleNamespace(id="FLD-TOP=", name="Top of Information Store")

    class _FolderQuery(_Query):
        def __init__(self, raw_id, name, items=(), *, parents=(), cls="IPF.Note"):
            super().__init__(items)
            self.id = raw_id
            self.name = name
            self.folder_class = cls
            self.parts = [root_node, top_node, *parents, self]

    inbox = _FolderQuery(
        "FLD-IN=", "Inbox",
        [_neutral_msg("IN-1=", "in-root", dt=datetime(2026, 6, 10, tzinfo=TZ))],
    )
    trash = _FolderQuery(
        "FLD-TR=", "Deleted Items",
        [_neutral_msg("TR-1=", "trash-root", dt=datetime(2026, 6, 3, tzinfo=TZ))],
    )
    trash_sub = _FolderQuery(
        "FLD-TR-SUB=", "2025",
        [_neutral_msg("TR-2=", "trash-sub", dt=datetime(2026, 6, 6, tzinfo=TZ))],
        parents=[trash],
    )
    trash_deep = _FolderQuery(
        "FLD-TR-DEEP=", "Q1",
        [_neutral_msg("TR-3=", "trash-deep", dt=datetime(2026, 6, 8, tzinfo=TZ))],
        parents=[trash, trash_sub],
    )
    trash_cal = _FolderQuery(
        "FLD-TR-CAL=", "OldCal", [], parents=[trash], cls="IPF.Appointment",
    )
    drafts = _FolderQuery(
        "FLD-DR=", "Drafts",
        [_neutral_msg("DR-1=", "draft-root", dt=datetime(2026, 6, 2, tzinfo=TZ))],
    )
    drafts_sub = _FolderQuery(
        "FLD-DR-SUB=", "Templates",
        [_neutral_msg("DR-2=", "draft-sub", dt=datetime(2026, 6, 7, tzinfo=TZ))],
        parents=[drafts],
    )
    junk = _FolderQuery(
        "FLD-JK=", "Junk Email",
        [_neutral_msg("JK-1=", "junk-root", dt=datetime(2026, 6, 1, tzinfo=TZ))],
    )
    junk_sub = _FolderQuery(
        "FLD-JK-SUB=", "Quarantine",
        [_neutral_msg("JK-2=", "junk-sub", dt=datetime(2026, 6, 5, tzinfo=TZ))],
        parents=[junk],
    )
    outbox = _FolderQuery(
        "FLD-OB=", "Outbox",
        [_neutral_msg("OB-1=", "outbox-root", dt=datetime(2026, 6, 4, tzinfo=TZ))],
    )
    outbox_sub = _FolderQuery(
        "FLD-OB-SUB=", "Queued",
        [_neutral_msg("OB-2=", "outbox-sub", dt=datetime(2026, 6, 9, tzinfo=TZ))],
        parents=[outbox],
    )

    account = _account(inbox=inbox)
    account.trash = trash
    account.drafts = drafts
    account.junk = junk
    account.outbox = outbox
    account.msg_folder_root.walk.return_value = [
        inbox,
        trash, trash_sub, trash_deep, trash_cal,
        drafts, drafts_sub,
        junk, junk_sub,
        outbox, outbox_sub,
    ]

    settings = make_settings(data_dir=str(tmp_path / "data"), ews_tz=FIXED_OFFSET_TZ)
    gw = EWSGateway(settings)
    gw._account = account
    try:
        ctx = Context(
            settings=settings,
            gateway=gw,
            manager=None,
            aliaser=get_aliaser(str(tmp_path / "alias")),
            audit=AuditLog(str(tmp_path / "audit")),
            cache=None,
        )

        # Whole-mailbox fan-out still excludes trash, drafts, junk, and outbox.
        all_res = _run(ctx, "search_messages", folder="f:all")
        assert all_res["ok"] is True
        assert all_res["folders_searched"] == 1
        assert [i["subject"] for i in all_res["items"]] == ["in-root"]

        trash_res = _run(ctx, "search_messages", folder="f:trash", recursive=True)
        assert trash_res["ok"] is True
        assert trash_res["folders_searched"] == 3
        assert [i["subject"] for i in trash_res["items"]] == [
            "trash-deep", "trash-sub", "trash-root",
        ]
        assert [i["folder"] for i in trash_res["items"]] == [
            "Deleted Items/2025/Q1", "Deleted Items/2025", "Deleted Items",
        ]

        drafts_res = _run(ctx, "search_messages", folder="f:drafts", recursive=True)
        assert drafts_res["folders_searched"] == 2
        assert [i["subject"] for i in drafts_res["items"]] == ["draft-sub", "draft-root"]

        junk_res = _run(ctx, "search_messages", folder="f:junk", recursive=True)
        assert junk_res["folders_searched"] == 2
        assert [i["subject"] for i in junk_res["items"]] == ["junk-sub", "junk-root"]

        outbox_res = _run(ctx, "search_messages", folder="f:outbox", recursive=True)
        assert outbox_res["folders_searched"] == 2
        assert [i["subject"] for i in outbox_res["items"]] == ["outbox-sub", "outbox-root"]
    finally:
        gw._pool.shutdown(wait=True)


# --- document extraction, paging, memory cache, item attachments -------------


class _IdCountingAttachment(_CountingAttachment):
    def __init__(self, att_id, name, content, content_type, *, size=None, gateway=None):
        super().__init__(name, content, content_type, size=size, gateway=gateway)
        self.attachment_id = SimpleNamespace(id=att_id)


class _CountingItemAttachment:
    def __init__(self, att_id, name, item_obj, *, size=128):
        self.attachment_id = SimpleNamespace(id=att_id)
        self.name = name
        self.content_type = "message/rfc822"
        self.size = size
        self._item_obj = item_obj
        self.reads = 0

    @property
    def item(self):
        self.reads += 1
        return self._item_obj


def test_get_attachment_auto_and_text_across_document_types_through_dispatch(tmp_path):
    docx_data = _docx_bytes("<w:p><w:r><w:t>Word body — نص</w:t></w:r></w:p>")
    xlsx_data = _xlsx_bytes([
        ("S1", '<row r="1"><c r="A1" t="inlineStr"><is><t>Cell1</t></is></c></row>'),
        ("S2", '<row r="1"><c r="A1"><v>42</v></c></row>'),
    ])
    pptx_data = _pptx_bytes([
        (
            "slides/slide1.xml",
            "<p:sp><p:txBody><a:p><a:r><a:t>One</a:t></a:r></a:p></p:txBody></p:sp>",
        ),
        (
            "slides/slide2.xml",
            "<p:sp><p:txBody><a:p><a:r><a:t>Two</a:t></a:r></a:p></p:txBody></p:sp>",
        ),
        (
            "slides/slide3.xml",
            "<p:sp><p:txBody><a:p><a:r><a:t>Three</a:t></a:r></a:p></p:txBody></p:sp>",
        ),
    ])
    eml_msg = EmailMessage()
    eml_msg["From"] = "sender@example.com"
    eml_msg["To"] = "recipient@example.com"
    eml_msg["Subject"] = "Attached EML"
    eml_msg.set_content("EML plain body.")
    eml_data = eml_msg.as_bytes()
    html_data = b"<html><body><p>HTML body</p></body></html>"
    txt_data = b"Plain UTF-8 text"

    atts = [
        _att("doc.docx", docx_data, "application/octet-stream"),
        _att("book.xlsx", xlsx_data, "application/octet-stream"),
        _att("slides.pptx", pptx_data, "application/octet-stream"),
        _att("mail.eml", eml_data, "message/rfc822"),
        _att("page.html", html_data, "text/html"),
        _att("note.txt", txt_data, "text/plain"),
    ]
    if extract.HAS_PYPDF:
        pdf_data = _build_minimal_pdf([
            {"ascii_lines": ["PDF Page 1"]},
            {"ascii_lines": ["PDF Page 2"]},
        ])
        atts.append(_att("spec.pdf", pdf_data, "application/pdf"))

    item = _neutral_msg("RAW-DOCS=", "Documents")
    item.attachments = atts
    account = _account()
    account.fetch = MagicMock(return_value=[item])

    for mode in ("auto", "text"):
        ctx = _ctx(tmp_path / mode, account)

        r_docx = _run(ctx, "get_attachment", message_id="RAW-DOCS=",
                      attachment="doc.docx", mode=mode)
        assert r_docx["mode"] == "text"
        assert r_docx["extracted_from"] == "docx"
        assert r_docx["text"] == "Word body — نص"
        assert r_docx["total_chars"] == len("Word body — نص")
        assert r_docx["offset"] == 0 and "next_offset" not in r_docx
        assert not ({"pages", "slides", "sheets"} & set(r_docx))

        r_xlsx = _run(ctx, "get_attachment", message_id="RAW-DOCS=",
                      attachment="book.xlsx", mode=mode)
        assert r_xlsx["extracted_from"] == "xlsx"
        assert r_xlsx["sheets"] == 2
        assert "pages" not in r_xlsx and "slides" not in r_xlsx
        assert "Cell1" in r_xlsx["text"] and "42" in r_xlsx["text"]

        r_pptx = _run(ctx, "get_attachment", message_id="RAW-DOCS=",
                      attachment="slides.pptx", mode=mode)
        assert r_pptx["extracted_from"] == "pptx"
        assert r_pptx["slides"] == 3
        assert "pages" not in r_pptx and "sheets" not in r_pptx
        assert "--- Slide 3 ---" in r_pptx["text"]

        r_eml = _run(ctx, "get_attachment", message_id="RAW-DOCS=",
                     attachment="mail.eml", mode=mode)
        assert r_eml["extracted_from"] == "eml"
        assert "Subject: Attached EML" in r_eml["text"]
        assert "EML plain body." in r_eml["text"]

        r_html = _run(ctx, "get_attachment", message_id="RAW-DOCS=",
                      attachment="page.html", mode=mode)
        assert r_html["extracted_from"] == "html"
        assert r_html["text"] == "HTML body"

        r_txt = _run(ctx, "get_attachment", message_id="RAW-DOCS=",
                     attachment="note.txt", mode=mode)
        assert r_txt["extracted_from"] == "text"
        assert r_txt["text"] == "Plain UTF-8 text"

        if extract.HAS_PYPDF:
            r_pdf = _run(ctx, "get_attachment", message_id="RAW-DOCS=",
                         attachment="spec.pdf", mode=mode)
            assert r_pdf["extracted_from"] == "pdf"
            assert r_pdf["pages"] == 2
            assert "slides" not in r_pdf and "sheets" not in r_pdf
            assert "PDF Page 1" in r_pdf["text"]


def test_get_attachment_unsupported_and_msg_modes_through_dispatch(tmp_path):
    account = _account()
    ctx = _ctx(tmp_path, account)
    png = _CountingAttachment("photo.png", b"\x89PNG\r\n\x1a\n", "image/png")
    msg_file = _CountingAttachment(
        "thread.msg", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/vnd.ms-outlook"
    )
    msg_as_text_ct = _CountingAttachment(
        "saved.MSG", b"plain looking bytes inside .msg", "text/plain"
    )
    item = _neutral_msg("RAW-UNSUP=", "Binaries")
    item.attachments = [png, msg_file, msg_as_text_ct]
    account.fetch = MagicMock(return_value=[item])

    auto_png = _run(ctx, "get_attachment", message_id="RAW-UNSUP=",
                    attachment="photo.png", mode="auto")
    assert auto_png["ok"] is True and auto_png["mode"] == "info"
    assert "save" in auto_png["hint"]
    assert png.reads == 0

    text_png = _run(ctx, "get_attachment", message_id="RAW-UNSUP=",
                    attachment="photo.png", mode="text")
    assert text_png["ok"] is False and text_png["error"]["code"] == "validation"
    for kind in ("pdf", "docx", "xlsx", "pptx", "eml", "html"):
        assert kind in text_png["error"]["hint"]
    assert png.reads == 0

    # Outlook .msg files (by extension regardless of MIME type) return documented
    # info metadata with a save-and-open note/hint in BOTH mode="auto" and mode="text",
    # never decoding bytes as text and never raising a validation error.
    for att_name, att_obj in (("thread.msg", msg_file), ("saved.MSG", msg_as_text_ct)):
        for req_mode in ("auto", "text"):
            res_msg = _run(
                ctx,
                "get_attachment",
                message_id="RAW-UNSUP=",
                attachment=att_name,
                mode=req_mode,
            )
            assert res_msg["ok"] is True
            assert res_msg["mode"] == "info"
            assert "text" not in res_msg
            assert "Outlook" in res_msg["hint"] and ".msg" in res_msg["hint"]
            assert "save" in res_msg["hint"] and "open" in res_msg["hint"]
            assert "Outlook" in res_msg["note"] and "save" in res_msg["note"]
            assert att_obj.reads == 0


def test_get_attachment_item_attachment_returned_as_text(tmp_path):
    # 1. Real offline exchangelib ItemAttachment(item=Message(...)).
    embedded_msg = Message(
        subject="Embedded summary",
        sender=Mailbox(name="Operations Desk", email_address="ops@example.com"),
        to_recipients=[Mailbox(email_address="review@example.com")],
        text_body="Forwarded content — محتوى مرفق.",
    )
    real_item_att = ItemAttachment(name="Embedded summary", item=embedded_msg)

    # 2. Counting ItemAttachment double to prove lazy .item access and caching.
    lazy_msg = SimpleNamespace(
        sender=SimpleNamespace(name="Team", email_address="team@example.com"),
        to_recipients=[SimpleNamespace(name=None, email_address="desk@example.com")],
        cc_recipients=[],
        datetime_received=datetime(2026, 6, 10, 12, 0, tzinfo=TZ),
        subject="Lazy item",
        text_body="PartA-" + ("x" * 25_000),
        body=None,
        attachments=[SimpleNamespace(name="nested.pdf")],
    )
    counting_item_att = _CountingItemAttachment("ATT-ITEM-1", "Lazy item", lazy_msg)

    parent = _neutral_msg("RAW-ITEMATT=", "Container")
    parent.attachments = [real_item_att, counting_item_att]
    account = _account()
    account.fetch = MagicMock(return_value=[parent])
    ctx = _ctx(tmp_path, account)

    res_real = _run(ctx, "get_attachment", message_id="RAW-ITEMATT=", attachment="0")
    assert res_real["ok"] is True
    assert res_real["mode"] == "text"
    assert res_real["extracted_from"] == "message"
    assert "From: Operations Desk <ops@example.com>" in res_real["text"]
    assert "Subject: Embedded summary" in res_real["text"]
    assert "Forwarded content — محتوى مرفق." in res_real["text"]

    # mode="info" never evaluates .item.
    info_res = _run(ctx, "get_attachment", message_id="RAW-ITEMATT=",
                    attachment="1", mode="info")
    assert info_res["mode"] == "info"
    assert counting_item_att.reads == 0

    # Paging an item attachment reads .item once and serves the second part from memory.
    p1 = _run(ctx, "get_attachment", message_id="RAW-ITEMATT=", attachment="1")
    assert p1["extracted_from"] == "message"
    assert p1["next_offset"] == 20_000
    assert counting_item_att.reads == 1

    p2 = _run(ctx, "get_attachment", message_id="RAW-ITEMATT=",
              attachment="1", offset=p1["next_offset"])
    assert "Attachments: nested.pdf" in p2["text"]
    assert "next_offset" not in p2
    assert counting_item_att.reads == 1

    save_err = _run(ctx, "get_attachment", message_id="RAW-ITEMATT=",
                    attachment="1", mode="save")
    assert save_err["ok"] is False and save_err["error"]["code"] == "validation"


def test_get_attachment_reading_in_parts_consistent_and_caches_without_refetch(tmp_path):
    schema = SPECS["get_attachment"].public_schema()["inputSchema"]
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate({"message_id": "m1", "offset": 0})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"message_id": "m1", "offset": -1})

    paragraphs = [f"Section {i:04d}: " + ("alpha " * 8) for i in range(750)]
    # `ewsmcp.extract` strips trailing spaces at line ends across all extractors.
    expected_full = "\n".join(p.rstrip(" ") for p in paragraphs)
    assert len(expected_full) > 40_000
    docx_xml = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    docx_bytes = _docx_bytes(docx_xml)

    att = _IdCountingAttachment(
        "ATT-LONG-1", "handbook.docx", docx_bytes, "application/octet-stream"
    )
    item = _neutral_msg("RAW-LONG=", "Long handbook")
    item.attachments = [att]
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)

    assembled = []
    offset = 0
    calls = 0
    while True:
        calls += 1
        res = _run(ctx, "get_attachment", message_id="RAW-LONG=", offset=offset)
        assert res["ok"] is True
        assert res["mode"] == "text"
        assert res["extracted_from"] == "docx"
        assert res["offset"] == offset
        assert res["total_chars"] == len(expected_full)
        assembled.append(res["text"])
        if "next_offset" not in res:
            assert "truncated" not in res
            break
        assert res["truncated"] is True
        assert res["next_offset"] == offset + len(res["text"])
        offset = res["next_offset"]

    assert calls == 3
    assert "".join(assembled) == expected_full
    assert att.reads == 1  # downloaded and parsed ONCE across all three parts

    neg = _run(ctx, "get_attachment", message_id="RAW-LONG=", offset=-5)
    assert neg["ok"] is False and neg["error"]["code"] == "validation"


def test_attachment_text_cache_respects_doc_count_total_chars_ttl_and_key(tmp_path):
    assert mail_read.TEXT_CACHE_MAX_DOCS == 4
    assert mail_read.TEXT_CACHE_MAX_CHARS == 8_000_000
    assert mail_read.TEXT_CACHE_TTL_SECONDS == 600

    now = [1000.0]
    cache = mail_read.AttachmentTextCache(clock=lambda: now[0])

    # 1. At most 4 documents (LRU eviction).
    for idx in range(1, 6):
        cache.put((f"id-{idx}", 10), extract.ExtractionResult(f"doc-{idx}", "text"))
    assert len(cache) == 4
    assert cache.get(("id-1", 10)) is None
    assert cache.get(("id-2", 10)) is not None

    # 2. Every MAX_EXTRACTED_CHARS (5,000,000) document fits; total cap is 8,000,000 chars.
    cache.clear()
    five_m = "m" * extract.MAX_EXTRACTED_CHARS
    cache.put(("max-doc", 1), extract.ExtractionResult(five_m, "text"))
    assert cache.get(("max-doc", 1)) is not None
    assert cache.total_chars == extract.MAX_EXTRACTED_CHARS

    cache.clear()
    three_m = "a" * 3_000_000
    cache.put(("b1", 1), extract.ExtractionResult(three_m, "text"))
    cache.put(("b2", 1), extract.ExtractionResult(three_m, "text"))
    cache.put(("b3", 1), extract.ExtractionResult(three_m, "text"))
    assert cache.total_chars == 6_000_000
    assert cache.total_chars <= mail_read.TEXT_CACHE_MAX_CHARS
    assert cache.get(("b1", 1)) is None
    assert cache.get(("b2", 1)) is not None and cache.get(("b3", 1)) is not None

    over_cap = "z" * (mail_read.TEXT_CACHE_MAX_CHARS + 10)
    cache.put(("oversize", 1), extract.ExtractionResult(over_cap, "text"))
    assert cache.get(("oversize", 1)) is None
    assert cache.get(("b2", 1)) is not None

    # 3. Unambiguous tuple fallback cache key when attachments lack attachment_id.
    no_id_a = SimpleNamespace(name="0:x.txt", content=b"first", content_type="text/plain", size=5)
    no_id_b = SimpleNamespace(name="x.txt", content=b"second", content_type="text/plain", size=5)
    key_a = mail_read._attachment_cache_key(no_id_a, "A", 0, "0:x.txt", 5)
    key_b = mail_read._attachment_cache_key(no_id_b, "A:0", 0, "x.txt", 5)
    assert isinstance(key_a[0], tuple) and isinstance(key_b[0], tuple)
    assert key_a != key_b

    msg_a = _neutral_msg("A", "Msg A")
    msg_a.attachments = [no_id_a]
    msg_b = _neutral_msg("A:0", "Msg B")
    msg_b.attachments = [no_id_b]
    by_id = {"A": msg_a, "A:0": msg_b}
    account_collide = _account()
    # fetch receives (id, changekey) pairs, not objects
    account_collide.fetch = MagicMock(
        side_effect=lambda ids, **_kw: [by_id[getattr(ids[0], "id", None) or ids[0][0]]]
    )
    ctx_collide = _ctx(tmp_path / "collide", account_collide)
    r_first = _run(ctx_collide, "get_attachment", message_id="A", mode="text")
    r_second = _run(ctx_collide, "get_attachment", message_id="A:0", mode="text")
    assert r_first["text"] == "first"
    assert r_second["text"] == "second"

    # 4. 10-minute TTL and (attachment_id, size) key sensitivity through dispatch().
    account = _account()
    ctx = _ctx(tmp_path, account)
    ctx._attachment_text_cache = mail_read.AttachmentTextCache(clock=lambda: now[0])

    att = _IdCountingAttachment("ATT-TTL", "memo.txt", b"cached text", "text/plain")
    item = _neutral_msg("RAW-TTL=", "TTL check")
    item.attachments = [att]
    account.fetch = MagicMock(return_value=[item])

    assert _run(ctx, "get_attachment", message_id="RAW-TTL=")["text"] == "cached text"
    assert att.reads == 1

    now[0] += 599.0
    assert _run(ctx, "get_attachment", message_id="RAW-TTL=")["text"] == "cached text"
    assert att.reads == 1

    # Changing size invalidates the key even before TTL expiry.
    att.size = 999
    assert _run(ctx, "get_attachment", message_id="RAW-TTL=")["text"] == "cached text"
    assert att.reads == 2

    # Advancing >= 600s expires the entry and forces a fresh fetch.
    now[0] += 600.0
    assert _run(ctx, "get_attachment", message_id="RAW-TTL=")["text"] == "cached text"
    assert att.reads == 3


def test_get_attachment_enforces_ews_max_attachment_mb_ceiling(tmp_path):
    account = _account()
    ctx = _ctx(tmp_path, account, ews_max_attachment_mb=1)
    too_big = _IdCountingAttachment(
        "ATT-BIG", "large.txt", b"x" * (2 * 1024 * 1024), "text/plain"
    )
    undeclared_big = _IdCountingAttachment(
        "ATT-UNDECL", "sneaky.txt", b"y" * (2 * 1024 * 1024), "text/plain", size=None
    )
    undeclared_big.size = None
    item = _neutral_msg("RAW-CEIL=", "Ceiling")
    item.attachments = [too_big, undeclared_big]
    account.fetch = MagicMock(return_value=[item])

    res1 = _run(ctx, "get_attachment", message_id="RAW-CEIL=", attachment="0")
    assert res1["ok"] is False and res1["error"]["code"] == "validation"
    assert "EWS_MAX_ATTACHMENT_MB" in res1["error"]["message"]
    assert too_big.reads == 0  # refused from metadata before GetAttachment

    res2 = _run(ctx, "get_attachment", message_id="RAW-CEIL=", attachment="1")
    assert res2["ok"] is False and res2["error"]["code"] == "validation"
    assert "EWS_MAX_ATTACHMENT_MB" in res2["error"]["message"]
    assert undeclared_big.reads == 1


def test_get_attachment_pdf_without_pypdf_through_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "_pypdf", None)
    account = _account()
    ctx = _ctx(tmp_path, account)
    pdf = _IdCountingAttachment("ATT-PDF", "doc.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")
    item = _neutral_msg("RAW-NOPDF=", "PDF no extra")
    item.attachments = [pdf]
    account.fetch = MagicMock(return_value=[item])

    for mode in ("auto", "text"):
        res = _run(ctx, "get_attachment", message_id="RAW-NOPDF=", mode=mode)
        assert res["ok"] is False
        assert res["error"]["code"] == "validation"
        assert "ews-mcp[extract]" in res["error"]["hint"]
    assert pdf.reads == 0

    info = _run(ctx, "get_attachment", message_id="RAW-NOPDF=", mode="info")
    assert info["ok"] is True and info["mode"] == "info"
    saved = _run(ctx, "get_attachment", message_id="RAW-NOPDF=", mode="save")
    assert saved["ok"] is True and saved["mode"] == "save"


def test_worker_isolation_env_scrubbing_path_cwd_and_no_tool_gateway_imports(
    tmp_path, monkeypatch
):
    assert extract_worker.EXTRACT_TIMEOUT_SECONDS == 20
    assert extract_worker.EXTRACT_MEMORY_LIMIT_MB == 1024
    assert extract_worker.EXTRACT_MAX_CONCURRENT == 2
    assert extract_worker.EXTRACT_MAX_OUTPUT_BYTES > extract.MAX_EXTRACTED_CHARS

    # 1. `extract_worker.py` must import nothing from `ewsmcp.tools` or `ewsmcp.gateway`.
    worker_src = Path(extract_worker.__file__).read_text(encoding="utf-8")
    tree = ast.parse(worker_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "tools" not in alias.name and "gateway" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert "tools" not in mod and "gateway" not in mod

    # 2. Scrubbed environment & reduced PATH.
    fake_bin = tmp_path / "fake-jbig2-bin"
    fake_bin.mkdir()
    extra_pp = tmp_path / "extra-pythonpath"
    extra_pp.mkdir()
    monkeypatch.setenv("EWS_URL", "https://mail.example.com/EWS/Exchange.asmx")
    monkeypatch.setenv("EWS_PASSWORD", "s3cr3t-pass")
    monkeypatch.setenv("MCP_API_TOKEN", "mcp-secret")
    monkeypatch.setenv("SEND_CONFIRM_SECRET", "hmac-secret")
    monkeypatch.setenv("DB_PASSWORD", "db-pass")
    monkeypatch.setenv("CUSTOM_SECRET_VALUE", "top-secret")
    monkeypatch.setenv("BEARER_TOKEN", "tok-123")
    monkeypatch.setenv("SIGNING_KEY", "key-456")
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("PYTHONPATH", str(extra_pp))

    scrubbed = extract_worker.build_worker_env()
    assert not any(extract_worker.is_forbidden_env_name(k) for k in scrubbed)
    path_parts = scrubbed["PATH"].split(os.pathsep)
    assert str(fake_bin) not in path_parts
    assert os.path.dirname(os.path.abspath(sys.executable)) in path_parts
    assert str(extra_pp) in scrubbed["PYTHONPATH"].split(os.pathsep)

    # 3. Verify inside a real child subprocess launched through dispatch().
    probe_code = (
        "import json, os, sys; "
        "import ewsmcp.extract_child; "
        "sys.stdin.buffer.readline(); "
        "info = {"
        "'env_keys': list(os.environ.keys()), "
        "'path': os.environ.get('PATH', ''), "
        "'cwd': os.getcwd(), "
        "'cwd_entries': os.listdir(os.getcwd()), "
        "'has_tools': any(m == 'ewsmcp.tools' or m.startswith('ewsmcp.tools.') for m in sys.modules), "
        "'has_gateway': any(m == 'ewsmcp.gateway' or m.startswith('ewsmcp.gateway.') for m in sys.modules)"
        "}; "
        "out = {'ok': True, 'extracted_from': 'html', 'text': json.dumps(info)}; "
        "sys.stdout.write(json.dumps(out))"
    )
    account = _account()
    ctx = _ctx(tmp_path, account)
    item = _neutral_msg("RAW-ENV=", "Env probe")
    item.attachments = [
        _IdCountingAttachment("ATT-ENV", "probe.html", b"<p>hi</p>", "text/html")
    ]
    account.fetch = MagicMock(return_value=[item])

    with monkeypatch.context() as mp:
        mp.setattr(
            extract_worker,
            "_worker_command",
            lambda _mb: [sys.executable, "-c", probe_code],
        )
        res = _run(ctx, "get_attachment", message_id="RAW-ENV=", mode="text")

    assert res["ok"] is True
    probe = json.loads(res["text"])
    assert not any(extract_worker.is_forbidden_env_name(k) for k in probe["env_keys"])
    assert "USERNAME" not in {k.upper() for k in probe["env_keys"]}
    assert str(fake_bin) not in probe["path"]
    assert probe["cwd_entries"] == []
    assert not Path(probe["cwd"]).exists()
    assert probe["has_tools"] is False
    assert probe["has_gateway"] is False


def test_worker_limits_clock_memory_garbage_and_output_through_dispatch(
    tmp_path, monkeypatch
):
    account = _account()
    ctx = _ctx(tmp_path, account)
    atts = [
        _IdCountingAttachment("ATT-CLOCK", "clock.html", b"<p>payload</p>", "text/html"),
        _IdCountingAttachment("ATT-MEM", "mem.html", b"<p>payload</p>", "text/html"),
        _IdCountingAttachment("ATT-GARB", "garb.html", b"<p>payload</p>", "text/html"),
        _IdCountingAttachment("ATT-OUT", "out.html", b"<p>payload</p>", "text/html"),
        _IdCountingAttachment("ATT-OK-1", "ok1.html", b"<p>clean-1</p>", "text/html"),
        _IdCountingAttachment("ATT-OK-2", "ok2.html", b"<p>clean-2</p>", "text/html"),
        _IdCountingAttachment("ATT-OK-3", "ok3.html", b"<p>clean-3</p>", "text/html"),
        _IdCountingAttachment("ATT-OK-4", "ok4.html", b"<p>clean-4</p>", "text/html"),
    ]
    item = _neutral_msg("RAW-LIMITS=", "Worker limits")
    item.attachments = atts
    account.fetch = MagicMock(return_value=[item])
    expected_msg = "the document could not be read within the time and memory limits"

    # 1. Clock overrun (0.5 s limit).
    with monkeypatch.context() as mp:
        mp.setattr(extract_worker, "EXTRACT_TIMEOUT_SECONDS", 0.5)
        mp.setattr(
            extract_worker,
            "_worker_command",
            lambda _mb: [sys.executable, "-c", "import time; time.sleep(10)"],
        )
        t0 = time.perf_counter()
        r_clock = _run(
            ctx,
            "get_attachment",
            message_id="RAW-LIMITS=",
            attachment="clock.html",
            mode="text",
        )
        dt_clock = time.perf_counter() - t0
    assert dt_clock < 4.5 * TIME_MARGIN
    assert r_clock["ok"] is False and r_clock["error"]["code"] == "validation"
    assert r_clock["error"]["message"] == expected_msg
    ok1 = _run(
        ctx, "get_attachment", message_id="RAW-LIMITS=", attachment="ok1.html", mode="text"
    )
    assert ok1["ok"] is True and ok1["text"] == "clean-1"

    # 2. Memory overrun (128 MB self-applied limit vs 320 MB allocation).
    if extract_worker.memory_ceiling_supported():
        mem_code = (
            "import sys; from ewsmcp import extract_child as c; "
            "ok, enf = c.apply_self_memory_limit(128); "
            "assert ok and enf; "
            "sys.stdin.buffer.readline(); "
            "_ = bytearray(320 * 1024 * 1024); "
            "sys.stdout.write('{\"ok\":true,\"text\":\"unexpected\",\"extracted_from\":\"html\"}')"
        )
        with monkeypatch.context() as mp:
            mp.setattr(extract_worker, "EXTRACT_MEMORY_LIMIT_MB", 128)
            mp.setattr(
                extract_worker,
                "_worker_command",
                lambda _mb: [sys.executable, "-c", mem_code],
            )
            t0 = time.perf_counter()
            r_mem = _run(
                ctx,
                "get_attachment",
                message_id="RAW-LIMITS=",
                attachment="mem.html",
                mode="text",
            )
            dt_mem = time.perf_counter() - t0
        assert dt_mem < 6.0 * TIME_MARGIN
        assert r_mem["ok"] is False and r_mem["error"]["code"] == "validation"
        assert r_mem["error"]["message"] == expected_msg
        ok2 = _run(
            ctx,
            "get_attachment",
            message_id="RAW-LIMITS=",
            attachment="ok2.html",
            mode="text",
        )
        assert ok2["ok"] is True and ok2["text"] == "clean-2"

    # 3. Worker prints non-JSON garbage.
    with monkeypatch.context() as mp:
        mp.setattr(
            extract_worker,
            "_worker_command",
            lambda _mb: [
                sys.executable,
                "-c",
                "import sys; sys.stdin.buffer.readline(); sys.stdout.write('NOT-JSON{{{')",
            ],
        )
        t0 = time.perf_counter()
        r_garb = _run(
            ctx,
            "get_attachment",
            message_id="RAW-LIMITS=",
            attachment="garb.html",
            mode="text",
        )
        dt_garb = time.perf_counter() - t0
    assert dt_garb < 4.5 * TIME_MARGIN
    assert r_garb["ok"] is False and r_garb["error"]["code"] == "validation"
    assert r_garb["error"]["message"] == expected_msg
    ok3 = _run(
        ctx, "get_attachment", message_id="RAW-LIMITS=", attachment="ok3.html", mode="text"
    )
    assert ok3["ok"] is True and ok3["text"] == "clean-3"

    # 4. Worker prints more than EXTRACT_MAX_OUTPUT_BYTES (4 KiB injected limit).
    with monkeypatch.context() as mp:
        mp.setattr(extract_worker, "EXTRACT_MAX_OUTPUT_BYTES", 4096)
        mp.setattr(
            extract_worker,
            "_worker_command",
            lambda _mb: [
                sys.executable,
                "-c",
                (
                    "import sys; sys.stdin.buffer.readline(); "
                    "sys.stdout.write('{\"ok\":true,\"extracted_from\":\"html\",\"text\":\"' "
                    "+ ('x' * 500000) + '\"}')"
                ),
            ],
        )
        t0 = time.perf_counter()
        r_out = _run(
            ctx,
            "get_attachment",
            message_id="RAW-LIMITS=",
            attachment="out.html",
            mode="text",
        )
        dt_out = time.perf_counter() - t0
    assert dt_out < 4.5 * TIME_MARGIN
    assert r_out["ok"] is False and r_out["error"]["code"] == "validation"
    assert r_out["error"]["message"] == expected_msg
    ok4 = _run(
        ctx, "get_attachment", message_id="RAW-LIMITS=", attachment="ok4.html", mode="text"
    )
    assert ok4["ok"] is True and ok4["text"] == "clean-4"


def test_five_failing_documents_do_not_open_circuit_breaker_and_gateway_not_held(
    tmp_path, monkeypatch
):
    deep_xml = "<w:p>" + ("<w:r>" * 1000) + "<w:t>deep</w:t>" + ("</w:r>" * 1000) + "</w:p>"
    deep_docx = _docx_bytes(deep_xml)
    long_ref = "A" * 400_000 + "1"
    bad_ref_xlsx = _xlsx_bytes([("S1", f'<row r="1"><c r="{long_ref}"><v>1</v></c></row>')])
    zero_slide_pptx = _pptx_bytes([])
    long_bad_name = "bad-" + ("z" * 500) + ".docx"

    html_msg = Message(
        subject="Attached HTML Message",
        sender=Mailbox(name="Desk", email_address="desk@example.com"),
        to_recipients=[Mailbox(email_address="team@example.com")],
        body="<html><body><h2>Heading</h2><p>Item HTML content</p></body></html>",
    )
    html_item_att = ItemAttachment(name="Attached HTML Message", item=html_msg)

    atts = [
        _IdCountingAttachment("F1", "deep.docx", deep_docx, "application/octet-stream"),
        _IdCountingAttachment("F2", long_bad_name, b"PK-not-a-valid-zip", "application/octet-stream"),
        _IdCountingAttachment("F3", "badref.xlsx", bad_ref_xlsx, "application/octet-stream"),
        _IdCountingAttachment("F4", "corrupt.pptx", b"not-an-archive-at-all", "application/octet-stream"),
        _IdCountingAttachment("F5", "broken.xlsx", b"not-a-zip-archive", "application/octet-stream"),
        _IdCountingAttachment(
            "OK-HTML", "page.html", b"<html><body><p>Clean HTML</p></body></html>", "text/html"
        ),
        html_item_att,
        _IdCountingAttachment("C1", "c1.html", b"<p>conc-1</p>", "text/html"),
        _IdCountingAttachment("C2", "c2.html", b"<p>conc-2</p>", "text/html"),
        _IdCountingAttachment("C3", "c3.html", b"<p>conc-3</p>", "text/html"),
        _IdCountingAttachment("EMPTY-PPTX", "empty.pptx", zero_slide_pptx, "application/octet-stream"),
    ]
    item = _neutral_msg("RAW-CB=", "Circuit breaker check")
    item.attachments = atts
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account, circuit_failure_threshold=5)

    worker_calls = []
    real_run_sync = extract_worker._run_worker_sync

    def spy_run_sync(header, payload, **kwargs):
        worker_calls.append((header.get("op"), ctx.gateway.in_worker))
        return real_run_sync(header, payload, **kwargs)

    monkeypatch.setattr(extract_worker, "_run_worker_sync", spy_run_sync)

    # 1. Five failing documents in a row must all be bounded `validation` errors
    # and must NOT increment or open the circuit breaker.
    for idx in range(5):
        res = _run(ctx, "get_attachment", message_id="RAW-CB=", attachment=str(idx), mode="text")
        assert res["ok"] is False
        assert res["error"]["code"] == "validation"
        assert len(res["error"]["message"]) <= extract.MAX_ERROR_MESSAGE_CHARS
        assert ("z" * 81) not in res["error"]["message"]
    assert ctx._circuit_failures == 0
    assert ctx._circuit_open_until == 0.0

    # An empty presentation returns ok with empty text and a note that it holds no readable text.
    r_empty = _run(ctx, "get_attachment", message_id="RAW-CB=", attachment="empty.pptx", mode="text")
    assert r_empty["ok"] is True
    assert r_empty["mode"] == "text"
    assert r_empty["extracted_from"] == "pptx"
    assert r_empty["slides"] == 0
    assert r_empty["text"] == ""
    assert "no readable text" in r_empty["note"].lower()

    # 2. Both HTML bodies and ItemAttachments run through the worker with gateway released.
    r_html = _run(ctx, "get_attachment", message_id="RAW-CB=", attachment="page.html", mode="text")
    assert r_html["ok"] is True and "Clean HTML" in r_html["text"]

    r_item = _run(ctx, "get_attachment", message_id="RAW-CB=", attachment="6", mode="auto")
    assert r_item["ok"] is True and r_item["extracted_from"] == "message"
    assert "Item HTML content" in r_item["text"]

    ops_seen = [op for op, _in_gw in worker_calls]
    assert "bytes" in ops_seen and "item" in ops_seen
    assert all(in_gw == 0 for _op, in_gw in worker_calls)

    # 3. At most EXTRACT_MAX_CONCURRENT = 2 extractions run simultaneously; a third waits.
    lock = threading.Lock()
    active = [0]
    peak = [0]

    def paced_run_sync(header, payload, **kwargs):
        with lock:
            active[0] += 1
            if active[0] > peak[0]:
                peak[0] = active[0]
        try:
            time.sleep(0.15)
            return ExtractionResult_or_real(header, payload, **kwargs)
        finally:
            with lock:
                active[0] -= 1

    def ExtractionResult_or_real(header, payload, **kwargs):
        return extract.extract_html(payload)

    with monkeypatch.context() as mp:
        mp.setattr(extract_worker, "EXTRACT_MAX_CONCURRENT", 2)
        mp.setattr(extract_worker, "_run_worker_sync", paced_run_sync)

        async def run_three():
            return await asyncio.gather(
                dispatch(
                    ctx, SPECS["get_attachment"],
                    {"message_id": "RAW-CB=", "attachment": "c1.html", "mode": "text"},
                ),
                dispatch(
                    ctx, SPECS["get_attachment"],
                    {"message_id": "RAW-CB=", "attachment": "c2.html", "mode": "text"},
                ),
                dispatch(
                    ctx, SPECS["get_attachment"],
                    {"message_id": "RAW-CB=", "attachment": "c3.html", "mode": "text"},
                ),
            )

        results = asyncio.run(run_three())

    assert [r["text"] for r in results] == ["conc-1", "conc-2", "conc-3"]
    assert peak[0] == 2


def test_smime_p7m_p7s_and_csv_prefixes_match_main_and_auto_is_cache_independent(tmp_path):
    signed_p7m = _IdCountingAttachment(
        "S-SIGNED", "smime.p7m", _P7M_SIGNED_HEAD, "application/pkcs7-mime"
    )
    env_p7m = _IdCountingAttachment(
        "S-ENV", "enc.p7m", _P7M_ENVELOPED_HEAD, "application/pkcs7-mime"
    )
    blob_p7m = _IdCountingAttachment(
        "S-BLOB", "blob.p7m", b"not-a-valid-der-header", "application/pkcs7-mime"
    )
    sig_p7s = _IdCountingAttachment(
        "S-SIG", "smime.p7s", b"\x30\x82\x00\x04detached-sig", "application/pkcs7-signature"
    )
    mz_csv = _IdCountingAttachment(
        "CSV-MZ", "mz.csv", b"MZ,count\nPK,42\n", "text/csv"
    )
    pk_txt = _IdCountingAttachment(
        "TXT-PK", "notes.txt", b"PK\x03\x04 plain log line\n", "text/plain"
    )
    docx_att = _IdCountingAttachment(
        "DOC-1",
        "memo.docx",
        _docx_bytes("<w:p><w:r><w:t>Memo text</w:t></w:r></w:p>"),
        "application/octet-stream",
    )
    png_att = _IdCountingAttachment("PNG-1", "chart.png", b"\x89PNG\r\n\x1a\n", "image/png")

    item = _neutral_msg("RAW-PARTE=", "Part E parity")
    item.attachments = [
        signed_p7m, env_p7m, blob_p7m, sig_p7s, mz_csv, pk_txt, docx_att, png_att
    ]
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)

    # 1. Signed .p7m in info / auto / text / save matches `main`.
    s_info = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="smime.p7m", mode="info")
    assert s_info["mode"] == "info" and s_info["smime_type"] == "signed" and "text" not in s_info
    s_auto_1 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="smime.p7m", mode="auto")
    assert s_auto_1["mode"] == "text" and s_auto_1["smime_type"] == "signed"
    assert "extracted_from" not in s_auto_1 and "not encrypted" in s_auto_1["hint"]
    s_text = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="smime.p7m", mode="text")
    assert s_text["mode"] == "text" and s_text["smime_type"] == "signed"
    s_auto_2 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="smime.p7m", mode="auto")
    assert s_auto_2 == s_auto_1
    s_save = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="smime.p7m", mode="save")
    assert s_save["mode"] == "save" and Path(s_save["saved_path"]).read_bytes() == _P7M_SIGNED_HEAD

    # 2. Enveloped .p7m: auto stays info, explicit text decodes raw UTF-8 as on `main`,
    # and auto after text still returns info.
    e_auto_1 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="enc.p7m", mode="auto")
    assert e_auto_1["mode"] == "info" and e_auto_1["smime_type"] == "enveloped"
    e_text = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="enc.p7m", mode="text")
    assert e_text["ok"] is True and e_text["mode"] == "text"
    assert "opaque-encrypted-bytes" in e_text["text"]
    e_auto_2 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="enc.p7m", mode="auto")
    assert e_auto_2 == e_auto_1

    # 3. Unrecognized .p7m: auto -> info, text -> text, auto -> info (no cache pollution).
    b_auto_1 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="blob.p7m", mode="auto")
    assert b_auto_1["mode"] == "info" and "smime_type" not in b_auto_1
    b_text = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="blob.p7m", mode="text")
    assert b_text["ok"] is True and b_text["mode"] == "text"
    assert b_text["text"] == "not-a-valid-der-header"
    b_auto_2 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="blob.p7m", mode="auto")
    assert b_auto_2 == b_auto_1

    # 4. Detached signature .p7s: info & auto stay info without reading bytes; text & save work;
    # auto after text is unchanged.
    p7s_info = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="smime.p7s", mode="info")
    p7s_auto_1 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="smime.p7s", mode="auto")
    assert p7s_info["mode"] == "info" and p7s_auto_1["mode"] == "info"
    assert sig_p7s.reads == 0
    p7s_text = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="smime.p7s", mode="text")
    assert p7s_text["ok"] is True and p7s_text["mode"] == "text"
    assert "detached-sig" in p7s_text["text"]
    p7s_auto_2 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="smime.p7s", mode="auto")
    assert p7s_auto_2 == p7s_auto_1
    p7s_save = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="smime.p7s", mode="save")
    assert p7s_save["mode"] == "save" and Path(p7s_save["saved_path"]).exists()

    # 5. Text-like files starting with `MZ` or `PK` are read cleanly, and `mode="auto"`
    # returns the exact same result before and after caching.
    csv_auto_1 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="mz.csv", mode="auto")
    assert csv_auto_1["ok"] is True and csv_auto_1["mode"] == "text"
    # plain text is returned as decoded, final newline included, as on main
    assert csv_auto_1["text"] == "MZ,count\nPK,42\n"
    csv_auto_2 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="mz.csv", mode="auto")
    assert csv_auto_2 == csv_auto_1

    txt_res = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="notes.txt", mode="text")
    assert txt_res["ok"] is True and "PK" in txt_res["text"]

    doc_auto_1 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="memo.docx", mode="auto")
    doc_auto_2 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="memo.docx", mode="auto")
    assert doc_auto_1 == doc_auto_2 and docx_att.reads == 1

    png_auto_1 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="chart.png", mode="auto")
    png_auto_2 = _run(ctx, "get_attachment", message_id="RAW-PARTE=", attachment="chart.png", mode="auto")
    assert png_auto_1 == png_auto_2


# --- r5: real child isolation, self memory ceiling, plain-text fast path,
#         zero-start paging, 75% soft deadline, and hostile padded-space PDF ---


def _shared_stream_pdf(content_stream: bytes, *, n_pages: int) -> bytes:
    """Build a valid PDF 1.4 where ``n_pages`` pages share one Flate stream."""
    comp = zlib.compress(content_stream)
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: (
            f"<< /Length {len(comp)} /Filter /FlateDecode >>\nstream\n".encode("ascii")
            + comp
            + b"\nendstream"
        ),
    }
    kids = []
    for idx in range(n_pages):
        obj_num = 5 + idx
        kids.append(f"{obj_num} 0 R".encode("ascii"))
        objs[obj_num] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>"
        )
    objs[2] = (
        b"<< /Type /Pages /Kids ["
        + b" ".join(kids)
        + f"] /Count {n_pages} >>".encode("ascii")
    )
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    max_num = max(objs)
    for num in range(1, max_num + 1):
        offsets[num] = len(out)
        out.extend(f"{num} 0 obj\n".encode("ascii"))
        out.extend(objs[num])
        out.extend(b"\nendobj\n")
    xref_pos = len(out)
    out.extend(f"xref\n0 {max_num + 1}\n0000000000 65535 f \n".encode("ascii"))
    for num in range(1, max_num + 1):
        out.extend(f"{offsets[num]:010d} 00000 n \n".encode("ascii"))
    out.extend(
        (
            f"trailer\n<< /Size {max_num + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(out)


def test_real_child_scrubbed_env_loaded_modules_and_lazy_pypdf(tmp_path):
    # No monkeypatching of os.environ, build_worker_env, or _worker_command.
    assert extract_child.EXTRACT_SOFT_DEADLINE_RATIO == 0.75
    assert extract_child.EXIT_MEMORY_LIMIT_FAILED == 3

    child_src = Path(extract_child.__file__).read_text(encoding="utf-8")
    child_tree = ast.parse(child_src)
    for node in child_tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(("exchangelib", "mcp", "requests", "pypdf"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith(("exchangelib", "mcp", "requests", "pypdf", "errors"))

    scrubbed = extract_worker.build_worker_env()
    assert "USERNAME" not in {k.upper() for k in scrubbed}
    assert "USER" not in {k.upper() for k in scrubbed}

    forbidden_prefixes = (
        "exchangelib",
        "mcp",
        "requests",
        "ewsmcp.tools",
        "ewsmcp.gateway",
        "ewsmcp.errors",
        "ewsmcp.cache",
        "ewsmcp.server",
    )

    docx_payload = _docx_bytes("<w:p><w:r><w:t>Real child isolation</w:t></w:r></w:p>")
    docx_att = _IdCountingAttachment(
        "R5-MOD-DOCX", "clean.docx", docx_payload, "application/octet-stream"
    )
    atts = [docx_att]
    if extract.HAS_PYPDF:
        pdf_payload = _build_minimal_pdf([{"ascii_lines": ["Real child PDF"]}])
        atts.append(
            _IdCountingAttachment(
                "R5-MOD-PDF", "clean.pdf", pdf_payload, "application/pdf"
            )
        )

    item = _neutral_msg("RAW-R5-MOD=", "Real child modules")
    item.attachments = atts
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)

    res_docx = _run(
        ctx, "get_attachment", message_id="RAW-R5-MOD=", attachment="clean.docx"
    )
    assert res_docx["ok"] is True
    assert res_docx["text"] == "Real child isolation"

    cache = mail_read._text_cache(ctx)
    cached_docx = cache.get((("id", "R5-MOD-DOCX"), len(docx_payload)))
    assert cached_docx is not None
    mods_docx = cached_docx.doc.loaded_modules
    # started with -m, the child's own module is named __main__
    assert "__main__" in mods_docx
    assert "ewsmcp.extract" in mods_docx
    assert "ewsmcp.toolerror" in mods_docx
    assert not any(
        any(m == pfx or m.startswith(pfx + ".") or m.startswith(pfx) for pfx in forbidden_prefixes)
        for m in mods_docx
    )
    # pypdf must NOT be imported at child start when handling non-PDF documents.
    assert not any(m == "pypdf" or m.startswith("pypdf.") for m in mods_docx)

    if extract.HAS_PYPDF:
        res_pdf = _run(
            ctx, "get_attachment", message_id="RAW-R5-MOD=", attachment="clean.pdf"
        )
        assert res_pdf["ok"] is True
        assert "Real child PDF" in res_pdf["text"]
        cached_pdf = cache.get((("id", "R5-MOD-PDF"), len(pdf_payload)))
        assert cached_pdf is not None
        mods_pdf = cached_pdf.doc.loaded_modules
        assert "pypdf" in mods_pdf
        assert not any(
            any(m.startswith(pfx) for pfx in forbidden_prefixes)
            for m in mods_pdf
        )


def test_real_child_self_memory_ceiling_too_small_vs_normal(tmp_path, monkeypatch):
    docx_payload = _docx_bytes("<w:p><w:r><w:t>Memory ceiling check</w:t></w:r></w:p>")
    att_small = _IdCountingAttachment(
        "R5-MEM-SMALL", "same.docx", docx_payload, "application/octet-stream"
    )
    att_norm = _IdCountingAttachment(
        "R5-MEM-NORM", "same_ok.docx", docx_payload, "application/octet-stream"
    )
    item = _neutral_msg("RAW-R5-MEM=", "Real child ceiling")
    item.attachments = [att_small, att_norm]
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)

    if extract_worker.memory_ceiling_supported():
        # 2 MB ceiling is smaller than the Python interpreter itself -> child exits
        # before reading stdin and parent returns the validation limits error.
        with monkeypatch.context() as mp:
            mp.setattr(extract_worker, "EXTRACT_MEMORY_LIMIT_MB", 2)
            too_small = _run(
                ctx,
                "get_attachment",
                message_id="RAW-R5-MEM=",
                attachment="same.docx",
                mode="text",
            )
        assert too_small["ok"] is False
        assert too_small["error"]["code"] == "validation"
        assert too_small["error"]["message"] == extract_worker.WORKER_LIMIT_ERROR_MESSAGE

        # Normal ceiling on the exact same file bytes succeeds and reports ceiling in force.
        normal = _run(
            ctx,
            "get_attachment",
            message_id="RAW-R5-MEM=",
            attachment="same_ok.docx",
            mode="text",
        )
        assert normal["ok"] is True
        assert normal["text"] == "Memory ceiling check"
        cached = mail_read._text_cache(ctx).get(
            (("id", "R5-MEM-NORM"), len(docx_payload))
        )
        assert cached is not None
        assert cached.doc.memory_ceiling_enforced is True
    else:
        normal = _run(
            ctx,
            "get_attachment",
            message_id="RAW-R5-MEM=",
            attachment="same_ok.docx",
            mode="text",
        )
        assert normal["ok"] is True
        cached = mail_read._text_cache(ctx).get(
            (("id", "R5-MEM-NORM"), len(docx_payload))
        )
        assert cached is not None
        assert cached.doc.memory_ceiling_enforced is False


def test_plain_text_starts_zero_processes_and_paging_starts_nothing(
    tmp_path, monkeypatch
):
    real_popen = subprocess.Popen
    popen_starts = []

    def counting_popen(*args, **kwargs):
        popen_starts.append(args[0] if args else kwargs.get("args"))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counting_popen)

    # 1. Plain-text family (.txt, .csv, .md, .log, .json, and plain-text MIME)
    # runs in-process and starts ZERO subprocesses.
    text_atts = [
        _IdCountingAttachment("T-TXT", "a.txt", b"alpha text", "text/plain"),
        _IdCountingAttachment("T-CSV", "b.csv", b"col1,col2\n1,2", "text/csv"),
        _IdCountingAttachment("T-MD", "c.md", b"# Heading", "text/markdown"),
        _IdCountingAttachment("T-LOG", "d.log", b"INFO boot ok", "application/octet-stream"),
        _IdCountingAttachment("T-JSON", "e.json", b'{"k": 1}', "application/json"),
        _IdCountingAttachment("T-MIME", "README", b"plain mime note", "text/plain"),
    ]
    # 2. Multi-part .docx (> 40,000 chars -> 3 parts) for testing paging starts/fetches.
    paras = [f"PartLine {i:04d}: " + ("word " * 9).rstrip() for i in range(800)]
    long_docx_bytes = _docx_bytes(
        "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
    )
    long_docx = _IdCountingAttachment(
        "DOC-PAGED", "paged.docx", long_docx_bytes, "application/octet-stream"
    )
    # 3. Every document family through dispatch + real child + real scrubbed env.
    xlsx_bytes = _xlsx_bytes([
        ("Summary", '<row r="1"><c r="A1" t="inlineStr"><is><t>SheetCell</t></is></c></row>')
    ])
    pptx_bytes = _pptx_bytes([
        ("slides/slide1.xml", "<p:sp><p:txBody><a:p><a:r><a:t>DeckSlide</a:t></a:r></a:p></p:txBody></p:sp>")
    ])
    eml_bytes = (
        b"From: ops@example.com\r\nTo: team@example.com\r\nSubject: RealWorkerEML\r\n\r\n"
        b"Message body via worker.\r\n"
    )
    html_bytes = b"<html><body><h1>WorkerHTML</h1><p>Paragraph</p></body></html>"
    embedded_item = ItemAttachment(
        name="EmbeddedItem",
        item=Message(
            subject="WorkerItemSubject",
            sender=Mailbox(name="Ops", email_address="ops@example.com"),
            to_recipients=[Mailbox(email_address="team@example.com")],
            body="<html><body><p>ItemBodyViaWorker</p></body></html>",
        ),
    )

    doc_atts = [
        long_docx,
        _IdCountingAttachment("D-XLSX", "book.xlsx", xlsx_bytes, "application/octet-stream"),
        _IdCountingAttachment("D-PPTX", "deck.pptx", pptx_bytes, "application/octet-stream"),
        _IdCountingAttachment("D-EML", "msg.eml", eml_bytes, "message/rfc822"),
        _IdCountingAttachment("D-HTML", "index.html", html_bytes, "text/html"),
        embedded_item,
    ]
    if extract.HAS_PYPDF:
        pdf_bytes = _build_minimal_pdf([{"ascii_lines": ["WorkerPDFPage"]}])
        doc_atts.append(
            _IdCountingAttachment("D-PDF", "doc.pdf", pdf_bytes, "application/pdf")
        )

    item = _neutral_msg("RAW-R5-ALL=", "Text and docs")
    item.attachments = text_atts + doc_atts
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)

    for att in text_atts:
        r_txt = _run(
            ctx, "get_attachment", message_id="RAW-R5-ALL=", attachment=att.name
        )
        assert r_txt["ok"] is True
        assert r_txt["mode"] == "text"
        assert r_txt["extracted_from"] == "text"
    assert len(popen_starts) == 0

    # Read part 1 of paged.docx -> 1 worker process, 1 attachment read.
    p1 = _run(
        ctx, "get_attachment", message_id="RAW-R5-ALL=", attachment="paged.docx", offset=0
    )
    assert p1["ok"] is True and p1["extracted_from"] == "docx"
    assert p1["next_offset"] == 20_000
    assert len(popen_starts) == 1
    assert long_docx.reads == 1
    fetches_after_p1 = account.fetch.call_count

    # Reading part 2 and part 3 starts NO worker process and fetches NOTHING.
    p2 = _run(
        ctx,
        "get_attachment",
        message_id="RAW-R5-ALL=",
        attachment="paged.docx",
        offset=p1["next_offset"],
    )
    assert p2["ok"] is True and "next_offset" in p2
    assert len(popen_starts) == 1
    assert long_docx.reads == 1
    assert account.fetch.call_count == fetches_after_p1

    p3 = _run(
        ctx,
        "get_attachment",
        message_id="RAW-R5-ALL=",
        attachment="paged.docx",
        offset=p2["next_offset"],
    )
    assert p3["ok"] is True and "next_offset" not in p3
    assert (p1["text"] + p2["text"] + p3["text"]) == "\n".join(paras)
    assert len(popen_starts) == 1
    assert long_docx.reads == 1
    assert account.fetch.call_count == fetches_after_p1

    # Remaining document families through dispatch + real child.
    expected_starts = 1
    for att_sel, kind, snippet in [
        ("book.xlsx", "xlsx", "SheetCell"),
        ("deck.pptx", "pptx", "DeckSlide"),
        ("msg.eml", "eml", "Message body via worker."),
        ("index.html", "html", "WorkerHTML"),
        ("EmbeddedItem", "message", "ItemBodyViaWorker"),
    ]:
        r_doc = _run(
            ctx, "get_attachment", message_id="RAW-R5-ALL=", attachment=att_sel
        )
        expected_starts += 1
        assert r_doc["ok"] is True
        assert r_doc["extracted_from"] == kind
        assert snippet in r_doc["text"]
        assert len(popen_starts) == expected_starts

    if extract.HAS_PYPDF:
        r_pdf = _run(
            ctx, "get_attachment", message_id="RAW-R5-ALL=", attachment="doc.pdf"
        )
        expected_starts += 1
        assert r_pdf["ok"] is True
        assert r_pdf["extracted_from"] == "pdf"
        assert "WorkerPDFPage" in r_pdf["text"]
        assert len(popen_starts) == expected_starts


def test_small_clock_partial_return_repeats_note_and_hostile_padded_pdf_fails(
    tmp_path, monkeypatch
):
    real_popen = subprocess.Popen
    popen_starts = []

    def counting_popen(*args, **kwargs):
        popen_starts.append(1)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counting_popen)

    # Build a 190-sheet workbook sharing one worksheet XML (~415 KB) so each sheet
    # costs ~45-55 ms (~9.5 s total >> 1.2 s soft deadline), sheet 1 exceeds 20,000
    # chars for paging, and 190 sheets stay under MAX_ARCHIVE_TOTAL_BYTES & 5M chars.
    ns_main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns_pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    n_sheets = 190
    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{ns_main}" xmlns:r="{ns_r}"><sheets>'
        + "".join(
            f'<sheet name="S{i}" sheetId="{i}" r:id="rId{i}"/>'
            for i in range(1, n_sheets + 1)
        )
        + "</sheets></workbook>"
    ).encode("utf-8")
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{ns_pkg}">'
        + "".join(
            f'<Relationship Id="rId{i}" Type="{ns_r}/worksheet" Target="worksheets/sheet1.xml"/>'
            for i in range(1, n_sheets + 1)
        )
        + "</Relationships>"
    ).encode("utf-8")
    text_row = (
        '<c r="A1" t="inlineStr"><is><t>AlphaCellValue123456</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>BravoCellValue123456</t></is></c>'
    )
    pad_row = (
        '<c r="A1"/><c r="B1"/><c r="C1"/><c r="D1"/>'
        '<c r="E1"/><c r="F1"/><c r="G1"/><c r="H1"/>'
    )
    ws_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{ns_main}"><sheetData>'
        + "".join(f'<row r="{r}">{text_row}</row>' for r in range(1, 601))
        + "".join(f'<row r="{r}">{pad_row}</row>' for r in range(601, 3501))
        + "</sheetData></worksheet>"
    ).encode("utf-8")
    from test_extract import _zip_bytes

    many_sheets_xlsx = _zip_bytes([
        ("xl/workbook.xml", wb_xml),
        ("xl/_rels/workbook.xml.rels", rels_xml),
        ("xl/worksheets/sheet1.xml", ws_xml),
    ])

    atts = [
        _IdCountingAttachment(
            "R5-MANY-XLSX", "many.xlsx", many_sheets_xlsx, "application/octet-stream"
        ),
        _IdCountingAttachment(
            "R5-AFTER-OK",
            "after.docx",
            _docx_bytes("<w:p><w:r><w:t>Recovered cleanly</w:t></w:r></w:p>"),
            "application/octet-stream",
        ),
    ]

    if extract.HAS_PYPDF:
        # 150-page honest PDF (~52 ms/page => ~7.8 s total >> 1.2 s soft deadline).
        page_lines = [b"BT /F1 1 Tf "] + [
            f"1 0 0 1 10 {y} Tm (HonestPageRowText) Tj ".encode("ascii")
            for y in range(1, 1801)
        ] + [b"ET"]
        many_pages_pdf = _shared_stream_pdf(b"".join(page_lines), n_pages=150)
        # Hostile padded-space PDF (`case_gaps_ltr_020k` from reviewer suite):
        # 20,000 wide horizontal shifts on Page 1 stall inside pypdf layout mode.
        hostile_gaps_stream = (
            b"BT /F1 10 Tf 1 0 0 1 10 700 Tm (A) Tj "
            + b"9000000 0 Td (A) Tj " * 20_000
            + b"ET"
        )
        hostile_gaps_pdf = _shared_stream_pdf(hostile_gaps_stream, n_pages=1)
        atts.append(
            _IdCountingAttachment(
                "R5-MANY-PDF", "long_report.pdf", many_pages_pdf, "application/pdf"
            )
        )
        atts.append(
            _IdCountingAttachment(
                "R5-HOSTILE-PDF", "gaps.pdf", hostile_gaps_pdf, "application/pdf"
            )
        )

    item = _neutral_msg("RAW-R5-CLOCK=", "Clock partial vs hang")
    item.attachments = atts
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)

    with monkeypatch.context() as mp:
        mp.setattr(extract_worker, "EXTRACT_TIMEOUT_SECONDS", 1.6)
        x_p1 = _run(
            ctx,
            "get_attachment",
            message_id="RAW-R5-CLOCK=",
            attachment="many.xlsx",
            offset=0,
        )
    assert x_p1["ok"] is True
    assert x_p1["sheets"] == n_sheets
    assert "AlphaCellValue" in x_p1["text"]
    assert "Stopped after sheet " in x_p1["note"]
    assert f"of {n_sheets} (time limit)" in x_p1["note"]
    assert x_p1["next_offset"] == 20_000

    starts_before_x_p2 = len(popen_starts)
    x_p2 = _run(
        ctx,
        "get_attachment",
        message_id="RAW-R5-CLOCK=",
        attachment="many.xlsx",
        offset=x_p1["next_offset"],
    )
    assert x_p2["ok"] is True
    assert x_p2["note"] == x_p1["note"]
    assert len(popen_starts) == starts_before_x_p2

    if extract.HAS_PYPDF:
        with monkeypatch.context() as mp:
            mp.setattr(extract_worker, "EXTRACT_TIMEOUT_SECONDS", 1.6)
            pdf_p1 = _run(
                ctx,
                "get_attachment",
                message_id="RAW-R5-CLOCK=",
                attachment="long_report.pdf",
                offset=0,
            )
        assert pdf_p1["ok"] is True
        assert pdf_p1["pages"] == 150
        assert "HonestPageRowText" in pdf_p1["text"]
        assert "Stopped after page " in pdf_p1["note"]
        assert "of 150 (time limit)" in pdf_p1["note"]
        assert pdf_p1["next_offset"] == 20_000

        starts_before_pdf_p2 = len(popen_starts)
        pdf_p2 = _run(
            ctx,
            "get_attachment",
            message_id="RAW-R5-CLOCK=",
            attachment="long_report.pdf",
            offset=pdf_p1["next_offset"],
        )
        assert pdf_p2["ok"] is True
        assert pdf_p2["note"] == pdf_p1["note"]
        assert len(popen_starts) == starts_before_pdf_p2

        # Hostile padded-space PDF whose first page hangs inside pypdf returns validation.
        with monkeypatch.context() as mp:
            mp.setattr(extract_worker, "EXTRACT_TIMEOUT_SECONDS", 1.6)
            t0 = time.perf_counter()
            r_hang = _run(
                ctx,
                "get_attachment",
                message_id="RAW-R5-CLOCK=",
                attachment="gaps.pdf",
                mode="text",
            )
            elapsed_hang = time.perf_counter() - t0
        assert elapsed_hang < 6.0 * TIME_MARGIN
        assert r_hang["ok"] is False
        assert r_hang["error"]["code"] == "validation"
        assert r_hang["error"]["message"] == extract_worker.WORKER_LIMIT_ERROR_MESSAGE

        # Subsequent call works cleanly.
        r_after = _run(
            ctx,
            "get_attachment",
            message_id="RAW-R5-CLOCK=",
            attachment="after.docx",
            mode="text",
        )
        assert r_after["ok"] is True
        assert r_after["text"] == "Recovered cleanly"


# --- r6: importance filter, get_messages, list_folders(query), get_message(eml) ---


class _FilteringQuery(_Query):
    """QuerySet double that evaluates structured filters for combined live tests."""

    def filter(self, *args, **kwargs):
        self._root.filter_calls.append((args, kwargs))
        items = list(self)
        if "subject__icontains" in kwargs:
            needle = kwargs["subject__icontains"].lower()
            items = [
                it for it in items
                if needle in (getattr(it, "subject", "") or "").lower()
            ]
        if "categories__contains" in kwargs:
            wanted = set(kwargs["categories__contains"])
            items = [
                it for it in items
                if wanted.issubset(set(getattr(it, "categories", None) or []))
            ]
        if "datetime_received__gte" in kwargs:
            floor = kwargs["datetime_received__gte"]
            items = [it for it in items if it.datetime_received >= floor]
        if "datetime_received__lte" in kwargs:
            ceil = kwargs["datetime_received__lte"]
            items = [it for it in items if it.datetime_received <= ceil]
        if "is_read" in kwargs:
            items = [it for it in items if bool(it.is_read) is kwargs["is_read"]]
        if "has_attachments" in kwargs:
            items = [
                it for it in items
                if bool(it.has_attachments) is kwargs["has_attachments"]
            ]
        if "flag_status" in kwargs:
            items = [
                it for it in items
                if getattr(it, "flag_status", None) == kwargs["flag_status"]
            ]
        if "importance" in kwargs:
            items = [
                it for it in items
                if (getattr(it, "importance", None) or "Normal") == kwargs["importance"]
            ]
        return _FilteringQuery(items, _root=self._root)


def test_search_importance_mirror_and_live_alone_and_combined(tmp_path):
    search_schema = SPECS["search_messages"].public_schema()["inputSchema"]
    validator = jsonschema.Draft202012Validator(search_schema)
    for level in ("high", "normal", "low"):
        validator.validate({"importance": level})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"importance": "urgent"})

    dt_old = datetime(2026, 6, 1, 9, 0, tzinfo=TZ)
    dt_mid = datetime(2026, 6, 10, 9, 0, tzinfo=TZ)
    dt_new = datetime(2026, 6, 12, 9, 0, tzinfo=TZ)

    def build_mirror_row(
        ews_id, subject, sender_email, dt_obj, *, is_read, importance, categories=()
    ):
        row = _cache_row(
            ews_id,
            subject=subject,
            categories=categories,
            date_ts=int(dt_obj.timestamp()),
        )
        row["sender_email"] = sender_email
        row["sender_name"] = sender_email.split("@")[0].title()
        row["date_iso"] = dt_obj.isoformat(timespec="minutes")
        row["is_read"] = 1 if is_read else 0
        row["importance"] = importance
        return row

    store = CacheStore(tmp_path / "mirror.db")
    try:
        store.upsert_messages([
            build_mirror_row(
                "C-HIGH-MATCH", "Target escalation", "alice@example.com",
                dt_new, is_read=False, importance="High",
                categories=["Finance", "Review"],
            ),
            build_mirror_row(
                "C-HIGH-OLD", "Old escalation", "alice@example.com",
                dt_old, is_read=False, importance="High",
                categories=["Finance"],
            ),
            build_mirror_row(
                "C-HIGH-READ", "Read escalation", "alice@example.com",
                dt_mid, is_read=True, importance="High",
                categories=["Finance"],
            ),
            build_mirror_row(
                "C-HIGH-OTHER", "Other sender escalation", "bob@example.com",
                dt_mid, is_read=False, importance="High",
                categories=["Finance"],
            ),
            build_mirror_row(
                "C-NORM-EXP", "Explicit normal", "alice@example.com",
                dt_mid, is_read=False, importance="Normal",
                categories=["Finance"],
            ),
            build_mirror_row(
                "C-NORM-NULL", "Implicit normal", "bob@example.com",
                dt_old, is_read=True, importance=None,
            ),
            build_mirror_row(
                "C-LOW", "Digest bulletin", "news@example.com",
                dt_mid, is_read=False, importance="Low",
                categories=["Digest"],
            ),
        ])
        store.set_sync_state("item:inbox", "TOK", int(time.time()))

        def build_live_msg(
            raw_id, subject, sender_email, dt_obj, *, is_read, importance, categories=None
        ):
            m = _msg(
                raw_id,
                subject=subject,
                sender=sender_email,
                sender_name=sender_email.split("@")[0].title(),
                dt=dt_obj,
                is_read=is_read,
                message_id=f"<{raw_id}@example.com>",
                categories=categories,
            )
            m.importance = importance
            return m

        live_inbox = _FilteringQuery([
            build_live_msg(
                "L-HIGH-MATCH=", "Target escalation", "alice@example.com",
                dt_new, is_read=False, importance="High",
                categories=["Finance", "Review"],
            ),
            build_live_msg(
                "L-HIGH-OLD=", "Old escalation", "alice@example.com",
                dt_old, is_read=False, importance="High",
                categories=["Finance"],
            ),
            build_live_msg(
                "L-HIGH-READ=", "Read escalation", "alice@example.com",
                dt_mid, is_read=True, importance="High",
                categories=["Finance"],
            ),
            build_live_msg(
                "L-HIGH-OTHER=", "Other sender escalation", "bob@example.com",
                dt_mid, is_read=False, importance="High",
                categories=["Finance"],
            ),
            build_live_msg(
                "L-NORM=", "Normal update", "alice@example.com",
                dt_mid, is_read=False, importance="Normal",
                categories=["Finance"],
            ),
            build_live_msg(
                "L-LOW=", "Digest bulletin", "news@example.com",
                dt_mid, is_read=False, importance="Low",
                categories=["Digest"],
            ),
        ])
        live_inbox_sub = _FilteringQuery([
            build_live_msg(
                "L-SUB-HIGH=", "Subfolder escalation", "alice@example.com",
                datetime(2026, 6, 11, 14, 0, tzinfo=TZ),
                is_read=False, importance="High",
                categories=["Finance"],
            ),
            build_live_msg(
                "L-SUB-NORM=", "Subfolder normal", "alice@example.com",
                dt_mid, is_read=False, importance="Normal",
                categories=["Finance"],
            ),
        ])
        live_archive = _FilteringQuery([
            build_live_msg(
                "L-ARCH-HIGH=", "Archived escalation", "alice@example.com",
                datetime(2026, 6, 11, 9, 0, tzinfo=TZ),
                is_read=False, importance="High",
                categories=["Finance"],
            ),
            build_live_msg(
                "L-ARCH-LOW=", "Archived digest", "news@example.com",
                dt_old, is_read=True, importance="Low",
            ),
        ])
        account = _account(inbox=live_inbox)
        account.mail_folders = [
            ("Inbox", live_inbox),
            ("Inbox/Sub", live_inbox_sub),
            ("Archive", live_archive),
        ]
        ctx = _ctx(tmp_path, account, cache=store)

        # 1. Mirror lane: alone for high, normal, low.
        c_high = _run(ctx, "search_messages", importance="high")
        assert c_high["ok"] is True and c_high["source"] == "cache"
        assert c_high["total_available"] == 4
        assert all(i["importance"] == "high" for i in c_high["items"])
        assert live_inbox.filter_calls == []

        c_low = _run(ctx, "search_messages", importance="low")
        assert c_low["source"] == "cache" and c_low["total_available"] == 1
        assert c_low["items"][0]["subject"] == "Digest bulletin"
        assert c_low["items"][0]["importance"] == "low"

        c_norm = _run(ctx, "search_messages", importance="normal")
        assert c_norm["source"] == "cache" and c_norm["total_available"] == 2
        assert {i["subject"] for i in c_norm["items"]} == {
            "Explicit normal", "Implicit normal",
        }
        assert all("importance" not in i for i in c_norm["items"])

        # 2. Mirror lane: combined with sender, date window, unread, and categories.
        c_comb = _run(
            ctx,
            "search_messages",
            importance="high",
            sender="alice@example.com",
            since="2026-06-05",
            until="2026-06-15",
            is_unread=True,
            categories=["Finance"],
        )
        assert c_comb["source"] == "cache"
        assert [i["subject"] for i in c_comb["items"]] == ["Target escalation"]
        assert c_comb["total_available"] == 1

        # 3. Live lane: alone for high, normal, low — never calls QuerySet.count().
        l_high = _run(ctx, "search_messages", importance="high", fresh=True)
        assert l_high["source"] == "live"
        assert len(l_high["items"]) == 4
        assert all(i["importance"] == "high" for i in l_high["items"])
        assert live_inbox.filter_calls[-1] == ((), {"importance": "High"})
        assert live_inbox.count_calls == 0
        assert live_inbox.refresh_calls == 0

        l_norm = _run(ctx, "search_messages", importance="normal", fresh=True)
        assert l_norm["source"] == "live"
        assert [i["subject"] for i in l_norm["items"]] == ["Normal update"]
        assert "importance" not in l_norm["items"][0]
        assert live_inbox.filter_calls[-1] == ((), {"importance": "Normal"})
        assert live_inbox.count_calls == 0

        l_low = _run(ctx, "search_messages", importance="low", fresh=True)
        assert l_low["source"] == "live"
        assert [i["subject"] for i in l_low["items"]] == ["Digest bulletin"]
        assert l_low["items"][0]["importance"] == "low"
        assert live_inbox.filter_calls[-1] == ((), {"importance": "Low"})
        assert live_inbox.count_calls == 0

        # 4. Live lane: combined with sender, date window, unread, and categories.
        l_comb = _run(
            ctx,
            "search_messages",
            importance="high",
            sender="alice@example.com",
            since="2026-06-05",
            until="2026-06-15",
            is_unread=True,
            categories=["Finance"],
            fresh=True,
        )
        assert l_comb["source"] == "live"
        assert [i["subject"] for i in l_comb["items"]] == ["Target escalation"]

        # 5. Whole-mailbox search (folder="f:all") combined with importance & filters.
        whole = _run(
            ctx,
            "search_messages",
            folder="f:all",
            importance="high",
            sender="alice@example.com",
            since="2026-06-05",
            is_unread=True,
            categories=["Finance"],
        )
        assert whole["ok"] is True and whole["source"] == "live"
        assert whole["folders_searched"] == 3
        assert [i["subject"] for i in whole["items"]] == [
            "Target escalation",
            "Subfolder escalation",
            "Archived escalation",
        ]
        assert [i["folder"] for i in whole["items"]] == [
            "Inbox", "Inbox/Sub", "Archive",
        ]

        # 6. Recursive search (recursive=True) combined with importance & filters.
        rec = _run(
            ctx,
            "search_messages",
            folder="Inbox",
            recursive=True,
            importance="high",
            sender="alice@example.com",
            since="2026-06-05",
            is_unread=True,
            categories=["Finance"],
        )
        assert rec["ok"] is True and rec["source"] == "live"
        assert rec["folders_searched"] == 2
        assert [i["subject"] for i in rec["items"]] == [
            "Target escalation",
            "Subfolder escalation",
        ]
        assert [i["folder"] for i in rec["items"]] == ["Inbox", "Inbox/Sub"]
        assert live_inbox.count_calls == 0
        assert live_inbox_sub.count_calls == 0
        assert live_archive.count_calls == 0

        # Validation: unknown importance and AQS query conflict.
        bad_imp = _run(ctx, "search_messages", importance="urgent")
        assert bad_imp["ok"] is False and bad_imp["error"]["code"] == "validation"
        conflict = _run(
            ctx, "search_messages", query="from:alice@example.com", importance="high"
        )
        assert conflict["ok"] is False and conflict["error"]["code"] == "validation"
    finally:
        store.close()


def test_get_messages_order_limit_partial_failure_formats_no_download_and_tier(
    tmp_path,
):
    class ErrorItemNotFound(Exception):
        pass

    class ErrorAccessDenied(Exception):
        pass

    schema = SPECS["get_messages"].public_schema()["inputSchema"]
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate({"ids": ["m1", "m2"], "format": "concise"})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"ids": []})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"ids": [f"m{i}" for i in range(21)]})

    account = _account()
    ctx = _ctx(tmp_path, account, ews_capability_tier="read")
    build_registry(ctx)
    assert "get_messages" in ctx.registry
    assert ctx.registry["get_messages"].side_effect_class == "read"

    gw = ctx.gateway
    pdf = _CountingAttachment(
        "contract.pdf", b"%PDF-1.7-payload", "application/pdf", gateway=gw
    )
    txt = _CountingAttachment(
        "notes.txt", b"plain notes", "text/plain", gateway=gw
    )
    embedded = _CountingItemAttachment(
        "ATT-EMB", "Forwarded", _neutral_msg("EMB-1=", "Inner")
    )
    small_p7m = _CountingAttachment(
        "smime.p7m", _P7M_SIGNED_HEAD, "application/pkcs7-mime", gateway=gw
    )
    huge_p7m = _CountingAttachment(
        "huge.p7m",
        _P7M_SIGNED_HEAD,
        "application/pkcs7-mime",
        size=5 * 1024 * 1024 + 1,
        gateway=gw,
    )

    msg1 = _neutral_msg("RAW-GM-1=", "First message")
    msg1.text_body = "First cleaned body." + QUOTED_TAIL
    msg1.has_attachments = True
    msg1.attachments = [pdf, txt, embedded, small_p7m, huge_p7m]

    msg2 = _neutral_msg("RAW-GM-2=", "Second message")
    msg2.text_body = "Second cleaned body."
    msg2.importance = "Low"

    msg3 = _neutral_msg("RAW-GM-3=", "Third message")
    msg3.text_body = "Third cleaned body."
    msg3.importance = "High"

    by_raw = {
        "RAW-GM-1=": msg1,
        "RAW-GM-2=": msg2,
        "RAW-GM-3=": msg3,
        "RAW-GM-BAD=": ErrorItemNotFound("RAW-GM-BAD= item moved or deleted"),
    }
    fetch_calls = []

    def fake_fetch(ids, only_fields=None):
        ids_list = list(ids)
        fetch_calls.append(([r for r, _ck in ids_list], list(only_fields or [])))
        return [
            by_raw.get(raw_id, ErrorItemNotFound(f"{raw_id} missing"))
            for raw_id, _ck in ids_list
        ]

    account.fetch = fake_fetch

    a1 = ctx.aliaser.alias_for("RAW-GM-1=", "m")
    a2 = ctx.aliaser.alias_for("RAW-GM-2=", "m")
    a3 = ctx.aliaser.alias_for("RAW-GM-3=", "m")
    a_bad = ctx.aliaser.alias_for("RAW-GM-BAD=", "m")

    # 1. Concise format: order kept (including duplicates), canonical envelope,
    #    zero attachment reads.
    concise = _run(ctx, "get_messages", ids=[a3, a1, a2], format="concise")
    assert concise["ok"] is True and concise["source"] == "live"
    assert {"ok", "items", "count", "total_available", "next_offset"}.issubset(
        concise.keys()
    )
    assert concise["count"] == 3 and concise["total_available"] == 3
    assert concise["next_offset"] is None
    assert concise["failed"] == []
    assert [it["id"] for it in concise["items"]] == [a3, a1, a2]
    assert [it["subject"] for it in concise["items"]] == [
        "Third message", "First message", "Second message",
    ]
    assert concise["items"][0]["importance"] == "high"
    assert concise["items"][2]["importance"] == "low"
    assert "snippet" in concise["items"][1] and "body" not in concise["items"][1]
    assert "OLD QUOTED" not in concise["items"][1]["snippet"]
    assert [pdf.reads, txt.reads, embedded.reads, small_p7m.reads, huge_p7m.reads] == [
        0, 0, 0, 0, 0,
    ]
    assert "mime_content" not in fetch_calls[-1][1]

    dups = _run(ctx, "get_messages", ids=[a2, a1, a2, a3, a1], format="concise")
    assert [it["id"] for it in dups["items"]] == [a2, a1, a2, a3, a1]
    assert dups["count"] == 5 and dups["total_available"] == 5

    # 2. Full format + one bad id among good ones: order kept, ZERO attachment
    #    byte downloads (even on PKCS#7 .p7m candidates, which report
    #    smime_type='candidate' from metadata only).
    full = _run(ctx, "get_messages", ids=[a2, a_bad, a1], format="full")
    assert full["ok"] is True and full["source"] == "live"
    assert full["count"] == 2 and full["total_available"] == 2
    assert full["next_offset"] is None
    assert [it["id"] for it in full["items"]] == [a2, a1]
    assert full["items"][0]["body"] == "Second cleaned body."
    assert "snippet" not in full["items"][0]
    assert full["items"][1]["body"] == "First cleaned body."
    assert pdf.reads == 0 and txt.reads == 0 and embedded.reads == 0
    assert small_p7m.reads == 0 and small_p7m.read_in_worker == []
    assert huge_p7m.reads == 0
    assert full["items"][1]["attachments"][3]["smime_type"] == "candidate"
    assert full["items"][1]["attachments"][4]["smime_type"] == "candidate"
    assert "mime_content" not in fetch_calls[-1][1]
    assert len(full["failed"]) == 1
    assert full["failed"][0]["id"] == a_bad
    assert full["failed"][0]["index"] == 1
    assert full["failed"][0]["code"] == "not_found"
    assert "ErrorItemNotFound" in full["failed"][0]["error"]
    assert "RAW-GM-BAD=" not in full["failed"][0]["error"]

    # format=None keeps default 'full'.
    null_fmt = _run(ctx, "get_messages", ids=[a2], format=None)
    assert null_fmt["ok"] is True and "body" in null_fmt["items"][0]

    # 3. Unknown alias, non-message aliases (folder, thread, attachment),
    #    padded alias, and unminted garbage raw id: per-id failed entries carry
    #    exact caller id + index, non-message aliases never touch Exchange,
    #    and nothing failed is minted in aliases.db.
    f_alias = ctx.aliaser.alias_for("FOLDER-RAW-ID=", "f")
    t_alias = ctx.aliaser.alias_for("CONV-RAW-ID=", "t")
    stats_before = ctx.aliaser.stats()
    mixed_bad = _run(
        ctx,
        "get_messages",
        ids=[
            f" {a1} ",
            "m99999",
            f_alias,
            t_alias,
            "a0",
            "some-garbage-raw",
            a2,
        ],
        format="concise",
    )
    assert mixed_bad["ok"] is True
    assert [it["id"] for it in mixed_bad["items"]] == [a1, a2]
    assert [(e["id"], e["index"], e["code"]) for e in mixed_bad["failed"]] == [
        ("m99999", 1, "not_found"),
        (f_alias, 2, "validation"),
        (t_alias, 3, "validation"),
        ("a0", 4, "validation"),
        ("some-garbage-raw", 5, "not_found"),
    ]
    for non_msg_err in mixed_bad["failed"][1:4]:
        assert "not a message id" in non_msg_err["error"]
    assert "FOLDER-RAW-ID=" not in fetch_calls[-1][0]
    assert "CONV-RAW-ID=" not in fetch_calls[-1][0]
    assert fetch_calls[-1][0] == ["RAW-GM-1=", "some-garbage-raw", "RAW-GM-2="]
    assert ctx.aliaser.stats() == stats_before

    # 4. Library raising mid-stream instead of yielding: falls back to per-id
    #    fetch so only the raising id lands in failed and circuit stays at 0.
    def raising_fetch(ids, only_fields=None):
        for raw_id, _ck in ids:
            if raw_id == "RAW-GM-2=":
                raise ErrorAccessDenied(
                    "Access is denied. Check credentials and try again."
                )
            yield by_raw[raw_id]

    account.fetch = raising_fetch
    raised_res = _run(ctx, "get_messages", ids=[a1, a2, a3], format="concise")
    assert raised_res["ok"] is True
    assert [it["id"] for it in raised_res["items"]] == [a1, a3]
    assert len(raised_res["failed"]) == 1
    assert raised_res["failed"][0]["id"] == a2
    assert raised_res["failed"][0]["index"] == 1
    assert raised_res["failed"][0]["code"] == "upstream_error"
    assert "ErrorAccessDenied" in raised_res["failed"][0]["error"]
    assert ctx._circuit_failures == 0
    account.fetch = fake_fetch

    # 5. Mirror lane (all-cache) and mixed lanes (cache + live):
    store = CacheStore(tmp_path / "gm_mirror.db")
    try:
        now = int(time.time())
        store.upsert_messages([
            _cache_row("RAW-GM-1=", subject="Cached 1", date_ts=now - 30),
            _cache_row("RAW-GM-3=", subject="Cached 3", date_ts=now - 10),
        ])
        store.set_sync_state("item:inbox", "TOK", now - 120)
        ctx.cache = store

        before_fetches = len(fetch_calls)
        cached_res = _run(ctx, "get_messages", ids=[a3, a1], format="concise")
        assert cached_res["source"] == "cache" and cached_res["as_of"]
        assert [it["subject"] for it in cached_res["items"]] == [
            "Cached 3", "Cached 1",
        ]
        assert all("source" not in it for it in cached_res["items"])
        assert cached_res["failed"] == []
        assert len(fetch_calls) == before_fetches

        # Mixed lanes: RAW-GM-1= from mirror, RAW-GM-2= from live EWS.
        mixed_lane = _run(ctx, "get_messages", ids=[a1, a2], format="full")
        assert mixed_lane["ok"] is True
        assert mixed_lane["source"] == "mixed"
        assert "as_of" not in mixed_lane
        assert mixed_lane["items"][0]["subject"] == "Cached 1"
        assert mixed_lane["items"][0]["source"] == "cache"
        assert mixed_lane["items"][0]["as_of"]
        assert mixed_lane["items"][1]["subject"] == "Second message"
        assert mixed_lane["items"][1]["source"] == "live"
        assert "as_of" not in mixed_lane["items"][1]
        assert fetch_calls[-1][0] == ["RAW-GM-2="]
    finally:
        ctx.cache = None
        store.close()

    # 6. 20-id batch with S/MIME attachments: 0 attachment reads; 21 and empty
    #    fail with validation; no mailbox-changing calls on account.
    batch_p7s = []
    for idx in range(20):
        raw_twenty = f"RAW-20-{idx}="
        att = _CountingAttachment(
            "smime.p7m",
            _P7M_SIGNED_HEAD + b"x" * 2048,
            "application/pkcs7-mime",
            gateway=gw,
        )
        batch_p7s.append(att)
        m_twenty = _neutral_msg(raw_twenty, f"Batch {idx}")
        m_twenty.has_attachments = True
        m_twenty.attachments = [att]
        by_raw[raw_twenty] = m_twenty
    twenty_ids = [
        ctx.aliaser.alias_for(f"RAW-20-{idx}=", "m") for idx in range(20)
    ]
    ok_20 = _run(ctx, "get_messages", ids=twenty_ids, format="full")
    assert ok_20["ok"] is True and ok_20["count"] == 20
    assert sum(p.reads for p in batch_p7s) == 0
    assert all(
        it["attachments"][0]["smime_type"] == "candidate"
        for it in ok_20["items"]
    )

    before_over = len(fetch_calls)
    over_20 = _run(ctx, "get_messages", ids=[*twenty_ids, a1], format="concise")
    assert over_20["ok"] is False and over_20["error"]["code"] == "validation"
    assert "20" in over_20["error"]["message"]
    assert len(fetch_calls) == before_over

    empty_ids = _run(ctx, "get_messages", ids=[], format="concise")
    assert empty_ids["ok"] is False and empty_ids["error"]["code"] == "validation"

    eml_fmt = _run(ctx, "get_messages", ids=[a1], format="eml")
    assert eml_fmt["ok"] is False and eml_fmt["error"]["code"] == "validation"

    assert account.mock_calls == []


def test_list_folders_query_matches_name_and_path_case_and_ignores_depth(tmp_path):
    root_node = SimpleNamespace(id="FLD-ROOT=", name="root")
    top_node = SimpleNamespace(id="FLD-TOP=", name="Top of Information Store")

    signed = _folder("FLD-SIGNED=", "Signed Contracts", total=3)
    yr_2026 = _folder("FLD-2026=", "2026", total=0, children=[signed])
    projects = _folder("FLD-PROJ=", "Projects", total=2, children=[yr_2026])
    inbox_fld = _folder(
        "FLD-INBOX=", "Inbox", total=10, unread=1,
        children=[projects], folder_class="IPF.Note",
    )
    audits = _folder("FLD-AUD=", "Audits", total=5)
    finance = _folder("FLD-FIN=", "Finance", total=1, children=[audits])
    archive_fld = _folder("FLD-ARCH=", "Archive", total=0, children=[finance])

    inbox_fld.parts = [root_node, top_node, inbox_fld]
    projects.parts = [root_node, top_node, inbox_fld, projects]
    yr_2026.parts = [root_node, top_node, inbox_fld, projects, yr_2026]
    signed.parts = [root_node, top_node, inbox_fld, projects, yr_2026, signed]
    archive_fld.parts = [root_node, top_node, archive_fld]
    finance.parts = [root_node, top_node, archive_fld, finance]
    audits.parts = [root_node, top_node, archive_fld, finance, audits]

    account = _account()
    account.msg_folder_root = _folder(
        "FLD-ROOT=", "root", children=[inbox_fld, archive_fld]
    )
    account.inbox = inbox_fld
    account.mail_folders = [
        ("Inbox", inbox_fld),
        ("Inbox/Projects", projects),
        ("Archive", archive_fld),
    ]

    store = CacheStore(tmp_path / "folders_mirror.db")
    try:
        store.replace_folders([
            {
                "ews_id": "FLD-INBOX=", "name": "Inbox", "path": "Inbox",
                "wk": "f:inbox", "folder_class": "IPF.Note",
                "total": 10, "unread": 1, "children": 1,
            },
            {
                "ews_id": "FLD-PROJ=", "name": "Projects",
                "path": "Inbox/Projects", "wk": None, "folder_class": "IPF.Note",
                "total": 2, "unread": 0, "children": 1,
            },
            {
                "ews_id": "FLD-2026=", "name": "2026",
                "path": "Inbox/Projects/2026", "wk": None,
                "folder_class": "IPF.Note", "total": 0, "unread": 0, "children": 1,
            },
            {
                "ews_id": "FLD-SIGNED=", "name": "Signed Contracts",
                "path": "Inbox/Projects/2026/Signed Contracts", "wk": None,
                "folder_class": "IPF.Note", "total": 3, "unread": 0, "children": 0,
            },
            {
                "ews_id": "FLD-ARCH=", "name": "Archive", "path": "Archive",
                "wk": None, "folder_class": "IPF.Note",
                "total": 0, "unread": 0, "children": 1,
            },
            {
                "ews_id": "FLD-FIN=", "name": "Finance",
                "path": "Archive/Finance", "wk": None, "folder_class": "IPF.Note",
                "total": 1, "unread": 0, "children": 1,
            },
            {
                "ews_id": "FLD-AUD=", "name": "Audits",
                "path": "Archive/Finance/Audits", "wk": None,
                "folder_class": "IPF.Note", "total": 5, "unread": 0, "children": 0,
            },
        ])
        store.set_sync_state("events", None, int(time.time()))
        ctx = _ctx(tmp_path, account, cache=store)

        for fresh_flag, expected_src in ((False, "cache"), (True, "live")):
            # Without query: depth=1 limits strictly to top-level folders.
            plain = _run(ctx, "list_folders", depth=1, fresh=fresh_flag)
            assert plain["source"] == expected_src
            assert {r["path"] for r in plain["items"]} == {"Inbox", "Archive"}

            # Name match at depth 4 with depth=1: depth ignored, case-insensitive.
            by_name = _run(
                ctx, "list_folders", query="sIgNeD", depth=1, fresh=fresh_flag
            )
            assert by_name["ok"] is True and by_name["source"] == expected_src
            assert [r["path"] for r in by_name["items"]] == [
                "Inbox/Projects/2026/Signed Contracts"
            ]
            assert by_name["items"][0]["name"] == "Signed Contracts"

            # Path match (contains "/" so no single name can match it).
            by_path = _run(
                ctx,
                "list_folders",
                query="ARCHIVE/FINANCE",
                depth=1,
                fresh=fresh_flag,
            )
            assert by_path["source"] == expected_src
            assert [r["path"] for r in by_path["items"]] == [
                "Archive/Finance",
                "Archive/Finance/Audits",
            ]

            # No match: canonical empty envelope.
            none_res = _run(
                ctx, "list_folders", query="no-such-folder", fresh=fresh_flag
            )
            assert none_res["ok"] is True and none_res["source"] == expected_src
            assert none_res["items"] == []
            assert none_res["count"] == 0 and none_res["total_available"] == 0
            assert none_res["next_offset"] is None

            # Empty or whitespace-only query is a validation error, never a
            # silent ordinary listing.
            for blank_q in ("", "   "):
                bad_q = _run(
                    ctx, "list_folders", query=blank_q, fresh=fresh_flag
                )
                assert bad_q["ok"] is False
                assert bad_q["error"]["code"] == "validation"

        # With parent + query together, path is the full path from mailbox root
        # (built via _folder_parts(root)).
        sub_query = _run(
            ctx,
            "list_folders",
            parent="Inbox/Projects",
            query="sIgNeD",
            depth=1,
        )
        assert sub_query["ok"] is True and sub_query["source"] == "live"
        assert [r["path"] for r in sub_query["items"]] == [
            "Inbox/Projects/2026/Signed Contracts"
        ]
    finally:
        store.close()


def test_get_message_eml_saves_safe_file_enforces_ceiling_and_returns_only_path_and_size(
    tmp_path,
):
    mime_payload = (
        b"From: sender@example.com\r\n"
        b"To: team@example.com\r\n"
        b"Subject: Quarterly Report\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Confidential MIME payload line.\r\n"
    )

    class _MimeItem:
        def __init__(self, raw_id, subject, mime_bytes, *, size=None):
            self.id = raw_id
            self.subject = subject
            self._mime = mime_bytes
            self.size = len(mime_bytes) if size is None and mime_bytes else size
            self.mime_reads = 0

        @property
        def mime_content(self):
            self.mime_reads += 1
            return self._mime

    good_item = _MimeItem(
        "RAW-EML-1=",
        "Q3 / Report?: ../../outside\\draft*",
        mime_payload,
    )
    declared_big = _MimeItem(
        "RAW-EML-BIG=",
        "Declared Huge",
        b"x" * (2 * 1024 * 1024),
        size=2 * 1024 * 1024,
    )
    undeclared_big = _MimeItem(
        "RAW-EML-UNDECL=",
        "Undeclared Huge",
        b"y" * (2 * 1024 * 1024),
        size=None,
    )
    undeclared_big.size = None

    by_id = {
        "RAW-EML-1=": good_item,
        "RAW-EML-BIG=": declared_big,
        "RAW-EML-UNDECL=": undeclared_big,
    }
    fetch_calls = []

    def fake_fetch(ids, only_fields=None):
        ids_list = list(ids)
        projected = None if only_fields is None else list(only_fields)
        fetch_calls.append((ids_list[0][0], projected))
        return [by_id[ids_list[0][0]]]

    account = _account()
    account.fetch = fake_fetch

    # Seed the mirror for RAW-EML-1= to prove format="eml" bypasses the mirror,
    # while format=None (null) and omitted format keep the 'full' default.
    store = CacheStore(tmp_path / "eml_mirror.db")
    try:
        now = int(time.time())
        store.upsert_messages([
            _cache_row("RAW-EML-1=", subject="Cached card", date_ts=now),
        ])
        store.set_sync_state("item:inbox", "TOK", now)
        ctx = _ctx(tmp_path, account, cache=store, ews_max_attachment_mb=1)
        alias = ctx.aliaser.alias_for("RAW-EML-1=", "m")

        # format=None (JSON null) and omitted format keep default 'full'.
        default_res = _run(ctx, "get_message", id=alias)
        null_res = _run(ctx, "get_message", id=alias, format=None)
        assert default_res["ok"] is True and null_res["ok"] is True
        assert default_res["message"] == null_res["message"]
        for bad_fmt in ("EML", "xyz", "", 5):
            bad_res = _run(ctx, "get_message", id=alias, format=bad_fmt)
            assert bad_res["ok"] is False
            assert bad_res["error"]["code"] == "validation"

        fetch_calls.clear()
        res = _run(ctx, "get_message", id=alias, format="eml")
        assert res["ok"] is True
        assert set(res.keys()) == {"ok", "saved_path", "size_bytes", "source"}
        assert res["source"] == "live"
        assert res["size_bytes"] == len(mime_payload)
        assert "Confidential MIME payload" not in json.dumps(res)
        assert fetch_calls == [
            ("RAW-EML-1=", ["id", "subject", "size"]),
            ("RAW-EML-1=", ["id", "subject", "mime_content"]),
        ]

        saved = Path(res["saved_path"])
        att_dir = Path(ctx.settings.data_dir) / "attachments"
        assert saved.parent == att_dir
        assert saved.name.startswith(f"{alias}-")
        assert saved.suffix == ".eml"
        assert "?" not in saved.name and ".." not in saved.name
        assert " " not in saved.name and "*" not in saved.name
        assert saved.read_bytes() == mime_payload

        # Exclusive creation: repeated saves add -2, -3 without overwriting
        # across both get_message(format="eml") and get_attachment(mode="save").
        second_payload = mime_payload + b"SECOND\r\n"
        good_item._mime = second_payload
        good_item.size = len(second_payload)
        res_2 = _run(ctx, "get_message", id=alias, format="eml")
        assert res_2["ok"] is True
        saved_2 = Path(res_2["saved_path"])
        assert saved_2 != saved
        assert saved_2.name.endswith("-2.eml")
        assert saved.read_bytes() == mime_payload
        assert saved_2.read_bytes() == second_payload

        carrier = _neutral_msg("RAW-ATT-COLL=", "Carrier")
        carrier.has_attachments = True
        carrier.attachments = [
            _CountingAttachment(saved.name, b"ATTACHMENT BYTES", "message/rfc822")
        ]
        by_id["RAW-ATT-COLL="] = carrier
        att_res = _run(
            ctx,
            "get_attachment",
            message_id="RAW-ATT-COLL=",
            attachment="0",
            mode="save",
        )
        assert att_res["ok"] is True
        saved_3 = Path(att_res["saved_path"])
        assert saved_3.name.endswith("-3.eml")
        assert saved.read_bytes() == mime_payload
        assert saved_2.read_bytes() == second_payload
        assert saved_3.read_bytes() == b"ATTACHMENT BYTES"

        # Declared size above ceiling: size pre-check requests ONLY
        # ['id', 'subject', 'size'] (never mime_content) and refuses before read.
        fetch_calls.clear()
        res_big = _run(ctx, "get_message", id="RAW-EML-BIG=", format="eml")
        assert res_big["ok"] is False
        assert res_big["error"]["code"] == "validation"
        assert "EWS_MAX_ATTACHMENT_MB" in res_big["error"]["message"]
        assert fetch_calls == [("RAW-EML-BIG=", ["id", "subject", "size"])]
        assert declared_big.mime_reads == 0

        # Undeclared size whose MIME bytes exceed ceiling: refused without saving.
        files_before = set(att_dir.iterdir())
        res_undecl = _run(ctx, "get_message", id="RAW-EML-UNDECL=", format="eml")
        assert res_undecl["ok"] is False
        assert res_undecl["error"]["code"] == "validation"
        assert "EWS_MAX_ATTACHMENT_MB" in res_undecl["error"]["message"]
        assert undeclared_big.mime_reads == 1
        assert set(att_dir.iterdir()) == files_before

        # 200-byte UTF-8 stem cap at a character boundary (3-byte CJK & 2-byte
        # Arabic) and Windows reserved device stems across eml and get_attachment.
        cjk_item = _MimeItem("RAW-CJK=", "\u6f22" * 300, mime_payload)
        ar_item = _MimeItem("RAW-AR=", "\u0639" * 300, mime_payload)
        by_id["RAW-CJK="] = cjk_item
        by_id["RAW-AR="] = ar_item
        for raw_u, ch in (("RAW-CJK=", "\u6f22"), ("RAW-AR=", "\u0639")):
            u_res = _run(ctx, "get_message", id=raw_u, format="eml")
            assert u_res["ok"] is True
            u_path = Path(u_res["saved_path"])
            assert u_path.exists()
            assert len(u_path.stem.encode("utf-8")) <= 200
            assert "\ufffd" not in u_path.stem
            assert ch in u_path.stem

        long_att_msg = _neutral_msg("RAW-LONG-ATT=", "Long attachment")
        long_att_msg.has_attachments = True
        long_att_msg.attachments = [
            _CountingAttachment(
                "\u6f22" * 300 + ".pdf", b"%PDF-1.7", "application/pdf"
            ),
            _CountingAttachment("CON.txt", b"device-name", "text/plain"),
        ]
        by_id["RAW-LONG-ATT="] = long_att_msg
        r_long_att = _run(
            ctx, "get_attachment", message_id="RAW-LONG-ATT=", attachment="0", mode="save"
        )
        assert r_long_att["ok"] is True
        p_long_att = Path(r_long_att["saved_path"])
        assert len(p_long_att.stem.encode("utf-8")) <= 200
        assert p_long_att.suffix == ".pdf"

        r_con = _run(
            ctx, "get_attachment", message_id="RAW-LONG-ATT=", attachment="1", mode="save"
        )
        assert r_con["ok"] is True
        assert Path(r_con["saved_path"]).stem.upper() != "CON"

        assert account.mock_calls == []
    finally:
        store.close()


def test_save_helper_null_aliaser_collision_and_local_disk_fault_is_internal(
    tmp_path,
):
    # 1. NullAliaser with two long raw ids sharing a >32-char prefix & subject:
    #    second export gets -2.eml instead of overwriting the first.
    prefix = "AAMkAGI2NzQ5ZTk4LTdjYzEtNGE0Ny1iMTk1LWZhYjM0"
    raw_a = prefix + "AAAAAAAA1="
    raw_b = prefix + "BBBBBBBB2="
    by_id = {
        raw_a: SimpleNamespace(
            id=raw_a, subject="RE: Meeting", mime_content=b"MESSAGE A", size=9
        ),
        raw_b: SimpleNamespace(
            id=raw_b, subject="RE: Meeting", mime_content=b"MESSAGE B", size=9
        ),
    }
    account = _account()
    account.fetch = lambda ids, only_fields=None: [by_id[list(ids)[0][0]]]
    ctx = _ctx(tmp_path / "null_alias", account)
    ctx.aliaser = NullAliaser()

    res_a = _run(ctx, "get_message", id=raw_a, format="eml")
    res_b = _run(ctx, "get_message", id=raw_b, format="eml")
    assert res_a["ok"] is True and res_b["ok"] is True
    assert res_a["saved_path"] != res_b["saved_path"]
    assert Path(res_a["saved_path"]).read_bytes() == b"MESSAGE A"
    assert Path(res_b["saved_path"]).read_bytes() == b"MESSAGE B"

    # 2. Unusable attachment folder (regular file at DATA_DIR/attachments):
    #    returns 'internal' naming the attachment folder and NEVER opens the
    #    Exchange circuit breaker on either get_message(eml) or get_attachment(save).
    for mode_kind in ("eml", "get_attachment"):
        sub_dir = tmp_path / f"broken_{mode_kind}"
        acc_broken = _account(inbox=_Query([_neutral_msg("Z=", "ok-msg")]))
        carrier = _neutral_msg("RAW-Y=", "Carrier")
        carrier.has_attachments = True
        carrier.attachments = [
            _CountingAttachment("a.bin", b"1234", "application/octet-stream")
        ]
        broken_items = {
            "RAW-X=": SimpleNamespace(
                id="RAW-X=", subject="Hello", mime_content=b"MIME", size=4
            ),
            "RAW-Y=": carrier,
        }
        acc_broken.fetch = lambda ids, only_fields=None, _b=broken_items: [
            _b[list(ids)[0][0]]
        ]
        ctx_broken = _ctx(sub_dir, acc_broken)
        data_root = Path(ctx_broken.settings.data_dir)
        data_root.mkdir(parents=True, exist_ok=True)
        (data_root / "attachments").write_bytes(b"not a directory")

        for _ in range(ctx_broken.settings.circuit_failure_threshold):
            if mode_kind == "eml":
                err_res = _run(
                    ctx_broken, "get_message", id="RAW-X=", format="eml"
                )
            else:
                err_res = _run(
                    ctx_broken,
                    "get_attachment",
                    message_id="RAW-Y=",
                    attachment="0",
                    mode="save",
                )
            assert err_res["ok"] is False
            assert err_res["error"]["code"] == "internal"
            assert (
                "attachment folder cannot be written"
                in err_res["error"]["message"]
            )
        assert ctx_broken._circuit_failures == 0
        assert ctx_broken._circuit_open_until == 0.0
        after = _run(ctx_broken, "search_messages")
        assert after["ok"] is True


# --- Round 7: interleaved paging identity, partial + notes list, child stderr,
#              untrusted child JSON validation, cancellation, unavailable temp dir,
#              process-group / grandchild cleanup, and EWS_KEEP_DUMPABLE ----------


def test_interleaved_paging_same_name_numeric_name_and_two_messages(tmp_path):
    text_m1_a = ("AAAA-msg1-first " * 2_800).encode("utf-8")
    text_m1_b = ("BBBB-msg1-second " * 2_800).encode("utf-8")
    text_m2_rep = ("RRRR-msg2-report " * 2_800).encode("utf-8")
    text_m2_zero = ("ZZZZ-msg2-named-0 " * 2_800).encode("utf-8")
    text_m2_scan = ("SSSS-msg2-scan " * 2_800).encode("utf-8")

    m1_a = _IdCountingAttachment("M1-A", "scan.txt", text_m1_a, "text/plain")
    m1_b = _IdCountingAttachment("M1-B", "scan.txt", text_m1_b, "text/plain")
    msg1 = _neutral_msg("RAW-M1=", "Message 1")
    msg1.attachments = [m1_a, m1_b]

    m2_rep = _IdCountingAttachment("M2-REP", "report.txt", text_m2_rep, "text/plain")
    m2_zero = _IdCountingAttachment("M2-ZERO", "0", text_m2_zero, "text/plain")
    m2_scan = _IdCountingAttachment("M2-SCAN", "scan.txt", text_m2_scan, "text/plain")
    msg2 = _neutral_msg("RAW-M2=", "Message 2")
    msg2.attachments = [m2_rep, m2_zero, m2_scan]

    by_id = {"RAW-M1=": msg1, "RAW-M2=": msg2}
    account = _account()
    account.fetch = MagicMock(
        side_effect=lambda ids, **_kw: [by_id[getattr(ids[0], "id", None) or ids[0][0]]]
    )
    ctx = _ctx(tmp_path, account)

    # 1. Same name `scan.txt` twice on M1, interleaved with index `"1"` AND `scan.txt` on M2.
    p1_m1_scan = _run(
        ctx, "get_attachment", message_id="RAW-M1=", attachment="scan.txt", mode="text"
    )
    assert p1_m1_scan["text"].startswith("AAAA-msg1-first ")
    assert p1_m1_scan["next_offset"] == 20_000

    p1_m1_idx1 = _run(
        ctx, "get_attachment", message_id="RAW-M1=", attachment="1", mode="text"
    )
    assert p1_m1_idx1["text"].startswith("BBBB-msg1-second ")

    p1_m2_scan = _run(
        ctx, "get_attachment", message_id="RAW-M2=", attachment="scan.txt", mode="text"
    )
    assert p1_m2_scan["text"].startswith("SSSS-msg2-scan ")

    fetches_before = account.fetch.call_count
    p2_m1_scan = _run(
        ctx,
        "get_attachment",
        message_id="RAW-M1=",
        attachment="scan.txt",
        mode="text",
        offset=20_000,
    )
    assert p2_m1_scan["text"] == text_m1_a.decode("utf-8")[20_000:40_000]
    assert "AAAA-msg1-first" in p2_m1_scan["text"]
    assert "BBBB-msg1-second" not in p2_m1_scan["text"]
    assert "SSSS-msg2-scan" not in p2_m1_scan["text"]
    assert account.fetch.call_count == fetches_before

    p2_m2_scan = _run(
        ctx,
        "get_attachment",
        message_id="RAW-M2=",
        attachment="scan.txt",
        mode="text",
        offset=20_000,
    )
    assert "SSSS-msg2-scan" in p2_m2_scan["text"]
    assert "AAAA-msg1-first" not in p2_m2_scan["text"]
    assert account.fetch.call_count == fetches_before

    # 2. Unseen selector `"0"` at offset=20000 on M1 resolves via picker once
    # (1 metadata fetch, 0 payload download) and binds `"0"` for later parts.
    p2_m1_by_unseen_idx0 = _run(
        ctx,
        "get_attachment",
        message_id="RAW-M1=",
        attachment="0",
        mode="text",
        offset=20_000,
    )
    assert "AAAA-msg1-first" in p2_m1_by_unseen_idx0["text"]
    assert account.fetch.call_count == fetches_before + 1
    assert m1_a.reads == 1  # NO second download!

    p3_m1_by_idx0 = _run(
        ctx,
        "get_attachment",
        message_id="RAW-M1=",
        attachment="0",
        mode="text",
        offset=40_000,
    )
    assert "AAAA-msg1-first" in p3_m1_by_idx0["text"]
    assert account.fetch.call_count == fetches_before + 1  # now bound, 0 fetches!
    assert m1_a.reads == 1

    # 3. Attachment literally named `"0"` on M2 alongside `report.txt` at index 0.
    # Make room in the 4-doc cache and interleave `"0"` and `"report.txt"`.
    p1_m2_zero = _run(
        ctx, "get_attachment", message_id="RAW-M2=", attachment="0", mode="text"
    )
    assert p1_m2_zero["name"] == "0"
    assert p1_m2_zero["text"].startswith("ZZZZ-msg2-named-0 ")

    p1_m2_rep = _run(
        ctx, "get_attachment", message_id="RAW-M2=", attachment="report.txt", mode="text"
    )
    assert p1_m2_rep["name"] == "report.txt"
    assert p1_m2_rep["text"].startswith("RRRR-msg2-report ")

    fetches_before_zero_p2 = account.fetch.call_count
    p2_m2_zero = _run(
        ctx,
        "get_attachment",
        message_id="RAW-M2=",
        attachment="0",
        mode="text",
        offset=20_000,
    )
    assert p2_m2_zero["name"] == "0"
    assert "ZZZZ-msg2-named-0" in p2_m2_zero["text"]
    assert "RRRR-msg2-report" not in p2_m2_zero["text"]
    assert account.fetch.call_count == fetches_before_zero_p2
    assert m2_zero.reads == 1


def test_partial_notes_list_read_counts_and_repeated_across_parts(tmp_path, monkeypatch):
    # 14-sheet workbook where every sheet exceeds the 5,000-row limit and the
    # soft deadline stops mid-workbook: cut notes come first, repeated row-cut
    # notes fold into one with a count, `partial` is True, `sheets_read < sheets`,
    # and every paged part repeats `partial` and `notes`.
    rows = "".join(
        f'<row r="{r}"><c r="A{r}"><v>{r}</v></c><c r="B{r}"><v>{r * 3}</v></c></row>'
        for r in range(1, 5_150)
    )
    xlsx_data = _xlsx_bytes([
        (f"Sales detail FY2025 region {i:02d} central province", rows)
        for i in range(1, 15)
    ])
    part_doc = extract.extract_xlsx(xlsx_data, soft_deadline=time.monotonic() - 1.0)
    assert part_doc.partial is True
    assert part_doc.sheets == 14
    assert part_doc.sheets_read == 1

    # Also test folding of 9 repeated sheet-cut notes + time-limit note directly
    # through `_render_text_response` and cached multi-part paging.
    raw_nine_notes = [
        f"Sheet 'Sales detail FY2025 region {i:02d} central province' was cut at 5000 rows."
        for i in range(1, 10)
    ] + ["Stopped after sheet 9 of 14 (time limit); the rest was not read."]
    folded = extract.fold_and_prioritize_notes(raw_nine_notes)
    assert len(folded) == 2
    assert "Stopped after sheet 9 of 14 (time limit)" in folded[0]
    assert "9 sheets were cut at 5000 rows" in folded[1]

    att = _IdCountingAttachment("ATT-P14", "sales14.xlsx", xlsx_data, "application/octet-stream")
    item = _neutral_msg("RAW-P14=", "14-sheet sales")
    item.attachments = [att]
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)

    with monkeypatch.context() as mp:
        mp.setattr(extract_worker, "EXTRACT_TIMEOUT_SECONDS", 0.4)
        # Ensure child soft deadline stops before reading all 14 sheets.
        p1 = _run(ctx, "get_attachment", message_id="RAW-P14=", offset=0)

    assert p1["ok"] is True
    assert p1["partial"] is True
    assert p1["sheets"] == 14
    assert 1 <= p1["sheets_read"] <= 14
    assert isinstance(p1["notes"], list)
    assert 1 <= len(p1["notes"]) <= 8
    assert all(len(n) <= 300 for n in p1["notes"])
    if p1["sheets_read"] < 14:
        assert "time limit" in p1["notes"][0]
        assert "time limit" in p1["note"]
    assert "next_offset" in p1

    p2 = _run(ctx, "get_attachment", message_id="RAW-P14=", offset=p1["next_offset"])
    assert p2["ok"] is True
    assert p2["partial"] is True
    assert p2["sheets"] == 14
    assert p2["sheets_read"] == p1["sheets_read"]
    assert p2["notes"] == p1["notes"]
    assert p2["note"] == p1["note"]


def test_child_stderr_does_not_reach_server_stderr(tmp_path, monkeypatch, capfd):
    noisy_child = (
        "import json, os, sys; "
        "sys.stdin.buffer.readline(); "
        "os.write(2, b'CHILD-STDERR-SENTINEL-RAW\\n'); "
        "sys.stderr.write('CHILD-STDERR-SENTINEL-PY\\n'); "
        "sys.stderr.flush(); "
        "sys.stdout.write(json.dumps({'ok': True, 'extracted_from': 'html', 'text': 'quiet-ok'}))"
    )
    account = _account()
    ctx = _ctx(tmp_path, account)
    item = _neutral_msg("RAW-STDERR=", "Noisy child")
    item.attachments = [
        _IdCountingAttachment("ATT-NOISY", "noisy.html", b"<p>hi</p>", "text/html")
    ]
    account.fetch = MagicMock(return_value=[item])

    capfd.readouterr()
    with monkeypatch.context() as mp:
        mp.setattr(
            extract_worker,
            "_worker_command",
            lambda _mb: [sys.executable, "-c", noisy_child],
        )
        res = _run(ctx, "get_attachment", message_id="RAW-STDERR=", mode="text")

    captured = capfd.readouterr()
    assert res["ok"] is True and res["text"] == "quiet-ok"
    assert "CHILD-STDERR-SENTINEL" not in captured.err
    assert "CHILD-STDERR-SENTINEL" not in captured.out


def test_parent_validates_untrusted_child_answers_as_hostile_input(tmp_path, monkeypatch):
    fake_script = tmp_path / "standin_child.py"
    fake_script.write_text(
        "import json, sys\n"
        "mode = sys.argv[1]\n"
        "sys.stdin.buffer.readline()\n"
        "sys.stdin.buffer.read()\n"
        "out = sys.stdout.buffer\n"
        "if mode == 'bigtext':\n"
        "    out.write(b'{\"ok\":true,\"extracted_from\":\"pdf\",\"text\":\"' + (b'a' * 20000000) + b'\",\"notes\":[]}')\n"
        "elif mode == 'note_surrogate':\n"
        "    s = json.dumps({'ok': True, 'extracted_from': 'pdf', 'text': 'x' * 25000, 'notes': ['warn \\ud800 tail']}, ensure_ascii=True)\n"
        "    out.write(s.encode('ascii'))\n"
        "elif mode == 'err_surrogate':\n"
        "    s = json.dumps({'ok': False, 'code': 'validation', 'message': 'bad \\ud800 msg', 'hint': 'h \\udfff'}, ensure_ascii=True)\n"
        "    out.write(s.encode('ascii'))\n"
        "elif mode == 'ext_surrogate':\n"
        "    s = json.dumps({'ok': True, 'extracted_from': 'pdf\\ud800', 'text': 'hi'}, ensure_ascii=False)\n"
        "    out.write(s.encode('utf-8', 'surrogatepass'))\n"
        "elif mode == 'ext_long':\n"
        "    out.write(json.dumps({'ok': True, 'extracted_from': 'E' * 2000000, 'text': 'hi'}).encode())\n"
        "elif mode == 'pages_huge':\n"
        "    out.write(b'{\"ok\":true,\"extracted_from\":\"pdf\",\"text\":\"hi\",\"pages\":' + (b'9' * 4000) + b'}')\n"
        "elif mode == 'pages_negative':\n"
        "    out.write(b'{\"ok\":true,\"extracted_from\":\"pdf\",\"text\":\"hi\",\"pages\":-7}')\n"
        "elif mode == 'read_gt_total':\n"
        "    out.write(b'{\"ok\":true,\"extracted_from\":\"pdf\",\"text\":\"hi\",\"pages\":2,\"pages_read\":5}')\n"
        "elif mode == 'notes_million':\n"
        "    out.write(json.dumps({'ok': True, 'extracted_from': 'pdf', 'text': 'hi', 'notes': ['n'] * 1000000}).encode())\n"
        "elif mode == 'notes_51':\n"
        "    out.write(json.dumps({'ok': True, 'extracted_from': 'pdf', 'text': 'hi', 'notes': [f'n{i}' for i in range(51)]}).encode())\n"
        "elif mode == 'mods_million':\n"
        "    out.write(json.dumps({'ok': True, 'extracted_from': 'pdf', 'text': 'hi', 'loaded_modules': ['m'] * 1000000}).encode())\n"
        "elif mode == 'unknown_field':\n"
        "    out.write(b'{\"ok\":true,\"extracted_from\":\"pdf\",\"text\":\"hi\",\"smuggled\":1}')\n"
        "elif mode == 'deep_json':\n"
        "    out.write((b'[' * 200000) + b'0' + (b']' * 200000))\n"
        "out.flush()\n",
        encoding="utf-8",
    )

    modes = [
        "bigtext",
        "note_surrogate",
        "err_surrogate",
        "ext_surrogate",
        "ext_long",
        "pages_huge",
        "pages_negative",
        "read_gt_total",
        "notes_million",
        "notes_51",
        "mods_million",
        "unknown_field",
        "deep_json",
    ]
    atts = [
        _IdCountingAttachment(f"FAKE-{m}", f"{m}.html", b"<p>x</p>", "text/html")
        for m in modes
    ]
    item = _neutral_msg("RAW-FAKE=", "Untrusted child checks")
    item.attachments = atts
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path, account)

    def _call_mode(m: str, offset: int = 0):
        with monkeypatch.context() as mp:
            mp.setattr(
                extract_worker,
                "_worker_command",
                lambda _mb, mode=m: [sys.executable, str(fake_script), mode],
            )
            return _run(
                ctx,
                "get_attachment",
                message_id="RAW-FAKE=",
                attachment=f"{m}.html",
                mode="text",
                offset=offset,
            )

    # 1. `bigtext` (20M chars) is cut to MAX_EXTRACTED_CHARS (5M), marked partial=True,
    # and carries the cut note.
    r_big = _call_mode("bigtext")
    assert r_big["ok"] is True
    assert r_big["total_chars"] == extract.MAX_EXTRACTED_CHARS
    assert r_big["partial"] is True
    assert any(str(extract.MAX_EXTRACTED_CHARS) in n for n in r_big["notes"])
    json.dumps(r_big, ensure_ascii=False).encode("utf-8")

    # 2. Lone surrogates written as JSON escapes in `notes` and in an error `message`/`hint`
    # are replaced with U+FFFD, so strict UTF-8 serialization succeeds on both offset=0 and
    # cached offset>0. (An answer that is not valid UTF-8 at all is refused: the
    # `ext_surrogate` case below writes such bytes.)
    r_ns1 = _call_mode("note_surrogate", offset=0)
    r_ns2 = _call_mode("note_surrogate", offset=20_000)
    assert r_ns1["ok"] is True and r_ns2["ok"] is True
    assert "\ufffd" in r_ns1["notes"][0]
    json.dumps(r_ns1, ensure_ascii=False).encode("utf-8")
    json.dumps(r_ns2, ensure_ascii=False).encode("utf-8")

    r_es = _call_mode("err_surrogate")
    assert r_es["ok"] is False and r_es["error"]["code"] == "validation"
    assert "\ufffd" in r_es["error"]["message"]
    assert "\ufffd" in r_es["error"]["hint"]
    json.dumps(r_es, ensure_ascii=False).encode("utf-8")

    # 3. Every malformed / out-of-range / unknown-field / oversized answer is rejected
    # with the worker limits error and never cached.
    for bad_mode in (
        "ext_surrogate",
        "ext_long",
        "pages_huge",
        "pages_negative",
        "read_gt_total",
        "notes_million",
        "notes_51",
        "mods_million",
        "unknown_field",
        "deep_json",
    ):
        res_bad = _call_mode(bad_mode)
        assert res_bad["ok"] is False, f"{bad_mode} unexpectedly succeeded: {res_bad}"
        assert res_bad["error"]["code"] == "validation"
        assert res_bad["error"]["message"] == extract_worker.WORKER_LIMIT_ERROR_MESSAGE
        json.dumps(res_bad, ensure_ascii=False).encode("utf-8")


def test_cancelled_calls_kill_children_hold_slot_and_unwritable_tempdir_and_grandchild(
    tmp_path, monkeypatch
):
    # 1. Cancellation kills children immediately, keeps live children <= EXTRACT_MAX_CONCURRENT,
    # and does not stall an honest document submitted right afterwards.
    hang_script = tmp_path / "hang_after_close.py"
    hang_script.write_text(
        "import os, sys, time\n"
        "sys.stdin.buffer.readline()\n"
        "os.close(1)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    spawned_procs = []
    real_popen = subprocess.Popen

    class _TrackPopen(real_popen):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            spawned_procs.append(self)

    def _live_count() -> int:
        return sum(1 for p in spawned_procs if p.poll() is None)

    honest_docx = _docx_bytes("<w:p><w:r><w:t>Honest after cancel</w:t></w:r></w:p>")

    async def _exercise_cancellation():
        peak_live = 0
        with monkeypatch.context() as mp:
            mp.setattr(subprocess, "Popen", _TrackPopen)
            mp.setattr(
                extract_worker,
                "_worker_command",
                lambda _mb: [sys.executable, str(hang_script)],
            )
            for _wave in range(3):
                tasks = [
                    asyncio.create_task(
                        extract_worker.extract_bytes_in_worker(
                            b"x", name="s.pdf", timeout_seconds=12.0
                        )
                    )
                    for _ in range(2)
                ]
                await asyncio.sleep(0.18)
                peak_live = max(peak_live, _live_count())
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                assert _live_count() == 0

        t0 = time.perf_counter()
        honest_res = await extract_worker.extract_bytes_in_worker(
            honest_docx, name="honest.docx"
        )
        return peak_live, time.perf_counter() - t0, honest_res

    peak_live, honest_wait, honest_res = asyncio.run(_exercise_cancellation())
    assert peak_live <= extract_worker.EXTRACT_MAX_CONCURRENT
    assert _live_count() == 0
    assert honest_wait < 4.0 * TIME_MARGIN
    assert honest_res.text == "Honest after cancel"

    # 2. Unwritable / missing temporary folder returns `internal` saying the document
    # reader is unavailable, and 5 failures do NOT open the circuit breaker.
    pdf_or_docx = _docx_bytes("<w:p><w:r><w:t>ordinary doc</w:t></w:r></w:p>")
    item = _neutral_msg("RAW-TMPBRK=", "Tempdir fault")
    item.attachments = [
        _IdCountingAttachment("TMP-DOC", "a.docx", pdf_or_docx, "application/octet-stream"),
        _IdCountingAttachment("TMP-TXT", "n.txt", b"plain note", "text/plain"),
    ]
    account = _account()
    account.fetch = MagicMock(return_value=[item])
    ctx = _ctx(tmp_path / "brk", account, circuit_failure_threshold=5)

    saved_tempdir = tempfile.tempdir
    tempfile.tempdir = str(tmp_path / "missing-temp-root-dir")
    try:
        for _ in range(5):
            r_fail = _run(
                ctx,
                "get_attachment",
                message_id="RAW-TMPBRK=",
                attachment="a.docx",
                mode="text",
            )
            assert r_fail["ok"] is False
            assert r_fail["error"]["code"] == "internal"
            assert "unavailable" in r_fail["error"]["message"].lower()
            ctx._attachment_text_cache.clear()
        # Circuit breaker is still closed even before tempdir is restored!
        assert ctx._circuit_failures == 0
        assert ctx._circuit_open_until == 0.0
        r_plain = _run(
            ctx, "get_attachment", message_id="RAW-TMPBRK=", attachment="n.txt"
        )
        assert r_plain["ok"] is True and r_plain["text"] == "plain note"
    finally:
        tempfile.tempdir = saved_tempdir

    r_recovered = _run(
        ctx, "get_attachment", message_id="RAW-TMPBRK=", attachment="a.docx"
    )
    assert r_recovered["ok"] is True and r_recovered["text"] == "ordinary doc"

    # 3. Grandchild process started by the child cannot outlive the clock / call.
    pid_file = tmp_path / "grandchild.pid"
    gc_script = tmp_path / "spawn_grandchild.py"
    gc_script.write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "sys.stdin.buffer.readline()\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"Path({str(pid_file)!r}).write_text(str(p.pid), encoding='utf-8')\n",
        encoding="utf-8",
    )
    with monkeypatch.context() as mp:
        mp.setattr(extract_worker, "EXTRACT_TIMEOUT_SECONDS", 0.5)
        mp.setattr(
            extract_worker,
            "_worker_command",
            lambda _mb: [sys.executable, str(gc_script)],
        )
        with pytest.raises(ToolError):
            asyncio.run(extract_worker.extract_bytes_in_worker(b"x", name="g.html"))

    assert pid_file.exists()
    gc_pid = int(pid_file.read_text(encoding="utf-8").strip())
    time.sleep(0.15)
    if os.name == "nt":
        import ctypes as _ct

        h_gc = _ct.windll.kernel32.OpenProcess(0x1000, False, gc_pid)
        if h_gc:
            exit_code = _ct.c_ulong(0)
            _ct.windll.kernel32.GetExitCodeProcess(h_gc, _ct.byref(exit_code))
            _ct.windll.kernel32.CloseHandle(h_gc)
            assert exit_code.value != 259, f"Grandchild {gc_pid} still STILL_ACTIVE"
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(gc_pid, 0)

    # 4. EWS_KEEP_DUMPABLE setting & Linux prctl(PR_SET_DUMPABLE, 0) wiring.
    s_default = make_settings()
    assert s_default.ews_keep_dumpable is False
    s_keep = make_settings(ews_keep_dumpable=True)
    assert s_keep.ews_keep_dumpable is True

    assert (
        extract_worker.ensure_server_non_dumpable(
            keep_dumpable=True, platform_name="linux"
        )
        is False
    )
    prctl_calls = []

    class _FakePrctl:
        argtypes = None
        restype = None

        def __call__(self, *args):
            prctl_calls.append(args)
            return 0

    fake_libc = SimpleNamespace(prctl=_FakePrctl())
    with monkeypatch.context() as mp:
        mp.setattr(extract_worker.ctypes, "CDLL", lambda *_a, **_k: fake_libc)
        assert (
            extract_worker.ensure_server_non_dumpable(
                keep_dumpable=False, platform_name="linux"
            )
            is True
        )
    assert prctl_calls == [(extract_worker.PR_SET_DUMPABLE, 0, 0, 0, 0)]


def test_get_attachment_refuses_bytes_over_the_ceiling_when_the_declared_size_lies(
    tmp_path, monkeypatch
):
    """The declared size comes from the sender's side and can be wrong: the bytes that
    really arrive are measured before anything parses them, for a document as for a text
    file. Saving is not a parse: it writes what Exchange delivered, whole."""
    account = _account()
    ctx = _ctx(tmp_path, account, ews_max_attachment_mb=1)
    big = b"PK" + os.urandom(2 * 1024 * 1024)
    lying_doc = _IdCountingAttachment(
        "ATT-LIE-DOC", "report.docx", big, "application/octet-stream", size=10
    )
    lying_doc.size = 10
    lying_txt = _IdCountingAttachment("ATT-LIE-TXT", "notes.txt", big, "text/plain", size=10)
    lying_txt.size = 10
    item = _neutral_msg("RAW-LIE=", "Lying sizes")
    item.attachments = [lying_doc, lying_txt]
    account.fetch = MagicMock(return_value=[item])

    reader_starts = []

    async def _no_worker(*args, **kwargs):
        reader_starts.append("worker")
        raise AssertionError("the worker must not be started for bytes over the ceiling")

    def _no_plain(*args, **kwargs):
        reader_starts.append("plain")
        raise AssertionError("the text reader must not be called for bytes over the ceiling")

    monkeypatch.setattr(mail_read, "extract_bytes_in_worker", _no_worker)
    monkeypatch.setattr(mail_read, "extract_plain_text", _no_plain)

    for selector, att in (("0", lying_doc), ("1", lying_txt)):
        for mode in ("text", "auto"):
            res = _run(ctx, "get_attachment", message_id="RAW-LIE=", attachment=selector, mode=mode)
            assert res["ok"] is False, (selector, mode)
            assert res["error"]["code"] == "validation", (selector, mode, res["error"])
            assert "EWS_MAX_ATTACHMENT_MB" in res["error"]["message"], (selector, mode)
            assert "text" not in res
        assert att.reads >= 1
    assert reader_starts == []

    saved = _run(ctx, "get_attachment", message_id="RAW-LIE=", attachment="0", mode="save")
    assert saved["ok"] is True and saved["mode"] == "save"
    assert os.path.getsize(saved["saved_path"]) == len(big)


def _child_answer(**fields):
    base = {"ok": True, "text": "plain", "extracted_from": "docx"}
    base.update(fields)
    return json.dumps(base).encode("utf-8")


def test_parent_treats_the_childs_answer_as_hostile():
    """The child parsed a hostile document, so its answer is hostile input too: wrong types
    are refused with the one fixed message, and text that cannot be encoded is repaired."""
    validate = extract_worker._parse_and_validate_child_output

    good = validate(_child_answer())
    assert good.text == "plain" and good.extracted_from == "docx"

    lone = "\ud83d"
    cleaned = validate(_child_answer(text=f"a{lone}b", notes=[f"note {lone} end"]))
    assert cleaned.text == "a\ufffdb"
    cleaned.text.encode("utf-8")
    for note in cleaned.notes:
        note.encode("utf-8")

    wrong_answers = [
        _child_answer(text=123),
        _child_answer(text=["a", "b"]),
        _child_answer(text=None),
        _child_answer(text={"a": 1}),
        _child_answer(text=True),
        _child_answer(extracted_from=5),
        _child_answer(extracted_from=None),
        _child_answer(extracted_from=["docx"]),
        _child_answer(extracted_from="exe"),
        _child_answer(notes="one note"),
        _child_answer(notes=[1]),
        _child_answer(partial="yes"),
        _child_answer(pages="3"),
        _child_answer(pages=-1),
        _child_answer(pages=True),
        _child_answer(surprise="field"),
        json.dumps(["ok"]).encode("utf-8"),
        json.dumps({"ok": "true", "text": "x", "extracted_from": "docx"}).encode("utf-8"),
        b"",
        b"\xff\xfe not utf-8",
        b"{not json",
    ]
    for raw in wrong_answers:
        with pytest.raises(ToolError) as caught:
            validate(raw)
        assert caught.value.code == "validation", raw[:60]
        assert caught.value.message == extract_worker.WORKER_LIMIT_ERROR_MESSAGE, raw[:60]

