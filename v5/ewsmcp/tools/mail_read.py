"""Tool pack: mail_read — the read-class mail tools of DESIGN.md §Tools.

Handlers receive RAW EWS ids (the dispatcher already resolved m##/t##
aliases on the way in) and emit DTOs whose ids are short aliases. No
safety logic lives here — the dispatcher owns tier / kill-switch /
confirm gates. No direct exchangelib import: all EWS work goes through
``account`` attributes inside closures run on the gateway pool.

CACHE-FIRST CONTRACT: when the local mirror can answer (folder synced,
watermark present, ``fresh`` not requested) reads come from SQLite in
milliseconds and are stamped ``{"source": "cache", "as_of": …}``;
otherwise the live EWS path runs and stamps ``{"source": "live"}``. Any
cache error silently falls back to live — the mirror accelerates reads,
it never gates them.
"""

import asyncio
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from exchangelib.attachments import FileAttachment, ItemAttachment
from exchangelib.errors import UnauthorizedError

from ..bodyclean import clean_body
from ..dates import parse_when
from ..dto import (
    envelope,
    event_card,
    fmt_dt,
    is_pkcs7_candidate,
    msg_card,
    msg_full,
    sniff_smime_type,
)
from ..errors import ToolError, map_exception
from ..extract import (
    MAX_ERROR_MESSAGE_CHARS,
    MAX_NOTE_CHARS,
    ExtractionResult,
    _PYPDF_MISSING_HINT,
    _safe_snippet,
    cap_message,
    detect_kind,
    extract_plain_text,
    fold_and_prioritize_notes,
    is_msg_attachment,
    pypdf_available,
    serialize_message_item_fields,
    unsupported_type_error,
)
from ..extract_worker import extract_bytes_in_worker, extract_item_in_worker
from ..flag_property import ensure_registered as ensure_flag_status_registered
from ..gateway.client import WELL_KNOWN, paginate
from ..ids import NullAliaser
from .base import Context, ToolSpec

ensure_flag_status_registered()

logger = logging.getLogger(__name__)

_FRESH_PROPERTY = {
    "type": "boolean", "default": False,
    "description": "true forces a live Exchange read instead of the local "
                   "mirror (responses are stamped source=cache|live).",
}


def _stamp(result: Dict[str, Any], source: str,
           as_of_ts: Optional[int] = None) -> Dict[str, Any]:
    result["source"] = source
    if source == "cache" and as_of_ts:
        result["as_of"] = datetime.fromtimestamp(
            as_of_ts, tz=timezone.utc).isoformat(timespec="seconds")
    return result


def _cache_folder_key(ctx: Context, folder_ref: Optional[str]) -> Optional[str]:
    """Map a folder argument onto a mirrored folder key, or None (→ live)."""
    raw = (folder_ref or "f:inbox").strip().lower()
    if raw == ALL_FOLDERS:
        return None
    key = raw[2:] if raw.startswith("f:") else raw
    cached = {k.strip().lower()
              for k in (ctx.settings.ews_cache_folders or "").split(",")}
    return key if key in cached else None


def _cache_watermark(ctx: Context, folder_key: str) -> Optional[int]:
    if ctx.cache is None:
        return None
    return ctx.cache.watermark(f"item:{folder_key}")


def _row_body(ctx: Context, row: Any) -> str:
    """Cleaned body with the sender's LEARNED signature stripped (the
    deterministic per-sender trailing-block learning from sync time)."""
    body = row["body_clean"] or ""
    if ctx.cache is not None and body:
        try:
            body = ctx.cache.strip_learned_signature(row["sender_email"], body)
        except Exception:
            pass
    return body


def _row_card(ctx: Context, row: Any) -> Dict[str, Any]:
    """Mirror row → the same MsgCard shape the live path emits."""
    body = _row_body(ctx, row)
    card: Dict[str, Any] = {
        "id": ctx.aliaser.alias_for(row["ews_id"], "m",
                                    changekey=row["changekey"],
                                    internet_message_id=row["internet_message_id"]),
        "from": (f"{row['sender_name']} <{row['sender_email']}>"
                 if row["sender_name"] and row["sender_name"] != row["sender_email"]
                 else (row["sender_email"] or "")),
        "subject": row["subject"] or "",
        "date": row["date_iso"],
        "snippet": body[:200],
    }
    if row["conversation_id"]:
        card["thread"] = ctx.aliaser.alias_for(row["conversation_id"], "t")
    if not row["is_read"]:
        card["unread"] = True
    if row["has_attachments"]:
        card["attach"] = True
    imp = (row["importance"] or "").strip().lower()
    if imp in ("high", "low"):
        card["importance"] = imp
    return card


def _row_full(ctx: Context, row: Any) -> Dict[str, Any]:
    full = _row_card(ctx, row)
    full.pop("snippet", None)
    try:
        full["to"] = json.loads(row["to_json"] or "[]")
    except ValueError:
        full["to"] = []
    body = _row_body(ctx, row)
    limit = int(ctx.settings.body_max_chars)
    full["body"] = body[:limit]
    if len(body) > limit:
        full["body_truncated"] = True
    if row["internet_message_id"]:
        full["internet_message_id"] = row["internet_message_id"]
    try:
        cats = json.loads(row["categories_json"] or "[]")
    except ValueError:
        cats = []
    if cats:
        full["categories"] = cats
    if row["has_attachments"]:
        full["attachments_hint"] = ("message has attachments — call again "
                                    "with fresh=true for the inventory, or "
                                    "get_attachment to read one")
    return full

# Fields fetched for every list/search projection (token economy: never
# pull full MIME just to render a card).
_PROJECTION = (
    "id", "subject", "sender", "datetime_received", "is_read",
    "has_attachments", "text_body", "to_recipients", "conversation_id",
    "message_id", "importance", "attachments",
)

# Full-message projection for get_messages(format="full"): everything msg_full
# reads, without pulling base64 mime_content across a multi-message batch.
_FULL_PROJECTION = (
    *_PROJECTION, "changekey", "cc_recipients", "body", "categories",
    "flag_status",
)

_EML_SIZE_FIELDS = ("id", "subject", "size")
_EML_MIME_FIELDS = ("id", "subject", "mime_content")
_IMPORTANCE_TO_EWS = {"high": "High", "normal": "Normal", "low": "Low"}
GET_MESSAGES_MAX_IDS = 20
_ALIAS_SHAPE_RE = re.compile(r"^[a-z]{1,2}[0-9]+$")
_ATT_ALIAS_RE = re.compile(r"^a[0-9]+$")
_MAX_STEM_UTF8_BYTES = 200
_WIN_RESERVED_STEMS = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})

# Slim FindItem-only projection for check_messages (avoids per-item GetItem
# for text_body / attachments when only four summary fields are returned).
_CHECK_PROJECTION = ("id", "subject", "sender", "datetime_received")

_TEXT_EXTS = (".txt", ".csv", ".md", ".log", ".json")
_TEXT_CAP = 20_000
_SMIME_SNIFF_MAX_BYTES = 5 * 1024 * 1024

TEXT_CACHE_MAX_DOCS = 4
TEXT_CACHE_MAX_CHARS = 8_000_000
TEXT_CACHE_TTL_SECONDS = 600
_MAX_SELECTOR_BINDINGS = 64
_UNSET_SELECTOR = object()


@dataclass
class _CachedAttachmentText:
    doc: ExtractionResult
    smime_type: Optional[str]
    char_len: int
    created_at: float
    raw_msg_id: Optional[str] = None
    att_idx: Optional[int] = None
    att_count: Optional[int] = None
    name: Optional[str] = None
    size_bytes: Optional[Any] = None
    content_type: Optional[str] = None


class AttachmentTextCache:
    """In-memory-only LRU+TTL cache of extracted document text (character-counted)."""

    def __init__(
        self,
        *,
        max_docs: int = TEXT_CACHE_MAX_DOCS,
        max_chars: int = TEXT_CACHE_MAX_CHARS,
        ttl_seconds: float = TEXT_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_docs = max_docs
        self.max_chars = max_chars
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: "OrderedDict[Tuple[Any, ...], _CachedAttachmentText]" = (
            OrderedDict()
        )
        self._selector_keys: "OrderedDict[Tuple[str, Optional[str]], Tuple[Any, ...]]" = (
            OrderedDict()
        )
        self._total_chars = 0

    def _drop_selectors_for(self, target_key: Tuple[Any, ...]) -> None:
        for sel_pair, mapped_key in list(self._selector_keys.items()):
            if mapped_key == target_key:
                del self._selector_keys[sel_pair]

    def _bind_selector_unlocked(
        self,
        raw_msg_id: str,
        selector: Optional[str],
        key: Tuple[Any, ...],
    ) -> None:
        sel_pair = (str(raw_msg_id), None if selector is None else str(selector))
        self._selector_keys.pop(sel_pair, None)
        while len(self._selector_keys) >= _MAX_SELECTOR_BINDINGS:
            self._selector_keys.popitem(last=False)
        self._selector_keys[sel_pair] = key

    def _prune_expired(self, now: float) -> None:
        for key, entry in list(self._entries.items()):
            if now - entry.created_at >= self.ttl_seconds:
                self._total_chars -= entry.char_len
                del self._entries[key]
                self._drop_selectors_for(key)

    @property
    def total_chars(self) -> int:
        with self._lock:
            self._prune_expired(self._clock())
            return self._total_chars

    def __len__(self) -> int:
        with self._lock:
            self._prune_expired(self._clock())
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._selector_keys.clear()
            self._total_chars = 0

    def get(self, key: Tuple[Any, ...]) -> Optional[_CachedAttachmentText]:
        with self._lock:
            self._prune_expired(self._clock())
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry

    def bind_selector(
        self,
        raw_msg_id: Optional[str],
        selector: Optional[str],
        key: Tuple[Any, ...],
    ) -> None:
        """Record the exact ``(message_id, selector) -> cache_key`` binding."""
        if raw_msg_id is None:
            return
        with self._lock:
            self._prune_expired(self._clock())
            if key in self._entries:
                self._bind_selector_unlocked(raw_msg_id, selector, key)

    def get_for_paging(
        self, raw_msg_id: str, selector: Optional[str]
    ) -> Optional[_CachedAttachmentText]:
        """Return a cached document for ``offset > 0`` via exact selector identity."""
        with self._lock:
            self._prune_expired(self._clock())
            sel_pair = (
                str(raw_msg_id),
                None if selector is None else str(selector),
            )
            key = self._selector_keys.get(sel_pair)
            if key is None:
                return None
            entry = self._entries.get(key)
            if entry is None:
                self._selector_keys.pop(sel_pair, None)
                return None
            if entry.name is None:
                return None
            self._entries.move_to_end(key)
            self._selector_keys.move_to_end(sel_pair)
            return entry

    def put(
        self,
        key: Tuple[Any, ...],
        doc: ExtractionResult,
        *,
        smime_type: Optional[str] = None,
        raw_msg_id: Optional[str] = None,
        selector: Any = _UNSET_SELECTOR,
        att_idx: Optional[int] = None,
        att_count: Optional[int] = None,
        name: Optional[str] = None,
        size_bytes: Optional[Any] = None,
        content_type: Optional[str] = None,
    ) -> None:
        char_len = len(doc.text)
        with self._lock:
            now = self._clock()
            self._prune_expired(now)
            old = self._entries.pop(key, None)
            if old is not None:
                self._total_chars -= old.char_len
            if self.max_docs <= 0 or char_len > self.max_chars:
                self._drop_selectors_for(key)
                return
            while self._entries and (
                len(self._entries) >= self.max_docs
                or self._total_chars + char_len > self.max_chars
            ):
                evicted_key, evicted = self._entries.popitem(last=False)
                self._total_chars -= evicted.char_len
                self._drop_selectors_for(evicted_key)
            self._entries[key] = _CachedAttachmentText(
                doc=doc,
                smime_type=smime_type,
                char_len=char_len,
                created_at=now,
                raw_msg_id=raw_msg_id,
                att_idx=att_idx,
                att_count=att_count,
                name=name,
                size_bytes=size_bytes,
                content_type=content_type,
            )
            self._total_chars += char_len
            if raw_msg_id is not None and selector is not _UNSET_SELECTOR:
                self._bind_selector_unlocked(raw_msg_id, selector, key)


# --------------------------------------------------------------------------
# Shared sync helpers (called inside gateway lambdas, on the EWS pool)
# --------------------------------------------------------------------------


def _project(
    qs: Any,
    order: Optional[str] = "-datetime_received",
    fields: Tuple[str, ...] = _PROJECTION,
) -> Any:
    """Best-effort .order_by()/.only() — some folder types reject them."""
    if order:
        try:
            qs = qs.order_by(order)
        except Exception:
            pass
    try:
        qs = qs.only(*fields)
    except Exception:
        pass
    return qs


def _fetch_one(account: Any, raw_id: str, only: Optional[List[str]] = None) -> Any:
    """Fetch a single item by raw EWS id via account.fetch.

    account.fetch yields per-item results; a missing/stale id arrives as an
    Exception INSTANCE in the stream — re-raise it so the dispatcher's error
    mapper classifies it (ErrorItemNotFound → not_found + re-search hint).
    """
    results = list(account.fetch(ids=[(raw_id, None)], only_fields=only))
    if not results:
        raise ToolError(
            "not_found", "Message not found — the id may be stale.",
            hint="Ids change when items move; re-run search_messages for a fresh id.",
        )
    first = results[0]
    if isinstance(first, Exception):
        raise first
    return first


def _fetch_many(
    account: Any, raw_ids: List[str], only: Optional[List[str]] = None
) -> List[Any]:
    """Bulk fetch items in input order via account.fetch with a projection."""
    return list(
        account.fetch(
            ids=[(raw_id, None) for raw_id in raw_ids], only_fields=only
        )
    )


async def _cards_off_loop(ctx: Context, items: List[Any], tz: str) -> List[Dict[str, Any]]:
    """Build msg cards on a worker thread: alias mints are SQLite write
    transactions and body cleaning is regex-heavy — neither belongs on the
    event loop. alias_many pre-mints the whole page in ONE transaction so
    the per-card alias_for calls hit the read-only fast path."""
    def build() -> List[Dict[str, Any]]:
        ctx.aliaser.alias_many([
            (str(it.id), "m", getattr(it, "changekey", None),
             getattr(it, "message_id", None))
            for it in items if getattr(it, "id", None)
        ])
        return [msg_card(it, ctx.aliaser, tz) for it in items]

    return await asyncio.to_thread(build)


def _from(item: Any) -> str:
    sender = getattr(item, "sender", None)
    name = getattr(sender, "name", None)
    email = getattr(sender, "email_address", None) or ""
    return f"{name} <{email}>" if name and name != email else email


def _attachment_id(att: Any) -> Optional[str]:
    raw = getattr(att, "attachment_id", None)
    if isinstance(raw, str) and raw:
        return raw
    nested = getattr(raw, "id", None) if raw is not None else None
    if isinstance(nested, str) and nested:
        return nested
    direct = getattr(att, "id", None)
    if isinstance(direct, str) and direct:
        return direct
    return None


def _pick_attachment_with_index(
    atts: List[Any], selector: Optional[str], aliaser: Any = None
) -> Tuple[int, Any]:
    names = [getattr(a, "name", "") or f"attachment-{i}" for i, a in enumerate(atts)]
    safe_names = [_safe_snippet(n) for n in names[:20]]
    listed = ", ".join(safe_names)
    if len(names) > 20:
        listed += f" (+{len(names) - 20} more)"
    if selector is None:
        if len(atts) == 1:
            return 0, atts[0]
        raise ToolError(
            "validation",
            cap_message(
                f"The message has {len(atts)} attachments — pick one: {listed}.",
                MAX_ERROR_MESSAGE_CHARS,
            ),
            hint=(
                "Pass attachment=<name>, attachment=<zero-based index as a string>, "
                "or attachment=<id>."
            ),
        )
    sel = str(selector).strip()
    for idx, (att, name) in enumerate(zip(atts, names)):
        if name.lower() == sel.lower():
            return idx, att
    if _ATT_ALIAS_RE.match(sel) and aliaser is not None and not isinstance(
        aliaser, NullAliaser
    ):
        try:
            resolved_id: Optional[str] = aliaser.resolve(sel)
        except KeyError:
            resolved_id = None
        if resolved_id is not None and resolved_id != sel:
            for idx, att in enumerate(atts):
                if _attachment_id(att) == resolved_id:
                    return idx, att
            raise ToolError(
                "not_found",
                cap_message(
                    f"Attachment id {_safe_snippet(sel)!r} belongs to another "
                    f"message's attachment. Available here: {listed}.",
                    MAX_ERROR_MESSAGE_CHARS,
                ),
            )
    if sel:
        for idx, att in enumerate(atts):
            if _attachment_id(att) == sel:
                return idx, att
    digits = sel[1:] if sel[:1] == "a" and sel[1:].isdigit() else sel
    if digits.isdigit() and int(digits) < len(atts):
        chosen_idx = int(digits)
        return chosen_idx, atts[chosen_idx]
    raise ToolError(
        "not_found",
        cap_message(
            f"No attachment matches {_safe_snippet(selector)!r}. Available: {listed}.",
            MAX_ERROR_MESSAGE_CHARS,
        ),
    )


def _pick_attachment(
    atts: List[Any], selector: Optional[str], aliaser: Any = None
) -> Any:
    return _pick_attachment_with_index(atts, selector, aliaser=aliaser)[1]


def _is_texty(name: str, content_type: Optional[str]) -> bool:
    return ((content_type or "").lower().startswith("text/")
            or (name or "").lower().endswith(_TEXT_EXTS))


def _is_item_attachment(att: Any) -> bool:
    """Detect an Exchange item attachment without evaluating lazy EWS properties."""
    if isinstance(att, ItemAttachment):
        return True
    if isinstance(att, FileAttachment):
        return False
    inst_dict = getattr(att, "__dict__", None) or {}
    cls = type(att)
    has_content = (
        "content" in inst_dict
        or "_content" in inst_dict
        or isinstance(getattr(cls, "content", None), property)
    )
    has_item = (
        "item" in inst_dict
        or "_item" in inst_dict
        or isinstance(getattr(cls, "item", None), property)
    )
    return has_item and not has_content


def _read_attachment_bytes(att: Any) -> Optional[bytes]:
    """Single site that touches ``att.content`` (triggers GetAttachment).

    Must only run inside a gateway worker function when bytes are needed.
    """
    raw = getattr(att, "content", None)
    return bytes(raw) if isinstance(raw, (bytes, bytearray)) else None


def _read_attachment_item(att: Any) -> Any:
    """Single site that touches ``att.item`` (triggers GetAttachment on items)."""
    return getattr(att, "item", None)


def _text_cache(ctx: Context) -> AttachmentTextCache:
    cache = getattr(ctx, "_attachment_text_cache", None)
    if not isinstance(cache, AttachmentTextCache):
        cache = AttachmentTextCache()
        setattr(ctx, "_attachment_text_cache", cache)
    return cache


def _attachment_cache_key(
    att: Any, raw_msg_id: str, att_idx: int, name: str, size: Any
) -> Tuple[Any, Optional[int]]:
    att_id = _attachment_id(att)
    size_val = int(size) if isinstance(size, int) and not isinstance(size, bool) else None
    if att_id:
        return (("id", str(att_id)), size_val)
    return (("fallback", str(raw_msg_id), int(att_idx), str(name)), size_val)


def _render_text_response(
    out: Dict[str, Any],
    doc: ExtractionResult,
    *,
    offset: int,
    smime_type: Optional[str] = None,
) -> Dict[str, Any]:
    full_text = doc.text
    total_chars = len(full_text)
    chunk = full_text[offset : offset + _TEXT_CAP]
    end = offset + len(chunk)
    out["mode"] = "text"
    out["extracted_from"] = doc.extracted_from
    if doc.pages is not None:
        out["pages"] = doc.pages
    if doc.pages_read is not None:
        out["pages_read"] = doc.pages_read
    if doc.slides is not None:
        out["slides"] = doc.slides
    if doc.slides_read is not None:
        out["slides_read"] = doc.slides_read
    if doc.sheets is not None:
        out["sheets"] = doc.sheets
    if doc.sheets_read is not None:
        out["sheets_read"] = doc.sheets_read
    if doc.partial:
        out["partial"] = True
    out["text"] = chunk
    out["offset"] = offset
    out["total_chars"] = total_chars
    if end < total_chars:
        out["next_offset"] = end
        out["truncated"] = True
    folded_notes = fold_and_prioritize_notes(doc.notes)
    if folded_notes:
        out["notes"] = list(folded_notes)
        out["note"] = cap_message("; ".join(folded_notes), MAX_NOTE_CHARS)
    if smime_type == "signed":
        out["hint"] = (
            "Signed, not encrypted (PKCS#7 SignedData) — this "
            "is the raw DER wrapper decoded as text, so the "
            "certificate/signature bytes around the readable "
            "message content appear as \ufffd replacement "
            "characters."
        )
    return out


# --------------------------------------------------------------------------
# 1. list_folders
# --------------------------------------------------------------------------


async def _list_folders(ctx: Context, parent: Optional[str] = None, depth: int = 2,
                        include_empty: bool = True,
                        query: Optional[str] = None,
                        fresh: bool = False) -> Dict[str, Any]:
    depth = max(1, min(int(depth), 5))
    needle = ""
    if query is not None:
        if not isinstance(query, str):
            raise ToolError("validation", "`query` must be a string.")
        needle = query.strip().lower()
        if not needle:
            raise ToolError(
                "validation", "`query` must not be empty or whitespace."
            )

    # ---- mirror (the hierarchy lane refreshes counts every ~10 min) --------
    if not fresh and parent is None and ctx.cache is not None:
        try:
            folder_rows = await asyncio.to_thread(ctx.cache.folder_rows)
        except Exception as exc:
            logger.warning("cache list_folders failed (%s) — live", exc)
            folder_rows = []
        if folder_rows:
            rows = []
            for r in folder_rows:
                r_name = r["name"] or ""
                r_path = r["path"] or ""
                if not needle and r_path.count("/") + 1 > depth:
                    continue
                if not include_empty and not r["total"]:
                    continue
                if (
                    needle
                    and needle not in r_name.lower()
                    and needle not in r_path.lower()
                ):
                    continue
                row: Dict[str, Any] = {
                    "id": ctx.aliaser.alias_for(r["ews_id"], "f"),
                    "name": r["name"], "path": r["path"],
                    "total": r["total"], "unread": r["unread"],
                    "children": r["children"],
                }
                if r["wk"]:
                    row["wk"] = r["wk"]
                if r["folder_class"]:
                    row["folder_class"] = r["folder_class"]
                rows.append(row)
            as_of = ctx.cache.watermark("events")  # slow-lane watermark
            return _stamp(envelope(rows, total_available=len(rows), offset=0),
                          "cache", as_of)

    def work(account: Any) -> List[Dict[str, Any]]:
        root = (ctx.gateway.resolve_folder(account, parent, ctx.aliaser)
                if parent else account.msg_folder_root)
        wk_by_raw_id: Dict[Any, str] = {}
        for wk_alias, attr in WELL_KNOWN.items():
            try:  # best effort — wk tagging must never break the walk
                fid = getattr(getattr(account, attr, None), "id", None)
                if fid:
                    wk_by_raw_id.setdefault(fid, wk_alias)
            except UnauthorizedError:
                raise  # never ask again after a rejection
            except Exception:
                continue
        rows: List[Dict[str, Any]] = []
        start_prefix = ""
        if needle and parent:
            parts = _folder_parts(root)
            if parts:
                start_prefix = (
                    "/".join(str(getattr(p, "name", "") or "") for p in parts[2:])
                    or str(getattr(root, "name", "") or "")
                )
            else:
                start_prefix = str(getattr(root, "name", "") or "")

        def walk(
            folder: Any, level: int, prefix: str, ancestors: frozenset
        ) -> None:
            if not needle and level > depth:
                return
            for child in list(getattr(folder, "children", None) or []):
                if id(child) in ancestors:
                    continue
                name = getattr(child, "name", "") or ""
                path = f"{prefix}/{name}" if prefix else name
                total = getattr(child, "total_count", None) or 0
                raw_id = getattr(child, "id", None)
                grandchildren = list(getattr(child, "children", None) or [])
                matched = (
                    not needle
                    or needle in name.lower()
                    or needle in path.lower()
                )
                if matched and (include_empty or total):
                    row: Dict[str, Any] = {
                        "id": (
                            ctx.aliaser.alias_for(str(raw_id), "f")
                            if raw_id
                            else None
                        ),
                        "name": name,
                        "path": path,
                        "total": total,
                        "unread": getattr(child, "unread_count", None) or 0,
                        "children": len(grandchildren),
                    }
                    wk = wk_by_raw_id.get(raw_id)
                    if wk:
                        row["wk"] = wk
                    # getattr — not every folder object carries a FolderClass
                    folder_class = getattr(child, "folder_class", None)
                    if folder_class:
                        row["folder_class"] = folder_class
                    rows.append(row)
                walk(child, level + 1, path, ancestors | {id(child)})

        walk(root, 1, start_prefix, frozenset({id(root)}))
        return rows

    rows = await ctx.gateway.call(work)
    return _stamp(envelope(rows, total_available=len(rows), offset=0), "live")


# --------------------------------------------------------------------------
# 2. search_messages
# --------------------------------------------------------------------------


async def _search_semantic(ctx: Context, query: str, folder_key: str,
                           limit: int) -> Dict[str, Any]:
    """Hybrid retrieval: FTS rank + vector rank fused with RRF. Vector-tier
    outages degrade to keyword-only with a `degraded` note — never an error."""
    from ..semantic import rrf_merge
    fts_rows, _total = await asyncio.to_thread(
        ctx.cache.search_messages, folders=[folder_key], text=query,
        offset=0, limit=max(20, limit))
    fts_ids = [r["ews_id"] for r in fts_rows]
    degraded: Optional[str] = None
    sem_ids: List[str] = []
    try:
        hits = await asyncio.to_thread(ctx.semantic.query, query, 20)
        sem_ids = [ews_id for ews_id, _score in hits]
    except Exception as exc:
        degraded = f"semantic tier unavailable ({type(exc).__name__}) — keyword-only"
    ranked = rrf_merge(fts_ids, sem_ids) if sem_ids else fts_ids
    rows_by_id = {r["ews_id"]: r for r in fts_rows}
    cards: List[Dict[str, Any]] = []
    for ews_id in ranked[:limit]:
        row = rows_by_id.get(ews_id)
        if row is None:
            row = await asyncio.to_thread(ctx.cache.get_message, ews_id)
        if row is not None:
            cards.append(_row_card(ctx, row))
    out = envelope(cards, total_available=None, offset=0, next_offset=None)
    if degraded:
        out["degraded"] = degraded
    return _stamp(out, "cache", _cache_watermark(ctx, folder_key))


ALL_FOLDERS = "f:all"
MAX_FANOUT_FOLDERS = 200
MAX_FANOUT_WINDOW = 500


def _sender_matches_substring(item: Any, needle: str) -> bool:
    sd = getattr(item, "sender", None)
    email = (getattr(sd, "email_address", "") or "").lower()
    name = (getattr(sd, "name", "") or "").lower()
    return needle in email or needle in name


def _build_queryset(
    target: Any,
    *,
    query: Optional[str] = None,
    subject: Optional[str] = None,
    categories: Optional[List[str]] = None,
    since_dt: Optional[datetime] = None,
    until_dt: Optional[datetime] = None,
    is_unread: Optional[bool] = None,
    has_attachments: Optional[bool] = None,
    flag_complete: Optional[bool] = None,
    importance: Optional[str] = None,
) -> Any:
    """Build the unprojected EWS QuerySet for one folder.

    Shared by the single-folder and multi-folder paths so every restriction
    and exclusion applies identically across scopes.
    """
    if query:
        return target.filter(query)
    filters: Dict[str, Any] = {}
    if subject:
        filters["subject__icontains"] = subject
    if categories:
        filters["categories__contains"] = categories
    if since_dt is not None:
        filters["datetime_received__gte"] = since_dt
    if until_dt is not None:
        filters["datetime_received__lte"] = until_dt
    if is_unread is not None:
        filters["is_read"] = not is_unread
    if has_attachments is not None:
        filters["has_attachments"] = bool(has_attachments)
    if flag_complete is True:
        filters["flag_status"] = 1
    if importance is not None:
        filters["importance"] = _IMPORTANCE_TO_EWS[importance]
    qs = target.filter(**filters)
    if flag_complete is False:
        # Incomplete covers both unflagged (None) and open-flagged (2);
        # excluding Complete (1) captures both.
        qs = qs.exclude(flag_status=1)
    return qs


def _folder_parts(folder: Any) -> List[Any]:
    raw_parts = getattr(folder, "parts", None)
    if raw_parts is None:
        return []
    try:
        return list(raw_parts)
    except TypeError:
        return []


def _folder_map_entries(gateway: Any, account: Any) -> List[Tuple[str, Any]]:
    """Build ``(path, folder)`` entries for every mail folder in the gateway's
    folder map — including trees omitted from ``f:all`` (trash, drafts, junk,
    outbox) so ``recursive=True`` rooted there still walks its subtree.
    """
    folder_map_fn = getattr(gateway, "_folder_map", None)
    if not callable(folder_map_fn):
        return []
    fmap = folder_map_fn(account)
    if not isinstance(fmap, dict) or not fmap:
        return []
    path_by_obj: Dict[int, str] = {}
    for key, folder in fmap.items():
        if key != getattr(folder, "id", None) and isinstance(key, str):
            path_by_obj.setdefault(id(folder), key)
    out: List[Tuple[str, Any]] = []
    seen: set = set()
    for folder in fmap.values():
        fid = getattr(folder, "id", None)
        dedup_key = ("id", fid) if fid is not None else ("obj", id(folder))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        fclass = getattr(folder, "folder_class", None)
        if isinstance(fclass, str) and fclass and not fclass.startswith("IPF.Note"):
            continue
        parts = _folder_parts(folder)
        if parts:
            path = (
                "/".join(str(getattr(p, "name", "") or "") for p in parts[2:])
                or str(getattr(folder, "name", "") or "")
            )
        else:
            path = path_by_obj.get(id(folder)) or str(getattr(folder, "name", "") or "")
        out.append((path, folder))
    return out


def _find_subtree_in_entries(
    target: Any,
    root_ref: str,
    entries: List[Tuple[str, Any]],
) -> Optional[List[Tuple[str, Any]]]:
    target_id = getattr(target, "id", None)
    root_path: Optional[str] = None
    for path, folder in entries:
        if folder is target or (
            target_id is not None and getattr(folder, "id", None) == target_id
        ):
            root_path = path
            break
    if root_path is None and target_id is None:
        ref_key = root_ref.strip().strip("/").lower()
        wk_key = WELL_KNOWN.get(ref_key) or WELL_KNOWN.get(f"f:{ref_key}") or ref_key
        candidates = [wk_key]
        target_name = getattr(target, "name", None)
        if isinstance(target_name, str) and target_name.strip():
            norm_name = target_name.strip().strip("/").lower()
            if norm_name not in candidates:
                candidates.append(norm_name)
        for cand in candidates:
            for path, _folder in entries:
                if path.rstrip("/").lower() == cand:
                    root_path = path
                    break
            if root_path is not None:
                break
    if root_path is None:
        return None
    prefix = root_path.rstrip("/").lower()
    selected: List[Tuple[str, Any]] = []
    for path, folder in entries:
        parts = _folder_parts(folder)
        if target_id is not None and parts and any(
            getattr(p, "id", None) is not None for p in parts
        ):
            if (folder is target
                    or getattr(folder, "id", None) == target_id
                    or any(getattr(p, "id", None) == target_id for p in parts)):
                selected.append((path, folder))
            continue
        norm = path.rstrip("/").lower()
        if norm == prefix or norm.startswith(prefix + "/"):
            selected.append((path, folder))
    return selected


def _select_subtree(
    target: Any,
    root_ref: str,
    all_folders: List[Tuple[str, Any]],
    fallback_folders: Optional[List[Tuple[str, Any]]] = None,
) -> List[Tuple[str, Any]]:
    """Return ``(path, folder)`` entries at or under ``target``."""
    found = _find_subtree_in_entries(target, root_ref, all_folders)
    if found is not None:
        return found
    if fallback_folders:
        found = _find_subtree_in_entries(target, root_ref, fallback_folders)
        if found is not None:
            return found
    fallback = getattr(target, "name", None) or root_ref.strip()
    return [(str(fallback), target)]


async def _search_all_folders(
    ctx: Context,
    build_qs: Callable[[Any], Any],
    offset: int,
    limit: int,
    root_folder: Optional[str] = None,
    sender_needle: Optional[str] = None,
) -> Dict[str, Any]:
    """One search per mail folder in scope, merged newest first.

    A single FindItem over several folders is no substitute: hits come back
    folder by folder, unsorted across them, and the offset is applied to each
    folder rather than the merged list. Each folder therefore delivers its
    newest offset+limit hits before merging.
    """
    def list_scope(account: Any) -> List[Tuple[str, Any]]:
        target = (
            ctx.gateway.resolve_folder(account, root_folder, ctx.aliaser)
            if root_folder is not None else None
        )
        all_folders = ctx.gateway.mail_folders(account)
        if target is None or root_folder is None:
            if not all_folders:
                raise ToolError("upstream_unavailable", "the folder list is not available.")
            return all_folders
        found = _find_subtree_in_entries(target, root_folder, all_folders)
        if found is not None:
            return found
        map_folders = _folder_map_entries(ctx.gateway, account)
        if not all_folders and not map_folders:
            raise ToolError("upstream_unavailable", "the folder list is not available.")
        return _select_subtree(
            target, root_folder, all_folders, fallback_folders=map_folders,
        )

    all_in_scope = await ctx.gateway.call(list_scope)
    folders = all_in_scope[:MAX_FANOUT_FOLDERS]
    skipped = len(all_in_scope) - len(folders)
    wanted = offset + limit

    async def one(target: Any) -> Tuple[List[Any], Optional[int]]:
        def work(_account: Any) -> Tuple[List[Any], Optional[int]]:
            return paginate(_project(build_qs(target)), offset=0, limit=wanted)
        return await ctx.gateway.call(work)

    results = await asyncio.gather(*(one(f) for _, f in folders),
                                   return_exceptions=True)
    for r in results:
        if isinstance(r, asyncio.CancelledError) or (
            isinstance(r, BaseException) and not isinstance(r, Exception)
        ):
            raise r
        if isinstance(r, Exception):
            mapped = map_exception(r)
            if (mapped.code == "throttled"
                    or "throttl" in f"{type(r).__name__} {r}".lower()):
                if mapped.code == "throttled":
                    raise r
                raise ToolError(
                    "throttled",
                    f"{type(r).__name__}: {r}",
                    hint="Exchange asked us to slow down. Retry after the delay.",
                    retry_after_s=60,
                )
    errors = [r for r in results if isinstance(r, Exception)]
    if len(errors) == len(folders):
        raise errors[0]
    hits: List[Tuple[str, Any]] = []
    failed: List[str] = []
    truncated = False
    for (path, _), result in zip(folders, results):
        if isinstance(result, Exception):
            logger.warning("search failed in a folder: %s", type(result).__name__)
            failed.append(path)
            continue
        page, more = result
        truncated = truncated or more is not None
        hits.extend((path, item) for item in page)
    hits.sort(key=lambda hit: getattr(hit[1], "datetime_received", None)
              or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    next_offset = wanted if truncated or len(hits) > wanted else None
    page_hits = hits[offset:wanted]
    if sender_needle:
        page_hits = [
            h for h in page_hits
            if _sender_matches_substring(h[1], sender_needle)
        ]
    return {
        "hits": page_hits,
        "total": (None if (truncated or failed or skipped or sender_needle)
                  else len(hits)),
        "next_offset": next_offset,
        "searched": len(folders) - len(failed),
        "failed": failed,
        "skipped": skipped,
    }


async def _search_messages(ctx: Context, query: Optional[str] = None,
                           folder: str = "f:inbox", sender: Optional[str] = None,
                           from_: Optional[str] = None,
                           subject: Optional[str] = None, since: Optional[str] = None,
                           until: Optional[str] = None, is_unread: Optional[bool] = None,
                           has_attachments: Optional[bool] = None,
                           categories: Optional[List[str]] = None,
                           importance: Optional[str] = None,
                           recursive: bool = False,
                           flag_complete: Optional[bool] = None,
                           offset: int = 0, limit: int = 20,
                           mode: str = "keyword",
                           fresh: bool = False) -> Dict[str, Any]:
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 50))
    if mode == "semantic":
        if not query:
            raise ToolError("validation", "mode='semantic' requires `query`.")
        if ctx.semantic is None:
            raise ToolError(
                "validation",
                "semantic search is not enabled on this server "
                "(EWS_SEMANTIC_INDEX=none)",
                hint="Use mode='keyword' — the FTS index handles Arabic and "
                     "English orthographic variants.",
            )
        folder_key = _cache_folder_key(ctx, folder)
        if not folder_key or not _cache_watermark(ctx, folder_key):
            raise ToolError("upstream_unavailable",
                            "semantic search needs the local mirror, which "
                            "has not finished its first sync yet.")
        return await _search_semantic(ctx, query, folder_key, limit)
    if sender and from_:
        raise ToolError("validation",
                        "pass `sender` only — `from_` is its deprecated alias.")
    sender = sender or from_
    if importance is not None and (
        not isinstance(importance, str) or importance not in _IMPORTANCE_TO_EWS
    ):
        raise ToolError(
            "validation",
            "importance must be 'high', 'normal', or 'low'.",
        )
    structured_given = any(
        v is not None for v in
        (sender, subject, since, until, is_unread, has_attachments, categories,
         flag_complete, importance)
    )
    if query and structured_given:
        raise ToolError(
            "validation",
            "`query` (AQS) cannot be combined with the structured filters "
            "(sender/subject/since/until/is_unread/has_attachments/categories/"
            "flag_complete/importance) — "
            "Exchange runs them on different engines.",
            hint="Either fold everything into the AQS string "
                 "(e.g. 'from:alice@example.com subject:rfp received>=2026-06-01') or drop "
                 "`query` and use only structured filters.",
        )
    tz = ctx.settings.ews_tz
    whole_mailbox = (folder or "").strip().lower() == ALL_FOLDERS
    fan_out = whole_mailbox or bool(recursive)
    if fan_out and offset + limit > MAX_FANOUT_WINDOW:
        raise ToolError(
            "validation",
            f"offset + limit ({offset + limit}) may not exceed "
            f"{MAX_FANOUT_WINDOW} on a multi-folder search.",
            hint="Narrow the search with `since`/`until` or `subject`, "
                 "or search one folder.",
        )
    since_dt = parse_when(since, "since", tz) if since else None
    until_dt = parse_when(until, "until", tz) if until else None

    # ---- cache-first: local FTS + SQL filters, exact COUNT(*) total -------
    # Multi-folder fan-out (`f:all`, `recursive`) and `flag_complete` (not
    # stored in the mirror schema) always run live.
    folder_key = _cache_folder_key(ctx, folder)
    if not fresh and not fan_out and flag_complete is None and folder_key:
        as_of = _cache_watermark(ctx, folder_key)
        if as_of:
            try:
                rows, total = await asyncio.to_thread(
                    ctx.cache.search_messages,
                    folders=[folder_key], text=query, sender=sender,
                    subject=subject,
                    since_ts=int(since_dt.timestamp()) if since_dt else None,
                    until_ts=int(until_dt.timestamp()) if until_dt else None,
                    is_unread=is_unread, has_attachments=has_attachments,
                    categories=categories, importance=importance,
                    offset=offset, limit=limit,
                )
                cards = await asyncio.to_thread(
                    lambda: [_row_card(ctx, r) for r in rows])
                return _stamp(envelope(cards, total_available=total,
                                       offset=offset), "cache", as_of)
            except ToolError:
                raise
            except Exception as exc:
                logger.warning("cache search failed (%s) — falling back live", exc)

    # ---- live path ---------------------------------------------------------
    unfiltered = (
        not query and not sender and not subject and not categories
        and since_dt is None and until_dt is None
        and is_unread is None and has_attachments is None
        and flag_complete is None and importance is None
    )

    def build_qs(target: Any) -> Any:
        return _build_queryset(
            target,
            query=query,
            subject=subject,
            categories=categories,
            since_dt=since_dt,
            until_dt=until_dt,
            is_unread=is_unread,
            has_attachments=has_attachments,
            flag_complete=flag_complete,
            importance=importance,
        )

    def work(account: Any) -> Tuple[List[Any], Optional[int], Optional[int]]:
        target = ctx.gateway.resolve_folder(account, folder, ctx.aliaser)
        total: Optional[int] = None
        if unfiltered:
            # Exact totals are only cheap for a plain listing: one refreshed
            # folder property instead of a count() full-folder scan.
            try:
                target.refresh()
                total = getattr(target, "total_count", None)
            except Exception:
                total = None
        qs = build_qs(target)
        if (total is None and not sender
                and (categories or flag_complete is True) and not query):
            # Matching sets for categories and completed flags are small by
            # construction; flag_complete=False spans most of the folder, so
            # its total stays None and next_offset indicates more pages.
            try:
                total = qs.count()
            except Exception:
                total = None
        qs = _project(qs)
        page, next_off = paginate(qs, offset=offset, limit=limit)
        return page, next_off, total

    sender_needle = sender.strip().lower() if sender else None
    found: Dict[str, Any] = {}
    if fan_out:
        root_ref = None if whole_mailbox else folder
        found = await _search_all_folders(
            ctx, build_qs, offset=offset, limit=limit,
            root_folder=root_ref, sender_needle=sender_needle,
        )
        paired = list(found["hits"])
        next_offset, total = found["next_offset"], found["total"]
    else:
        single_items, next_offset, total = await ctx.gateway.call(work)
        paired = [("", it) for it in single_items]
        if sender_needle:
            paired = [(path, it) for path, it in paired
                      if _sender_matches_substring(it, sender_needle)]
            total = None  # client-side post-filter: upstream total no longer applies
    items = [it for _, it in paired]
    cards = await _cards_off_loop(ctx, items, tz)
    if fan_out:
        for (path, _), card in zip(paired, cards):
            card["folder"] = path
    out = envelope(cards, total_available=total, offset=offset,
                   next_offset=next_offset)
    if fan_out:
        out["folders_searched"] = found["searched"]
        if found["failed"]:
            out["folders_failed"] = found["failed"]
        if found["skipped"]:
            out["folders_skipped"] = found["skipped"]
    return _stamp(out, "live")


# --------------------------------------------------------------------------
# 2b. check_messages: a whole list in one call
# --------------------------------------------------------------------------

CHECK_MAX_VALUES = 200
CHECK_MAX_HITS = 5


def _parse_values(raw: Any) -> List[str]:
    """String array, JSON array string, or values separated by commas,
    semicolons or new lines; empty entries and duplicates are dropped, order kept."""
    if isinstance(raw, list):
        if not all(isinstance(item, str) for item in raw):
            raise ToolError("validation", "`values` array items must be strings.")
        items = list(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        items = []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                items = [str(item) for item in parsed] if isinstance(parsed, list) else []
            except ValueError:
                items = []
        if not items:
            items = re.split(r"[,;\n]+", text)
    elif raw is None:
        items = []
    else:
        raise ToolError("validation", "`values` must be a string or an array of strings.")
    seen: set = set()
    out: List[str] = []
    for item in (i.strip() for i in items):
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def _word_match(term: str, text: str) -> bool:
    """Whole word: "RFP-12" does not match inside "RFP-123"."""
    return re.search(r"(?i)(^|[^\w])" + re.escape(term) + r"($|[^\w])", text or "") is not None


def _aqs_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _aqs_sender_query(
    term: str,
    since_dt: Optional[datetime],
    until_dt: Optional[datetime],
    tz: str,
) -> str:
    parts = [f'from:"{_aqs_escape(term)}"']
    if since_dt is not None:
        parts.append(
            f"received>={since_dt.astimezone(ZoneInfo(tz)).date().isoformat()}"
        )
    if until_dt is not None:
        parts.append(
            f"received<={until_dt.astimezone(ZoneInfo(tz)).date().isoformat()}"
        )
    return " ".join(parts)


def _split_from(from_field: str) -> Tuple[str, str]:
    text = (from_field or "").strip()
    if "<" in text and text.endswith(">"):
        name, _, addr = text.rpartition("<")
        return name.strip(), addr[:-1].strip()
    if "@" in text:
        return "", text
    return text, ""


def _sender_match(term: str, from_field: str) -> bool:
    """Confirm a sender card: whole-word on display name; exact/domain/word on address."""
    needle = (term or "").strip()
    if not needle:
        return False
    name, email = _split_from(from_field)
    if name and _word_match(needle, name):
        return True
    if not email:
        return False
    low_email = email.lower()
    low_needle = needle.lower()
    if low_needle.startswith("@"):
        return low_email.endswith(low_needle)
    if "@" in low_needle:
        return low_email == low_needle
    return _word_match(needle, email)


def _card_in_window(
    card: Dict[str, Any],
    since_dt: Optional[datetime],
    until_dt: Optional[datetime],
) -> bool:
    if since_dt is None and until_dt is None:
        return True
    raw = card.get("date")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if since_dt is not None and dt < since_dt:
        return False
    if until_dt is not None and dt > until_dt:
        return False
    return True


def _row_from(row: Any) -> str:
    name = row["sender_name"]
    email = row["sender_email"] or ""
    return f"{name} <{email}>" if name and name != email else email


async def _check_messages(ctx: Context, values: Any, field: str = "subject",
                          folder: str = "f:inbox", since: Optional[str] = None,
                          until: Optional[str] = None,
                          only_missing: bool = False,
                          fresh: bool = False,
                          recursive: bool = False) -> Dict[str, Any]:
    norm_folder = (folder or "f:inbox").strip() or "f:inbox"
    if (norm_folder.lower() == ALL_FOLDERS
            or bool(recursive)
            or norm_folder.endswith("/*")):
        raise ToolError(
            "validation",
            "`check_messages` requires a single folder (`f:all` and recursive "
            "scopes are not supported).",
            hint="Pass a single folder such as `f:inbox` or `f:sent`.",
        )
    terms = _parse_values(values)
    if not terms:
        raise ToolError("validation", "`values` is empty.")
    if len(terms) > CHECK_MAX_VALUES:
        raise ToolError("validation",
                        f"at most {CHECK_MAX_VALUES} values per call, got {len(terms)}.")
    if field not in ("subject", "sender"):
        raise ToolError("validation", "`field` must be 'subject' or 'sender'.")

    tz = ctx.settings.ews_tz if ctx is not None else "UTC"
    since_dt = parse_when(since, "since", tz) if since else None
    until_dt = parse_when(until, "until", tz) if until else None
    folder_key = _cache_folder_key(ctx, norm_folder) if ctx is not None else None
    mirror_as_of = (
        _cache_watermark(ctx, folder_key)
        if (ctx is not None and not fresh and folder_key)
        else None
    )
    since_ts = int(since_dt.timestamp()) if since_dt is not None else None
    until_ts = int(until_dt.timestamp()) if until_dt is not None else None

    gate = asyncio.Semaphore(4)
    aborted = False

    async def one(term: str) -> Tuple[List[Dict[str, Any]], str, Optional[str]]:
        async with gate:
            if aborted:
                return [], "live", None
            if mirror_as_of and ctx is not None and ctx.cache is not None:
                try:
                    rows, _total = await asyncio.to_thread(
                        ctx.cache.search_messages,
                        folders=[folder_key],
                        sender=term if field == "sender" else None,
                        subject=term if field == "subject" else None,
                        since_ts=since_ts,
                        until_ts=until_ts,
                        offset=0,
                        limit=50,
                    )
                    matched_rows: List[Any] = []
                    for r in rows:
                        if field == "subject":
                            if not _word_match(term, r["subject"] or ""):
                                continue
                        else:
                            if not _sender_match(term, _row_from(r)):
                                continue
                            if not _card_in_window({"date": r["date_iso"]},
                                                   since_dt, until_dt):
                                continue
                        matched_rows.append(r)
                        if len(matched_rows) >= CHECK_MAX_HITS:
                            break
                    if matched_rows:
                        def build_cached() -> List[Dict[str, Any]]:
                            minted = ctx.aliaser.alias_many([
                                (r["ews_id"], "m", r["changekey"],
                                 r["internet_message_id"])
                                for r in matched_rows if r["ews_id"]
                            ])
                            return [{
                                "id": (
                                    minted.get(r["ews_id"])
                                    or ctx.aliaser.alias_for(
                                        r["ews_id"], "m",
                                        changekey=r["changekey"],
                                        internet_message_id=r["internet_message_id"],
                                    )
                                ),
                                "subject": r["subject"] or "",
                                "from": _row_from(r),
                                "date": r["date_iso"],
                            } for r in matched_rows]

                        trimmed = await asyncio.to_thread(build_cached)
                    else:
                        trimmed = []
                    as_of_iso = datetime.fromtimestamp(
                        mirror_as_of, tz=timezone.utc,
                    ).isoformat(timespec="seconds")
                    return trimmed, "cache", as_of_iso
                except ToolError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "cache check_messages failed (%s) — falling back live", exc,
                    )

            def work(account: Any) -> List[Any]:
                target = ctx.gateway.resolve_folder(account, norm_folder, ctx.aliaser)
                if field == "subject":
                    qs = _build_queryset(
                        target, subject=term, since_dt=since_dt, until_dt=until_dt,
                    )
                else:
                    aqs = _aqs_sender_query(term, since_dt, until_dt, tz)
                    qs = _build_queryset(target, query=aqs)
                qs = _project(qs, fields=_CHECK_PROJECTION)
                return list(qs[0:50])

            items = await ctx.gateway.call(work)
            matched_live: List[Tuple[Any, str, str, Optional[str]]] = []
            for it in items:
                subj = getattr(it, "subject", "") or ""
                sender_str = _from(it)
                date_iso = fmt_dt(getattr(it, "datetime_received", None), tz)
                if field == "subject":
                    if not _word_match(term, subj):
                        continue
                else:
                    if not _sender_match(term, sender_str):
                        continue
                    if not _card_in_window({"date": date_iso}, since_dt, until_dt):
                        continue
                matched_live.append((it, subj, sender_str, date_iso))
                if len(matched_live) >= CHECK_MAX_HITS:
                    break
            if matched_live:
                def build_live() -> List[Dict[str, Any]]:
                    minted = ctx.aliaser.alias_many([
                        (str(it.id), "m", getattr(it, "changekey", None), None)
                        for it, _s, _f, _d in matched_live if getattr(it, "id", None)
                    ])
                    out_cards: List[Dict[str, Any]] = []
                    for it, subj, sender_str, date_iso in matched_live:
                        raw_id = getattr(it, "id", None)
                        key = str(raw_id) if raw_id else ""
                        out_cards.append({
                            "id": (
                                (minted.get(key) or ctx.aliaser.alias_for(
                                    key, "m", changekey=getattr(it, "changekey", None),
                                )) if key else None
                            ),
                            "subject": subj,
                            "from": sender_str,
                            "date": date_iso,
                        })
                    return out_cards

                trimmed = await asyncio.to_thread(build_live)
            else:
                trimmed = []
            return trimmed, "live", None

    tasks = [asyncio.create_task(one(term)) for term in terms]
    first_exc: Optional[BaseException] = None
    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_EXCEPTION,
            )
            for t in tasks:
                if t in done and not t.cancelled():
                    exc = t.exception()
                    if exc is not None and first_exc is None:
                        first_exc = exc
            if first_exc is not None:
                aborted = True
                for p in pending:
                    p.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                break
    except BaseException:
        aborted = True
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    if first_exc is not None:
        raise first_exc
    outcomes = [t.result() for t in tasks]
    found = {term: hits for term, (hits, _src, _ao) in zip(terms, outcomes) if hits}
    missing = [term for term in terms if term not in found]
    out: Dict[str, Any] = {"values_checked": len(terms), "found_count": len(found),
                           "missing_count": len(missing), "missing": missing}
    if not only_missing:
        out["found"] = found
    if all(src == "cache" for _hits, src, _ao in outcomes):
        out["source"] = "cache"
        as_of_iso = next((ao for _hits, _src, ao in outcomes if ao), None)
        if as_of_iso:
            out["as_of"] = as_of_iso
        elif mirror_as_of:
            _stamp(out, "cache", mirror_as_of)
    else:
        _stamp(out, "live")
    return out


# --------------------------------------------------------------------------
# 3. get_message / 3b. get_messages
# --------------------------------------------------------------------------


def _sniff_item_smime(fetched: Any) -> Dict[int, str]:
    """Sniff PKCS#7 candidates <= 5 MiB inside the gateway worker without
    downloading ordinary or oversized attachments."""
    smime_by_idx: Dict[int, str] = {}
    for idx, att in enumerate(getattr(fetched, "attachments", None) or []):
        name = getattr(att, "name", f"attachment-{idx}")
        content_type = getattr(att, "content_type", None)
        if not is_pkcs7_candidate(name, content_type):
            continue
        size = getattr(att, "size", None)
        if isinstance(size, (int, float)) and size > _SMIME_SNIFF_MAX_BYTES:
            continue
        try:
            raw = _read_attachment_bytes(att)
        except Exception:
            raw = None
        kind = sniff_smime_type(name, content_type, raw)
        if kind:
            smime_by_idx[idx] = kind
    return smime_by_idx


def _smime_candidates_by_metadata(fetched: Any) -> Dict[int, str]:
    """Mark PKCS#7 S/MIME attachments as candidates from name and MIME type
    only, without reading ``att.content`` (zero attachment byte downloads)."""
    smime_by_idx: Dict[int, str] = {}
    for idx, att in enumerate(getattr(fetched, "attachments", None) or []):
        if _is_item_attachment(att):
            continue
        name = getattr(att, "name", f"attachment-{idx}")
        content_type = getattr(att, "content_type", None)
        if is_pkcs7_candidate(name, content_type):
            smime_by_idx[idx] = "candidate"
    return smime_by_idx


def _truncate_utf8(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _eml_ceiling_error(ctx: Context, byte_count: int) -> ToolError:
    return ToolError(
        "validation",
        f"Message ({byte_count} bytes) exceeds the "
        f"{ctx.settings.ews_max_attachment_mb} MB ceiling "
        "(EWS_MAX_ATTACHMENT_MB).",
        hint="Ask the operator to raise EWS_MAX_ATTACHMENT_MB.",
    )


def _build_eml_filename(alias: str, subject: Optional[str]) -> str:
    safe_alias = (
        re.sub(r"[^\w\-]+", "_", alias or "").strip("_-")[:32] or "message"
    )
    subj = str(subject or "").strip()
    while subj.lower().endswith(".eml"):
        subj = subj[:-4].rstrip(" ._-")
    stem = re.sub(r"[^\w\-]+", "_", subj).strip("_-")
    combined = f"{safe_alias}-{stem}" if stem else safe_alias
    combined = _truncate_utf8(combined, _MAX_STEM_UTF8_BYTES).rstrip("_-")
    if not combined:
        combined = safe_alias
    return f"{combined}.eml"


def _is_fatal_batch_exception(exc: Exception) -> bool:
    if isinstance(exc, UnauthorizedError) or (
        isinstance(exc, ToolError) and exc.code == "auth_failed"
    ):
        return True
    mapped = map_exception(exc)
    return mapped.code in ("auth_failed", "throttled", "upstream_unavailable")


def _failed_message_entry(
    caller_id: str, idx: int, raw_id: str, exc: Exception
) -> Dict[str, Any]:
    if isinstance(exc, (TypeError, ValueError)):
        code = "validation"
        msg = f"{type(exc).__name__}: {exc}"
    else:
        mapped = map_exception(exc)
        code = mapped.code
        msg = mapped.message
    if raw_id and raw_id != caller_id and raw_id in msg:
        msg = msg.replace(raw_id, caller_id.strip())
    return {
        "id": caller_id,
        "index": idx,
        "code": code,
        "error": cap_message(msg, MAX_ERROR_MESSAGE_CHARS),
    }


async def _get_message(ctx: Context, id: str, format: Optional[str] = "full",
                       include_html: bool = False,
                       fresh: bool = False) -> Dict[str, Any]:
    if format is None:
        format = "full"
    if not isinstance(format, str) or format not in ("full", "concise", "eml"):
        raise ToolError(
            "validation",
            cap_message(
                f"Invalid format {_safe_snippet(str(format))!r}; expected "
                "one of full, concise, eml.",
                MAX_ERROR_MESSAGE_CHARS,
            ),
        )
    raw_id = id
    tz = ctx.settings.ews_tz
    if format == "eml":
        max_bytes = int(ctx.settings.ews_max_attachment_mb) * 1024 * 1024

        def eml_work(account: Any) -> Tuple[str, Optional[str], bytes]:
            meta = _fetch_one(account, raw_id, only=list(_EML_SIZE_FIELDS))
            declared_size = getattr(meta, "size", None)
            if (
                isinstance(declared_size, (int, float))
                and not isinstance(declared_size, bool)
                and declared_size > max_bytes
            ):
                raise _eml_ceiling_error(ctx, int(declared_size))
            fetched = _fetch_one(account, raw_id, only=list(_EML_MIME_FIELDS))
            raw_mime = getattr(fetched, "mime_content", None)
            if isinstance(raw_mime, (bytes, bytearray)):
                data = bytes(raw_mime)
            elif isinstance(raw_mime, str):
                data = raw_mime.encode("utf-8")
            else:
                data = b""
            if not data:
                raise ToolError(
                    "validation",
                    "Message has no retrievable MIME content.",
                )
            if len(data) > max_bytes:
                raise _eml_ceiling_error(ctx, len(data))
            resolved_raw = str(
                getattr(fetched, "id", None)
                or getattr(meta, "id", None)
                or raw_id
            )
            subj = (
                getattr(fetched, "subject", None)
                or getattr(meta, "subject", None)
            )
            return resolved_raw, subj, data

        resolved_raw, subj, mime_bytes = await ctx.gateway.call(eml_work)

        def save_eml() -> str:
            alias = ctx.aliaser.alias_for(resolved_raw, "m")
            eml_name = _build_eml_filename(alias, subj)
            return _save_attachment_file(ctx, eml_name, mime_bytes)

        saved_path = await asyncio.to_thread(save_eml)
        return _stamp(
            {
                "ok": True,
                "saved_path": saved_path,
                "size_bytes": len(mime_bytes),
            },
            "live",
        )

    # Mirror unless the caller needs live data: fresh=true, raw HTML, or an
    # attachment inventory (the mirror stores the flag, not the parts).
    if not fresh and not include_html and ctx.cache is not None:
        try:
            row = await asyncio.to_thread(ctx.cache.get_message, raw_id)
        except Exception as exc:
            logger.warning("cache get_message failed (%s) — live", exc)
            row = None
        if row is not None:
            as_of = _cache_watermark(ctx, row["folder"])
            message = (_row_card(ctx, row) if format == "concise"
                       else _row_full(ctx, row))
            return _stamp({"ok": True, "message": message}, "cache", as_of)
    if format == "concise":
        # Card projection: never pull the full item (incl. MIME) for a card.
        item = await ctx.gateway.call(
            lambda account: _fetch_one(account, raw_id, only=list(_PROJECTION)))
        return _stamp({"ok": True, "message": msg_card(item, ctx.aliaser, tz)},
                      "live")

    def work(account: Any) -> Tuple[Any, Dict[int, str]]:
        fetched = _fetch_one(account, raw_id)
        return fetched, _sniff_item_smime(fetched)

    item, smime_by_idx = await ctx.gateway.call(work)
    return _stamp({"ok": True, "message": msg_full(
        item, ctx.aliaser, tz, ctx.settings.body_max_chars,
        include_html=bool(include_html),
        smime_types=smime_by_idx,
    )}, "live")


async def _get_messages(
    ctx: Context,
    ids: List[str],
    format: Optional[str] = "full",
    fresh: bool = False,
) -> Dict[str, Any]:
    if not isinstance(ids, list) or not ids:
        raise ToolError(
            "validation", "`ids` must contain at least one message id."
        )
    if len(ids) > GET_MESSAGES_MAX_IDS:
        raise ToolError(
            "validation",
            f"at most {GET_MESSAGES_MAX_IDS} ids per call (got {len(ids)}).",
        )
    if not all(isinstance(i, str) and i.strip() for i in ids):
        raise ToolError(
            "validation", "`ids` entries must be non-empty strings."
        )
    if format is None:
        format = "full"
    if not isinstance(format, str) or format not in ("concise", "full"):
        raise ToolError(
            "validation",
            cap_message(
                f"Invalid format {_safe_snippet(str(format))!r}; expected "
                "'concise' or 'full'.",
                MAX_ERROR_MESSAGE_CHARS,
            ),
        )

    tz = ctx.settings.ews_tz

    def resolve_inputs() -> Tuple[
        List[Tuple[int, str, str]], List[Dict[str, Any]]
    ]:
        resolved: List[Tuple[int, str, str]] = []
        bad: List[Dict[str, Any]] = []
        for idx, caller_id in enumerate(ids):
            trimmed = caller_id.strip()
            if _ALIAS_SHAPE_RE.match(trimmed):
                prefix = trimmed.rstrip("0123456789")
                if prefix != "m":
                    bad.append({
                        "id": caller_id,
                        "index": idx,
                        "code": "validation",
                        "error": cap_message(
                            f"{trimmed!r} is not a message id "
                            "(expected an m-alias or raw EWS id).",
                            MAX_ERROR_MESSAGE_CHARS,
                        ),
                    })
                    continue
            try:
                raw_id = ctx.aliaser.resolve(trimmed)
            except KeyError as exc:
                msg = str(exc.args[0] if exc.args else exc)
                bad.append({
                    "id": caller_id,
                    "index": idx,
                    "code": "not_found",
                    "error": cap_message(msg, MAX_ERROR_MESSAGE_CHARS),
                })
                continue
            resolved.append((idx, caller_id, raw_id))
        return resolved, bad

    resolved_inputs, failed_entries = await asyncio.to_thread(resolve_inputs)
    if not resolved_inputs:
        failed_entries.sort(key=lambda entry: entry["index"])
        out = envelope([], total_available=0, offset=0)
        out["failed"] = failed_entries
        return _stamp(out, "live")

    by_index: Dict[int, Tuple[Dict[str, Any], str, Optional[int]]] = {}
    live_needed: List[Tuple[int, str, str]] = []

    if not fresh and ctx.cache is not None:
        try:
            def read_cached() -> Tuple[
                Dict[int, Tuple[Dict[str, Any], str, Optional[int]]],
                List[Tuple[int, str, str]],
            ]:
                cached_out: Dict[
                    int, Tuple[Dict[str, Any], str, Optional[int]]
                ] = {}
                misses: List[Tuple[int, str, str]] = []
                found_rows: List[Tuple[int, Any, Optional[int]]] = []
                for idx, caller_id, raw_id in resolved_inputs:
                    row = ctx.cache.get_message(raw_id)
                    if row is None:
                        misses.append((idx, caller_id, raw_id))
                    else:
                        wm = _cache_watermark(ctx, row["folder"])
                        found_rows.append((idx, row, wm))
                if found_rows:
                    ctx.aliaser.alias_many([
                        (
                            r["ews_id"],
                            "m",
                            r["changekey"],
                            r["internet_message_id"],
                        )
                        for _idx, r, _wm in found_rows
                        if r["ews_id"]
                    ])
                    for idx, row, wm in found_rows:
                        dto = (
                            _row_card(ctx, row)
                            if format == "concise"
                            else _row_full(ctx, row)
                        )
                        cached_out[idx] = (dto, "cache", wm)
                return cached_out, misses

            by_index, live_needed = await asyncio.to_thread(read_cached)
        except Exception as exc:
            logger.warning("cache get_messages failed (%s) — live", exc)
            by_index = {}
            live_needed = list(resolved_inputs)
    else:
        live_needed = list(resolved_inputs)

    if live_needed:
        only_fields = (
            list(_PROJECTION)
            if format == "concise"
            else list(_FULL_PROJECTION)
        )

        def _missing_err() -> ToolError:
            return ToolError(
                "not_found",
                "Message not found — the id may be stale.",
                hint=(
                    "Ids change when items move; re-run search_messages "
                    "for a fresh id."
                ),
            )

        def live_work(
            account: Any,
        ) -> List[Tuple[int, str, str, Any, Dict[int, str]]]:
            unique_raw: List[str] = []
            seen_raw: set = set()
            for _idx, _caller_id, raw_id in live_needed:
                if raw_id not in seen_raw:
                    seen_raw.add(raw_id)
                    unique_raw.append(raw_id)

            raw_results: Dict[str, Any] = {}
            try:
                fetched = _fetch_many(account, unique_raw, only=only_fields)
                for pos, raw_id in enumerate(unique_raw):
                    if pos >= len(fetched) or fetched[pos] is None:
                        raw_results[raw_id] = _missing_err()
                    else:
                        raw_results[raw_id] = fetched[pos]
            except Exception as batch_exc:
                if _is_fatal_batch_exception(batch_exc):
                    raise
                if len(unique_raw) == 1:
                    raw_results[unique_raw[0]] = batch_exc
                else:
                    for raw_id in unique_raw:
                        try:
                            single = _fetch_many(
                                account, [raw_id], only=only_fields
                            )
                            if not single or single[0] is None:
                                raw_results[raw_id] = _missing_err()
                            else:
                                raw_results[raw_id] = single[0]
                        except Exception as single_exc:
                            if _is_fatal_batch_exception(single_exc):
                                raise
                            raw_results[raw_id] = single_exc

            for item in raw_results.values():
                if isinstance(item, Exception) and _is_fatal_batch_exception(
                    item
                ):
                    raise item

            smime_by_raw: Dict[str, Dict[int, str]] = {}
            if format == "full":
                for raw_id, item in raw_results.items():
                    if not isinstance(item, Exception):
                        smime_by_raw[raw_id] = _smime_candidates_by_metadata(
                            item
                        )

            return [
                (
                    orig_idx,
                    caller_id,
                    raw_id,
                    raw_results[raw_id],
                    smime_by_raw.get(raw_id, {}),
                )
                for orig_idx, caller_id, raw_id in live_needed
            ]

        live_outcomes = await ctx.gateway.call(live_work)

        def build_live_dtos() -> Tuple[
            Dict[int, Tuple[Dict[str, Any], str, Optional[int]]],
            List[Dict[str, Any]],
        ]:
            good_items = [
                item
                for _idx, _cid, _raw, item, _sm in live_outcomes
                if not isinstance(item, Exception)
            ]
            if good_items:
                ctx.aliaser.alias_many([
                    (
                        str(it.id),
                        "m",
                        getattr(it, "changekey", None),
                        getattr(it, "message_id", None),
                    )
                    for it in good_items
                    if getattr(it, "id", None)
                ])
            built: Dict[int, Tuple[Dict[str, Any], str, Optional[int]]] = {}
            live_failed: List[Dict[str, Any]] = []
            for orig_idx, caller_id, raw_id, item, smime_by_idx in live_outcomes:
                if isinstance(item, Exception):
                    live_failed.append(
                        _failed_message_entry(
                            caller_id, orig_idx, raw_id, item
                        )
                    )
                elif format == "concise":
                    built[orig_idx] = (
                        msg_card(item, ctx.aliaser, tz),
                        "live",
                        None,
                    )
                else:
                    built[orig_idx] = (
                        msg_full(
                            item,
                            ctx.aliaser,
                            tz,
                            ctx.settings.body_max_chars,
                            include_html=False,
                            smime_types=smime_by_idx,
                        ),
                        "live",
                        None,
                    )
            return built, live_failed

        live_built, live_failed = await asyncio.to_thread(build_live_dtos)
        by_index.update(live_built)
        failed_entries.extend(live_failed)

    failed_entries.sort(key=lambda entry: entry["index"])
    ordered = [by_index[idx] for idx in range(len(ids)) if idx in by_index]
    has_cache = any(src == "cache" for _dto, src, _wm in ordered)
    has_live = any(src == "live" for _dto, src, _wm in ordered)

    if has_cache and has_live:
        items = [_stamp(dict(dto), src, wm) for dto, src, wm in ordered]
        out = envelope(items, total_available=len(items), offset=0)
        out["failed"] = failed_entries
        out["source"] = "mixed"
        return out

    items = [dict(dto) for dto, _src, _wm in ordered]
    out = envelope(items, total_available=len(items), offset=0)
    out["failed"] = failed_entries
    if has_cache:
        watermarks = [wm for _dto, src, wm in ordered if src == "cache" and wm]
        as_of = min(watermarks) if watermarks else None
        return _stamp(out, "cache", as_of)
    return _stamp(out, "live")


# --------------------------------------------------------------------------
# 4. get_thread
# --------------------------------------------------------------------------


def _thread_from_cache(ctx: Context, raw_id: str, limit: int,
                       offset: int) -> Optional[Dict[str, Any]]:
    """Local conversation_id join — sync helper, runs on a worker thread."""
    seed = ctx.cache.get_message(raw_id)
    if seed is None or not seed["conversation_id"]:
        return None
    rows = ctx.cache.thread(seed["conversation_id"])
    if not rows:
        return None
    total = len(rows)
    hi = max(0, total - offset)
    lo = max(0, hi - limit)
    window = rows[lo:hi]
    next_offset = offset + len(window) if lo > 0 else None
    counts: Dict[str, int] = {}
    for r in rows:
        who = r["sender_email"] or "unknown"
        counts[who] = counts.get(who, 0) + 1
    entries: List[Dict[str, Any]] = []
    for r in window:
        entry: Dict[str, Any] = {
            "id": ctx.aliaser.alias_for(r["ews_id"], "m",
                                        internet_message_id=r["internet_message_id"]),
            "from": r["sender_email"] or "unknown",
            "date": r["date_iso"],
            "body": _row_body(ctx, r)[:1500],
        }
        if r["has_attachments"]:
            entry["attach"] = True
        entries.append(entry)
    participants = [
        {"name_or_email": who, "msgs": n}
        for who, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {
        "ok": True,
        "thread_id": ctx.aliaser.alias_for(seed["conversation_id"], "t"),
        "subject": seed["subject"] or "",
        "participants": participants,
        "items": entries,
        "count": len(entries),
        "total_available": total,
        "next_offset": next_offset,
    }


async def _get_thread(ctx: Context, id: str, limit: int = 20,
                      offset: int = 0, fresh: bool = False) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 50))
    offset = max(0, int(offset))
    raw_id = id
    tz = ctx.settings.ews_tz

    if not fresh and ctx.cache is not None:
        try:
            cached = await asyncio.to_thread(
                _thread_from_cache, ctx, raw_id, limit, offset)
        except Exception as exc:
            logger.warning("cache get_thread failed (%s) — live", exc)
            cached = None
        if cached is not None:
            as_of = min(filter(None, (
                _cache_watermark(ctx, k) for k in ("inbox", "sent"))),
                default=None)
            return _stamp(cached, "cache", as_of)

    def work(account: Any) -> Tuple[Any, str, List[Any]]:
        seed = _fetch_one(account, raw_id)
        conv_obj = getattr(seed, "conversation_id", None)
        conv_id = getattr(conv_obj, "id", None)
        if not conv_id:
            raise ToolError(
                "not_found",
                "That message carries no conversation id; cannot rebuild a thread.",
                hint="Use get_message on it instead.",
            )
        # 5.0.3: filter needs the ConversationId OBJECT — a plain string
        # raises TypeError inside Q(). The seed item's own conversation_id
        # is exactly that object; the string form is only used for the
        # thread alias. (Behavior pinned in test_exchangelib_signatures.)
        conv_str = str(conv_id)
        merged: Dict[str, Any] = {}
        for source in (account.inbox, account.sent):
            qs = _project(source.filter(conversation_id=conv_obj), order=None)
            for it in qs:
                key = str(getattr(it, "id", "") or "")
                if key:
                    merged.setdefault(key, it)
        if not merged:  # seed lives elsewhere (archive…) — show at least it
            merged[str(getattr(seed, "id", "") or raw_id)] = seed
        return seed, conv_str, list(merged.values())

    seed, conv_str, found = await ctx.gateway.call(work)

    def sort_key(it: Any) -> datetime:
        dt = getattr(it, "datetime_received", None)
        return dt if dt is not None else datetime.min.replace(tzinfo=timezone.utc)

    found.sort(key=sort_key)
    total = len(found)
    # Paging walks BACKWARDS through history: offset=0 is the most recent
    # `limit` entries; pass the returned next_offset to fetch older ones.
    hi = max(0, total - offset)
    lo = max(0, hi - limit)
    window = found[lo:hi]
    next_offset = offset + len(window) if lo > 0 else None

    def build() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        counts: Dict[str, int] = {}
        for it in found:  # participants count the WHOLE thread, not the page
            counts[_from(it) or "unknown"] = counts.get(_from(it) or "unknown", 0) + 1
        ctx.aliaser.alias_many([
            (str(it.id), "m", getattr(it, "changekey", None),
             getattr(it, "message_id", None))
            for it in window if getattr(it, "id", None)
        ])
        entries: List[Dict[str, Any]] = []
        for it in window:
            raw = str(getattr(it, "id", "") or "")
            entry: Dict[str, Any] = {
                "id": ctx.aliaser.alias_for(
                    raw, "m", internet_message_id=getattr(it, "message_id", None),
                ) if raw else None,
                "from": _from(it) or "unknown",
                "date": fmt_dt(getattr(it, "datetime_received", None), tz),
                "body": clean_body(getattr(it, "text_body", None) or "",
                                   max_chars=1500)["text"],
            }
            if getattr(it, "has_attachments", False):
                entry["attach"] = True
            entries.append(entry)
        participants = [
            {"name_or_email": who, "msgs": n}
            for who, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return entries, participants

    entries, participants = await asyncio.to_thread(build)
    return _stamp({
        "ok": True,
        "thread_id": ctx.aliaser.alias_for(conv_str, "t"),
        "subject": getattr(seed, "subject", "") or "",
        "participants": participants,
        "items": entries,
        "count": len(entries),
        "total_available": total,
        "next_offset": next_offset,
    }, "live")


# --------------------------------------------------------------------------
# 5. get_attachment
# --------------------------------------------------------------------------


def _is_pkcs7_signature(name: Optional[str], content_type: Optional[str]) -> bool:
    ct = (content_type or "").lower()
    nm = (name or "").strip().lower()
    return "pkcs7-signature" in ct or nm.endswith(".p7s")


def _save_attachment_file(ctx: Context, name: str, data: bytes) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name or "")
    cleaned = re.sub(r"\.{2,}", "_", cleaned).strip("._-")
    if not cleaned:
        cleaned = "attachment.bin"
    dot = cleaned.rfind(".")
    if dot > 0:
        raw_stem, raw_ext = cleaned[:dot], cleaned[dot:]
        if len(raw_ext) <= 1 or len(raw_ext.encode("utf-8")) > 24:
            raw_stem, raw_ext = cleaned, ""
    else:
        raw_stem, raw_ext = cleaned, ""
    stem = raw_stem.strip("._-") or "attachment"
    ext = raw_ext
    if stem.upper() in _WIN_RESERVED_STEMS:
        stem = f"_{stem}"
    stem = (
        _truncate_utf8(stem, _MAX_STEM_UTF8_BYTES).rstrip("._-")
        or "attachment"
    )
    dest = Path(ctx.settings.data_dir) / "attachments"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for counter in range(1, 10_001):
            if counter == 1:
                suffix = ""
                cand_stem = stem
            else:
                suffix = f"-{counter}"
                cand_stem = (
                    _truncate_utf8(
                        stem, _MAX_STEM_UTF8_BYTES - len(suffix)
                    ).rstrip("._-")
                    or "attachment"
                )
            candidate = dest / f"{cand_stem}{suffix}{ext}"
            try:
                with candidate.open("xb") as fh:
                    fh.write(data)
                return str(candidate)
            except FileExistsError:
                if not dest.is_dir():
                    raise
                continue
            except PermissionError:
                if dest.is_dir() and candidate.exists():
                    continue
                raise
        raise OSError("attachment filename collision limit reached")
    except OSError:
        raise ToolError(
            "internal",
            "The server attachment folder cannot be written.",
            hint=(
                "Ask the operator to check DATA_DIR/attachments permissions "
                "and available disk space."
            ),
        ) from None


async def _get_attachment(ctx: Context, message_id: str,
                          attachment: Optional[str] = None,
                          mode: str = "auto",
                          offset: int = 0) -> Dict[str, Any]:
    if mode not in ("auto", "info", "text", "save"):
        raise ToolError(
            "validation",
            cap_message(
                f"Invalid mode {_safe_snippet(mode)!r}; expected one of "
                "auto, info, text, save.",
                MAX_ERROR_MESSAGE_CHARS,
            ),
        )
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ToolError("validation", "`offset` must be a non-negative integer.")
    raw_id = message_id
    max_bytes = int(ctx.settings.ews_max_attachment_mb) * 1024 * 1024
    cache = _text_cache(ctx)

    # Reading subsequent parts of an already-cached document starts no worker
    # process and makes zero EWS calls.
    if offset > 0 and mode in ("auto", "text"):
        paged_hit = cache.get_for_paging(raw_id, attachment)
        if paged_hit is not None:
            paged_out: Dict[str, Any] = {
                "ok": True,
                "name": paged_hit.name,
                "size_bytes": paged_hit.size_bytes,
                "content_type": paged_hit.content_type,
            }
            return _render_text_response(
                paged_out,
                paged_hit.doc,
                offset=offset,
                smime_type=paged_hit.smime_type,
            )

    def _ceiling_error(name: str, byte_count: int) -> ToolError:
        return ToolError(
            "validation",
            cap_message(
                f"Attachment {_safe_snippet(name)!r} ({byte_count} bytes) exceeds "
                f"the {ctx.settings.ews_max_attachment_mb} MB ceiling "
                "(EWS_MAX_ATTACHMENT_MB).",
                MAX_ERROR_MESSAGE_CHARS,
            ),
            hint="Use mode='info' for metadata, or ask the operator to "
                 "raise EWS_MAX_ATTACHMENT_MB.",
        )

    def _check_declared_ceiling(name: str, size: Any) -> None:
        if (
            isinstance(size, (int, float))
            and not isinstance(size, bool)
            and size > max_bytes
        ):
            raise _ceiling_error(name, int(size))

    def fetch_work(account: Any) -> Tuple[str, ...]:
        item = _fetch_one(account, raw_id, only=["attachments"])
        atts = list(getattr(item, "attachments", None) or [])
        if not atts:
            raise ToolError("not_found", "This message has no attachments.")
        att_idx, att = _pick_attachment_with_index(
            atts, attachment, aliaser=ctx.aliaser
        )
        name = getattr(att, "name", "") or "attachment"
        content_type = getattr(att, "content_type", None)
        size = getattr(att, "size", None)
        out: Dict[str, Any] = {
            "ok": True,
            "name": name,
            "size_bytes": size,
            "content_type": content_type,
        }
        is_item = _is_item_attachment(att)
        pkcs7_mime = (not is_item) and is_pkcs7_candidate(name, content_type)
        pkcs7_sig = (not is_item) and (not pkcs7_mime) and _is_pkcs7_signature(
            name, content_type
        )

        # S/MIME (.p7m, .p7s): preserve exact `main` behaviour in every mode.
        if pkcs7_mime:
            oversized_sniff = (
                isinstance(size, (int, float))
                and not isinstance(size, bool)
                and size > _SMIME_SNIFF_MAX_BYTES
            )
            if oversized_sniff and mode in ("info", "auto"):
                out["mode"] = "info"
                out["hint"] = (
                    "PKCS#7 attachment exceeds the 5 MiB S/MIME sniff limit — "
                    "metadata only. Call again with mode='save' or mode='text' "
                    "to download the bytes."
                )
                return ("done", out)
            data = _read_attachment_bytes(att)
            smime_type = sniff_smime_type(name, content_type, data)
            if smime_type:
                out["smime_type"] = smime_type
            if mode == "info":
                out["mode"] = "info"
                return ("done", out)
            if mode == "auto":
                if smime_type == "enveloped":
                    out["mode"] = "info"
                    out["hint"] = (
                        "S/MIME encrypted (PKCS#7 EnvelopedData) — the "
                        "content is not readable without the recipient's "
                        "private key."
                    )
                    return ("done", out)
                if smime_type != "signed":
                    out["mode"] = "info"
                    out["hint"] = (
                        "Binary attachment — metadata only. Call again "
                        "with mode='save' to write it to disk."
                    )
                    return ("done", out)
            if data is None:
                out["mode"] = "info"
                out["hint"] = "Attachment bytes were not returned by Exchange."
                return ("done", out)
            if mode in ("text", "auto"):
                text = data.decode("utf-8", errors="replace")
                out["mode"] = "text"
                out["text"] = text[:_TEXT_CAP]
                if len(text) > _TEXT_CAP:
                    out["truncated"] = True
                    out["total_chars"] = len(text)
                if smime_type == "signed":
                    out["hint"] = (
                        "Signed, not encrypted (PKCS#7 SignedData) — this "
                        "is the raw DER wrapper decoded as text, so the "
                        "certificate/signature bytes around the readable "
                        "message content appear as \ufffd replacement "
                        "characters."
                    )
                return ("done", out)
            out["mode"] = "save"
            out["saved_path"] = _save_attachment_file(ctx, name, data)
            return ("done", out)

        if pkcs7_sig:
            if mode == "info":
                out["mode"] = "info"
                return ("done", out)
            if mode == "auto":
                out["mode"] = "info"
                out["hint"] = (
                    "Binary attachment — metadata only. Call again "
                    "with mode='save' to write it to disk."
                )
                return ("done", out)
            data = _read_attachment_bytes(att)
            if data is None:
                out["mode"] = "info"
                out["hint"] = "Attachment bytes were not returned by Exchange."
                return ("done", out)
            if mode == "text":
                text = data.decode("utf-8", errors="replace")
                out["mode"] = "text"
                out["text"] = text[:_TEXT_CAP]
                if len(text) > _TEXT_CAP:
                    out["truncated"] = True
                    out["total_chars"] = len(text)
                return ("done", out)
            out["mode"] = "save"
            out["saved_path"] = _save_attachment_file(ctx, name, data)
            return ("done", out)

        if mode == "info":
            out["mode"] = "info"
            return ("done", out)

        if mode == "save":
            if is_item:
                raise ToolError(
                    "validation",
                    cap_message(
                        f"Attachment {_safe_snippet(name)!r} is an attached "
                        "Exchange message and cannot be saved as raw file bytes.",
                        MAX_ERROR_MESSAGE_CHARS,
                    ),
                    hint="Use mode='auto' or mode='text' to read the attached message.",
                )
            data = _read_attachment_bytes(att)
            if data is None:
                raise ToolError(
                    "validation",
                    cap_message(
                        f"Attachment {_safe_snippet(name)!r} has no retrievable bytes.",
                        MAX_ERROR_MESSAGE_CHARS,
                    ),
                    hint="Use mode='info'.",
                )
            out["mode"] = "save"
            out["saved_path"] = _save_attachment_file(ctx, name, data)
            return ("done", out)

        doc_kind = "message" if is_item else detect_kind(name, content_type)
        if doc_kind is None:
            if is_msg_attachment(name, content_type):
                msg_note = (
                    "Outlook .msg message file — metadata only. "
                    "Call again with mode='save' to save it to disk and open it externally."
                )
                out["mode"] = "info"
                out["hint"] = msg_note
                out["note"] = msg_note
                return ("done", out)
            if mode == "auto":
                out["mode"] = "info"
                out["hint"] = (
                    "Binary attachment — metadata only. Call again "
                    "with mode='save' to write it to disk."
                )
                return ("done", out)
            raise unsupported_type_error(name, content_type)

        _check_declared_ceiling(name, size)
        cache_key = _attachment_cache_key(att, raw_id, att_idx, name, size)
        paging_info = {
            "raw_msg_id": raw_id,
            "selector": attachment,
            "att_idx": att_idx,
            "att_count": len(atts),
            "name": name,
            "size_bytes": size,
            "content_type": content_type,
        }

        cached = cache.get(cache_key)
        if cached is not None:
            if cached.name is None:
                cached.name = name
                cached.size_bytes = size
                cached.content_type = content_type
            cache.bind_selector(raw_id, attachment, cache_key)
            return ("done", _render_text_response(out, cached.doc, offset=offset))

        if is_item:
            attached_item = _read_attachment_item(att)
            if attached_item is None:
                raise ToolError(
                    "validation",
                    cap_message(
                        f"Attachment {_safe_snippet(name)!r} has no retrievable "
                        "message item.",
                        MAX_ERROR_MESSAGE_CHARS,
                    ),
                    hint="Use mode='info'.",
                )
            item_fields = serialize_message_item_fields(
                attached_item, tz=ctx.settings.ews_tz
            )
            return ("extract_item", out, cache_key, item_fields, paging_info)

        if doc_kind == "pdf" and not pypdf_available():
            raise ToolError(
                "validation",
                "PDF text extraction requires the optional 'pypdf' dependency, "
                "which is not installed.",
                hint=_PYPDF_MISSING_HINT,
            )

        data = _read_attachment_bytes(att)
        if data is None:
            raise ToolError(
                "validation",
                cap_message(
                    f"Attachment {_safe_snippet(name)!r} has no retrievable bytes.",
                    MAX_ERROR_MESSAGE_CHARS,
                ),
                hint="Use mode='info'.",
            )
        if len(data) > max_bytes:
            raise _ceiling_error(name, len(data))

        if doc_kind == "text":
            return ("extract_plain_text", out, cache_key, data, paging_info)

        return ("extract_bytes", out, cache_key, data, name, content_type, paging_info)

    fetched: Tuple[Any, ...] = await ctx.gateway.call(fetch_work)
    action = fetched[0]
    if action == "done":
        return fetched[1]

    if action == "extract_plain_text":
        _, out_dict, cache_key, raw_bytes, paging_info = fetched
        doc = await asyncio.to_thread(
            extract_plain_text, raw_bytes, max_bytes=max_bytes
        )
        cache.put(cache_key, doc, **paging_info)
        return _render_text_response(out_dict, doc, offset=offset)

    keep_dumpable = bool(getattr(ctx.settings, "ews_keep_dumpable", False))
    if action == "extract_item":
        _, out_dict, cache_key, item_fields, paging_info = fetched
        doc = await extract_item_in_worker(
            item_fields, max_bytes=max_bytes, keep_dumpable=keep_dumpable
        )
        cache.put(cache_key, doc, **paging_info)
        return _render_text_response(out_dict, doc, offset=offset)

    _, out_dict, cache_key, raw_bytes, att_name, att_ct, paging_info = fetched
    doc = await extract_bytes_in_worker(
        raw_bytes,
        name=att_name,
        content_type=att_ct,
        max_bytes=max_bytes,
        keep_dumpable=keep_dumpable,
    )
    cache.put(cache_key, doc, **paging_info)
    return _render_text_response(out_dict, doc, offset=offset)


# --------------------------------------------------------------------------
# 6. get_mailbox_overview
# --------------------------------------------------------------------------


async def _get_mailbox_overview(ctx: Context, horizon_days: int = 1,
                                fresh: bool = False) -> Dict[str, Any]:
    horizon_days = max(1, min(int(horizon_days), 14))
    tz = ctx.settings.ews_tz
    now = datetime.now(ZoneInfo(tz))
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=horizon_days)

    # ---- pure mirror + watermark (sub-100ms warm) --------------------------
    as_of = _cache_watermark(ctx, "inbox") if not fresh else None
    if as_of and ctx.cache is not None:
        try:
            def read_mirror():
                unread_total, rows = ctx.cache.unread_page(limit=10)
                events = ctx.cache.events_window(
                    int(day_start.timestamp()), int(day_end.timestamp()),
                    limit=10)
                return unread_total, rows, events

            unread_total, rows, event_rows = await asyncio.to_thread(read_mirror)
            cards = await asyncio.to_thread(
                lambda: [_row_card(ctx, r) for r in rows])
            events = [{
                "id": ctx.aliaser.alias_for(r["ews_id"], "e"),
                "subject": r["subject"],
                "start": r["start_iso"],
                "end": r["end_iso"],
                **({"location": r["location"]} if r["location"] else {}),
                **({"recurring": True} if r["is_recurring"] else {}),
            } for r in event_rows]
            return _stamp({
                "ok": True,
                "generated_at": now.isoformat(timespec="seconds"),
                "unread_total": unread_total,
                "recent_unread": cards,
                "today_events": events,
                "connection": ctx.manager.state if ctx.manager else "unmanaged",
            }, "cache", as_of)
        except Exception as exc:
            logger.warning("cache overview failed (%s) — live", exc)

    def work(account: Any) -> Tuple[int, List[Any], List[Any]]:
        try:
            # unread_count is a stored field — refresh or report yesterday's.
            account.inbox.refresh()
        except Exception:
            pass
        unread_total = getattr(account.inbox, "unread_count", None) or 0
        qs = _project(account.inbox.filter(is_read=False))
        recent, _next = paginate(qs, offset=0, limit=10)
        # .view() (not .filter()) so recurring events are expanded; cap the
        # expansion server-side instead of slicing an unbounded list.
        events = list(account.calendar.view(start=day_start, end=day_end,
                                            max_items=10))[:10]
        return unread_total, recent, events

    unread_total, recent, events = await ctx.gateway.call(work)
    recent_cards = await _cards_off_loop(ctx, recent, tz)
    return _stamp({
        "ok": True,
        "generated_at": now.isoformat(timespec="seconds"),
        "unread_total": unread_total,
        "recent_unread": recent_cards,
        "today_events": [event_card(ev, ctx.aliaser, tz) for ev in events],
        "connection": ctx.manager.state if ctx.manager else "unmanaged",
    }, "live")


# --------------------------------------------------------------------------
# 7. find_similar (registered ONLY when the semantic tier is enabled)
# --------------------------------------------------------------------------


async def _find_similar(ctx: Context, id: str, top_k: int = 5) -> Dict[str, Any]:
    top_k = max(1, min(int(top_k), 20))
    raw_id = id
    if ctx.semantic is None:  # defensive; the tool is unregistered when off
        raise ToolError("validation", "semantic tier is disabled")
    if ctx.cache is None:
        raise ToolError("upstream_unavailable",
                        "find_similar needs the local mirror")
    try:
        hits = await asyncio.to_thread(ctx.semantic.query_similar, raw_id, top_k)
    except Exception as exc:
        raise ToolError("upstream_unavailable",
                        f"semantic tier unavailable: {type(exc).__name__}",
                        hint="Retry later or use search_messages mode='keyword'.")
    cards: List[Dict[str, Any]] = []
    for ews_id, score in hits:
        row = await asyncio.to_thread(ctx.cache.get_message, ews_id)
        if row is not None:
            card = _row_card(ctx, row)
            card["similarity"] = round(float(score), 3)
            cards.append(card)
    out = envelope(cards, total_available=len(cards), offset=0)
    return _stamp(out, "cache")


SEMANTIC_TOOLS: List["ToolSpec"] = []  # populated below, after _schema is defined


# --------------------------------------------------------------------------
# Specs
# --------------------------------------------------------------------------


def _schema(properties: Dict[str, Any],
            required: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="list_folders",
        description=(
            "List mail folders as a depth-limited tree walk, or search the "
            "entire folder tree when `query` is given. Each row is "
            "{id, name, path, total, unread, children}; `id` is a short folder "
            "alias (f7) reusable as a `folder`/`parent` argument anywhere. "
            "Well-known folders also carry `wk` (e.g. 'f:inbox') — prefer "
            "passing that stable alias. Rows also carry the raw EWS FolderClass "
            "as `folder_class` when Exchange reports one (e.g. 'IPF.Note' for "
            "mail, 'IPF.Appointment' for calendar). Set include_empty=false to "
            "hide folders with zero items."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "parent": {
                "type": "string",
                "description": "Start folder: well-known alias (f:inbox), folder "
                               "alias (f7), path, or raw id. Default: the "
                               "message-folder root.",
            },
            "depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
            "include_empty": {"type": "boolean", "default": True},
            "query": {
                "type": "string",
                "description": "Non-empty case-insensitive substring match on "
                               "folder name or full path across the whole tree "
                               "(`depth` is ignored when `query` is set).",
            },
            "fresh": dict(_FRESH_PROPERTY),
        }),
        handler=_list_folders,
    ),
    ToolSpec(
        name="search_messages",
        description=(
            "Search mail. TWO ENGINES, mutually exclusive: pass `query` (an "
            "Exchange AQS string, e.g. 'from:alice@example.com subject:rfp hasattachment:yes') "
            "OR the structured filters (sender/subject/since/until/is_unread/"
            "has_attachments/categories/importance/flag_complete) — combining "
            "`query` with any structured filter is "
            "a validation error. `sender` is matched client-side against the "
            "fetched page's sender email/name, so total_available is unknown "
            "when it is used. Results are compact cards, newest first; their "
            "`id` values are short aliases (m12) for get_message / get_messages / "
            "get_thread / get_attachment. If an id later goes stale (items move), "
            "re-run this search for fresh ids."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "query": {
                "type": "string",
                "description": "AQS query string — cannot be combined with the "
                               "structured filters below.",
            },
            "folder": {
                "type": "string", "default": "f:inbox",
                "description": "Folder alias (f:inbox, f:sent, f7), path, raw id, or "
                               "f:all for the whole mailbox: all mail folders with "
                               "their subfolders, without deleted items, junk, drafts "
                               "and outbox; each hit then names its folder.",
            },
            "sender": {
                "type": "string",
                "description": "Sender email or display-name substring, "
                               "matched within each page; keep paging while "
                               "next_offset is set (a page may hold fewer "
                               "than `limit` items or none, and "
                               "total_available is null on live searches).",
            },
            "from_": {
                "type": "string",
                "description": "DEPRECATED alias of `sender` — do not combine "
                               "the two.",
            },
            "subject": {"type": "string", "description": "Subject substring."},
            "since": {
                "type": "string",
                "description": "Window start: 'today', '+Nd', YYYY-MM-DD, or "
                               "ISO datetime (server timezone).",
            },
            "until": {
                "type": "string",
                "description": "Window end: 'today', '+Nd', YYYY-MM-DD, or "
                               "ISO datetime (server timezone).",
            },
            "is_unread": {"type": "boolean"},
            "has_attachments": {"type": "boolean"},
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Match messages carrying all listed Outlook "
                               "categories; live search uses contains matching "
                               "whereas the local mirror matches exact category "
                               "elements. Cannot combine with `query`.",
            },
            "importance": {
                "type": "string",
                "enum": ["high", "normal", "low"],
                "description": "Match messages by importance (high, normal, or "
                               "low) on both the mirror and live lanes. Cannot "
                               "combine with `query`.",
            },
            "recursive": {
                "type": "boolean", "default": False,
                "description": "Search `folder` and all mail folders under it, "
                               "merged newest first; each card carries `folder` "
                               "and the result reports `folders_searched`. "
                               "Always runs live.",
            },
            "flag_complete": {
                "type": "boolean",
                "description": "true matches messages whose follow-up flag is "
                               "marked Complete; false matches all other "
                               "messages (unflagged or still-open flag). Always "
                               "runs live and cannot combine with `query`.",
            },
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            "mode": {
                "type": "string", "enum": ["keyword", "semantic"],
                "default": "keyword",
                "description": "semantic = hybrid FTS + vector retrieval "
                               "(only when the semantic tier is enabled; "
                               "requires `query`; other filters ignored).",
            },
            "fresh": dict(_FRESH_PROPERTY),
        }),
        handler=_search_messages,
    ),
    ToolSpec(
        name="check_messages",
        description=(
            "Check a whole list of values against one mail folder in one call: "
            "which values occur as a whole word in a message subject (or in the "
            "sender name/address) and which are missing. Returns up to 5 matching "
            "cards per value. Use this instead of one search_messages call per value."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "values": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "Values separated by commas or new lines, or an "
                               "array of strings (a JSON array string is "
                               "accepted too). At most 200.",
            },
            "field": {"type": "string", "enum": ["subject", "sender"],
                      "description": "Where to look for each value (default subject)."},
            "folder": {
                "type": "string",
                "description": "Single mail folder alias, path, or raw id, e.g. "
                               "f:inbox (default) or f:sent (f:all and recursive "
                               "scopes are not supported).",
            },
            "since": {"type": "string", "description": "Received at or after (ISO date)."},
            "until": {"type": "string", "description": "Received at or before (ISO date)."},
            "only_missing": {"type": "boolean",
                             "description": "Return only the values without a match."},
            "fresh": dict(_FRESH_PROPERTY),
        }, ["values"]),
        handler=_check_messages,
    ),
    ToolSpec(
        name="get_message",
        description=(
            "Fetch one message by id (short alias from search results, e.g. "
            "m12; raw EWS ids also work). format='full' (default) returns "
            "recipients, the cleaned body (quoted history stripped — use "
            "get_thread for the conversation) and the attachment inventory; "
            "format='concise' returns the compact card; format='eml' writes "
            "the message MIME content exclusively under DATA_DIR/attachments "
            "(adding -2, -3, … rather than overwriting, and refusing above "
            "EWS_MAX_ATTACHMENT_MB) and returns only saved_path and "
            "size_bytes — a message exported from Sent Items may contain its "
            "Bcc header because the file is the message as the mailbox holds "
            "it. `flag_complete` is only reported when read live, i.e. with "
            "fresh=true. A not_found means the id went stale (items move) — "
            "re-run search_messages and retry with a fresh id."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "id": {"type": "string", "description": "Message id (m-alias or raw)."},
            "format": {"type": "string", "enum": ["full", "concise", "eml"],
                       "default": "full"},
            "include_html": {
                "type": "boolean", "default": False,
                "description": "Also return the raw HTML body (rendering edge "
                               "cases only — it is token-expensive).",
            },
            "fresh": dict(_FRESH_PROPERTY),
        }, required=["id"]),
        handler=_get_message,
    ),
    ToolSpec(
        name="get_messages",
        description=(
            "Fetch up to 20 messages by id in one call, preserving the "
            "requested order. Returns the canonical envelope {items, count, "
            "total_available, next_offset} plus `failed` ({id, index, code, "
            "error}) for per-message failures without aborting the remaining "
            "ids; when some items come from the local mirror and some live, "
            "top-level `source` is 'mixed' and each item carries its own "
            "`source` (plus `as_of` on mirror items). format='full' (default) "
            "returns the same cleaned message shape as get_message without "
            "downloading attachment bytes: PKCS#7 S/MIME attachments are "
            "reported as smime_type='candidate' from name and MIME type only, "
            "whereas get_message gives the confirmed 'signed'/'enveloped' "
            "classification for one message. format='concise' returns compact "
            "cards."
        ),
        side_effect_class="read",
        requires_ews=True,
        resolve_ids=False,
        input_schema=_schema({
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": GET_MESSAGES_MAX_IDS,
                "description": "Message ids (m-aliases or raw EWS ids), 1 to 20 "
                               "per call.",
            },
            "format": {
                "type": "string",
                "enum": ["concise", "full"],
                "default": "full",
            },
            "fresh": dict(_FRESH_PROPERTY),
        }, required=["ids"]),
        handler=_get_messages,
    ),
    ToolSpec(
        name="get_thread",
        description=(
            "Rebuild the conversation containing the given message id: Inbox "
            "and Sent are merged and sorted chronologically, each entry's body "
            "cleaned to its latest-reply-only text. Returns thread_id "
            "(t-alias), participants with message counts, and the most recent "
            "`limit` entries. Entry ids are m-aliases usable with get_message/"
            "get_attachment. Stale id → re-run search_messages."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "id": {"type": "string",
                   "description": "Any message id in the thread (m-alias or raw)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            "offset": {
                "type": "integer", "minimum": 0, "default": 0,
                "description": "History paging: 0 = the most recent entries; "
                               "pass the returned next_offset for older ones.",
            },
            "fresh": dict(_FRESH_PROPERTY),
        }, required=["id"]),
        handler=_get_thread,
    ),
    ToolSpec(
        name="get_attachment",
        description=(
            "Read one attachment of a message. mode='info' → metadata only; "
            "'text' → extracted text in memory (up to 20000 chars from `offset`, "
            "with `total_chars`, `next_offset` when more follows, "
            "`extracted_from`, and `pages`/`slides`/`sheets` count) for PDF "
            "(.pdf, requires ews-mcp[extract]), Word (.docx/.docm), Excel "
            "(.xlsx/.xlsm), PowerPoint (.pptx/.pptm), RFC-822 (.eml), attached "
            "Exchange messages, HTML (.html/.htm), and plain text (.txt/.csv/"
            ".md/.log/.json or text/*; .msg is not supported); 'save' → write "
            "the bytes exclusively under DATA_DIR/attachments (adding -2, -3, "
            "… rather than overwriting) and return saved_path; 'auto' "
            "(default) → text for every supported document/text type, otherwise "
            "info plus a hint. An smime.p7m attachment (application/pkcs7-mime) "
            "is sniffed and reported as `smime_type: signed|enveloped` — signed "
            "is NOT encrypted (auto decodes as text, cert/signature bytes show "
            "as replacement chars); enveloped IS encrypted (auto stays info; "
            "content is unrecoverable without the recipient's private key). "
            "When the message has several attachments you MUST pick one via "
            "`attachment` (a file name, a zero-based index as a string, or the "
            "`id` returned by the draft tools)."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "message_id": {"type": "string",
                           "description": "Message id (m-alias or raw)."},
            "attachment": {
                "type": "string",
                "description": "Attachment file name, zero-based index as a "
                               "string (e.g. '0'), or `id` returned by the "
                               "draft tools (e.g. 'a1'). Optional only when "
                               "the message has exactly one attachment.",
            },
            "mode": {"type": "string", "enum": ["auto", "info", "text", "save"],
                     "default": "auto"},
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": (
                    "Character offset for reading extracted text in parts "
                    "(pass the returned next_offset for the next chunk)."
                ),
            },
        }, required=["message_id"]),
        handler=_get_attachment,
    ),
    ToolSpec(
        name="get_mailbox_overview",
        description=(
            "The morning-brief workflow tool — start here. One call returns "
            "unread_total, the 10 most recent unread message cards, today's "
            "calendar events (recurrences expanded) across `horizon_days` "
            "days, and the Exchange connection state. Card ids are short "
            "aliases (m12 / e3) for the other tools."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "horizon_days": {"type": "integer", "minimum": 1, "maximum": 14,
                             "default": 1},
            "fresh": dict(_FRESH_PROPERTY),
        }),
        handler=_get_mailbox_overview,
    ),
]

# Registered by build_registry ONLY when EWS_SEMANTIC_INDEX != none — the
# default public surface never carries a tool that cannot work.
SEMANTIC_TOOLS.append(ToolSpec(
    name="find_similar",
    description=(
        "Find messages semantically similar to a given one (vector "
        "similarity over cleaned bodies; Arabic + English). Returns compact "
        "cards with a `similarity` score. Only available when the optional "
        "semantic tier is enabled."
    ),
    side_effect_class="read",
    requires_ews=False,  # answers from the mirror + vector index
    input_schema=_schema({
        "id": {"type": "string",
               "description": "Message id (m-alias or raw) to compare against."},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
    }, required=["id"]),
    handler=_find_similar,
))
