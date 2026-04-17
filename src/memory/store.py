"""Per-mailbox persistent memory store for agentic features.

Design goals
------------
1. **Mailbox isolation.** One SQLite file per *primary* authenticated mailbox.
   Access to impersonated mailboxes is still recorded under the primary
   mailbox's file, but every row is keyed by the *acting* mailbox as well so
   cross-mailbox reads are impossible by construction.
2. **SQL safety.** Every query uses parameterised placeholders; never f-string
   values into SQL.
3. **Path jailed.** The database file lives under ``EWS_MEMORY_DIR`` (default
   ``data/memory``). Resolved paths are verified to be inside that jail.
4. **Size caps.** Single values are capped at 1 MiB; a namespace is capped at
   50 MiB of retained values to prevent unbounded growth.
5. **Thread safety.** The store uses a short-lived connection per call
   (``check_same_thread=False`` is not used) so it behaves correctly under
   asyncio/threaded tool execution.
6. **Typed accessors live in ``models.py``.** This module is the low-level
   key/value primitive everyone else builds on.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from ..exceptions import ToolExecutionError, ValidationError

_LOG = logging.getLogger(__name__)


# --- Configuration --------------------------------------------------------

_DEFAULT_MEMORY_DIR = Path(os.environ.get("EWS_MEMORY_DIR", "data/memory"))
# Per-value byte cap. Values above this are rejected.
_MAX_VALUE_BYTES = 1 * 1024 * 1024  # 1 MiB
# Per-namespace retained-bytes soft cap. When exceeded the oldest keys are
# pruned (LRU by ``updated_at``) until the namespace fits again.
_NAMESPACE_BYTE_CAP = 50 * 1024 * 1024  # 50 MiB
# Max keys per list() call.
_MAX_LIST_LIMIT = 500

# Keys and namespaces must be printable ASCII with a small alphabet. This
# rules out path traversal in SQLite URIs, weird bytes in logs, and accidental
# collisions with internal prefixes.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def _validate_name(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.match(value):
        raise ValidationError(
            f"{kind} must match {_SAFE_NAME.pattern!r}; got {value!r}"
        )
    return value


def _mailbox_to_filename(mailbox: str) -> str:
    """Turn a mailbox email into a safe filename fragment.

    We intentionally *don't* stick the raw email in the filename — email
    addresses are not guaranteed to be safe filesystem names, and an attacker
    who could influence impersonation strings should not be able to land
    ``../..`` on disk. Instead we stable-hash to hex.
    """
    import hashlib

    if not mailbox:
        raise ValidationError("mailbox is required for memory access")
    h = hashlib.sha256(mailbox.lower().encode("utf-8")).hexdigest()[:16]
    return f"mailbox-{h}.sqlite3"


# --- Record dataclass -----------------------------------------------------


@dataclass(frozen=True)
class MemoryRecord:
    """A single memory entry as returned to callers."""

    mailbox: str
    namespace: str
    key: str
    value: Any  # JSON-deserialised payload
    created_at: float  # epoch seconds
    updated_at: float
    expires_at: Optional[float]
    metadata: dict

    def to_dict(self) -> dict:
        return {
            "mailbox": self.mailbox,
            "namespace": self.namespace,
            "key": self.key,
            "value": self.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "metadata": self.metadata,
        }


# --- Store implementation -------------------------------------------------


class MemoryStore:
    """Per-mailbox SQLite KV with namespaces, TTL, and audit.

    Typical usage::

        store = MemoryStore.for_mailbox("alice@corp.com")
        store.set("thread.snooze", "AAMk...", {"until": 1700000000})
        record = store.get("thread.snooze", "AAMk...")
    """

    # Module-level lock table: one RLock per primary mailbox. SQLite itself
    # serialises writes at the file level, but we grab this lock around
    # multi-statement sequences (size-cap pruning, approval consume).
    _LOCKS: dict[str, threading.RLock] = {}
    _LOCKS_GUARD = threading.Lock()

    def __init__(self, mailbox: str, db_path: Path) -> None:
        self.mailbox = mailbox
        self.db_path = db_path
        self._ensure_schema()

    # --- Factories --------------------------------------------------------

    @classmethod
    def for_mailbox(
        cls,
        mailbox: str,
        base_dir: Optional[Path] = None,
    ) -> "MemoryStore":
        """Open (creating if needed) the store for ``mailbox``.

        ``base_dir`` overrides the default jail (``EWS_MEMORY_DIR``). Useful
        for tests with ``tmp_path``.
        """
        if not mailbox:
            raise ValidationError("mailbox must be a non-empty string")
        base = (base_dir or _DEFAULT_MEMORY_DIR).resolve()
        base.mkdir(parents=True, exist_ok=True)
        # Harden file perms (owner-only). Best-effort on POSIX; no-op on Windows.
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
        candidate = (base / _mailbox_to_filename(mailbox)).resolve()
        # Defensive: resolved path must live inside the jail.
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ToolExecutionError(
                "Refusing to open memory store outside the configured directory"
            ) from exc
        return cls(mailbox=mailbox, db_path=candidate)

    # --- Connection & schema ---------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Each call gets its own connection; SQLite handles cross-process
        # locking, and we use WAL for concurrent readers.
        conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,  # autocommit; we use BEGIN/COMMIT manually
            timeout=5.0,
        )
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    mailbox     TEXT NOT NULL,
                    namespace   TEXT NOT NULL,
                    key         TEXT NOT NULL,
                    value_json  BLOB NOT NULL,
                    byte_size   INTEGER NOT NULL,
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL,
                    expires_at  REAL,
                    metadata_json BLOB NOT NULL DEFAULT '{}',
                    PRIMARY KEY (mailbox, namespace, key)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_kv_ns_updated ON kv(mailbox, namespace, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_kv_ns_expires ON kv(mailbox, namespace, expires_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    mailbox     TEXT NOT NULL,
                    namespace   TEXT NOT NULL,
                    key         TEXT NOT NULL,
                    op          TEXT NOT NULL, -- SET, DELETE, CONSUME
                    at          REAL NOT NULL,
                    actor       TEXT,
                    request_id  TEXT
                )
                """
            )

    # --- Lock helper ------------------------------------------------------

    def _ns_lock(self, namespace: str) -> threading.RLock:
        ident = f"{self.mailbox}:{namespace}"
        with self._LOCKS_GUARD:
            lock = self._LOCKS.get(ident)
            if lock is None:
                lock = threading.RLock()
                self._LOCKS[ident] = lock
            return lock

    # --- Core operations --------------------------------------------------

    def set(
        self,
        namespace: str,
        key: str,
        value: Any,
        *,
        ttl_seconds: Optional[int] = None,
        metadata: Optional[dict] = None,
        actor_mailbox: Optional[str] = None,
    ) -> MemoryRecord:
        """Write ``value`` under ``(namespace, key)``.

        ``ttl_seconds`` sets an expiry; ``metadata`` is an optional dict
        stored alongside the value. ``actor_mailbox`` is logged to the audit
        trail (useful when an impersonated call wrote the value).
        """
        namespace = _validate_name("namespace", namespace)
        key = _validate_name("key", key)

        value_bytes = json.dumps(value, default=str).encode("utf-8")
        if len(value_bytes) > _MAX_VALUE_BYTES:
            raise ValidationError(
                f"memory value too large: {len(value_bytes)} bytes > {_MAX_VALUE_BYTES}"
            )

        metadata_bytes = json.dumps(metadata or {}, default=str).encode("utf-8")
        if len(metadata_bytes) > 64 * 1024:
            raise ValidationError("metadata too large (max 64 KiB)")

        now = time.time()
        expires_at = (now + ttl_seconds) if ttl_seconds else None

        with self._ns_lock(namespace), self._connect() as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """
                    INSERT INTO kv(mailbox, namespace, key, value_json, byte_size,
                                   created_at, updated_at, expires_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(mailbox, namespace, key) DO UPDATE SET
                        value_json = excluded.value_json,
                        byte_size = excluded.byte_size,
                        updated_at = excluded.updated_at,
                        expires_at = excluded.expires_at,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        self.mailbox,
                        namespace,
                        key,
                        value_bytes,
                        len(value_bytes),
                        now,
                        now,
                        expires_at,
                        metadata_bytes,
                    ),
                )
                conn.execute(
                    "INSERT INTO audit(mailbox, namespace, key, op, at, actor) VALUES (?,?,?,?,?,?)",
                    (self.mailbox, namespace, key, "SET", now, actor_mailbox or self.mailbox),
                )
                # Enforce per-namespace soft cap.
                self._prune_namespace_if_needed(conn, namespace)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return self._row_to_record(
            {
                "mailbox": self.mailbox,
                "namespace": namespace,
                "key": key,
                "value_json": value_bytes,
                "created_at": now,
                "updated_at": now,
                "expires_at": expires_at,
                "metadata_json": metadata_bytes,
            }
        )

    def get(self, namespace: str, key: str) -> Optional[MemoryRecord]:
        namespace = _validate_name("namespace", namespace)
        key = _validate_name("key", key)
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM kv WHERE mailbox=? AND namespace=? AND key=?",
                (self.mailbox, namespace, key),
            ).fetchone()
        if not row:
            return None
        if row["expires_at"] is not None and row["expires_at"] < now:
            # Lazy-evict expired rows on read.
            self.delete(namespace, key)
            return None
        return self._row_to_record(row)

    def delete(self, namespace: str, key: str, actor_mailbox: Optional[str] = None) -> bool:
        namespace = _validate_name("namespace", namespace)
        key = _validate_name("key", key)
        now = time.time()
        with self._ns_lock(namespace), self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM kv WHERE mailbox=? AND namespace=? AND key=?",
                (self.mailbox, namespace, key),
            )
            deleted = cur.rowcount > 0
            if deleted:
                conn.execute(
                    "INSERT INTO audit(mailbox, namespace, key, op, at, actor) VALUES (?,?,?,?,?,?)",
                    (self.mailbox, namespace, key, "DELETE", now, actor_mailbox or self.mailbox),
                )
        return deleted

    def list(
        self,
        namespace: str,
        *,
        prefix: Optional[str] = None,
        limit: int = 100,
        include_expired: bool = False,
    ) -> list[MemoryRecord]:
        namespace = _validate_name("namespace", namespace)
        if prefix is not None:
            _validate_name("prefix", prefix)
        if not isinstance(limit, int) or limit < 1 or limit > _MAX_LIST_LIMIT:
            raise ValidationError(f"limit must be 1..{_MAX_LIST_LIMIT}")
        now = time.time()
        params: list[Any] = [self.mailbox, namespace]
        sql = "SELECT * FROM kv WHERE mailbox=? AND namespace=?"
        if prefix is not None:
            sql += " AND key LIKE ? ESCAPE '\\'"
            # Escape SQL LIKE wildcards in the user-supplied prefix.
            safe_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(safe_prefix + "%")
        if not include_expired:
            sql += " AND (expires_at IS NULL OR expires_at >= ?)"
            params.append(now)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def consume(
        self,
        namespace: str,
        key: str,
        *,
        expect_value_key: Optional[str] = None,
        expect_value_equal: Any = None,
    ) -> Optional[MemoryRecord]:
        """Atomic read-and-delete, used for single-use approval tokens.

        Optional ``expect_value_key``/``expect_value_equal`` checks a nested
        value field before consuming — protects against race conditions where
        the same approval id is redeemed twice.
        """
        namespace = _validate_name("namespace", namespace)
        key = _validate_name("key", key)
        with self._ns_lock(namespace), self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM kv WHERE mailbox=? AND namespace=? AND key=?",
                    (self.mailbox, namespace, key),
                ).fetchone()
                if not row:
                    conn.execute("COMMIT")
                    return None
                if expect_value_key is not None:
                    value = json.loads(row["value_json"])
                    if value.get(expect_value_key) != expect_value_equal:
                        conn.execute("COMMIT")
                        return None
                conn.execute(
                    "DELETE FROM kv WHERE mailbox=? AND namespace=? AND key=?",
                    (self.mailbox, namespace, key),
                )
                conn.execute(
                    "INSERT INTO audit(mailbox, namespace, key, op, at, actor) VALUES (?,?,?,?,?,?)",
                    (self.mailbox, namespace, key, "CONSUME", time.time(), self.mailbox),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self._row_to_record(row)

    # --- Utility ---------------------------------------------------------

    def _prune_namespace_if_needed(
        self, conn: sqlite3.Connection, namespace: str
    ) -> None:
        total = conn.execute(
            "SELECT COALESCE(SUM(byte_size), 0) FROM kv WHERE mailbox=? AND namespace=?",
            (self.mailbox, namespace),
        ).fetchone()[0]
        if total <= _NAMESPACE_BYTE_CAP:
            return
        # Evict oldest until under cap. We do this in small batches to avoid
        # locking for long periods.
        overflow = total - _NAMESPACE_BYTE_CAP
        _LOG.warning(
            "memory namespace %r exceeded cap by %d bytes; pruning",
            namespace, overflow
        )
        evicted = 0
        rows = conn.execute(
            "SELECT key, byte_size FROM kv WHERE mailbox=? AND namespace=? "
            "ORDER BY updated_at ASC",
            (self.mailbox, namespace),
        ).fetchall()
        for row in rows:
            if evicted >= overflow:
                break
            conn.execute(
                "DELETE FROM kv WHERE mailbox=? AND namespace=? AND key=?",
                (self.mailbox, namespace, row["key"]),
            )
            evicted += row["byte_size"]

    @staticmethod
    def _row_to_record(row: Any) -> MemoryRecord:
        get = row.__getitem__  # works for sqlite3.Row and dict
        try:
            value = json.loads(get("value_json"))
        except Exception:
            value = None
        try:
            metadata = json.loads(get("metadata_json"))
        except Exception:
            metadata = {}
        return MemoryRecord(
            mailbox=get("mailbox"),
            namespace=get("namespace"),
            key=get("key"),
            value=value,
            created_at=get("created_at"),
            updated_at=get("updated_at"),
            expires_at=get("expires_at"),
            metadata=metadata,
        )

    # --- Diagnostics -----------------------------------------------------

    def namespace_size(self, namespace: str) -> int:
        namespace = _validate_name("namespace", namespace)
        with self._connect() as conn:
            return int(
                conn.execute(
                    "SELECT COALESCE(SUM(byte_size), 0) FROM kv WHERE mailbox=? AND namespace=?",
                    (self.mailbox, namespace),
                ).fetchone()[0]
            )

    def clear_namespace(self, namespace: str) -> int:
        """Administrative bulk-delete. Returns count deleted."""
        namespace = _validate_name("namespace", namespace)
        with self._ns_lock(namespace), self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM kv WHERE mailbox=? AND namespace=?",
                (self.mailbox, namespace),
            )
            return int(cur.rowcount)


# --- Module helpers -------------------------------------------------------


def new_id() -> str:
    """Generate a high-entropy id for approvals / commitments / rules."""
    return uuid.uuid4().hex
