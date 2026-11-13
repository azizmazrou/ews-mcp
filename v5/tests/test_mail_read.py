"""mail_read pack tests — all seven read tools driven through dispatch().

No network, no exchangelib objects: a MagicMock account behind a fake
gateway, list-subclass query doubles, and SimpleNamespace messages with
tz-aware datetimes (Asia/Riyadh).
"""

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import jsonschema
import pytest
from conftest import make_settings

from ewsmcp import dto
from ewsmcp.audit import AuditLog
from ewsmcp.cache.store import CacheStore
from ewsmcp.errors import ToolError
from ewsmcp.ids import get_aliaser
from ewsmcp.tools import mail_read
from ewsmcp.tools.base import Context, dispatch

TZ = ZoneInfo("Asia/Riyadh")
SPECS = {spec.name: spec for spec in mail_read.TOOLS}

QUOTED_TAIL = (
    "\n\nFrom: Exec <exec@corp.example>\nSent: Monday, June 8, 2026\n"
    "To: Ahmed\nSubject: Re: RFP\n\nOLD QUOTED LINE ONE\nOLD QUOTED LINE TWO"
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


def _msg(raw_id, subject="Subj", sender="ahmed@corp.example", *, sender_name=None,
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


def test_pack_exports_seven_read_specs():
    assert len(mail_read.TOOLS) == 7
    assert set(SPECS) == {
        "list_folders", "search_messages", "check_messages", "get_message",
        "get_thread", "get_attachment", "get_mailbox_overview",
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
    res = _run(ctx, "search_messages", folder="f:all", query="from:ahmed")
    assert [i["subject"] for i in res["items"]] == ["newest", "older", "oldest"]
    assert [i["folder"] for i in res["items"]] == ["Inbox/Archive", "Inbox", "Inbox/Archive"]
    assert res["total_available"] == 3 and res["next_offset"] is None
    assert res["folders_searched"] == 2 and "folders_failed" not in res
    assert inbox.filter_calls == [(("from:ahmed",), {})]


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
    res = _run(ctx, "search_messages", query="from:ahmed", subject="rfp")
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
        _msg("RAW-1=", sender="ahmed@corp.example"),
        _msg("RAW-2=", sender="sara@corp.example"),
    ])
    ctx = _ctx(tmp_path, _account(inbox=inbox))
    res = _run(ctx, "search_messages", from_="ahmed")
    assert res["ok"] is True
    assert res["count"] == 1
    assert res["items"][0]["from"] == "ahmed@corp.example"
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
        _msg("RAW-1=", sender="ahmed@corp.example"),
        _msg("RAW-2=", sender="sara@corp.example"),
    ])
    ctx = _ctx(tmp_path, _account(inbox=inbox))
    res = _run(ctx, "search_messages", sender="ahmed")
    assert res["count"] == 1
    both = _run(ctx, "search_messages", sender="ahmed", from_="ahmed")
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
    msg_a = _msg("RAW-A=", subject="RFP", sender="ahmed@corp.example",
                 dt=datetime(2026, 6, 10, 9, 0, tzinfo=TZ),
                 text_body="Opening question?")
    msg_b = _msg("RAW-B=", subject="Re: RFP", sender="exec@corp.example",
                 dt=datetime(2026, 6, 10, 10, 0, tzinfo=TZ),
                 text_body="Agreed, Sunday 10am." + QUOTED_TAIL)
    msg_c = _msg("RAW-C=", subject="Re: RFP", sender="ahmed@corp.example",
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
    assert res["participants"][0]["name_or_email"] == "ahmed@corp.example"
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
        _att("contract.pdf", b"%PDF-junk", "application/pdf"),
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


def test_check_messages_whole_word_and_missing(monkeypatch):
    subjects = ["Review RFP-123 draft", "RFP-12 signed", "Lunch"]

    async def fake_search(ctx, subject=None, **kwargs):
        cards = [{"id": f"m{i}", "subject": s, "from": "a@b", "date": "2026-01-01"}
                 for i, s in enumerate(subjects) if subject.lower() in s.lower()]
        return {"ok": True, "items": cards, "count": len(cards)}

    monkeypatch.setattr(mail_read, "_search_messages", fake_search)
    out = asyncio.run(mail_read._check_messages(None, "RFP-12\nrfp-12, Budget"))
    assert out["values_checked"] == 2
    assert out["missing"] == ["Budget"]
    assert [c["id"] for c in out["found"]["RFP-12"]] == ["m1"]
    only = asyncio.run(mail_read._check_messages(None, '["Budget", "Lunch"]', only_missing=True))
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
    direct = dto.msg_full(item, ctx.aliaser, "Asia/Riyadh", 4000)
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
    txt = _CountingAttachment("notes.txt", b"plain notes", "text/plain", gateway=gw)
    signed_p7m = _CountingAttachment(
        "smime.p7m", _P7M_SIGNED_HEAD, "application/pkcs7-mime", gateway=gw,
    )
    enveloped_p7m = _CountingAttachment(
        "enc.p7m", _P7M_ENVELOPED_HEAD, "application/pkcs7-mime", gateway=gw,
    )
    item = _neutral_msg("RAW-ATT=", "Files")
    item.attachments = [pdf, txt, signed_p7m, enveloped_p7m]
    account.fetch = MagicMock(return_value=[item])

    # mode="info" on ordinary attachments: zero downloads.
    info_pdf = _run(ctx, "get_attachment", message_id="RAW-ATT=",
                    attachment="contract.pdf", mode="info")
    info_txt = _run(ctx, "get_attachment", message_id="RAW-ATT=",
                    attachment="notes.txt", mode="info")
    assert info_pdf["mode"] == "info" and info_txt["mode"] == "info"
    assert pdf.reads == 0 and txt.reads == 0

    # mode="auto" on binary non-PKCS#7: stays info with zero downloads.
    auto_pdf = _run(ctx, "get_attachment", message_id="RAW-ATT=",
                    attachment="contract.pdf", mode="auto")
    assert auto_pdf["mode"] == "info" and pdf.reads == 0

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
    # Prefix trap: "Daniel" matches AQS from:"Dan" but NOT whole-word "Dan".
    items.append(_msg(
        "OLD-PREFIX=", subject="From Daniel",
        sender="daniel@example.com", sender_name="Daniel Ops",
        dt=datetime(2026, 6, 2, 9, 0, tzinfo=TZ),
        message_id="<OLD-PREFIX@example.com>",
    ))
    items.append(_msg(
        "OLD-DAN=", subject="From Dan",
        sender="dops@example.com", sender_name="Dan Ops",
        dt=datetime(2026, 6, 1, 9, 0, tzinfo=TZ),
        message_id="<OLD-DAN@example.com>",
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
        values=["Dan", 'Ops "West"\\Team', "missing@example.com"],
        field="sender",
    )
    assert res["ok"] is True
    assert res["source"] == "live" and "as_of" not in res
    assert res["values_checked"] == 3
    assert res["found_count"] == 2
    assert res["missing"] == ["missing@example.com"]
    assert [c["subject"] for c in res["found"]["Dan"]] == ["From Dan"]
    assert [c["subject"] for c in res["found"]['Ops "West"\\Team']] == ["Quoted display"]

    # Verify escaped AQS queries hit Exchange directly rather than a bare page scan.
    aqs_queries = [call_args[0] for call_args, _kw in inbox.filter_calls if call_args]
    assert 'from:"Dan"' in aqs_queries
    assert 'from:"Ops \\"West\\"\\\\Team"' in aqs_queries
    assert 'from:"missing@example.com"' in aqs_queries


def test_check_messages_mirror_provenance_fresh_and_values_schema(tmp_path):
    # Public JSON schema accepts both a string and an array of strings for `values`.
    schema = SPECS["check_messages"].public_schema()["inputSchema"]
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate({"values": "Dan, Ops"})
    validator.validate({"values": ["Dan", "Ops"], "fresh": True})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"values": [123]})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"values": 123})

    store = CacheStore(tmp_path / "mirror.db")
    try:
        now = int(time.time())
        row_daniel = _cache_row("M-DANIEL", subject="Daniel mail", date_ts=now)
        row_daniel["sender_name"] = "Daniel Ops"
        row_daniel["sender_email"] = "daniel@example.com"
        row_dan = _cache_row("M-DAN", subject="Dan mail", date_ts=now - 10)
        row_dan["sender_name"] = "Dan Ops"
        row_dan["sender_email"] = "dops@example.com"
        store.upsert_messages([row_daniel, row_dan])
        store.set_sync_state("item:inbox", "TOK", now)

        live_inbox = _AqsSenderFolder([
            _msg("L-DAN=", subject="Live Dan", sender="dops@example.com",
                 sender_name="Dan Ops", message_id="<L-DAN@example.com>"),
        ])
        ctx = _ctx(tmp_path, _account(inbox=live_inbox), cache=store)

        cached = _run(ctx, "check_messages", values=["Dan", "Nobody"], field="sender")
        assert cached["ok"] is True
        assert cached["source"] == "cache" and cached["as_of"]
        assert [c["subject"] for c in cached["found"]["Dan"]] == ["Dan mail"]
        assert cached["missing"] == ["Nobody"]
        assert live_inbox.filter_calls == []

        live = _run(ctx, "check_messages", values=["Dan"], field="sender", fresh=True)
        assert live["ok"] is True
        assert live["source"] == "live" and "as_of" not in live
        assert [c["subject"] for c in live["found"]["Dan"]] == ["Live Dan"]
        assert live_inbox.filter_calls == [(('from:"Dan"',), {})]
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


def test_search_fanout_applies_sender_filter_before_slicing(tmp_path):
    inbox = _Query([
        _msg("N6=", subject="other-6", sender="noise@example.com",
             sender_name="Noise", dt=datetime(2026, 6, 6, tzinfo=TZ),
             message_id="<N6@example.com>"),
        _msg("T4=", subject="target-4", sender="ally@example.com",
             sender_name="Ally", dt=datetime(2026, 6, 4, tzinfo=TZ),
             message_id="<T4@example.com>"),
    ])
    sent = _Query([
        _msg("N5=", subject="other-5", sender="noise@example.com",
             sender_name="Noise", dt=datetime(2026, 6, 5, tzinfo=TZ),
             message_id="<N5@example.com>"),
        _msg("T3=", subject="target-3", sender="ally@example.com",
             sender_name="Ally", dt=datetime(2026, 6, 3, tzinfo=TZ),
             message_id="<T3@example.com>"),
    ])
    ctx_two = _whole_mailbox(tmp_path / "two", {"Inbox": inbox, "Sent": sent})
    res = _run(ctx_two, "search_messages", folder="f:all",
               sender="ally@example.com", limit=2)
    assert res["ok"] is True
    assert [i["subject"] for i in res["items"]] == ["target-4", "target-3"]
    assert res["total_available"] is None
    assert res["next_offset"] is None  # all matches returned, no folder truncated

    archive = _Query([
        _msg("T1=", subject="target-1", sender="ally@example.com",
             sender_name="Ally", dt=datetime(2026, 6, 1, tzinfo=TZ),
             message_id="<T1@example.com>"),
    ])
    ctx_three = _whole_mailbox(
        tmp_path / "three", {"Inbox": inbox, "Sent": sent, "Archive": archive},
    )
    p1 = _run(ctx_three, "search_messages", folder="f:all",
              sender="ally@example.com", offset=0, limit=2)
    assert [i["subject"] for i in p1["items"]] == ["target-4", "target-3"]
    assert p1["next_offset"] == 2
    p2 = _run(ctx_three, "search_messages", folder="f:all",
              sender="ally@example.com", offset=2, limit=2)
    assert [i["subject"] for i in p2["items"]] == ["target-1"]
    assert p2["next_offset"] is None
