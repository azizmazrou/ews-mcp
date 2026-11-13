"""NORTH-STAR acceptance (the whole plan's yardstick).

Scenario A: "last email from <sender> → reply as a draft" must cost:
  1) search_messages(sender=…, limit=1) — served from the mirror, ONE
     compact card {id:"m…", from, subject, date, snippet, source:"cache",
     as_of} — no raw HTML, no raw EWS id.
  2) create_draft(mode="reply", reply_to="m…", body=…) — ONE EWS round
     trip — {draft_id, preview, note:"saved as draft — NOT sent"}.
Budget: ≤2 tool calls, <2k tokens of total tool output, zero id
transcription (aliases only). example.com senders only.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from conftest import make_settings

from ewsmcp.audit import AuditLog
from ewsmcp.cache.store import CacheStore
from ewsmcp.ids import get_aliaser
from ewsmcp.tools import build_registry
from ewsmcp.tools.base import Context, dispatch

from test_cache_store import make_row

RAW_EWS_ID = "AAMkAGI2TG93AAA" + "x" * 120 + "="  # realistically long


class CountingGateway:
    """Counts EWS round trips; only create_draft may use one."""

    def __init__(self, account):
        self.account = account
        self.calls = 0

    async def call(self, fn):
        self.calls += 1
        return fn(self.account)


def _seed(tmp_path):
    store = CacheStore(tmp_path / "mirror.db")
    now = int(time.time())
    store.upsert_messages([
        make_row(RAW_EWS_ID, subject="Q3 budget approval",
                 sender_email="director@example.com", sender_name="Director",
                 body="Please confirm the Q3 budget line by Sunday.",
                 date_ts=now - 3600, conv="CONV-NS", is_read=0),
        make_row("OTHER-1", subject="Unrelated", sender_email="x@example.com",
                 body="noise", date_ts=now - 60, conv="CONV-X"),
    ])
    store.set_sync_state("item:inbox", "TOK", now)
    store.set_sync_state("item:sent", "TOK", now)
    return store


def test_north_star_two_calls_under_two_k_tokens(tmp_path):
    # --- the ONE allowed EWS interaction: saving the reply draft ---------
    original = MagicMock(name="original")
    original.subject = "Q3 budget approval"
    reply = MagicMock(name="reply")
    reply.save.return_value = SimpleNamespace(id="RAW-NEW-REPLY",
                                              changekey="CK-R")
    reply.to_recipients = [SimpleNamespace(email_address="director@example.com")]
    reply.cc_recipients = []
    original.create_reply.return_value = reply
    account = MagicMock(name="account")
    account.drafts = SimpleNamespace(id="F-DRAFTS", name="Drafts")
    account.fetch = MagicMock(return_value=[original])
    gateway = CountingGateway(account)

    ctx = Context(
        settings=make_settings(),
        gateway=gateway,
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "alias")),
        audit=AuditLog(str(tmp_path / "audit")),
        cache=_seed(tmp_path),
    )
    build_registry(ctx)
    outputs = []

    # Call 1: find the last email from the sender — pure mirror.
    res1 = asyncio.run(dispatch(ctx, ctx.registry["search_messages"],
                                {"sender": "director@example.com", "limit": 1}))
    outputs.append(res1)
    assert res1["source"] == "cache" and res1["as_of"]
    assert res1["count"] == 1
    card = res1["items"][0]
    assert card["id"].startswith("m") and len(card["id"]) <= 6  # alias only
    assert RAW_EWS_ID not in json.dumps(res1)  # raw EWS id NEVER surfaces
    assert card["snippet"].startswith("Please confirm")
    assert "<html" not in json.dumps(res1).lower()  # no raw HTML
    assert gateway.calls == 0  # Exchange untouched so far

    # Call 2: reply as a draft using the alias, verbatim.
    res2 = asyncio.run(dispatch(ctx, ctx.registry["create_draft"], {
        "mode": "reply", "reply_to": card["id"],
        "body": "Confirmed — the Q3 line is approved.",
    }))
    outputs.append(res2)
    assert res2["ok"] is True
    assert res2["draft_id"].startswith("d")
    assert res2["note"] == "saved as draft — NOT sent"
    assert res2["preview"]["to"] == ["director@example.com"]
    assert gateway.calls == 1  # exactly ONE EWS round trip

    # The dispatcher resolved the alias back to the raw id for the fetch.
    assert account.fetch.call_args.args[0] == [(RAW_EWS_ID, None)]

    # Budget: <2k tokens of total tool output (~4 chars/token).
    total_chars = sum(len(json.dumps(o, ensure_ascii=False)) for o in outputs)
    assert total_chars < 2000 * 4, f"tool output too fat: {total_chars} chars"


def test_north_star_search_is_fast_warm(tmp_path):
    """<100ms warm is the local-storage target; in CI we only pin the shape
    of the guarantee — a pure-SQLite read with no EWS round trip."""
    ctx = Context(
        settings=make_settings(),
        gateway=CountingGateway(MagicMock()),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "alias")),
        audit=AuditLog(str(tmp_path / "audit")),
        cache=_seed(tmp_path),
    )
    build_registry(ctx)
    start = time.perf_counter()
    res = asyncio.run(dispatch(ctx, ctx.registry["search_messages"],
                               {"sender": "director@example.com", "limit": 1}))
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert res["source"] == "cache"
    assert ctx.gateway.calls == 0
    assert elapsed_ms < 1500  # generous CI bound; warm target is <100ms
