"""Phase F surface: tasks pack, get_contact, waiting_on, semantic mode,
find_similar registration, /metrics, learned-signature stripping."""

import asyncio
import time
from datetime import date
from types import SimpleNamespace

from conftest import make_settings

from ewsmcp.audit import AuditLog
from ewsmcp.cache.store import SIG_MIN_HITS, CacheStore
from ewsmcp.ids import get_aliaser
from ewsmcp.semantic import rrf_merge
from ewsmcp.tools import build_registry
from ewsmcp.tools.base import Context, dispatch

from test_cache_store import make_row


class Gateway:
    def __init__(self, account=None):
        self.account = account
        self.calls = 0

    async def call(self, fn):
        self.calls += 1
        return fn(self.account)


def _ctx(tmp_path, gateway=None, semantic=None, cache=None, **overrides):
    ctx = Context(
        settings=make_settings(**overrides),
        gateway=gateway or Gateway(),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "alias")),
        audit=AuditLog(str(tmp_path / "audit")),
        cache=cache,
        semantic=semantic,
    )
    build_registry(ctx)
    return ctx


def _run(ctx, name, **kwargs):
    return asyncio.run(dispatch(ctx, ctx.registry[name], dict(kwargs)))


# --- registry counts (change DELIBERATELY) ----------------------------------


def test_registry_counts_per_tier_and_semantic(tmp_path):
    full = _ctx(tmp_path, ews_capability_tier="full")
    assert len(full.registry) == 32
    assert "find_similar" not in full.registry  # semantic off by default
    draft = _ctx(tmp_path, ews_capability_tier="draft")
    assert len(draft.registry) == 27
    read = _ctx(tmp_path, ews_capability_tier="read")
    assert len(read.registry) == 16
    with_sem = _ctx(tmp_path, semantic=object(), ews_capability_tier="full")
    assert len(with_sem.registry) == 33
    assert "find_similar" in with_sem.registry


# --- tasks pack ---------------------------------------------------------------


def _tasks_store(tmp_path):
    store = CacheStore(tmp_path / "mirror.db")
    store.upsert_tasks([
        {"ews_id": "T1", "changekey": None, "subject": "File the report",
         "due_ts": 100, "due_iso": "2026-07-15", "is_complete": 0,
         "status": "NotStarted"},
    ])
    store.set_sync_state("item:tasks", "TOK", time.time())
    return store


def test_list_tasks_from_mirror(tmp_path):
    ctx = _ctx(tmp_path, cache=_tasks_store(tmp_path))
    res = _run(ctx, "list_tasks")
    assert res["source"] == "cache"
    assert res["count"] == 1
    assert res["items"][0]["subject"] == "File the report"
    assert res["items"][0]["id"].startswith("k")


def test_list_tasks_live_fallback_without_mirror(tmp_path):
    class Item:
        id = "T-RAW"
        changekey = None
        subject = "Live task"
        due_date = None
        is_complete = False
        status = "NotStarted"

    class Tasks:
        def all(self):
            return [Item()]

    account = SimpleNamespace(tasks=Tasks())
    ctx = _ctx(tmp_path, Gateway(account))
    res = _run(ctx, "list_tasks")
    assert res["source"] == "live"
    assert res["items"][0]["subject"] == "Live task"


def test_update_task_complete_and_due(tmp_path):
    class Item:
        def __init__(self):
            self.id = "T-RAW"
            self.due_date = None
            self.is_complete = False
            self.saved = []
            self.completed = 0

        def save(self, update_fields=None, **kw):
            self.saved.append(update_fields)

        def complete(self):
            self.completed += 1
            self.is_complete = True

    item = Item()
    account = SimpleNamespace(fetch=lambda ids, only_fields=None: [item])
    ctx = _ctx(tmp_path, Gateway(account))
    res = _run(ctx, "update_task", id="T-RAW", complete=True, due="2026-08-01")
    assert res["ok"] is True
    assert item.completed == 1
    assert item.due_date == date(2026, 8, 1)
    assert item.saved == [["due_date"]]
    nothing = _run(ctx, "update_task", id="T-RAW")
    assert nothing["error"]["code"] == "validation"


def test_waiting_on_from_mirror(tmp_path):
    store = CacheStore(tmp_path / "mirror.db")
    now = int(time.time())
    store.upsert_messages([
        make_row("S1", conv="CW", folder="sent", date_ts=now - 6 * 86400,
                 subject="Pending decision", to=["boss@corp.example"]),
    ])
    store.set_sync_state("item:sent", "TOK", now)
    ctx = _ctx(tmp_path, cache=store)
    res = _run(ctx, "waiting_on", days=5)
    assert res["source"] == "cache"
    assert res["count"] == 1
    assert res["items"][0]["subject"] == "Pending decision"
    assert res["items"][0]["to"] == ["boss@corp.example"]
    assert res["items"][0]["thread"].startswith("t")


def test_waiting_on_requires_mirror(tmp_path):
    ctx = _ctx(tmp_path)  # no cache
    res = _run(ctx, "waiting_on")
    assert res["error"]["code"] == "upstream_unavailable"


# --- get_contact ----------------------------------------------------------------


def test_get_contact_by_email_with_history(tmp_path):
    store = CacheStore(tmp_path / "mirror.db")
    now = int(time.time())
    store.upsert_messages([
        make_row("M1", sender_email="boss@corp.example", sender_name="Boss",
                 date_ts=now - 100),
    ])
    mailbox = SimpleNamespace(name="Boss Person", email_address="boss@corp.example")
    contact = SimpleNamespace(display_name="Boss Person", job_title="Director",
                              company_name="Acme", phone_numbers=[])
    account = SimpleNamespace(protocol=SimpleNamespace(
        resolve_names=lambda names, return_full_contact_data: [(mailbox, contact)]))
    ctx = _ctx(tmp_path, Gateway(account), cache=store)
    res = _run(ctx, "get_contact", id="boss@corp.example")
    assert res["ok"] is True
    person = res["person"]
    assert person["title"] == "Director"
    assert person["history"]["received_count"] == 1
    assert person["id"].startswith("p")


def test_get_contact_mirror_fallback_when_gal_down(tmp_path):
    store = CacheStore(tmp_path / "mirror.db")
    store.upsert_messages([
        make_row("M1", sender_email="boss@corp.example", sender_name="Boss")])

    class DeadGateway:
        async def call(self, fn):
            raise ConnectionError("GAL down")

    ctx = _ctx(tmp_path, DeadGateway(), cache=store)
    res = _run(ctx, "get_contact", id="boss@corp.example")
    assert res["ok"] is True
    assert res["person"]["source"] == "mirror"


# --- semantic mode ----------------------------------------------------------------


class FakeSemantic:
    def __init__(self, hits=None, similar=None, fail=False):
        self.hits = hits or []
        self.similar = similar or []
        self.fail = fail

    def query(self, text, top_k=20):
        if self.fail:
            raise ConnectionError("ollama down")
        return self.hits

    def query_similar(self, ews_id, top_k=5):
        if self.fail:
            raise ConnectionError("ollama down")
        return self.similar


def _sem_store(tmp_path):
    store = CacheStore(tmp_path / "mirror.db")
    now = int(time.time())
    store.upsert_messages([
        make_row("K1", subject="Vendor contract", body="terms agreed",
                 date_ts=now - 300),
        make_row("K2", subject="Vendor invoice", body="payment due",
                 date_ts=now - 200),
        make_row("K3", subject="Weekly report", body="numbers inside",
                 date_ts=now - 100),
    ])
    store.set_sync_state("item:inbox", "TOK", now)
    return store


def test_semantic_mode_fuses_fts_and_vector_ranks(tmp_path):
    # FTS finds K1,K2 for "vendor"; the vector tier ranks K3 first —
    # RRF puts a doubly-ranked id above single-source ids.
    sem = FakeSemantic(hits=[("K2", 0.9), ("K3", 0.8)])
    ctx = _ctx(tmp_path, cache=_sem_store(tmp_path), semantic=sem)
    res = _run(ctx, "search_messages", query="vendor", mode="semantic")
    assert res["source"] == "cache"
    subjects = [c["subject"] for c in res["items"]]
    assert subjects[0] == "Vendor invoice"  # K2: in both rankings
    assert "Weekly report" in subjects  # K3 pulled in by the vector tier
    assert "degraded" not in res


def test_semantic_outage_degrades_to_keyword(tmp_path):
    ctx = _ctx(tmp_path, cache=_sem_store(tmp_path),
               semantic=FakeSemantic(fail=True))
    res = _run(ctx, "search_messages", query="vendor", mode="semantic")
    assert res["ok"] is True
    assert "degraded" in res
    assert res["count"] == 2  # keyword-only survivors


def test_semantic_mode_disabled_is_a_clear_validation_error(tmp_path):
    ctx = _ctx(tmp_path, cache=_sem_store(tmp_path))  # semantic None
    res = _run(ctx, "search_messages", query="vendor", mode="semantic")
    assert res["error"]["code"] == "validation"
    assert "keyword" in res["error"]["hint"]


def test_find_similar(tmp_path):
    sem = FakeSemantic(similar=[("K2", 0.91)])
    ctx = _ctx(tmp_path, cache=_sem_store(tmp_path), semantic=sem)
    res = _run(ctx, "find_similar", id="K1")
    assert res["ok"] is True
    assert res["items"][0]["subject"] == "Vendor invoice"
    assert res["items"][0]["similarity"] == 0.91


def test_rrf_merge_prefers_double_ranked():
    assert rrf_merge(["a", "b"], ["b", "c"])[0] == "b"


# --- learned signature stripping -----------------------------------------------


def test_signature_learned_after_min_hits(tmp_path):
    store = CacheStore(tmp_path / "mirror.db")
    sig = "Best regards\nBoss Person\nDirector, Acme"
    for i in range(SIG_MIN_HITS):
        store.upsert_messages([make_row(
            f"S{i}", sender_email="boss@corp.example",
            body=f"Message number {i} content.\n\n{sig}")])
    body = f"Fresh content here.\n\n{sig}"
    stripped = store.strip_learned_signature("boss@corp.example", body)
    assert stripped == "Fresh content here."
    # A different sender with the same block is NOT affected.
    assert store.strip_learned_signature("other@corp.example", body) == body
    # One-off trailing content is never stripped.
    once = "Some text.\n\nUnique closing line"
    assert store.strip_learned_signature("boss@corp.example", once) == once


# --- /metrics --------------------------------------------------------------------


def test_metrics_exposition(tmp_path):

    from ewsmcp.http import build_app

    ctx = _ctx(tmp_path, cache=_sem_store(tmp_path))
    ctx.counters["tool.search_messages"] = 4
    ctx.counters["err.validation"] = 1
    app = build_app(ctx, make_settings())
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "http", "path": "/metrics", "method": "GET",
                     "headers": []}, receive, send))
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body").decode()
    assert 'ewsmcp_tool_calls_total{tool="search_messages"} 4' in body
    assert 'ewsmcp_errors_total{code="validation"} 1' in body
    assert 'ewsmcp_cache_rows{table="messages"} 3' in body
    assert "ewsmcp_uptime_seconds" in body
