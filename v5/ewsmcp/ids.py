"""Short-alias layer over raw EWS item identifiers.

Raw EWS ``ItemId`` values are ~100-150 characters of case-sensitive
base64 — expensive for an LLM to copy verbatim and silently remapped
whenever an item moves folders. This module mints short, stable aliases
(``m1``, ``e3``, ``a12`` …) that a central dispatcher attaches to tool
outputs and transparently resolves back to raw ids on tool inputs.

Design goals
------------
1. **Tiny, model-friendly handles.** Aliases match ``^[a-z]{1,2}[0-9]+$``
   — one or two lowercase kind letters plus a per-kind counter. Anything
   that does not look like an alias passes through :meth:`IdAliaser.resolve`
   unchanged, so raw EWS ids keep working everywhere.
2. **Stable across moves.** When a move/copy remaps an ``ItemId`` the
   dispatcher calls :meth:`IdAliaser.rebind` and the *same* alias starts
   pointing at the new raw id — the model never has to re-learn handles.
3. **SQL safety.** Every query uses parameterised placeholders; never
   f-string values into SQL.
4. **Persistent and shared.** State lives in ``<memory_dir>/aliases.db``
   next to the memory store's per-mailbox files, so aliases survive
   restarts and stay consistent across sessions.
5. **Thread safety.** A per-instance lock plus short-lived connections
   (one per call, WAL mode) keep the aliaser correct under asyncio /
   threaded tool execution; minting is a single ``BEGIN IMMEDIATE``
   transaction.
6. **Fail open, not loud.** Aliasing is sugar — a corrupt database must
   never break a tool call. :meth:`IdAliaser.alias_for` falls back to the
   raw id and :meth:`IdAliaser.rebind` to ``None`` on storage errors. The
   one deliberate exception: resolving an alias-shaped string that is
   *unknown* raises ``KeyError`` so the model learns the handle is stale
   instead of EWS rejecting a garbage id downstream.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

_LOG = logging.getLogger(__name__)


# --- Configuration --------------------------------------------------------

# What an alias looks like: one or two lowercase kind letters + counter.
_ALIAS_RE = re.compile(r"^[a-z]{1,2}[0-9]+$")
# Kinds must be alias-prefix shaped, otherwise minted handles would not
# round-trip through resolve().
_KIND_RE = re.compile(r"^[a-z]{1,2}$")

# Tool-argument key -> kind letter. Anything unrecognised maps to "x".
_KIND_BY_KEY = {
    "message_id": "m",
    "email_id": "m",
    "draft_id": "d",
    "event_id": "e",
    "appointment_id": "e",
    "task_id": "k",
    "contact_id": "c",
    "attachment_id": "a",
    "conversation_id": "t",
    "thread_id": "t",
    "folder_id": "f",
    "rule_id": "r",
}


def kind_for_key(key: str) -> str:
    """Infer the alias kind letter from a tool-argument key name."""
    return _KIND_BY_KEY.get((key or "").lower(), "x")


# --- Aliaser implementation ------------------------------------------------


class IdAliaser:
    """SQLite-backed bidirectional map between short aliases and EWS ids.

    Typical usage::

        aliaser = get_aliaser("data/memory")
        alias = aliaser.alias_for("AAMkAGI2...", kind="m")   # -> "m1"
        raw = aliaser.resolve("m1")                          # -> "AAMkAGI2..."
        aliaser.rebind("AAMkAGI2...", "AAMkNEW...")          # move happened
    """

    def __init__(self, memory_dir: "str | os.PathLike[str]") -> None:
        base = Path(memory_dir).resolve()
        base.mkdir(parents=True, exist_ok=True)
        # Harden dir perms (owner-only). Best-effort on POSIX; no-op on Windows.
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
        self.db_path = base / "aliases.db"
        # One lock per instance; get_aliaser() guarantees one instance per
        # resolved path, so this serialises all in-process writers.
        self._lock = threading.RLock()
        self._ensure_schema()

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
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS aliases (
                    alias               TEXT PRIMARY KEY,
                    kind                TEXT NOT NULL,
                    ews_id              TEXT NOT NULL UNIQUE,
                    changekey           TEXT,
                    internet_message_id TEXT,
                    first_seen          REAL,
                    last_seen           REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_aliases_imid "
                "ON aliases(internet_message_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS counters (
                    kind TEXT PRIMARY KEY,
                    n    INTEGER
                )
                """
            )

    # --- Core operations ---------------------------------------------------

    def alias_for(
        self,
        ews_id: str,
        kind: str = "m",
        changekey: Optional[str] = None,
        internet_message_id: Optional[str] = None,
    ) -> str:
        """Return the (existing or freshly minted) alias for ``ews_id``.

        Idempotent: the same raw id always maps to the same alias, whose
        ``last_seen`` is refreshed and whose changekey / Internet-Message-Id
        are backfilled when newly supplied. New aliases are ``<kind><n>``
        with a per-kind counter bumped atomically. Never raises on storage
        errors — falls back to returning the raw id.
        """
        if not ews_id:
            return ews_id
        if not _KIND_RE.match(kind):
            _LOG.warning("id_alias: invalid kind %r; using 'x'", kind)
            kind = "x"
        try:
            # Read-only fast path: an existing row with no new metadata to
            # backfill answers from a lock-free SELECT — list/search paths
            # re-alias the same hot ids constantly and must not pay a write
            # transaction each time (alias_many refreshes last_seen in bulk).
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT alias, changekey, internet_message_id "
                    "FROM aliases WHERE ews_id=?", (ews_id,)
                ).fetchone()
            if row is not None and (
                changekey is None or row["changekey"] == changekey
            ) and (
                internet_message_id is None
                or row["internet_message_id"] == internet_message_id
            ):
                return row["alias"]
        except (sqlite3.Error, OSError):
            pass  # fall through to the write path
        try:
            now = time.time()
            with self._lock, self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT alias FROM aliases WHERE ews_id=?", (ews_id,)
                    ).fetchone()
                    if row:
                        conn.execute(
                            "UPDATE aliases SET last_seen=?, "
                            "changekey=COALESCE(?, changekey), "
                            "internet_message_id=COALESCE(?, internet_message_id) "
                            "WHERE alias=?",
                            (now, changekey, internet_message_id, row["alias"]),
                        )
                        conn.execute("COMMIT")
                        return row["alias"]
                    conn.execute(
                        "INSERT INTO counters(kind, n) VALUES (?, 1) "
                        "ON CONFLICT(kind) DO UPDATE SET n = n + 1",
                        (kind,),
                    )
                    n = conn.execute(
                        "SELECT n FROM counters WHERE kind=?", (kind,)
                    ).fetchone()[0]
                    alias = f"{kind}{n}"
                    conn.execute(
                        "INSERT INTO aliases(alias, kind, ews_id, changekey, "
                        "internet_message_id, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (alias, kind, ews_id, changekey,
                         internet_message_id, now, now),
                    )
                    conn.execute("COMMIT")
                    return alias
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        except (sqlite3.Error, OSError) as exc:
            _LOG.warning(
                "id_alias: alias_for failed (%s); returning raw id", exc
            )
            return ews_id

    def alias_many(
        self,
        entries: "list[tuple[str, str, Optional[str], Optional[str]]]",
    ) -> "dict[str, str]":
        """Bulk ``alias_for``: one transaction for a whole result page.

        ``entries`` is ``[(ews_id, kind, changekey, internet_message_id)]``.
        Returns ``{ews_id: alias}``. Runs off the event loop (callers wrap
        card building in ``asyncio.to_thread``) so a 50-card page costs one
        write transaction instead of fifty. Never raises on storage errors
        — missing entries simply fall back to per-id alias_for behavior.
        """
        out: "dict[str, str]" = {}
        if not entries:
            return out
        try:
            now = time.time()
            with self._lock, self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for ews_id, kind, changekey, imid in entries:
                        if not ews_id:
                            continue
                        if not _KIND_RE.match(kind):
                            kind = "x"
                        row = conn.execute(
                            "SELECT alias FROM aliases WHERE ews_id=?", (ews_id,)
                        ).fetchone()
                        if row:
                            conn.execute(
                                "UPDATE aliases SET last_seen=?, "
                                "changekey=COALESCE(?, changekey), "
                                "internet_message_id=COALESCE(?, internet_message_id) "
                                "WHERE alias=?",
                                (now, changekey, imid, row["alias"]),
                            )
                            out[ews_id] = row["alias"]
                            continue
                        conn.execute(
                            "INSERT INTO counters(kind, n) VALUES (?, 1) "
                            "ON CONFLICT(kind) DO UPDATE SET n = n + 1",
                            (kind,),
                        )
                        n = conn.execute(
                            "SELECT n FROM counters WHERE kind=?", (kind,)
                        ).fetchone()[0]
                        alias = f"{kind}{n}"
                        conn.execute(
                            "INSERT INTO aliases(alias, kind, ews_id, changekey, "
                            "internet_message_id, first_seen, last_seen) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (alias, kind, ews_id, changekey, imid, now, now),
                        )
                        out[ews_id] = alias
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        except (sqlite3.Error, OSError) as exc:
            _LOG.warning("id_alias: alias_many failed (%s); falling back", exc)
        return out

    def resolve(self, value: str) -> str:
        """Translate an alias back to its raw EWS id.

        Non-alias-shaped input (raw EWS id, folder name, arbitrary string)
        passes through unchanged. An alias-shaped but *unknown* value raises
        ``KeyError`` — deliberately, so the model re-fetches fresh ids
        instead of EWS failing on a phantom handle.
        """
        if not isinstance(value, str) or not _ALIAS_RE.match(value):
            return value
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT ews_id FROM aliases WHERE alias=?", (value,)
                ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            _LOG.warning(
                "id_alias: resolve lookup failed for %r (%s); passing through",
                value, exc,
            )
            return value
        if row is None:
            raise KeyError(
                f"Unknown alias {value!r}: it is stale or from a previous "
                "session. EWS ids change when items move; re-run the "
                "search/list tool to get fresh ids, then retry."
            )
        return row["ews_id"]

    def rebind(
        self,
        old_ews_id: str,
        new_ews_id: str,
        changekey: Optional[str] = None,
    ) -> Optional[str]:
        """Repoint the alias of ``old_ews_id`` at ``new_ews_id`` after a move.

        Keeps the alias itself stable. If ``new_ews_id`` was already
        registered under a *different* alias, that duplicate row is dropped
        first (the surviving handle is the one the model already holds).
        Returns the alias, or ``None`` when the old id was never aliased.
        Never raises on storage errors.
        """
        try:
            now = time.time()
            with self._lock, self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT alias FROM aliases WHERE ews_id=?",
                        (old_ews_id,),
                    ).fetchone()
                    if not row:
                        conn.execute("COMMIT")
                        return None
                    alias = row["alias"]
                    # Clear a would-be UNIQUE(ews_id) conflict: the new id
                    # may already be registered under another alias.
                    conn.execute(
                        "DELETE FROM aliases WHERE ews_id=? AND alias<>?",
                        (new_ews_id, alias),
                    )
                    conn.execute(
                        "UPDATE aliases SET ews_id=?, "
                        "changekey=COALESCE(?, changekey), last_seen=? "
                        "WHERE alias=?",
                        (new_ews_id, changekey, now, alias),
                    )
                    conn.execute("COMMIT")
                    return alias
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        except (sqlite3.Error, OSError) as exc:
            _LOG.warning("id_alias: rebind failed (%s); returning None", exc)
            return None

    def imid_for(self, alias_or_id: str) -> Optional[str]:
        """Return the stored Internet-Message-Id for an alias or raw id."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT internet_message_id FROM aliases "
                    "WHERE alias=? OR ews_id=?",
                    (alias_or_id, alias_or_id),
                ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            _LOG.warning("id_alias: imid_for failed (%s)", exc)
            return None
        return row["internet_message_id"] if row else None

    # --- Diagnostics -------------------------------------------------------

    def stats(self) -> dict:
        """Alias count per kind, e.g. ``{"m": 12, "e": 3}``."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT kind, COUNT(*) AS c FROM aliases GROUP BY kind"
                ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            _LOG.warning("id_alias: stats failed (%s)", exc)
            return {}
        return {row["kind"]: row["c"] for row in rows}


class NullAliaser:
    """Pass-through stand-in used when the SQLite store cannot initialise.

    Aliasing is sugar — a broken disk must degrade the server to raw EWS
    ids, never kill the boot. ``resolve`` passes everything through (no
    KeyError: without a store, alias-shaped strings can't be looked up,
    and raw ids must keep working).
    """

    def alias_for(self, ews_id: str, kind: str = "m", changekey=None,
                  internet_message_id=None) -> str:
        return ews_id

    def alias_many(self, entries) -> dict:
        return {e[0]: e[0] for e in entries if e and e[0]}

    def resolve(self, value: str) -> str:
        return value

    def rebind(self, old_ews_id: str, new_ews_id: str, changekey=None):
        return None

    def imid_for(self, alias_or_id: str):
        return None

    def stats(self) -> dict:
        return {}


# --- Module helpers --------------------------------------------------------

# Process-wide singleton per resolved directory, so every tool shares one
# lock and one counter sequence.
_ALIASERS: dict[str, IdAliaser] = {}
_ALIASERS_GUARD = threading.Lock()


def get_aliaser(memory_dir: str) -> IdAliaser:
    """Return the process-wide :class:`IdAliaser` for ``memory_dir``."""
    key = str(Path(memory_dir).resolve())
    with _ALIASERS_GUARD:
        aliaser = _ALIASERS.get(key)
        if aliaser is None:
            aliaser = IdAliaser(memory_dir)
            _ALIASERS[key] = aliaser
        return aliaser


def reset_aliaser_cache() -> None:
    """Drop all cached aliasers (tests only)."""
    with _ALIASERS_GUARD:
        _ALIASERS.clear()
