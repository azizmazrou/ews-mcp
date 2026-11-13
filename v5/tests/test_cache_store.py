"""CacheStore: schema, FTS search, filters, write-through patches, stats.

Arabic-search correctness has its own mandatory gate suite
(test_arabic_search.py); here the store mechanics are pinned.
"""

import json
import sqlite3
import time

import pytest

from ewsmcp.cache.store import CacheStore


def make_row(ews_id, *, folder="inbox", subject="Budget review",
             sender_email="a@corp.example", sender_name="Ahmed",
             body="please review the numbers", date_ts=None, is_read=1,
             has_attachments=0, conv="CONV-1", imid=None, to=None):
    return {
        "ews_id": ews_id,
        "changekey": "CK",
        "folder": folder,
        "conversation_id": conv,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "to_json": json.dumps(to or []),
        "subject": subject,
        "date_ts": int(date_ts if date_ts is not None else time.time()),
        "date_iso": "2026-07-01T09:00+03:00",
        "is_read": is_read,
        "has_attachments": has_attachments,
        "importance": None,
        "categories_json": "[]",
        "body_clean": body,
        "internet_message_id": imid or f"<{ews_id}@corp.example>",
        "norm_text": CacheStore.norm_for_row(subject, sender_name,
                                             sender_email, body),
    }


@pytest.fixture
def store(tmp_path):
    s = CacheStore(tmp_path / "mirror.db")
    yield s
    s.close()


def test_upsert_and_fts_search(store):
    store.upsert_messages([
        make_row("M1", subject="Budget review", body="numbers attached"),
        make_row("M2", subject="Lunch", body="see you at noon"),
    ])
    rows, total = store.search_messages(text="budget")
    assert total == 1 and rows[0]["ews_id"] == "M1"
    rows, total = store.search_messages(text="noon")
    assert total == 1 and rows[0]["ews_id"] == "M2"
    # upsert replaces (same PK) and the FTS shadow follows via triggers
    updated = make_row("M2", subject="Lunch moved", body="now at one")
    store.upsert_messages([updated])
    rows, total = store.search_messages(text="noon")
    assert total == 0
    rows, total = store.search_messages(text="moved")
    assert total == 1


def test_structured_filters_and_exact_total(store):
    now = int(time.time())
    store.upsert_messages([
        make_row("M1", sender_email="a@x.example", is_read=0, date_ts=now - 100),
        make_row("M2", sender_email="b@x.example", is_read=1, date_ts=now - 50,
                 has_attachments=1),
        make_row("M3", sender_email="a@x.example", is_read=1, date_ts=now,
                 folder="sent"),
    ])
    rows, total = store.search_messages(folders=["inbox"])
    assert total == 2
    rows, total = store.search_messages(sender="a@x")
    assert total == 2
    rows, total = store.search_messages(is_unread=True)
    assert total == 1 and rows[0]["ews_id"] == "M1"
    rows, total = store.search_messages(has_attachments=True)
    assert total == 1 and rows[0]["ews_id"] == "M2"
    rows, total = store.search_messages(since_ts=now - 60)
    assert total == 2
    rows, total = store.search_messages(subject="Budget")
    assert total == 3
    # newest first + offset/limit paging
    rows, total = store.search_messages(offset=1, limit=1)
    assert total == 3 and rows[0]["ews_id"] == "M2"


def test_thread_join_and_get_message(store):
    store.upsert_messages([
        make_row("M1", conv="C9", date_ts=100),
        make_row("M2", conv="C9", date_ts=200, folder="sent"),
        make_row("M3", conv="OTHER", date_ts=300),
    ])
    rows = store.thread("C9")
    assert [r["ews_id"] for r in rows] == ["M1", "M2"]  # chronological
    assert store.get_message("M3")["conversation_id"] == "OTHER"
    # secondary lookup by internet_message_id
    assert store.get_message("<M1@corp.example>")["ews_id"] == "M1"
    assert store.get_message("GONE") is None


def test_write_through_patches(store):
    store.upsert_messages([make_row("M1", is_read=0)])
    store.set_read_flag(["M1"], True)
    assert store.get_message("M1")["is_read"] == 1
    store.apply_categories("M1", ["Follow up"])
    assert json.loads(store.get_message("M1")["categories_json"]) == ["Follow up"]
    store.tombstone_messages(["M1"])
    assert store.get_message("M1") is None
    rows, total = store.search_messages(text="budget")
    assert total == 0  # FTS shadow deleted with the row


def test_unread_page_and_watermarks(store):
    store.upsert_messages([
        make_row("M1", is_read=0, date_ts=100),
        make_row("M2", is_read=0, date_ts=200),
        make_row("M3", is_read=1, date_ts=300),
    ])
    total, rows = store.unread_page(limit=1)
    assert total == 2
    assert rows[0]["ews_id"] == "M2"  # newest unread first
    store.set_sync_state("item:inbox", "TOKEN-1", 1234.0)
    assert store.get_sync_state("item:inbox") == "TOKEN-1"
    assert store.watermark("item:inbox") == 1234
    assert "item:inbox" in store.watermarks()


def test_reads_are_read_only_connections(store, tmp_path):
    store.upsert_messages([make_row("M1")])
    with store._read() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM messages")


def test_stats_and_purge(store):
    store.upsert_messages([make_row("M1")])
    store.set_sync_state("item:inbox", "T", time.time())
    stats = store.stats()
    assert stats["rows"]["messages"] == 1
    assert stats["db_mb"] >= 0
    store.purge()
    assert store.stats()["rows"]["messages"] == 0
    assert store.get_sync_state("item:inbox") is None


def test_contact_stats_and_senders(store):
    now = int(time.time())
    store.upsert_messages([
        make_row("M1", sender_email="boss@corp.example", sender_name="Boss",
                 date_ts=now - 500),
        make_row("M2", sender_email="boss@corp.example", sender_name="Boss",
                 date_ts=now - 100),
        make_row("M3", folder="sent", sender_email="exec@corp.example",
                 to=["boss@corp.example"], date_ts=now - 50),
    ])
    stats = store.contact_stats("boss@corp.example")
    assert stats["received_count"] == 2
    assert stats["sent_count"] == 1
    rows = store.senders_matching("boss")
    assert rows[0]["sender_email"] == "boss@corp.example"
    assert rows[0]["msgs"] == 2


def test_sent_without_reply(store):
    now = int(time.time())
    old = now - 6 * 86400
    store.upsert_messages([
        # thread A: we sent last, no reply for 6 days → waiting_on
        make_row("A1", conv="CA", folder="sent", date_ts=old,
                 subject="Waiting thread"),
        # thread B: we sent, then they replied → NOT waiting
        make_row("B1", conv="CB", folder="sent", date_ts=old),
        make_row("B2", conv="CB", folder="inbox", date_ts=old + 3600),
        # thread C: we sent recently (inside the window) → NOT waiting yet
        make_row("C1", conv="CC", folder="sent", date_ts=now - 3600),
    ])
    rows = store.sent_without_reply(days=5)
    assert [r["ews_id"] for r in rows] == ["A1"]


def test_task_rows(store):
    store.upsert_tasks([
        {"ews_id": "T1", "changekey": None, "subject": "File report",
         "due_ts": 100, "due_iso": "2026-07-01", "is_complete": 0,
         "status": "NotStarted"},
        {"ews_id": "T2", "changekey": None, "subject": "Done thing",
         "due_ts": 50, "due_iso": "2026-06-01", "is_complete": 1,
         "status": "Completed"},
    ])
    rows, total = store.task_rows()
    assert total == 1 and rows[0]["ews_id"] == "T1"
    rows, total = store.task_rows(include_completed=True)
    assert total == 2
    store.delete_tasks_by_id(["T1"])
    rows, total = store.task_rows(include_completed=True)
    assert total == 1


# schema 1 folders table: no folder_class column.
_LEGACY_FOLDERS_DDL = """
CREATE TABLE folders (
    ews_id   TEXT PRIMARY KEY,
    name     TEXT,
    path     TEXT,
    wk       TEXT,
    total    INTEGER,
    unread   INTEGER,
    children INTEGER
);
"""


def test_schema_1_mirror_gains_folder_class_without_losing_rows(tmp_path):
    """Deployed mirrors predate folder_class; CREATE TABLE IF NOT EXISTS will
    not add it, and the read path selects the column unconditionally — so the
    ALTER has to run on open, and mirrored folders have to survive it."""
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(_LEGACY_FOLDERS_DDL)
    legacy.execute("INSERT INTO folders VALUES ('F-IN', 'Inbox', 'Inbox', "
                   "'f:inbox', 3, 1, 0)")
    legacy.commit()
    legacy.close()

    store = CacheStore(db_path)
    try:
        rows = store.folder_rows()
        assert [r["path"] for r in rows] == ["Inbox"]
        assert rows[0]["unread"] == 1
        assert rows[0]["folder_class"] is None  # migrated, not yet backfilled
    finally:
        store.close()
