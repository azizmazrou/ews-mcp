"""Unified per-mailbox SQLite cache.

Three tables, one file, zero new dependencies (sqlite3 is in stdlib).

* `body_format_cache`     — converted email bodies (HTML -> markdown), keyed
                            by (message_id, format).
* `attachment_text_cache` — extracted text from attachments (PDF/DOCX/XLSX/
                            PPTX/HTML/CSV/TXT), keyed by (attachment_id).
* `embedding_cache`       — float32 vectors for semantic_search, keyed by
                            (text_hash, model). Backwards-compatible with
                            the old data/embeddings/embeddings.json.

Why SQLite, not a vector DB:
    Forking users should not need to stand up an extra service. The cache
    fits in a single file, sqlite is in stdlib, cosine similarity in numpy
    is fast enough for any realistic personal-mailbox size (<= 100K vectors).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import struct
import time
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

_log = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS body_format_cache (
    message_id  TEXT NOT NULL,
    format      TEXT NOT NULL,    -- 'markdown' | 'text'
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (message_id, format)
);

CREATE TABLE IF NOT EXISTS attachment_text_cache (
    attachment_id  TEXT PRIMARY KEY,
    file_name      TEXT,
    content_type   TEXT,
    extracted_text TEXT NOT NULL,
    extractor      TEXT NOT NULL,    -- e.g. 'pypdf', 'python-pptx', 'markdownify'
    bytes_in       INTEGER,
    chars_out      INTEGER,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS embedding_cache (
    text_hash   TEXT NOT NULL,        -- sha256 of (subject + first 500 chars body)
    model       TEXT NOT NULL,
    vector      BLOB NOT NULL,        -- packed float32, len = dim*4
    dim         INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (text_hash, model)
);

CREATE INDEX IF NOT EXISTS idx_embedding_model ON embedding_cache(model);
CREATE INDEX IF NOT EXISTS idx_attachment_extractor ON attachment_text_cache(extractor);
"""


class SQLiteCache:
    """Thin wrapper. One connection per call — sqlite3 in default isolation
    auto-commits on each `with conn:` exit, and per-call connections sidestep
    threading-mode foot-guns when the MCP runs in async mode."""

    def __init__(self, db_path: str | os.PathLike[str]):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    # ------------------------------ low-level ------------------------------

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._path, timeout=10.0, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    # ------------------------------ body cache ------------------------------

    def get_body(self, message_id: str, fmt: str) -> Optional[str]:
        if not message_id or fmt not in ("markdown", "text"):
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT body FROM body_format_cache WHERE message_id=? AND format=?",
                (message_id, fmt),
            ).fetchone()
            return row[0] if row else None

    def put_body(self, message_id: str, fmt: str, body: str) -> None:
        if not message_id or fmt not in ("markdown", "text"):
            return
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO body_format_cache "
                "(message_id, format, body, created_at) VALUES (?, ?, ?, ?)",
                (message_id, fmt, body, time.time()),
            )

    # --------------------------- attachment cache ---------------------------

    def get_attachment_text(self, attachment_id: str) -> Optional[dict]:
        if not attachment_id:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT file_name, content_type, extracted_text, extractor, "
                "bytes_in, chars_out, created_at "
                "FROM attachment_text_cache WHERE attachment_id=?",
                (attachment_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "attachment_id": attachment_id,
                "file_name": row[0],
                "content_type": row[1],
                "extracted_text": row[2],
                "extractor": row[3],
                "bytes_in": row[4],
                "chars_out": row[5],
                "created_at": row[6],
            }

    def put_attachment_text(
        self,
        attachment_id: str,
        file_name: str,
        content_type: str,
        extracted_text: str,
        extractor: str,
        bytes_in: int,
    ) -> None:
        if not attachment_id:
            return
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO attachment_text_cache "
                "(attachment_id, file_name, content_type, extracted_text, "
                " extractor, bytes_in, chars_out, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attachment_id, file_name, content_type, extracted_text,
                    extractor, bytes_in, len(extracted_text), time.time(),
                ),
            )

    # ---------------------------- embedding cache ----------------------------

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def pack(vec: List[float]) -> bytes:
        return struct.pack(f"{len(vec)}f", *vec)

    @staticmethod
    def unpack(blob: bytes, dim: int) -> List[float]:
        return list(struct.unpack(f"{dim}f", blob))

    def get_embedding(self, text_hash: str, model: str) -> Optional[List[float]]:
        with self._conn() as c:
            row = c.execute(
                "SELECT vector, dim FROM embedding_cache WHERE text_hash=? AND model=?",
                (text_hash, model),
            ).fetchone()
            if not row:
                return None
            return self.unpack(row[0], row[1])

    def put_embedding(self, text_hash: str, model: str, vec: List[float]) -> None:
        if not vec:
            return
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO embedding_cache "
                "(text_hash, model, vector, dim, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (text_hash, model, self.pack(vec), len(vec), time.time()),
            )

    def get_embeddings_bulk(
        self, hashes: Iterable[str], model: str
    ) -> dict:
        """Return {text_hash: vector} for the requested hashes that exist."""
        hashes = list(hashes)
        if not hashes:
            return {}
        out: dict = {}
        with self._conn() as c:
            # SQLite has a SQLITE_MAX_VARIABLE_NUMBER limit (default 32766).
            # 200 candidates per call is well under, but chunk anyway for safety.
            CHUNK = 500
            for i in range(0, len(hashes), CHUNK):
                batch = hashes[i:i + CHUNK]
                placeholders = ",".join("?" * len(batch))
                for row in c.execute(
                    f"SELECT text_hash, vector, dim FROM embedding_cache "
                    f"WHERE model=? AND text_hash IN ({placeholders})",
                    [model] + batch,
                ):
                    out[row[0]] = self.unpack(row[1], row[2])
        return out

    def count_embeddings(self, model: Optional[str] = None) -> int:
        with self._conn() as c:
            if model:
                row = c.execute(
                    "SELECT COUNT(*) FROM embedding_cache WHERE model=?", (model,)
                ).fetchone()
            else:
                row = c.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()
            return row[0]

    # ----------------------- one-shot legacy migration -----------------------

    def import_legacy_embeddings_json(
        self, json_path: str | os.PathLike[str], model: str, dim_hint: int = 768
    ) -> dict:
        """Read the v3.4 `data/embeddings/embeddings.json` cache file once.

        The legacy file is a flat dict {sha256_hex: [floats]}. Each entry is
        attributed to ``model`` (the migration caller knows what model the
        legacy cache was built against — typically "nomic-embed-text").

        Idempotent — duplicate hashes are silently INSERT OR REPLACE.

        Returns: {"loaded": N, "skipped": K, "errors": E}.
        """
        p = Path(json_path)
        if not p.exists():
            return {"loaded": 0, "skipped": 0, "errors": 0, "missing": True}

        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            _log.warning("legacy embeddings.json unreadable: %s", exc)
            return {"loaded": 0, "skipped": 0, "errors": 1, "missing": False}

        if not isinstance(data, dict):
            _log.warning("legacy embeddings.json not a dict")
            return {"loaded": 0, "skipped": 0, "errors": 1, "missing": False}

        loaded = 0
        skipped = 0
        errors = 0
        with self._conn() as c:
            for h, vec in data.items():
                try:
                    if not isinstance(vec, list) or not vec:
                        skipped += 1
                        continue
                    dim = len(vec)
                    blob = self.pack([float(x) for x in vec])
                    c.execute(
                        "INSERT OR REPLACE INTO embedding_cache "
                        "(text_hash, model, vector, dim, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (h, model, blob, dim, time.time()),
                    )
                    loaded += 1
                except Exception:
                    errors += 1
        # v4.0 — DO NOT rename the legacy JSON. The current EmbeddingService
        # in src/ai/embedding_service.py still reads/writes embeddings.json
        # directly; the SQLite copy is a shadow that v4.1 will switch the
        # service over to. Renaming here would orphan ~1000 cached vectors
        # because the legacy service would then start with an empty file
        # and rebuild from scratch. Idempotent re-import on every startup
        # is cheap (INSERT OR REPLACE).

        _log.info(
            "embedding cache migrated: loaded=%d skipped=%d errors=%d (model=%s)",
            loaded, skipped, errors, model,
        )
        return {"loaded": loaded, "skipped": skipped, "errors": errors, "missing": False}
