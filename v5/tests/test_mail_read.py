"""mail_read pack tests — all six read tools driven through dispatch().

No network, no exchangelib objects: a MagicMock account behind a fake
gateway, list-subclass query doubles, and SimpleNamespace messages with
tz-aware datetimes (Asia/Riyadh).
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from conftest import make_settings

from ewsmcp.audit import AuditLog
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

    def __init__(self, items=()):
        super().__init__(items)
        self.filter_calls = []
        self.only_calls = []
        self.order_calls = []
        self.count_calls = 0
        self.refresh_calls = 0
        self.total_count = len(items)

    def filter(self, *args, **kwargs):
        self.filter_calls.append((args, kwargs))
        return self

    def only(self, *args):
        self.only_calls.append(args)
        return self

    def order_by(self, *args):
        self.order_calls.append(args)
        return self

    def count(self):
        # The perf contract says this must NEVER run (full-folder scan).
        self.count_calls += 1
        return len(self)

    def refresh(self):
        self.refresh_calls += 1


class _FakeGateway:
    def __init__(self, account):
        self.account = account

    async def call(self, fn):
        return fn(self.account)

    def resolve_folder(self, account, ref, aliaser):
        return self.account.inbox


def _msg(raw_id, subject="Subj", sender="ahmed@corp.example", *, sender_name=None,
         dt=None, is_read=True, has_attachments=False, text_body="",
         conv="CONV-RAW-1=", message_id=None, attachments=None, to=None):
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
    )


def _account(inbox=None, sent=None):
    account = MagicMock(name="account")
    account.inbox = inbox if inbox is not None else _Query()
    account.sent = sent if sent is not None else _Query()
    return account


def _ctx(tmp_path, account, **overrides):
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    return Context(
        settings=make_settings(**overrides),
        gateway=_FakeGateway(account),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "alias")),
        audit=AuditLog(str(tmp_path / "audit")),
    )


def _run(ctx, name, **kwargs):
    return asyncio.run(dispatch(ctx, SPECS[name], dict(kwargs)))


# --- pack shape ---------------------------------------------------------------


def test_pack_exports_six_read_specs():
    assert len(mail_read.TOOLS) == 6
    assert set(SPECS) == {
        "list_folders", "search_messages", "get_message",
        "get_thread", "get_attachment", "get_mailbox_overview",
    }
    for spec in mail_read.TOOLS:
        assert spec.side_effect_class == "read"
        assert spec.requires_ews is True
        assert spec.input_schema["additionalProperties"] is False


# --- search_messages -----------------------------------------------------------


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


def _folder(raw_id, name, total=1, unread=0, children=()):
    return SimpleNamespace(id=raw_id, name=name, total_count=total,
                           unread_count=unread, children=list(children))


def test_list_folders_walk_with_paths_and_wk(tmp_path):
    sub = _folder("FLD-SUB=", "2026", total=4)
    inbox_folder = _folder("FLD-INBOX=", "Inbox", total=5, unread=2,
                           children=[sub])
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
    assert "wk" not in by_path["Inbox/2026"]
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
