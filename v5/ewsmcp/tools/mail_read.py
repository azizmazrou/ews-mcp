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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

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
from ..flag_property import ensure_registered as ensure_flag_status_registered
from ..gateway.client import WELL_KNOWN, paginate
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
    if (row["importance"] or "").lower() == "high":
        card["importance"] = "high"
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

_TEXT_EXTS = (".txt", ".csv", ".md", ".log", ".json")
_TEXT_CAP = 20_000
_SMIME_SNIFF_MAX_BYTES = 5 * 1024 * 1024


# --------------------------------------------------------------------------
# Shared sync helpers (called inside gateway lambdas, on the EWS pool)
# --------------------------------------------------------------------------


def _project(qs: Any, order: Optional[str] = "-datetime_received") -> Any:
    """Best-effort .order_by()/.only() — some folder types reject them."""
    if order:
        try:
            qs = qs.order_by(order)
        except Exception:
            pass
    try:
        qs = qs.only(*_PROJECTION)
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


def _pick_attachment(atts: List[Any], selector: Optional[str]) -> Any:
    names = [getattr(a, "name", "") or f"attachment-{i}" for i, a in enumerate(atts)]
    if selector is None:
        if len(atts) == 1:
            return atts[0]
        raise ToolError(
            "validation",
            f"The message has {len(atts)} attachments — pick one: {', '.join(names)}.",
            hint="Pass attachment=<name> or attachment=<zero-based index as a string>.",
        )
    sel = str(selector).strip()
    for att, name in zip(atts, names):
        if name.lower() == sel.lower():
            return att
    digits = sel[1:] if sel[:1] == "a" and sel[1:].isdigit() else sel
    if digits.isdigit() and int(digits) < len(atts):
        return atts[int(digits)]
    raise ToolError(
        "not_found",
        f"No attachment matches {selector!r}. Available: {', '.join(names)}.",
    )


def _is_texty(name: str, content_type: Optional[str]) -> bool:
    return ((content_type or "").lower().startswith("text/")
            or (name or "").lower().endswith(_TEXT_EXTS))


def _read_attachment_bytes(att: Any) -> Optional[bytes]:
    """Single site that touches ``att.content`` (triggers GetAttachment).

    Must only run inside a gateway worker function when bytes are needed.
    """
    raw = getattr(att, "content", None)
    return bytes(raw) if isinstance(raw, (bytes, bytearray)) else None


# --------------------------------------------------------------------------
# 1. list_folders
# --------------------------------------------------------------------------


async def _list_folders(ctx: Context, parent: Optional[str] = None, depth: int = 2,
                        include_empty: bool = True,
                        fresh: bool = False) -> Dict[str, Any]:
    depth = max(1, min(int(depth), 5))

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
                if (r["path"] or "").count("/") + 1 > depth:
                    continue
                if not include_empty and not r["total"]:
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
            except Exception:
                continue
        rows: List[Dict[str, Any]] = []

        def walk(folder: Any, level: int, prefix: str) -> None:
            if level > depth:
                return
            for child in list(getattr(folder, "children", None) or []):
                name = getattr(child, "name", "") or ""
                path = f"{prefix}/{name}" if prefix else name
                total = getattr(child, "total_count", None) or 0
                raw_id = getattr(child, "id", None)
                grandchildren = list(getattr(child, "children", None) or [])
                if include_empty or total:
                    row: Dict[str, Any] = {
                        "id": ctx.aliaser.alias_for(str(raw_id), "f") if raw_id else None,
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
                walk(child, level + 1, path)

        walk(root, 1, "")
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
    qs = target.filter(**filters)
    if flag_complete is False:
        # Incomplete covers both unflagged (None) and open-flagged (2);
        # excluding Complete (1) captures both.
        qs = qs.exclude(flag_status=1)
    return qs


def _select_subtree(
    target: Any,
    root_ref: str,
    all_folders: List[Tuple[str, Any]],
) -> List[Tuple[str, Any]]:
    """Return ``(path, folder)`` entries from ``all_folders`` at or under ``target``."""
    target_id = getattr(target, "id", None)
    root_path: Optional[str] = None
    for path, folder in all_folders:
        if folder is target or (
            target_id is not None and getattr(folder, "id", None) == target_id
        ):
            root_path = path
            break
    if root_path is None and target_id is None:
        ref_key = root_ref.strip().strip("/").lower()
        ref_key = WELL_KNOWN.get(ref_key) or WELL_KNOWN.get(f"f:{ref_key}") or ref_key
        for path, _folder in all_folders:
            if path.rstrip("/").lower() == ref_key:
                root_path = path
                break
    if root_path is None:
        fallback = getattr(target, "name", None) or root_ref.strip()
        return [(str(fallback), target)]
    prefix = root_path.rstrip("/").lower()
    selected: List[Tuple[str, Any]] = []
    for path, folder in all_folders:
        norm = path.rstrip("/").lower()
        if norm == prefix or norm.startswith(prefix + "/"):
            selected.append((path, folder))
    return selected


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
        if not all_folders:
            raise ToolError("upstream_unavailable", "the folder list is not available.")
        if target is None or root_folder is None:
            return all_folders
        return _select_subtree(target, root_folder, all_folders)

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
    if sender_needle:
        hits = [h for h in hits if _sender_matches_substring(h[1], sender_needle)]
    return {
        "hits": hits[offset:wanted],
        "total": (None if (truncated or failed or skipped or sender_needle)
                  else len(hits)),
        "next_offset": wanted if truncated or len(hits) > wanted else None,
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
    structured_given = any(
        v is not None for v in
        (sender, subject, since, until, is_unread, has_attachments, categories,
         flag_complete)
    )
    if query and structured_given:
        raise ToolError(
            "validation",
            "`query` (AQS) cannot be combined with the structured filters "
            "(sender/subject/since/until/is_unread/has_attachments/categories/"
            "flag_complete) — "
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
                    categories=categories,
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
        and flag_complete is None
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


async def _check_messages(ctx: Context, values: Any, field: str = "subject",
                          folder: str = "f:inbox", since: Optional[str] = None,
                          until: Optional[str] = None,
                          only_missing: bool = False,
                          fresh: bool = False) -> Dict[str, Any]:
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
    folder_key = _cache_folder_key(ctx, folder) if ctx is not None else None
    mirror_as_of = (
        _cache_watermark(ctx, folder_key)
        if (ctx is not None and not fresh and folder_key)
        else None
    )

    gate = asyncio.Semaphore(4)

    async def one(term: str) -> Tuple[List[Dict[str, Any]], str, Optional[str]]:
        async with gate:
            if field == "subject":
                result = await _search_messages(
                    ctx, folder=folder, since=since, until=until,
                    limit=50, fresh=fresh, subject=term,
                )
                cards = [
                    c for c in (result.get("items") or [])
                    if _word_match(term, c.get("subject", ""))
                ]
            else:
                result = None
                if mirror_as_of:
                    cand = await _search_messages(
                        ctx, folder=folder, since=since, until=until,
                        limit=50, fresh=False, sender=term,
                    )
                    if cand.get("source") == "cache":
                        result = cand
                if result is None:
                    aqs = _aqs_sender_query(term, since_dt, until_dt, tz)
                    result = await _search_messages(
                        ctx, query=aqs, folder=folder, limit=50, fresh=True,
                    )
                cards = [
                    c for c in (result.get("items") or [])
                    if _sender_match(term, c.get("from", ""))
                    and _card_in_window(c, since_dt, until_dt)
                ]
        trimmed = [
            {k: c.get(k) for k in ("id", "subject", "from", "date")}
            for c in cards[:CHECK_MAX_HITS]
        ]
        return trimmed, str(result.get("source") or "live"), result.get("as_of")

    outcomes = await asyncio.gather(*(one(term) for term in terms))
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
# 3. get_message
# --------------------------------------------------------------------------


async def _get_message(ctx: Context, id: str, format: str = "full",
                       include_html: bool = False,
                       fresh: bool = False) -> Dict[str, Any]:
    raw_id = id
    tz = ctx.settings.ews_tz
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
        return fetched, smime_by_idx

    item, smime_by_idx = await ctx.gateway.call(work)
    return _stamp({"ok": True, "message": msg_full(
        item, ctx.aliaser, tz, ctx.settings.body_max_chars,
        include_html=bool(include_html),
        smime_types=smime_by_idx,
    )}, "live")


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


async def _get_attachment(ctx: Context, message_id: str,
                          attachment: Optional[str] = None,
                          mode: str = "auto") -> Dict[str, Any]:
    raw_id = message_id

    def work(account: Any) -> Dict[str, Any]:
        item = _fetch_one(account, raw_id, only=["attachments"])
        atts = list(getattr(item, "attachments", None) or [])
        if not atts:
            raise ToolError("not_found", "This message has no attachments.")
        att = _pick_attachment(atts, attachment)
        name = getattr(att, "name", "") or "attachment"
        content_type = getattr(att, "content_type", None)
        out: Dict[str, Any] = {
            "ok": True,
            "name": name,
            "size_bytes": getattr(att, "size", None),
            "content_type": content_type,
        }
        data: Optional[bytes] = None
        bytes_loaded = False
        smime_type: Optional[str] = None
        if is_pkcs7_candidate(name, content_type):
            data = _read_attachment_bytes(att)
            bytes_loaded = True
            smime_type = sniff_smime_type(name, content_type, data)
            if smime_type:
                out["smime_type"] = smime_type
        chosen = mode
        if mode == "auto":
            if smime_type == "enveloped":
                chosen = "info"
                out["hint"] = ("S/MIME encrypted (PKCS#7 EnvelopedData) — the "
                               "content is not readable without the recipient's "
                               "private key.")
            elif smime_type == "signed":
                chosen = "text"
            else:
                chosen = "text" if _is_texty(name, content_type) else "info"
                if chosen == "info":
                    out["hint"] = ("Binary attachment — metadata only. Call again "
                                   "with mode='save' to write it to disk.")
        if chosen == "info":
            out["mode"] = "info"
            return out
        if not bytes_loaded:
            data = _read_attachment_bytes(att)
        if data is None:
            raise ToolError(
                "validation",
                f"Attachment {name!r} has no retrievable bytes (item attachment?).",
                hint="Only file attachments can be read; use mode='info'.",
            )
        if chosen == "text":
            text = data.decode("utf-8", errors="replace")
            out["mode"] = "text"
            out["text"] = text[:_TEXT_CAP]
            if len(text) > _TEXT_CAP:
                out["truncated"] = True
            if smime_type == "signed":
                out["hint"] = ("Signed, not encrypted (PKCS#7 SignedData) — this "
                               "is the raw DER wrapper decoded as text, so the "
                               "certificate/signature bytes around the readable "
                               "message content appear as � replacement "
                               "characters.")
            return out
        safe = re.sub(r"[^\w.\-]+", "_", name).strip("._") or "attachment.bin"
        dest = Path(ctx.settings.data_dir) / "attachments"
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / safe
        path.write_bytes(data)
        out["mode"] = "save"
        out["saved_path"] = str(path)
        return out

    return await ctx.gateway.call(work)


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
            "List mail folders as a depth-limited tree walk. Each row is "
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
            "has_attachments/categories/flag_complete) — combining `query` with any "
            "structured filter is "
            "a validation error. `sender` is matched client-side against the "
            "fetched page's sender email/name, so total_available is unknown "
            "when it is used. Results are compact cards, newest first; their "
            "`id` values are short aliases (m12) for get_message / get_thread / "
            "get_attachment. If an id later goes stale (items move), re-run "
            "this search for fresh ids."
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
                "description": "Sender substring, matched client-side on the "
                               "fetched page (email or display name).",
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
                               "categories (exact names). Cannot combine with "
                               "`query`.",
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
            "folder": {"type": "string",
                       "description": "Folder alias, e.g. f:inbox (default) or f:sent."},
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
            "format='concise' returns the compact card. `flag_complete` is "
            "only reported when read live, i.e. with fresh=true. A not_found "
            "means the id went stale (items move) — re-run search_messages "
            "and retry with a fresh id."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "id": {"type": "string", "description": "Message id (m-alias or raw)."},
            "format": {"type": "string", "enum": ["full", "concise"],
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
            "'text' → decoded UTF-8 text (capped at 20000 chars, `truncated` "
            "flagged) for text-like attachments (text/* content type or "
            ".txt/.csv/.md/.log/.json name); 'save' → write the bytes under "
            "the server data dir and return saved_path; 'auto' (default) → "
            "text when text-like, otherwise info plus a hint. An smime.p7m "
            "attachment (application/pkcs7-mime) is sniffed and reported as "
            "`smime_type: signed|enveloped` — signed is NOT encrypted (auto "
            "decodes as text, cert/signature bytes show as replacement chars); "
            "enveloped IS encrypted (auto stays info; content is unrecoverable "
            "without the recipient's private key). When the message has "
            "several attachments you MUST pick one via `attachment` (a name, "
            "or a zero-based index as a string)."
        ),
        side_effect_class="read",
        requires_ews=True,
        input_schema=_schema({
            "message_id": {"type": "string",
                           "description": "Message id (m-alias or raw)."},
            "attachment": {
                "type": "string",
                "description": "Attachment name or zero-based index as a string "
                               "(e.g. '0'). Optional only when the message has "
                               "exactly one attachment.",
            },
            "mode": {"type": "string", "enum": ["auto", "info", "text", "save"],
                     "default": "auto"},
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
