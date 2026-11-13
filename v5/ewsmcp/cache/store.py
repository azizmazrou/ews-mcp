"""Per-mailbox SQLite mirror — the "fetch email very fast" core.

Design rules (hard requirements, lessons from a real SQLite corruption in
a sibling project):

- SINGLE WRITER. Only this store's writer connection (used by the sync
  engine and the write-through path, serialized by one lock) ever writes.
  Tool reads open short-lived ``mode=ro`` connections — under WAL they
  never block the writer and can't corrupt anything.
- Cleaned bodies are stored ONCE at sync time (``bodyclean`` output), so
  the measured 115 kB raw-HTML email costs its ~150 chars exactly once.
- Arabic-correct FTS: ``messages_fts`` indexes a NORMALIZED shadow text
  (``normalize.normalize_ar`` over subject + sender + body), queries are
  normalized with the same function, and the tokenizer adds
  ``unicode61 remove_diacritics 2``. Original text is returned.
- Timestamps are stored twice: epoch seconds (sortable/filterable) and
  the display ISO string in the server timezone.
- The mirror is mail-at-rest: it lives under the guarded ``data_dir``
  (absolute, never cloud-synced) with owner-only permissions.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..normalize import fts_match_expression, normalize_ar

logger = logging.getLogger(__name__)

# Recorded in meta.schema so an operator inspecting a mirror can tell which
# shape it holds; the migration itself is the additive check in _ensure_schema.
_SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    ews_id              TEXT PRIMARY KEY,
    changekey           TEXT,
    folder              TEXT NOT NULL,
    conversation_id     TEXT,
    sender_name         TEXT,
    sender_email        TEXT,
    to_json             TEXT,
    subject             TEXT,
    date_ts             INTEGER,
    date_iso            TEXT,
    is_read             INTEGER NOT NULL DEFAULT 1,
    has_attachments     INTEGER NOT NULL DEFAULT 0,
    importance          TEXT,
    categories_json     TEXT,
    body_clean          TEXT,
    internet_message_id TEXT,
    norm_text           TEXT
);
CREATE INDEX IF NOT EXISTS ix_msg_folder_date ON messages(folder, date_ts DESC);
CREATE INDEX IF NOT EXISTS ix_msg_conversation ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS ix_msg_sender ON messages(sender_email);
CREATE INDEX IF NOT EXISTS ix_msg_imid ON messages(internet_message_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    norm_text,
    content='messages',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, norm_text) VALUES (new.rowid, new.norm_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, norm_text)
    VALUES ('delete', old.rowid, old.norm_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, norm_text)
    VALUES ('delete', old.rowid, old.norm_text);
    INSERT INTO messages_fts(rowid, norm_text) VALUES (new.rowid, new.norm_text);
END;
CREATE TABLE IF NOT EXISTS events (
    ews_id       TEXT PRIMARY KEY,
    changekey    TEXT,
    subject      TEXT,
    start_ts     INTEGER,
    start_iso    TEXT,
    end_ts       INTEGER,
    end_iso      TEXT,
    location     TEXT,
    organizer    TEXT,
    is_recurring INTEGER NOT NULL DEFAULT 0,
    my_response  TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_start ON events(start_ts);
CREATE TABLE IF NOT EXISTS tasks (
    ews_id      TEXT PRIMARY KEY,
    changekey   TEXT,
    subject     TEXT,
    due_ts      INTEGER,
    due_iso     TEXT,
    is_complete INTEGER NOT NULL DEFAULT 0,
    status      TEXT
);
CREATE TABLE IF NOT EXISTS folders (
    ews_id       TEXT PRIMARY KEY,
    name         TEXT,
    path         TEXT,
    wk           TEXT,
    folder_class TEXT,
    total        INTEGER,
    unread       INTEGER,
    children     INTEGER
);
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    token TEXT,
    as_of INTEGER
);
CREATE TABLE IF NOT EXISTS sender_sigs (
    sender_email TEXT NOT NULL,
    sig_hash     TEXT NOT NULL,
    hits         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (sender_email, sig_hash)
);
"""

# A trailing block must recur this often before reads strip it — one
# coincidental match must never delete real content.
SIG_MIN_HITS = 3
_SIG_MAX_LINES = 6
_SIG_MAX_CHARS = 400


def trailing_block(body: str) -> Optional[str]:
    """The candidate signature block: the last blank-line-separated block,
    when it is short enough to be a signature and is not the whole body."""
    body = (body or "").rstrip()
    if not body:
        return None
    head, sep, tail = body.rpartition("\n\n")
    if not sep or not head.strip():
        return None
    tail = tail.strip()
    if not tail or len(tail) > _SIG_MAX_CHARS:
        return None
    if tail.count("\n") + 1 > _SIG_MAX_LINES:
        return None
    return tail


def _sig_hash(sender_email: str, block: str) -> str:
    import hashlib
    return hashlib.sha256(
        f"{sender_email.lower()}|{normalize_ar(block)}".encode()
    ).hexdigest()


class CacheStore:
    """Owner of the mirror database. One instance per process."""

    def __init__(self, db_path: "str | os.PathLike[str]"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:  # owner-only, best effort (no-op on Windows ACLs)
            os.chmod(self.db_path.parent, 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._writer: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    # ------------------------------------------------------------ plumbing

    def _writer_conn(self) -> sqlite3.Connection:
        if self._writer is None:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0,
                                   check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.row_factory = sqlite3.Row
            self._writer = conn
        return self._writer

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._writer_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """Short-lived read-only connection: tools NEVER touch the writer."""
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._writer_conn()
            conn.executescript(_SCHEMA)
            # CREATE TABLE IF NOT EXISTS is a no-op on an already-deployed
            # mirror, so schema 1 databases need the new column added by hand
            # — the read path selects it unconditionally.
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(folders)")}
            if "folder_class" not in cols:
                conn.execute("ALTER TABLE folders ADD COLUMN folder_class TEXT")
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)",
                (str(_SCHEMA_VERSION),),
            )
            conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._writer is not None:
                self._writer.close()
                self._writer = None

    # ------------------------------------------------------------- writers

    @staticmethod
    def norm_for_row(subject: str, sender_name: str, sender_email: str,
                     body_clean: str) -> str:
        return normalize_ar(
            " ".join(p for p in (subject, sender_name, sender_email, body_clean) if p)
        )

    def upsert_messages(self, rows: List[Dict[str, Any]]) -> int:
        """Insert/update message rows in ONE transaction (sync + write-through).

        Also LEARNS per-sender signatures: the trailing block of each body
        is hashed per sender; once the same block recurs SIG_MIN_HITS times
        the read paths strip it (closes the measured gap where a corporate
        signature survived clean_body)."""
        if not rows:
            return 0
        with self._write() as conn:
            for r in rows:
                block = trailing_block(r.get("body_clean") or "")
                sender = (r.get("sender_email") or "").lower()
                if block and sender:
                    conn.execute(
                        "INSERT INTO sender_sigs (sender_email, sig_hash, hits) "
                        "VALUES (?, ?, 1) "
                        "ON CONFLICT(sender_email, sig_hash) "
                        "DO UPDATE SET hits = hits + 1",
                        (sender, _sig_hash(sender, block)),
                    )
            for r in rows:
                conn.execute(
                    """
                    INSERT INTO messages (ews_id, changekey, folder,
                        conversation_id, sender_name, sender_email, to_json,
                        subject, date_ts, date_iso, is_read, has_attachments,
                        importance, categories_json, body_clean,
                        internet_message_id, norm_text)
                    VALUES (:ews_id, :changekey, :folder, :conversation_id,
                        :sender_name, :sender_email, :to_json, :subject,
                        :date_ts, :date_iso, :is_read, :has_attachments,
                        :importance, :categories_json, :body_clean,
                        :internet_message_id, :norm_text)
                    ON CONFLICT(ews_id) DO UPDATE SET
                        changekey=excluded.changekey,
                        folder=excluded.folder,
                        conversation_id=excluded.conversation_id,
                        sender_name=excluded.sender_name,
                        sender_email=excluded.sender_email,
                        to_json=excluded.to_json,
                        subject=excluded.subject,
                        date_ts=excluded.date_ts,
                        date_iso=excluded.date_iso,
                        is_read=excluded.is_read,
                        has_attachments=excluded.has_attachments,
                        importance=excluded.importance,
                        categories_json=excluded.categories_json,
                        body_clean=excluded.body_clean,
                        internet_message_id=excluded.internet_message_id,
                        norm_text=excluded.norm_text
                    """,
                    r,
                )
        return len(rows)

    def delete_messages_by_id(self, ews_ids: List[str]) -> int:
        if not ews_ids:
            return 0
        with self._write() as conn:
            for ews_id in ews_ids:
                conn.execute("DELETE FROM messages WHERE ews_id=?", (ews_id,))
        return len(ews_ids)

    # Write-through spellings used by the tool handlers:
    tombstone_messages = delete_messages_by_id

    def set_read_flag(self, ews_ids: List[str], is_read: bool) -> None:
        if not ews_ids:
            return
        with self._write() as conn:
            for ews_id in ews_ids:
                conn.execute("UPDATE messages SET is_read=? WHERE ews_id=?",
                             (1 if is_read else 0, ews_id))

    def apply_categories(self, ews_id: str, categories: Optional[List[str]]) -> None:
        with self._write() as conn:
            conn.execute("UPDATE messages SET categories_json=? WHERE ews_id=?",
                         (json.dumps(categories or []), ews_id))

    def replace_events(self, rows: List[Dict[str, Any]]) -> None:
        """Replace the expanded-occurrences window (refresh strategy)."""
        with self._write() as conn:
            conn.execute("DELETE FROM events")
            for r in rows:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO events (ews_id, changekey, subject,
                        start_ts, start_iso, end_ts, end_iso, location,
                        organizer, is_recurring, my_response)
                    VALUES (:ews_id, :changekey, :subject, :start_ts,
                        :start_iso, :end_ts, :end_iso, :location, :organizer,
                        :is_recurring, :my_response)
                    """,
                    r,
                )

    def upsert_tasks(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        with self._write() as conn:
            for r in rows:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO tasks (ews_id, changekey, subject,
                        due_ts, due_iso, is_complete, status)
                    VALUES (:ews_id, :changekey, :subject, :due_ts, :due_iso,
                        :is_complete, :status)
                    """,
                    r,
                )

    def delete_tasks_by_id(self, ews_ids: List[str]) -> None:
        if not ews_ids:
            return
        with self._write() as conn:
            for ews_id in ews_ids:
                conn.execute("DELETE FROM tasks WHERE ews_id=?", (ews_id,))

    def replace_folders(self, rows: List[Dict[str, Any]]) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM folders")
            for r in rows:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO folders (ews_id, name, path, wk,
                        folder_class, total, unread, children)
                    VALUES (:ews_id, :name, :path, :wk, :folder_class, :total,
                        :unread, :children)
                    """,
                    {"folder_class": None, **r},  # optional: Exchange may omit it
                )

    def get_sync_state(self, key: str) -> Optional[str]:
        with self._read() as conn:
            row = conn.execute("SELECT token FROM sync_state WHERE key=?",
                               (key,)).fetchone()
        return row["token"] if row else None

    def set_sync_state(self, key: str, token: Optional[str],
                       as_of_ts: Optional[float] = None) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sync_state(key, token, as_of) "
                "VALUES (?, ?, ?)",
                (key, token, int(as_of_ts if as_of_ts is not None else time.time())),
            )

    def purge(self) -> None:
        """Drop every mirrored row and sync token (admin path; the next
        sync cycle rebuilds from scratch)."""
        with self._write() as conn:
            for table in ("messages", "events", "tasks", "folders",
                          "sync_state", "sender_sigs"):
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed names
        logger.warning("cache purged: %s", self.db_path)

    # -------------------------------------------------------------- reads

    def watermark(self, key: str) -> Optional[int]:
        try:
            with self._read() as conn:
                row = conn.execute("SELECT as_of FROM sync_state WHERE key=?",
                                   (key,)).fetchone()
            return int(row["as_of"]) if row else None
        except sqlite3.Error:
            return None

    def watermarks(self) -> Dict[str, int]:
        try:
            with self._read() as conn:
                rows = conn.execute("SELECT key, as_of FROM sync_state").fetchall()
            return {r["key"]: int(r["as_of"]) for r in rows}
        except sqlite3.Error:
            return {}

    def stats(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        try:
            with self._read() as conn:
                for table in ("messages", "events", "tasks", "folders"):
                    counts[table] = conn.execute(
                        f"SELECT COUNT(*) AS c FROM {table}"  # noqa: S608
                    ).fetchone()["c"]
        except sqlite3.Error:
            pass
        db_mb = 0.0
        try:
            db_mb = round(self.db_path.stat().st_size / 1_048_576, 2)
        except OSError:
            pass
        return {"rows": counts, "db_mb": db_mb, "watermarks": self.watermarks()}

    def search_messages(
        self,
        *,
        folders: Optional[List[str]] = None,
        text: Optional[str] = None,
        sender: Optional[str] = None,
        subject: Optional[str] = None,
        since_ts: Optional[int] = None,
        until_ts: Optional[int] = None,
        is_unread: Optional[bool] = None,
        has_attachments: Optional[bool] = None,
        categories: Optional[List[str]] = None,
        offset: int = 0,
        limit: int = 20,
    ) -> Tuple[List[sqlite3.Row], int]:
        """Local search: FTS over the normalized shadow + SQL filters.
        Returns (rows, exact_total) — COUNT(*) is free here, which restores
        the exact total_available the live path can no longer afford."""
        where: List[str] = []
        params: List[Any] = []
        joins = ""
        if text:
            match = fts_match_expression(text)
            if match:
                joins = ("JOIN messages_fts f ON f.rowid = m.rowid "
                         "AND messages_fts MATCH ?")
                params.append(match)
        if folders:
            where.append(f"m.folder IN ({','.join('?' * len(folders))})")
            params.extend(folders)
        if sender:
            where.append("(m.sender_email LIKE ? OR m.sender_name LIKE ?)")
            needle = f"%{sender.strip()}%"
            params.extend([needle, needle])
        if subject:
            where.append("m.subject LIKE ?")
            params.append(f"%{subject.strip()}%")
        if since_ts is not None:
            where.append("m.date_ts >= ?")
            params.append(int(since_ts))
        if until_ts is not None:
            where.append("m.date_ts <= ?")
            params.append(int(until_ts))
        if is_unread is not None:
            where.append("m.is_read = ?")
            params.append(0 if is_unread else 1)
        if has_attachments is not None:
            where.append("m.has_attachments = ?")
            params.append(1 if has_attachments else 0)
        if categories:
            # AND semantics (message must carry ALL listed categories), to
            # match exchangelib's categories__contains behavior on the live
            # path (mail_read.py) - one EXISTS(json_each(...)) per category,
            # each requiring an exact (case-sensitive) match against the
            # JSON array stored in categories_json (see apply_categories()).
            for cat in categories:
                where.append(
                    "EXISTS (SELECT 1 FROM json_each(m.categories_json) "
                    "WHERE value = ?)")
                params.append(cat)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        base = f"FROM messages m {joins}{clause}"
        with self._read() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS c {base}",  # noqa: S608
                                 params).fetchone()["c"]
            rows = conn.execute(
                f"SELECT m.* {base} ORDER BY m.date_ts DESC "  # noqa: S608
                "LIMIT ? OFFSET ?",
                [*params, int(limit), int(offset)],
            ).fetchall()
        return rows, int(total)

    def strip_learned_signature(self, sender_email: str, body: str) -> str:
        """Drop the trailing block when it is a LEARNED signature for this
        sender (recurred >= SIG_MIN_HITS times). Deterministic, read-only."""
        block = trailing_block(body)
        sender = (sender_email or "").lower()
        if not block or not sender:
            return body
        try:
            with self._read() as conn:
                row = conn.execute(
                    "SELECT hits FROM sender_sigs WHERE sender_email=? "
                    "AND sig_hash=?",
                    (sender, _sig_hash(sender, block)),
                ).fetchone()
        except sqlite3.Error:
            return body
        if row is not None and row["hits"] >= SIG_MIN_HITS:
            return body.rstrip().rpartition("\n\n")[0].rstrip()
        return body

    def get_message(self, ews_id: str) -> Optional[sqlite3.Row]:
        with self._read() as conn:
            return conn.execute(
                "SELECT * FROM messages WHERE ews_id=? OR internet_message_id=?",
                (ews_id, ews_id),
            ).fetchone()

    def thread(self, conversation_id: str) -> List[sqlite3.Row]:
        with self._read() as conn:
            return conn.execute(
                "SELECT * FROM messages WHERE conversation_id=? "
                "ORDER BY date_ts ASC",
                (conversation_id,),
            ).fetchall()

    def unread_page(self, limit: int = 10) -> Tuple[int, List[sqlite3.Row]]:
        with self._read() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM messages "
                "WHERE folder='inbox' AND is_read=0"
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT * FROM messages WHERE folder='inbox' AND is_read=0 "
                "ORDER BY date_ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return int(total), rows

    def events_window(self, start_ts: int, end_ts: int,
                      limit: int = 25) -> List[sqlite3.Row]:
        with self._read() as conn:
            return conn.execute(
                "SELECT * FROM events WHERE start_ts < ? AND end_ts > ? "
                "ORDER BY start_ts ASC LIMIT ?",
                (int(end_ts), int(start_ts), int(limit)),
            ).fetchall()

    def folder_rows(self) -> List[sqlite3.Row]:
        with self._read() as conn:
            return conn.execute(
                "SELECT * FROM folders ORDER BY path ASC").fetchall()

    def task_rows(self, include_completed: bool = False,
                  offset: int = 0, limit: int = 50) -> Tuple[List[sqlite3.Row], int]:
        clause = "" if include_completed else " WHERE is_complete=0"
        with self._read() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM tasks{clause}"  # noqa: S608
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM tasks{clause} "  # noqa: S608
                "ORDER BY COALESCE(due_ts, 1e15) ASC LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            ).fetchall()
        return rows, int(total)

    def contact_stats(self, email: str) -> Dict[str, Any]:
        """Mirror-derived relationship stats for one address."""
        needle = (email or "").strip().lower()
        if not needle:
            return {}
        with self._read() as conn:
            received = conn.execute(
                "SELECT COUNT(*) AS c, MIN(date_iso) AS first, MAX(date_iso) AS last "
                "FROM messages WHERE lower(sender_email)=? AND folder != 'sent'",
                (needle,),
            ).fetchone()
            sent = conn.execute(
                "SELECT COUNT(*) AS c, MAX(date_iso) AS last FROM messages "
                "WHERE folder='sent' AND lower(to_json) LIKE ?",
                (f'%{needle}%',),
            ).fetchone()
        out: Dict[str, Any] = {}
        if received and received["c"]:
            out.update({"received_count": received["c"],
                        "first_seen": received["first"],
                        "last_received": received["last"]})
        if sent and sent["c"]:
            out.update({"sent_count": sent["c"], "last_sent": sent["last"]})
        return out

    def senders_matching(self, query: str, limit: int = 10) -> List[sqlite3.Row]:
        """People discovery from mail history (fallback when GAL is cold)."""
        needle = f"%{(query or '').strip()}%"
        with self._read() as conn:
            return conn.execute(
                "SELECT sender_email, sender_name, COUNT(*) AS msgs, "
                "MAX(date_iso) AS last_seen FROM messages "
                "WHERE folder != 'sent' AND (sender_email LIKE ? OR sender_name LIKE ?) "
                "GROUP BY lower(sender_email) ORDER BY msgs DESC LIMIT ?",
                (needle, needle, int(limit)),
            ).fetchall()

    def sent_without_reply(self, days: int = 5,
                           limit: int = 25) -> List[sqlite3.Row]:
        """waiting_on: sent-folder threads with no later inbound message."""
        cutoff = int(time.time() - days * 86400)
        with self._read() as conn:
            return conn.execute(
                """
                SELECT s.* FROM messages s
                WHERE s.folder = 'sent'
                  AND s.date_ts <= ?
                  AND s.conversation_id IS NOT NULL
                  AND s.date_ts = (
                        SELECT MAX(x.date_ts) FROM messages x
                        WHERE x.conversation_id = s.conversation_id
                          AND x.folder = 'sent')
                  AND NOT EXISTS (
                        SELECT 1 FROM messages i
                        WHERE i.conversation_id = s.conversation_id
                          AND i.folder != 'sent'
                          AND i.date_ts > s.date_ts)
                ORDER BY s.date_ts DESC LIMIT ?
                """,
                (cutoff, int(limit)),
            ).fetchall()
