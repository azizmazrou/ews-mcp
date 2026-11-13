"""Tool pack: writes — the 13 mutating tools (DESIGN.md §Tools, §Safety).

Safety lives in the dispatcher (tools/base.py): kill-switch, tier gate,
recipient guard, two-phase confirm, send rate cap and alias resolution all
run *before* any handler here executes. Specs below only DECLARE
``side_effect_class`` and ``confirm``; handlers receive raw EWS ids and
never see ``confirm_token``.

The single exception (documented inline): ``create_event``/``update_event``
are class "write" for *tier* purposes (a no-invite event is a reversible
calendar write), but invitations/notifications LEAVE the org. The
dispatcher's kill-switch only covers class "send", so those two handlers
re-check ``settings.send_enabled`` themselves when (and only when) the call
would notify attendees.

exchangelib 5.0.3 pins honoured here (verified against the installed lib):
- ``Item.delete()`` takes NO disposal kwarg — it IS HardDelete. Reversible
  paths are ``move_to_trash()`` / ``soft_delete()``.
- ``OofSettings.internal_reply``/``external_reply`` are PLAIN strings (the
  4.x ``OofReply`` wrapper is gone); state constants are
  ``OofSettings.ENABLED/SCHEDULED/DISABLED``.
- ``Item.save(update_fields=[...], send_meeting_invitations=...)``;
  invitation constants live in ``exchangelib.items``.
- ``Message.create_reply(subject, body, ...)`` / ``create_reply_all`` /
  ``Item.create_forward(subject, body, to_recipients, ...)`` return reply
  items persisted via ``.save(account.drafts)`` (Exchange quotes the
  original server-side).
- ``Item.move(folder)`` returns ``None`` and updates ``item.id`` in place.
- ``CalendarItem.recurring_master()`` sends no query and returns an item
  holding a ``RecurringMasterItemId``; ``refresh()`` loads the master.
- Follow-up flag completion uses ExtendedProperty registration
  (PidTagFlagStatus, tag 0x1090; see ../flag_property.py); update_messages
  exposes it via ``flag_complete``.
"""

import asyncio
import base64
import binascii
import html as _html
import logging
import mimetypes
import os
import stat
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from exchangelib import (
    UTC,
    CalendarItem,
    EWSDateTime,
    FileAttachment,
    HTMLBody,
    Message,
    OofSettings,
)
from exchangelib.items import (
    SEND_TO_ALL_AND_SAVE_COPY,
    SEND_TO_CHANGED_AND_SAVE_COPY,
    SEND_TO_NONE,
)
from exchangelib.recurrence import (
    AbsoluteMonthlyPattern,
    DailyPattern,
    Recurrence,
    WeeklyPattern,
)

from ..dates import parse_when
from ..dto import fmt_dt
from ..errors import ToolError, map_exception
from ..flag_property import ensure_registered as ensure_flag_status_registered
from .base import Context, ToolSpec, preview_content_hash

ensure_flag_status_registered()

logger = logging.getLogger(__name__)

MAX_BULK_IDS = 50
MAX_ATTACHMENTS = 20


def max_attachment_bytes(settings: Any) -> int:
    return int(settings.ews_max_attachment_mb) * 1024 * 1024


def _write_through(ctx: Context, method: str, *args: Any) -> None:
    """Patch the mirror after a successful EWS mutation. Cache failures are
    logged and swallowed — the delta sync repairs the mirror within one
    cycle, and a cache hiccup must never fail a write that already
    succeeded upstream."""
    cache = getattr(ctx, "cache", None)
    if cache is None:
        return
    try:
        getattr(cache, method)(*args)
    except Exception as exc:
        logger.warning("write-through %s failed (sync will repair): %s",
                       method, exc)
_DRAFT_MODES = ("new", "reply", "reply_all", "forward")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_SERIES_TYPE = "RecurringMaster"
_OCCURRENCE_TYPES = ("Occurrence", "Exception")
_RESPONSE_METHOD = {"accept": "accept", "tentative": "tentatively_accept", "decline": "decline"}
_OOF_STATE = {
    "disabled": OofSettings.DISABLED,
    "enabled": OofSettings.ENABLED,
    "scheduled": OofSettings.SCHEDULED,
}

# --- send_draft idempotency (DESIGN.md §Safety, Stripe semantics) ---------------
# Bounded: entries expire after 48h and the store caps at 512 keys (oldest
# evicted first) so a long-lived process can't grow it without limit.
_IDEMPOTENT_SENDS: Dict[str, Dict[str, Any]] = {}
_IDEMPOTENT_LOCK = threading.Lock()
_IDEMPOTENCY_TTL_S = 48 * 3600
_IDEMPOTENCY_MAX = 512


def reset_idempotency_store() -> None:
    """Tests only."""
    with _IDEMPOTENT_LOCK:
        _IDEMPOTENT_SENDS.clear()


def _idempotency_get(key: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _IDEMPOTENT_LOCK:
        for stale in [k for k, v in _IDEMPOTENT_SENDS.items()
                      if now - v.get("ts", 0) > _IDEMPOTENCY_TTL_S]:
            del _IDEMPOTENT_SENDS[stale]
        return _IDEMPOTENT_SENDS.get(key)


def _idempotency_put(key: str, record: Dict[str, Any]) -> None:
    with _IDEMPOTENT_LOCK:
        while len(_IDEMPOTENT_SENDS) >= _IDEMPOTENCY_MAX:
            oldest = min(_IDEMPOTENT_SENDS, key=lambda k: _IDEMPOTENT_SENDS[k].get("ts", 0))
            del _IDEMPOTENT_SENDS[oldest]
        _IDEMPOTENT_SENDS[key] = record


# --- per-draft serialization (self-pruning; never grows unbounded) -----------


@dataclass
class _DraftLockEntry:
    locked: bool = False
    waiters: deque[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = dc_field(
        default_factory=deque
    )


_DRAFT_LOCKS: Dict[str, _DraftLockEntry] = {}
_DRAFT_LOCKS_GUARD = threading.Lock()


def reset_draft_locks() -> None:
    """Tests only."""
    with _DRAFT_LOCKS_GUARD:
        _DRAFT_LOCKS.clear()


def _wake_future(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)


def _handoff_or_prune_draft_lock(raw_id: str, entry: _DraftLockEntry) -> None:
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    while entry.waiters:
        w_loop, w_fut = entry.waiters.popleft()
        if w_fut.done() or w_loop.is_closed():
            continue
        if w_loop is running:
            w_fut.set_result(None)
            return
        try:
            w_loop.call_soon_threadsafe(_wake_future, w_fut)
            return
        except RuntimeError:
            continue
    entry.locked = False
    _DRAFT_LOCKS.pop(raw_id, None)


@asynccontextmanager
async def _draft_lock(raw_id: str):
    """Serialize mutations and sends per raw draft id in-process."""
    loop = asyncio.get_running_loop()
    waiter: Optional[asyncio.Future[None]] = None
    with _DRAFT_LOCKS_GUARD:
        entry = _DRAFT_LOCKS.get(raw_id)
        if entry is None:
            entry = _DraftLockEntry(locked=True)
            _DRAFT_LOCKS[raw_id] = entry
        elif not entry.locked and not entry.waiters:
            entry.locked = True
        else:
            waiter = loop.create_future()
            entry.waiters.append((loop, waiter))

    if waiter is not None:
        try:
            await waiter
        except BaseException:
            with _DRAFT_LOCKS_GUARD:
                current = _DRAFT_LOCKS.get(raw_id)
                if current is entry:
                    try:
                        entry.waiters.remove((loop, waiter))
                    except ValueError:
                        pass
                    if waiter.done() and not waiter.cancelled():
                        _handoff_or_prune_draft_lock(raw_id, entry)
                    elif not entry.locked and not entry.waiters:
                        _DRAFT_LOCKS.pop(raw_id, None)
            raise

    try:
        yield
    finally:
        with _DRAFT_LOCKS_GUARD:
            current = _DRAFT_LOCKS.get(raw_id)
            if current is entry:
                _handoff_or_prune_draft_lock(raw_id, entry)


# Exact field projections for draft/reply fetches (never pull mime_content).
_REPLY_SOURCE_FIELDS: List[str] = [
    "id", "changekey", "subject", "sender", "author", "reply_to",
    "to_recipients", "cc_recipients", "bcc_recipients",
]
_DRAFT_ATTACH_FIELDS: List[str] = [
    "id", "changekey", "parent_folder_id", "attachments",
]
_DRAFT_UPDATE_FIELDS: List[str] = [
    "id", "changekey", "parent_folder_id", "subject", "to_recipients", "cc_recipients",
]
_DRAFT_DELETE_FIELDS: List[str] = [
    "id", "changekey", "parent_folder_id",
]
_DRAFT_PREVIEW_FIELDS: List[str] = [
    "id", "changekey", "parent_folder_id", "subject",
    "to_recipients", "cc_recipients", "bcc_recipients",
    "text_body", "body", "attachments",
]
_DRAFT_SEND_FIELDS: List[str] = [*_DRAFT_PREVIEW_FIELDS, "message_id"]


# --- shared helpers ----------------------------------------------------------


def _wrap_html(text: str) -> HTMLBody:
    """Plain text → minimal HTML (<p> per blank-line paragraph, <br/> inside)."""
    paragraphs = (text or "").split("\n\n")
    rendered = "".join(
        "<p>" + _html.escape(p).replace("\n", "<br/>") + "</p>" for p in paragraphs
    ) or "<p></p>"
    return HTMLBody(f"<html><body>{rendered}</body></html>")


def _email_list(recipients: Any) -> List[str]:
    out: List[str] = []
    for r in recipients or []:
        addr = getattr(r, "email_address", None)
        out.append(addr if isinstance(addr, str) else str(r))
    return out


def _addr_key(entry: Any) -> str:
    raw = getattr(entry, "email_address", None)
    text = (raw if isinstance(raw, str) else str(entry)).strip()
    if "<" in text and text.endswith(">"):
        text = text[text.find("<") + 1 : -1].strip()
    return text.lower()


def _merge_reply_all_recipients(
    reply: Any,
    own_email: str,
    to: Optional[List[str]],
    cc: Optional[List[str]],
    bcc: Optional[List[str]],
) -> tuple[List[Any], List[Any]]:
    own = (own_email or "").strip().lower()
    orig_to = list(getattr(reply, "to_recipients", None) or [])
    orig_cc = list(getattr(reply, "cc_recipients", None) or [])
    orig_bcc = list(getattr(reply, "bcc_recipients", None) or [])

    final_to: List[Any] = []
    seen_to: set[str] = set()
    for entry in orig_to:
        key = _addr_key(entry)
        if not key or key == own or key in seen_to:
            continue
        seen_to.add(key)
        final_to.append(entry)
    for entry in to or []:
        key = _addr_key(entry)
        if not key or key in seen_to:
            continue
        seen_to.add(key)
        final_to.append(entry)
    if not final_to:
        own_entry = next((r for r in orig_to if _addr_key(r) == own), None)
        fallback = own_entry if own_entry is not None else own_email
        if fallback:
            final_to.append(fallback)
            seen_to.add(_addr_key(fallback))

    final_cc: List[Any] = []
    seen_cc: set[str] = set()
    for entry in orig_cc:
        key = _addr_key(entry)
        if not key or key == own or key in seen_to or key in seen_cc:
            continue
        seen_cc.add(key)
        final_cc.append(entry)
    for entry in cc or []:
        key = _addr_key(entry)
        if not key or key in seen_to or key in seen_cc:
            continue
        seen_cc.add(key)
        final_cc.append(entry)

    final_bcc: List[Any] = []
    seen_bcc: set[str] = set()
    for entry in orig_bcc:
        key = _addr_key(entry)
        if not key or key == own or key in seen_to or key in seen_cc or key in seen_bcc:
            continue
        seen_bcc.add(key)
        final_bcc.append(entry)
    for entry in bcc or []:
        key = _addr_key(entry)
        if not key or key in seen_to or key in seen_cc or key in seen_bcc:
            continue
        seen_bcc.add(key)
        final_bcc.append(entry)

    reply.to_recipients = final_to
    reply.cc_recipients = final_cc or None
    reply.bcc_recipients = final_bcc or None
    return final_to, final_cc


def _prefixed(prefix: str, original_subject: Any) -> str:
    base = original_subject if isinstance(original_subject, str) else ""
    if base.lower().startswith(prefix.strip().lower()):
        return base
    return f"{prefix}{base}"


def _fetch_one(
    account: Any, raw_id: str, only: Optional[List[str]] = None
) -> Any:
    """account.fetch yields items in input order; missing ids surface as
    in-line exception instances (exchangelib bulk semantics)."""
    fetched = list(account.fetch([(raw_id, None)], only_fields=only))
    if not fetched:
        raise ToolError("not_found", "item not found; the id may be stale",
                        hint="Re-run search_messages/list_events for a fresh id.")
    item = fetched[0]
    if isinstance(item, Exception):
        raise item  # map_exception classifies (ErrorItemNotFound → not_found)
    return item


def _fetch_many(account: Any, raw_ids: List[str],
                only: Optional[List[str]] = None) -> List[Any]:
    """Bulk fetch with a projection: without only_fields, exchangelib pulls
    the FULL item — including base64 MIME — so a 'mark 50 read' on messages
    with attachments used to move megabytes to flip one boolean each."""
    return list(account.fetch([(i, None) for i in raw_ids], only_fields=only))


def _require_bulk(ids: List[str]) -> None:
    if not ids:
        raise ToolError("validation", "ids must contain at least one id")
    if len(ids) > MAX_BULK_IDS:
        raise ToolError("validation",
                        f"at most {MAX_BULK_IDS} ids per call (got {len(ids)})")


def _item_folder_id(item: Any) -> Optional[str]:
    parent = getattr(item, "parent_folder_id", None)
    if parent is not None and getattr(parent, "id", None):
        return parent.id
    return getattr(getattr(item, "folder", None), "id", None)


def _require_in_drafts(item: Any, account: Any, tool: str) -> None:
    """Input validation mandated by the spec (#15, #18) — not a safety gate."""
    drafts_id = getattr(account.drafts, "id", None)
    if drafts_id is None or _item_folder_id(item) != drafts_id:
        raise ToolError(
            "validation",
            f"{tool}: the item is not a draft (it is not in f:drafts)",
            hint="Only items in the Drafts folder are valid here; "
                 "use create_draft to author one.",
        )


def _failed_entry(ctx: Context, raw_id: str, exc: Exception) -> Dict[str, str]:
    return {"id": ctx.aliaser.alias_for(raw_id, "m"),
            "error": f"{type(exc).__name__}: {exc}"}


def _draft_preview(to: List[str], cc: List[str], subject: str,
                   body: Optional[str]) -> Dict[str, Any]:
    return {"to": to, "cc": cc, "subject": subject,
            "body_snippet": (body or "")[:200]}


def _clean_filename(raw_name: Any) -> str:
    return str(raw_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()


def _read_path_attachment(
    attachment: Dict[str, Any], settings: Any, max_bytes: int, used_bytes: int
) -> tuple[str, bytes]:
    raw_dir = (getattr(settings, "ews_attachment_dir", "") or "").strip()
    if not raw_dir:
        raise ToolError(
            "validation",
            "attachment path is disabled: set EWS_ATTACHMENT_DIR to enable file paths",
            hint="Configure EWS_ATTACHMENT_DIR on the server, or pass content_base64.",
        )
    raw_path = attachment.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise ToolError("validation", "attachment path must be a non-empty relative path")
    win_p = PureWindowsPath(raw_path)
    posix_p = PurePosixPath(raw_path)
    if (
        raw_path.startswith(("/", "\\"))
        or posix_p.is_absolute()
        or win_p.is_absolute()
        or bool(win_p.drive)
        or bool(win_p.root)
    ):
        raise ToolError("validation", "attachment path must be a relative path")
    parts = [p for p in raw_path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ToolError(
            "validation",
            "attachment path must stay within the attachment directory",
        )
    if attachment.get("filename") is not None:
        name = _clean_filename(attachment.get("filename"))
    else:
        name = parts[-1].strip()
    if not name:
        raise ToolError("validation", "an attachment needs a filename")

    base_dir = Path(os.path.realpath(Path(raw_dir).expanduser()))
    if not base_dir.is_dir():
        raise ToolError("validation", "configured attachment directory is unavailable")
    candidate = base_dir.joinpath(*parts)
    real_file = Path(os.path.realpath(candidate))
    if real_file == base_dir or not real_file.is_relative_to(base_dir):
        raise ToolError(
            "validation",
            "attachment path resolves outside the attachment directory",
        )
    try:
        st = real_file.stat()
    except FileNotFoundError:
        raise ToolError("validation", f"attachment {name!r} was not found") from None
    except OSError:
        raise ToolError("validation", f"attachment {name!r} cannot be read") from None
    if not stat.S_ISREG(st.st_mode):
        raise ToolError("validation", f"attachment {name!r} is not a regular file")
    if st.st_size == 0:
        raise ToolError("validation", f"attachment {name!r} is empty")
    if used_bytes + st.st_size > max_bytes:
        raise ToolError("validation", f"attachments exceed {max_bytes} bytes per call")
    try:
        with real_file.open("rb") as fh:
            content = fh.read(max_bytes - used_bytes + 1)
    except OSError:
        raise ToolError("validation", f"attachment {name!r} cannot be read") from None
    if not content:
        raise ToolError("validation", f"attachment {name!r} is empty")
    return name, content


def _decode_attachments(
    attachments: List[Dict[str, Any]], settings: Any
) -> List[Dict[str, Any]]:
    """Validate attachments before anything is written, so a bad one cannot
    leave a half-finished draft behind."""
    if len(attachments) > MAX_ATTACHMENTS:
        raise ToolError(
            "validation",
            f"at most {MAX_ATTACHMENTS} attachments per call (got {len(attachments)})",
        )
    max_bytes = max_attachment_bytes(settings)
    decoded: List[Dict[str, Any]] = []
    total = 0
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise ToolError("validation", "each attachment must be an object")
        has_b64 = attachment.get("content_base64") is not None
        has_path = attachment.get("path") is not None
        if has_b64 and has_path:
            raise ToolError(
                "validation",
                "pass either content_base64 or path per attachment, not both",
            )
        if not has_b64 and not has_path:
            raise ToolError(
                "validation",
                "each attachment requires content_base64 or path",
            )
        if has_path:
            name, content = _read_path_attachment(attachment, settings, max_bytes, total)
        else:
            name = _clean_filename(attachment.get("filename"))
            if not name:
                raise ToolError("validation", "an attachment needs a filename")
            raw_b64 = attachment.get("content_base64")
            if not isinstance(raw_b64, str):
                raise ToolError(
                    "validation",
                    f"attachment {name!r}: content_base64 must be a base64 string",
                )
            try:
                content = base64.b64decode("".join(raw_b64.split()), validate=True)
            except (binascii.Error, ValueError):
                raise ToolError(
                    "validation",
                    f"attachment {name!r}: content_base64 is not valid base64",
                ) from None
            if not content:
                raise ToolError("validation", f"attachment {name!r} is empty")
        total += len(content)
        if total > max_bytes:
            raise ToolError(
                "validation",
                f"attachments exceed {max_bytes} bytes per call",
            )
        decoded.append({
            "name": name,
            "content": content,
            "content_type": (
                attachment.get("content_type")
                or mimetypes.guess_type(name)[0]
                or "application/octet-stream"
            ),
        })
    return decoded


def _attach_one(msg: Any, attachment: Dict[str, Any]) -> Dict[str, Any]:
    """Create the attachment server-side; this also moves msg.changekey on."""
    msg.attach(FileAttachment(name=attachment["name"], content=attachment["content"],
                              content_type=attachment["content_type"]))
    return {"name": attachment["name"], "size": len(attachment["content"])}


def _attach_all(msg: Any, attachments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Attach to a draft that already exists: a failure must not hide its id."""
    added: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for attachment in attachments:
        try:
            added.append(_attach_one(msg, attachment))
        except Exception as exc:
            failed.append({"name": attachment["name"], "error": map_exception(exc).message})
    result: Dict[str, Any] = {}
    if added:
        result["attachments"] = added
    if failed:
        result["attachments_failed"] = failed
    return result


# --- WRITE class (reversible; tier ≥ draft) ----------------------------------


async def _create_draft(ctx: Context, *, body: str, mode: str = "new",
                        reply_to: Optional[str] = None,
                        to: Optional[List[str]] = None,
                        cc: Optional[List[str]] = None,
                        bcc: Optional[List[str]] = None,
                        subject: Optional[str] = None,
                        importance: Optional[str] = None,
                        attachments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if mode not in _DRAFT_MODES:
        raise ToolError("validation", f"mode must be one of {_DRAFT_MODES}, got {mode!r}")
    if mode != "new" and not reply_to:
        raise ToolError("validation", f"reply_to (a message id) is required for mode={mode!r}")
    if mode in ("new", "forward") and not to:
        raise ToolError("validation", f"to is required for mode={mode!r}")
    if importance not in (None, "normal", "high"):
        raise ToolError("validation", "importance must be 'normal' or 'high'")
    html_body = _wrap_html(body)
    decoded = _decode_attachments(attachments or [], ctx.settings)

    def work(account: Any) -> Any:
        if mode == "new":
            msg = Message(
                account=account, folder=account.drafts,
                subject=subject or "", body=html_body,
                to_recipients=list(to),
                cc_recipients=list(cc) if cc else None,
                bcc_recipients=list(bcc) if bcc else None,
            )
            if importance == "high":
                msg.importance = "High"
            msg.save()  # SAVE_ONLY into Drafts — nothing leaves the mailbox
            attached = _attach_all(msg, decoded)
            return (msg.id, getattr(msg, "changekey", None), list(to), list(cc or []),
                    subject or "", attached)
        original = _fetch_one(account, reply_to, only=_REPLY_SOURCE_FIELDS)
        extra: Dict[str, Any] = {}
        reply_all_to: Optional[List[Any]] = None
        reply_all_cc: Optional[List[Any]] = None
        if mode == "reply":
            final_subject = subject or _prefixed("Re: ", getattr(original, "subject", None))
            if to:
                extra["to_recipients"] = list(to)
            if cc:
                extra["cc_recipients"] = list(cc)
            if bcc:
                extra["bcc_recipients"] = list(bcc)
            reply = original.create_reply(final_subject, html_body, **extra)
        elif mode == "reply_all":
            final_subject = subject or _prefixed("Re: ", getattr(original, "subject", None))
            reply = original.create_reply_all(final_subject, html_body)
            reply_all_to, reply_all_cc = _merge_reply_all_recipients(
                reply, ctx.settings.ews_email, to, cc, bcc
            )
        else:  # forward — create_forward REQUIRES to_recipients (5.0.3)
            final_subject = subject or _prefixed("Fwd: ", getattr(original, "subject", None))
            reply = original.create_forward(
                final_subject, html_body, list(to),
                cc_recipients=list(cc) if cc else None,
                bcc_recipients=list(bcc) if bcc else None,
            )
        # Exchange quotes the original server-side, keeping threading headers.
        saved = reply.save(account.drafts)
        if reply_all_to is not None and reply_all_cc is not None:
            prev_to = _email_list(reply_all_to)
            prev_cc = _email_list(reply_all_cc)
        else:
            prev_to = _email_list(getattr(reply, "to_recipients", None)) or list(to or [])
            prev_cc = _email_list(getattr(reply, "cc_recipients", None)) or list(cc or [])
        changekey = getattr(saved, "changekey", None)
        attached: Dict[str, Any] = {}
        if decoded:
            # reply.save() returns a bare id holder that cannot attach.
            draft = _fetch_one(account, saved.id, only=_DRAFT_ATTACH_FIELDS)
            attached = _attach_all(draft, decoded)
            changekey = getattr(draft, "changekey", None)
        return saved.id, changekey, prev_to, prev_cc, final_subject, attached

    raw_id, changekey, prev_to, prev_cc, prev_subject, attached = await ctx.gateway.call(work)
    alias = ctx.aliaser.alias_for(str(raw_id), "d", changekey=changekey)
    return {
        "ok": True,
        "draft_id": alias,
        "folder": "f:drafts",
        "preview": _draft_preview(prev_to, prev_cc, prev_subject, body),
        **attached,
        "note": "saved as draft — NOT sent",
    }


async def _update_draft(ctx: Context, *, draft_id: str,
                        to: Optional[List[str]] = None,
                        cc: Optional[List[str]] = None,
                        bcc: Optional[List[str]] = None,
                        subject: Optional[str] = None,
                        body: Optional[str] = None) -> Dict[str, Any]:
    if to is None and cc is None and bcc is None and subject is None and body is None:
        raise ToolError("validation", "nothing to update: pass to/cc/bcc/subject/body")

    def work(account: Any) -> Dict[str, Any]:
        msg = _fetch_one(account, draft_id, only=_DRAFT_UPDATE_FIELDS)
        _require_in_drafts(msg, account, "update_draft")
        changed: List[str] = []  # update_fields uses exchangelib field names
        for value, field in ((to, "to_recipients"), (cc, "cc_recipients"),
                             (bcc, "bcc_recipients")):
            if value is not None:
                setattr(msg, field, list(value))
                changed.append(field)
        if subject is not None:
            msg.subject = subject
            changed.append("subject")
        if body is not None:
            msg.body = _wrap_html(body)
            changed.append("body")
        msg.save(update_fields=changed)
        prev_subject = msg.subject if isinstance(msg.subject, str) else None
        return {
            "to": _email_list(getattr(msg, "to_recipients", None)),
            "cc": _email_list(getattr(msg, "cc_recipients", None)),
            "subject": prev_subject,
            "body_snippet": (body or "")[:200] if body is not None else None,
        }

    async with _draft_lock(draft_id):
        preview = await ctx.gateway.call(work)
    return {"ok": True, "draft_id": ctx.aliaser.alias_for(draft_id, "d"),
            "preview": preview}


async def _add_draft_attachment(
    ctx: Context,
    *,
    draft_id: str,
    filename: Optional[str] = None,
    content_base64: Optional[str] = None,
    path: Optional[str] = None,
    content_type: Optional[str] = None,
) -> Dict[str, Any]:
    raw_item: Dict[str, Any] = {"content_type": content_type}
    if filename is not None:
        raw_item["filename"] = filename
    if content_base64 is not None:
        raw_item["content_base64"] = content_base64
    if path is not None:
        raw_item["path"] = path
    attachment = _decode_attachments([raw_item], ctx.settings)[0]

    def work(account: Any) -> Any:
        msg = _fetch_one(account, draft_id, only=_DRAFT_ATTACH_FIELDS)
        _require_in_drafts(msg, account, "add_draft_attachment")
        added = _attach_one(msg, attachment)
        return added, _attachment_list(msg), getattr(msg, "changekey", None)

    async with _draft_lock(draft_id):
        added, on_draft, changekey = await ctx.gateway.call(work)
    return {"ok": True, "draft_id": ctx.aliaser.alias_for(draft_id, "d", changekey=changekey),
            "added": added, "attachments": on_draft, "note": "saved as draft — NOT sent"}


async def _delete_draft(ctx: Context, *, draft_id: str) -> Dict[str, Any]:
    def work(account: Any) -> Dict[str, Any]:
        msg = _fetch_one(account, draft_id, only=_DRAFT_DELETE_FIELDS)
        _require_in_drafts(msg, account, "delete_draft")
        msg.move_to_trash()  # reversible — hence write class, no confirm
        return {"deleted": True}

    async with _draft_lock(draft_id):
        return await ctx.gateway.call(work)


async def _update_messages(ctx: Context, *, ids: List[str],
                           set_read: Optional[bool] = None,
                           categories_add: Optional[List[str]] = None,
                           categories_remove: Optional[List[str]] = None,
                           flag_complete: Optional[bool] = None) -> Dict[str, Any]:
    _require_bulk(ids)
    if (set_read is None and not categories_add and not categories_remove
            and flag_complete is None):
        raise ToolError(
            "validation",
            "nothing to update: pass set_read, categories_add/_remove, and/or flag_complete")
    remove = set(categories_remove or [])
    ok_ids: List[str] = []
    final_cats: List[tuple] = []

    def work(account: Any) -> Dict[str, Any]:
        updated, failed = 0, []
        fetched = _fetch_many(
            account, ids,
            only=["id", "changekey", "is_read", "categories", "flag_status"])
        for raw_id, item in zip(ids, fetched):
            try:  # per-item isolation: one failure never aborts the batch
                if isinstance(item, Exception):
                    raise item
                changed: List[str] = []
                if set_read is not None:
                    item.is_read = bool(set_read)
                    changed.append("is_read")
                if categories_add or remove:
                    cats = [c for c in (item.categories or []) if c not in remove]
                    for c in categories_add or []:
                        if c not in cats:
                            cats.append(c)
                    item.categories = cats or None
                    changed.append("categories")
                    final_cats.append((raw_id, cats))
                if flag_complete is True:
                    # 1 = flagged and marked Complete; None = not flagged.
                    # Deliberately does not write state 2 (flagged, still
                    # open) — this tool only marks/unmarks completion.
                    item.flag_status = 1
                    changed.append("flag_status")
                elif flag_complete is False and getattr(item, "flag_status", None) == 1:
                    # Unmark completion ONLY when currently Complete (1); leave
                    # open follow-up flags (2) and unflagged items untouched.
                    item.flag_status = None
                    changed.append("flag_status")
                if changed:
                    item.save(update_fields=changed)
                    updated += 1
                    ok_ids.append(raw_id)
            except Exception as exc:
                failed.append(_failed_entry(ctx, raw_id, exc))
        return {"updated": updated, "failed": failed}

    result = await ctx.gateway.call(work)
    if ok_ids and set_read is not None:
        _write_through(ctx, "set_read_flag", ok_ids, bool(set_read))
    for raw_id, cats in final_cats:
        _write_through(ctx, "apply_categories", raw_id, cats)
    return result


async def _move_messages(ctx: Context, *, ids: List[str],
                         to_folder: str) -> Dict[str, Any]:
    _require_bulk(ids)

    moved_old_ids: List[str] = []

    def work(account: Any) -> Dict[str, Any]:
        folder = ctx.gateway.resolve_folder(account, to_folder, ctx.aliaser)
        moved, failed = [], []
        fetched = _fetch_many(account, ids, only=["id", "changekey"])
        for raw_id, item in zip(ids, fetched):
            try:
                if isinstance(item, Exception):
                    raise item
                item.move(folder)  # returns None; updates item.id in place (5.0.3)
                # 5.0.3 may leave item.id None when the response carries no
                # new id — never crash on it; fall back to the old binding.
                new_raw = str(item.id) if getattr(item, "id", None) else None
                alias = None
                if new_raw and new_raw != raw_id:
                    # EWS ids change on move — rebind so the model's alias
                    # keeps working (DESIGN.md §Ids).
                    alias = ctx.aliaser.rebind(raw_id, new_raw)
                if alias is None:
                    alias = ctx.aliaser.alias_for(new_raw or raw_id, "m")
                row: Dict[str, Any] = {"id": alias}
                if new_raw is None:
                    row["note"] = ("moved, but Exchange returned no new id — "
                                   "re-search before reusing this id")
                moved.append(row)
                moved_old_ids.append(raw_id)
            except Exception as exc:
                failed.append(_failed_entry(ctx, raw_id, exc))
        return {"moved": len(moved), "items": moved, "failed": failed}

    result = await ctx.gateway.call(work)
    # Tombstone the moved rows: their old ids are gone from the source
    # folder; the delta sync re-adds them under the new id if the target
    # folder is mirrored.
    _write_through(ctx, "tombstone_messages", moved_old_ids)
    return result


def _check_reminder(minutes: Any, minimum: int) -> None:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < minimum:
        raise ToolError("validation", f"reminder_minutes must be a whole number of at least {minimum}")


def _series_time(value: datetime, tz_name: str) -> datetime:
    """A series keeps its wall-clock time only in a named zone. A fixed offset
    such as +01:00 would move every occurrence by an hour across a DST change."""
    return value.astimezone(ZoneInfo(tz_name))


def _build_recurrence(spec: Dict[str, Any], start: datetime) -> Recurrence:
    """Translate the tool's ``recurrence`` argument. ``start`` is the first
    occurrence, in the series' zone; the series begins on that date."""
    frequency = spec.get("frequency")
    interval = spec.get("interval", 1)
    weekdays = spec.get("weekdays")
    until = spec.get("until")
    count = spec.get("count")
    first_day = start.date()

    if not isinstance(interval, int) or not 1 <= interval <= 99:
        raise ToolError("validation", "recurrence.interval must be between 1 and 99")
    if weekdays and frequency != "weekly":
        raise ToolError("validation", "recurrence.weekdays requires frequency 'weekly'")

    if frequency == "daily":
        pattern = DailyPattern(interval=interval)
    elif frequency == "weekly":
        names = [str(day).lower() for day in weekdays or [_WEEKDAYS[first_day.weekday()]]]
        unknown = [name for name in names if name not in _WEEKDAYS]
        if unknown:
            raise ToolError("validation", f"recurrence.weekdays: unknown day {unknown[0]!r}")
        if _WEEKDAYS[first_day.weekday()] not in names:
            raise ToolError(
                "validation",
                f"start falls on a {_WEEKDAYS[first_day.weekday()]}, which is not in "
                "recurrence.weekdays",
                hint="Use the date of the first occurrence as start.",
            )
        # exchangelib weekdays are ISO numbers, 1 = Monday.
        pattern = WeeklyPattern(interval=interval,
                                weekdays=sorted({_WEEKDAYS.index(n) + 1 for n in names}),
                                first_day_of_week=1)
    elif frequency == "monthly":
        pattern = AbsoluteMonthlyPattern(interval=interval, day_of_month=first_day.day)
    else:
        raise ToolError("validation",
                        "recurrence.frequency must be 'daily', 'weekly' or 'monthly'")

    if until is not None and count is not None:
        raise ToolError("validation", "pass recurrence.until or recurrence.count, not both")
    if count is not None:
        if not isinstance(count, int) or count < 1:
            raise ToolError("validation", "recurrence.count must be at least 1")
        return Recurrence(pattern=pattern, start=first_day, number=count)
    if until is not None:
        try:
            end_day = date.fromisoformat(str(until))
        except ValueError:
            raise ToolError("validation",
                            f"recurrence.until: cannot parse {until!r} as YYYY-MM-DD") from None
        if end_day < first_day:
            raise ToolError("validation", "recurrence.until is before the first occurrence")
        return Recurrence(pattern=pattern, start=first_day, end=end_day)
    return Recurrence(pattern=pattern, start=first_day)


async def _create_event(ctx: Context, *, subject: str, start: str, end: str,
                        attendees: Optional[List[str]] = None,
                        location: Optional[str] = None,
                        body: Optional[str] = None,
                        reminder_minutes: Optional[int] = None,
                        recurrence: Optional[Dict[str, Any]] = None,
                        send_invitations: bool = False) -> Dict[str, Any]:
    # The ONE handler-side safety check in this pack, by design: this tool is
    # class "write" so a no-invite event stays available at draft tier, but
    # the dispatcher kill-switch only covers class "send". Invitations leave
    # the org, so the same switch must hold here. (Confirm is already gated
    # by the dispatcher via confirm=lambda kw: send_invitations.)
    if send_invitations and not ctx.settings.send_enabled:
        raise ToolError(
            "kill_switch",
            "create_event with send_invitations=true is blocked: "
            "SEND_ENABLED=false on this server.",
            hint="Save the event without invitations: the attendees stay on it and "
                 "the organizer can send it from Outlook. Or have the operator flip "
                 "SEND_ENABLED.",
        )
    start_dt = parse_when(start, "start", ctx.settings.ews_tz)
    end_dt = parse_when(end, "end", ctx.settings.ews_tz)
    if end_dt <= start_dt:
        raise ToolError("validation", "end must be after start")
    if reminder_minutes is not None:
        _check_reminder(reminder_minutes, 0)
    rule = None
    if recurrence is not None:
        start_dt = _series_time(start_dt, ctx.settings.ews_tz)
        end_dt = _series_time(end_dt, ctx.settings.ews_tz)
        rule = _build_recurrence(recurrence, start_dt)

    def work(account: Any) -> str:
        item = CalendarItem(account=account, folder=account.calendar,
                            subject=subject, start=start_dt, end=end_dt)
        if attendees:
            # AttendeesField coerces plain email strings to Attendee (5.0.3).
            item.required_attendees = list(attendees)
        if location:
            item.location = location
        if body is not None:
            item.body = _wrap_html(body)
        if reminder_minutes is not None:
            item.reminder_is_set = True
            item.reminder_minutes_before_start = reminder_minutes
        if rule is not None:
            item.recurrence = rule
        item.save(send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY
                  if send_invitations else SEND_TO_NONE)
        return str(item.id)

    raw_id = await ctx.gateway.call(work)
    result: Dict[str, Any] = {"ok": True, "event_id": ctx.aliaser.alias_for(raw_id, "e"),
                              "invitations_sent": bool(send_invitations)}
    if reminder_minutes is not None:
        result["reminder_minutes"] = reminder_minutes
    if rule is not None:
        result["recurring"] = True
    if attendees and not send_invitations:
        result["invitations_pending"] = True
        result["note"] = (
            "Saved like a draft: the attendees are on the event, but nobody was invited. "
            "The organizer can open it in Outlook and send it."
        )
    return result


async def _update_event(ctx: Context, *, event_id: str,
                        subject: Optional[str] = None,
                        start: Optional[str] = None,
                        end: Optional[str] = None,
                        location: Optional[str] = None,
                        body: Optional[str] = None,
                        reminder_minutes: Optional[int] = None,
                        scope: Optional[str] = None,
                        notify_attendees: bool = False) -> Dict[str, Any]:
    # Same documented exception as create_event: write-class for tier, but
    # notifications leave the org → the send kill-switch applies.
    if notify_attendees and not ctx.settings.send_enabled:
        raise ToolError(
            "kill_switch",
            "update_event with notify_attendees=true is blocked: "
            "SEND_ENABLED=false on this server.",
            hint="Update without notifying, or have the operator flip SEND_ENABLED.",
        )
    if all(v is None for v in (subject, start, end, location, body, reminder_minutes)):
        raise ToolError("validation",
                        "nothing to update: pass subject/start/end/location/body/reminder_minutes")
    if reminder_minutes is not None:
        _check_reminder(reminder_minutes, -1)
    if scope not in (None, "occurrence", "series"):
        raise ToolError("validation", "scope must be 'occurrence' or 'series'")
    start_dt = parse_when(start, "start", ctx.settings.ews_tz) if start else None
    end_dt = parse_when(end, "end", ctx.settings.ews_tz) if end else None
    if scope == "series":
        start_dt = _series_time(start_dt, ctx.settings.ews_tz) if start_dt else None
        end_dt = _series_time(end_dt, ctx.settings.ews_tz) if end_dt else None

    def work(account: Any) -> Dict[str, Any]:
        item = _fetch_one(account, event_id)
        kind = getattr(item, "type", None)
        if scope == "series":
            if kind in _OCCURRENCE_TYPES:
                item = item.recurring_master()
                item.refresh()
            elif kind != _SERIES_TYPE:
                raise ToolError("validation", "this event is not part of a series",
                                hint="Omit scope to update a single event.")
            # A master's start and end are those of its first occurrence.
            if isinstance(item.start, datetime):
                first_day: Optional[date] = item.start.astimezone(
                    ZoneInfo(ctx.settings.ews_tz)
                ).date()
            elif isinstance(item.start, date):
                first_day = date(item.start.year, item.start.month, item.start.day)
            else:
                first_day = None
            for value, field in ((start_dt, "start"), (end_dt, "end")):
                if first_day is not None and value is not None and value.date() != first_day:
                    raise ToolError(
                        "validation",
                        f"{field} must be on the series' first date, {first_day.isoformat()}, "
                        "to change the time of all occurrences",
                        hint="get_event shows it as recurrence.first_date.",
                    )
        elif kind == _SERIES_TYPE:
            raise ToolError("validation", "this id is the whole series",
                            hint="Pass scope='series', or an occurrence id from list_events "
                                 "to change a single occurrence.")
        changed: List[str] = []
        for value, field in ((subject, "subject"), (start_dt, "start"),
                             (end_dt, "end"), (location, "location")):
            if value is not None:
                setattr(item, field, value)
                changed.append(field)
        if body is not None:
            item.body = _wrap_html(body)
            changed.append("body")
        if reminder_minutes is not None:
            item.reminder_is_set = reminder_minutes >= 0
            changed.append("reminder_is_set")
            if reminder_minutes >= 0:
                item.reminder_minutes_before_start = reminder_minutes
                changed.append("reminder_minutes_before_start")
        item.save(update_fields=changed,
                  send_meeting_invitations=SEND_TO_CHANGED_AND_SAVE_COPY
                  if notify_attendees else SEND_TO_NONE)
        return {"updated_fields": changed, "attendees_notified": bool(notify_attendees)}

    return await ctx.gateway.call(work)


# --- SEND class (leaves the mailbox; tier full, two-phase, kill-switch) ------


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


def _attachment_list(msg: Any) -> List[Dict[str, Any]]:
    listed = [{"name": getattr(a, "name", None), "size": getattr(a, "size", None)}
              for a in (getattr(msg, "attachments", None) or [])]
    return sorted(listed, key=lambda a: str(a["name"]))


def _bound_attachments(msg: Any) -> List[Dict[str, Any]]:
    bound: List[Dict[str, Any]] = []
    for a in getattr(msg, "attachments", None) or []:
        entry: Dict[str, Any] = {
            "name": getattr(a, "name", None),
            "size": getattr(a, "size", None),
        }
        att_id = _attachment_id(a)
        if att_id is not None:
            entry["id"] = att_id
        bound.append(entry)
    return sorted(bound, key=lambda a: (str(a["name"]), str(a.get("id") or "")))


def _draft_content(msg: Any) -> Dict[str, Any]:
    """The draft fields the confirm token binds and the preview shows.

    Recipients are sorted so reordering doesn't invalidate a token, but
    adding/removing one does (a content change worth re-previewing).
    """
    subject = getattr(msg, "subject", None)
    raw_body = getattr(msg, "body", None)
    body_text = getattr(msg, "text_body", None)
    if not isinstance(body_text, str):
        body_text = str(raw_body) if raw_body is not None else ""
    return {
        "subject": subject if isinstance(subject, str) else "",
        "to": sorted(_email_list(getattr(msg, "to_recipients", None))),
        "cc": sorted(_email_list(getattr(msg, "cc_recipients", None))),
        "bcc": sorted(_email_list(getattr(msg, "bcc_recipients", None))),
        "body_text": body_text,
        "_body_html": str(raw_body) if isinstance(raw_body, str) else "",
        "attachment_count": len(getattr(msg, "attachments", None) or []),
        "attachments": _attachment_list(msg),
        "_attachments": _bound_attachments(msg),
    }


async def _send_draft_preview(ctx: Context, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Confirm-gate hook: fetch the draft's CURRENT content (both phases).

    Phase 1 hashes it into the token and shows it as the preview; phase 2
    refetches and the hash mismatch kills the token if the draft changed in
    between (update_draft TOCTOU) — ported from the v3.5 implementation.
    """
    draft_id = kwargs.get("draft_id")
    if not isinstance(draft_id, str) or not draft_id:
        raise ToolError("validation", "draft_id is required")
    try:
        raw_id = ctx.aliaser.resolve(draft_id)
    except KeyError as e:
        raise ToolError("validation", str(e.args[0] if e.args else e))

    def work(account: Any) -> Dict[str, Any]:
        msg = _fetch_one(account, raw_id, only=_DRAFT_PREVIEW_FIELDS)
        _require_in_drafts(msg, account, "send_draft")
        return _draft_content(msg)

    content = await ctx.gateway.call(work)
    content["draft_id"] = draft_id  # echo the caller's handle in the preview
    return content


def _send_confirm_needed(kwargs: Dict[str, Any]) -> bool:
    """Skip the confirm gate on an idempotent REPLAY (v3.5 semantics).

    A retry with the same idempotency_key + draft_id returns the cached
    receipt without sending anything, so demanding a fresh (single-use)
    token there would break exactly the retry-after-timeout case the key
    exists for. A mismatched draft_id still goes through the gate and is
    rejected by the handler.
    """
    key = kwargs.get("idempotency_key")
    if isinstance(key, str) and key:
        prior = _idempotency_get(key)
        if prior is not None and prior.get("draft_id") == kwargs.get("draft_id"):
            return False
    return True


_STALE_DRAFT_HINT = (
    "The content changed since the preview (or the arguments differ) — "
    "re-preview to get a token bound to the current content."
)


async def _send_draft(
    ctx: Context,
    *,
    draft_id: str,
    idempotency_key: Optional[str] = None,
    _verified_content_hash: Optional[str] = None,
) -> Dict[str, Any]:
    if idempotency_key:
        prior = _idempotency_get(idempotency_key)
        if prior is not None:
            if prior["draft_id"] != draft_id:
                raise ToolError(
                    "validation",
                    "idempotency_key was already used for a different draft",
                    hint="Use a fresh idempotency_key per distinct send.",
                )
            return {**prior["result"], "replayed": True}

    if not isinstance(_verified_content_hash, str) or not _verified_content_hash:
        raise ToolError(
            "confirm_invalid",
            "confirm_token rejected (stale): the draft changed after the preview",
            hint=_STALE_DRAFT_HINT,
        )

    def work(account: Any) -> Dict[str, Any]:
        msg = _fetch_one(account, draft_id, only=_DRAFT_SEND_FIELDS)
        _require_in_drafts(msg, account, "send_draft")
        current_hash = preview_content_hash(_draft_content(msg))
        if current_hash != _verified_content_hash:
            raise ToolError(
                "confirm_invalid",
                "confirm_token rejected (stale): the draft changed after the preview",
                hint=_STALE_DRAFT_HINT,
            )
        # Draft with an id → SendItem; Exchange moves it from Drafts to Sent
        # on the SAME fetched object whose content hash was just verified.
        msg.send(save_copy=True)
        imid = getattr(msg, "message_id", None)
        return {"sent": True,
                "internet_message_id": imid if isinstance(imid, str) else None}

    async with _draft_lock(draft_id):
        result = await ctx.gateway.call(work)
    if idempotency_key:
        _idempotency_put(idempotency_key, {"draft_id": draft_id,
                                           "result": dict(result),
                                           "ts": time.time()})
    return result


async def _respond_to_event(ctx: Context, *, event_id: str, response: str,
                            message: Optional[str] = None) -> Dict[str, Any]:
    method = _RESPONSE_METHOD.get(response)
    if method is None:
        raise ToolError("validation",
                        f"response must be one of {sorted(_RESPONSE_METHOD)}, got {response!r}")

    def work(account: Any) -> Dict[str, Any]:
        item = _fetch_one(account, event_id)
        kwargs = {"body": message} if message else {}
        # 5.0.3: accept()/tentatively_accept()/decline() build the meeting
        # reply item and send it to the organizer.
        getattr(item, method)(**kwargs)
        return {"responded": response}

    return await ctx.gateway.call(work)


async def _set_oof(ctx: Context, *, state: str,
                   internal_reply: Optional[str] = None,
                   external_reply: Optional[str] = None,
                   start: Optional[str] = None,
                   end: Optional[str] = None) -> Dict[str, Any]:
    target_state = _OOF_STATE.get(state)
    if target_state is None:
        raise ToolError("validation",
                        f"state must be one of {sorted(_OOF_STATE)}, got {state!r}")
    if state != "disabled" and not internal_reply:
        raise ToolError("validation", f"internal_reply is required when state={state!r}")
    if state == "scheduled":
        if not start or not str(start).strip():
            raise ToolError("validation", "'start' is required when state='scheduled'")
        if not end or not str(end).strip():
            raise ToolError("validation", "'end' is required when state='scheduled'")
    start_dt = parse_when(start, "start", ctx.settings.ews_tz) if start else None
    end_dt = parse_when(end, "end", ctx.settings.ews_tz) if end else None
    if start_dt is not None and end_dt is not None and start_dt >= end_dt:
        raise ToolError("validation", "'start' must be before 'end'")
    if state == "scheduled" and end_dt is not None and end_dt <= datetime.now(timezone.utc):
        raise ToolError("validation", "'end' must be in the future")
    # from_datetime() accepts only a plain datetime, and astimezone() onto an
    # EWSTimeZone already returns an EWSDateTime: convert first, then shift.
    oof_start = EWSDateTime.from_datetime(start_dt).astimezone(UTC) if start_dt else None
    oof_end = EWSDateTime.from_datetime(end_dt).astimezone(UTC) if end_dt else None
    start_iso = fmt_dt(start_dt, ctx.settings.ews_tz) if state == "scheduled" else None
    end_iso = fmt_dt(end_dt, ctx.settings.ews_tz) if state == "scheduled" else None

    def work(account: Any) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"state": target_state}
        if internal_reply is not None:
            # 5.0.3: plain strings — the OofReply wrapper class is gone.
            kwargs["internal_reply"] = internal_reply
            # OofSettings.clean requires BOTH replies when state != disabled.
            kwargs["external_reply"] = external_reply or internal_reply
        if oof_start is not None:
            kwargs["start"] = oof_start
        if oof_end is not None:
            kwargs["end"] = oof_end
        account.oof_settings = OofSettings(**kwargs)
        result: Dict[str, Any] = {"state": state}
        if state == "scheduled":
            result["start"] = start_iso
            result["end"] = end_iso
        return result

    return await ctx.gateway.call(work)


# --- DESTRUCTIVE class (tier full) -------------------------------------------


async def _cancel_event(ctx: Context, *, event_id: str,
                        message: Optional[str] = None) -> Dict[str, Any]:
    def work(account: Any) -> Dict[str, Any]:
        item = _fetch_one(account, event_id)
        kwargs = {"new_body": message} if message else {}
        # 5.0.3 CalendarItem.cancel() exists (CancelCalendarItem service);
        # Exchange itself rejects the call when we are not the organizer.
        item.cancel(**kwargs)
        return {"cancelled": True}

    return await ctx.gateway.call(work)


async def _delete_messages(ctx: Context, *, ids: List[str],
                           disposition: str = "trash") -> Dict[str, Any]:
    if disposition not in ("trash", "soft", "permanent"):
        raise ToolError("validation",
                        "disposition must be 'trash', 'soft' or 'permanent'")
    _require_bulk(ids)

    deleted_ids: List[str] = []

    def work(account: Any) -> Dict[str, Any]:
        deleted, failed = 0, []
        fetched = _fetch_many(account, ids, only=["id", "changekey"])
        for raw_id, item in zip(ids, fetched):
            try:
                if isinstance(item, Exception):
                    raise item
                if disposition == "trash":
                    item.move_to_trash()
                elif disposition == "soft":
                    item.soft_delete()
                else:
                    # 5.0.3: Item.delete() IS HardDelete — no disposal kwarg.
                    item.delete()
                deleted += 1
                deleted_ids.append(raw_id)
            except Exception as exc:
                failed.append(_failed_entry(ctx, raw_id, exc))
        return {"deleted": deleted, "disposition": disposition, "failed": failed}

    result = await ctx.gateway.call(work)
    _write_through(ctx, "tombstone_messages", deleted_ids)
    return result


# --- schemas ------------------------------------------------------------------


def _obj(props: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": props,
            "required": required or [], "additionalProperties": False}


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_EMAILS = {"type": "array", "items": {"type": "string"}}
_RECURRENCE = {
    "type": "object",
    "description": (
        "Repeat the event. start and end are the first occurrence; the time of day "
        "stays the same across DST changes. Without until or count the series has no end."
    ),
    "properties": {
        "frequency": {"type": "string", "enum": ["daily", "weekly", "monthly"]},
        "interval": {"type": "integer", "minimum": 1, "maximum": 99, "default": 1,
                     "description": "Every N days, weeks or months."},
        "weekdays": {"type": "array", "items": {"type": "string", "enum": list(_WEEKDAYS)},
                     "description": "Weekly only; must include the weekday of start. "
                                    "Default: the weekday of start."},
        "until": {"type": "string", "description": "Last possible date, YYYY-MM-DD."},
        "count": {"type": "integer", "minimum": 1, "description": "Number of occurrences."},
    },
    "required": ["frequency"],
    "additionalProperties": False,
}
_IDS = {"type": "array", "items": {"type": "string"},
        "minItems": 1, "maxItems": MAX_BULK_IDS}
_ATTACHMENT_FIELDS = {
    "filename": {
        "type": "string",
        "description": "File name; required with content_base64, defaults to the "
                       "file's own name when path is given.",
    },
    "content_base64": {
        "type": "string",
        "description": "The file content, base64-encoded (mutually exclusive with path).",
    },
    "path": {
        "type": "string",
        "description": "Relative file path inside EWS_ATTACHMENT_DIR "
                       "(mutually exclusive with content_base64).",
    },
    "content_type": {"type": "string",
                     "description": "MIME type; derived from the file name if omitted."},
}
_ATTACHMENTS = {"type": "array", "maxItems": MAX_ATTACHMENTS,
                "items": _obj(_ATTACHMENT_FIELDS)}


# --- the pack -----------------------------------------------------------------

TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="create_draft",
        description=(
            "Author mail as a DRAFT (never sends). mode=new needs to+body; "
            "reply/reply_all/forward need reply_to (the original message id) "
            "and quote the original server-side; forward also needs to. "
            "body is plain text (the server wraps it in minimal HTML). "
            "importance applies to mode=new only. attachments adds files; if one "
            "fails the draft still exists and attachments_failed names it. "
            "Use send_draft to send."
        ),
        side_effect_class="write",
        input_schema=_obj({
            "mode": {"type": "string", "enum": list(_DRAFT_MODES), "default": "new"},
            "reply_to": _STR, "to": _EMAILS, "cc": _EMAILS, "bcc": _EMAILS,
            "subject": _STR, "body": _STR,
            "importance": {"type": "string", "enum": ["normal", "high"]},
            "attachments": _ATTACHMENTS,
        }, required=["body"]),
        handler=_create_draft,
        confirm=False,
    ),
    ToolSpec(
        name="update_draft",
        description=("Update fields of an existing draft (to/cc/bcc/subject/body). "
                     "Only the supplied fields change."),
        side_effect_class="write",
        input_schema=_obj({
            "draft_id": _STR, "to": _EMAILS, "cc": _EMAILS, "bcc": _EMAILS,
            "subject": _STR, "body": _STR,
        }, required=["draft_id"]),
        handler=_update_draft,
        confirm=False,
    ),
    ToolSpec(
        name="add_draft_attachment",
        description=("Add one file to an existing draft (via content_base64 or "
                     "path inside EWS_ATTACHMENT_DIR). Refuses items that are "
                     "not in f:drafts. Returns all attachments of the draft."),
        side_effect_class="write",
        input_schema=_obj({"draft_id": _STR, **_ATTACHMENT_FIELDS},
                          required=["draft_id"]),
        handler=_add_draft_attachment,
        confirm=False,
    ),
    ToolSpec(
        name="delete_draft",
        description=("Move a draft to trash (recoverable). Refuses items that "
                     "are not in f:drafts."),
        side_effect_class="write",
        input_schema=_obj({"draft_id": _STR}, required=["draft_id"]),
        handler=_delete_draft,
        confirm=False,
    ),
    ToolSpec(
        name="update_messages",
        description=(
            "Bulk-update up to 50 messages: set_read, categories_add/"
            "categories_remove, and/or flag_complete. Per-item failures are "
            "isolated and reported. flag_complete marks/unmarks the message's "
            "follow-up flag as Complete (true) or clears completion (false) - "
            "it does not raise a NEW follow-up flag on an unflagged message, "
            "only sets/clears the completion state."
        ),
        side_effect_class="write",
        input_schema=_obj({
            "ids": _IDS, "set_read": _BOOL,
            "categories_add": _EMAILS, "categories_remove": _EMAILS,
            "flag_complete": _BOOL,
        }, required=["ids"]),
        handler=_update_messages,
        confirm=False,
    ),
    ToolSpec(
        name="move_messages",
        description=("Move up to 50 messages to a folder (id, f:alias or path). "
                     "Message ids are re-bound automatically: the SAME id keeps "
                     "working after the move."),
        side_effect_class="write",
        input_schema=_obj({"ids": _IDS, "to_folder": _STR},
                          required=["ids", "to_folder"]),
        handler=_move_messages,
        confirm=False,
    ),
    ToolSpec(
        name="create_event",
        description=(
            "Create a calendar event or, with recurrence, a recurring series. "
            "Default send_invitations=false saves it like a draft WITHOUT emailing "
            "attendees (no confirm needed): they stay on the event, and the organizer "
            "can send it from Outlook. With send_invitations=true, invitations leave "
            "the org: two-phase confirm + send kill-switch apply. Without "
            "reminder_minutes the event has no reminder."
        ),
        side_effect_class="write",  # tier: draft; invite path confirm-gated below
        input_schema=_obj({
            "subject": _STR, "start": _STR, "end": _STR,
            "attendees": _EMAILS, "location": _STR, "body": _STR,
            "reminder_minutes": {"type": "integer", "minimum": 0,
                                 "description": "Remind this many minutes before the start; "
                                                "0 is at the start."},
            "recurrence": _RECURRENCE,
            "send_invitations": {"type": "boolean", "default": False},
        }, required=["subject", "start", "end"]),
        handler=_create_event,
        confirm=lambda kw: bool(kw.get("send_invitations")),
    ),
    ToolSpec(
        name="update_event",
        description=(
            "Update event fields (subject/start/end/location/body/reminder). An occurrence id "
            "from list_events changes that occurrence only; scope='series' changes "
            "the whole series and overwrites those fields on occurrences changed "
            "individually before. Default notify_attendees=false is a silent metadata "
            "edit; true notifies changed attendees → two-phase confirm + send "
            "kill-switch apply."
        ),
        side_effect_class="write",
        input_schema=_obj({
            "event_id": _STR, "subject": _STR, "start": _STR, "end": _STR,
            "location": _STR, "body": _STR,
            "reminder_minutes": {"type": "integer", "minimum": -1,
                                 "description": "Remind this many minutes before the start; "
                                                "0 is at the start, -1 removes the reminder."},
            "scope": {"type": "string", "enum": ["occurrence", "series"],
                      "description": "For recurring events. With 'series', start and end "
                                     "must be on the series' first date."},
            "notify_attendees": {"type": "boolean", "default": False},
        }, required=["event_id"]),
        handler=_update_event,
        confirm=lambda kw: bool(kw.get("notify_attendees")),
    ),
    ToolSpec(
        name="send_draft",
        description=(
            "Send an existing draft (the ONLY way mail leaves this mailbox). "
            "Two-phase: the first call fetches the draft and returns its REAL "
            "recipients/subject/body/attachments as a preview + confirm_token bound to "
            "that content; the second call with the token re-verifies the "
            "content and sends (editing the draft in between invalidates the "
            "token). Optional idempotency_key dedupes retries (replays return "
            "the stored result)."
        ),
        side_effect_class="send",
        input_schema=_obj({"draft_id": _STR, "idempotency_key": _STR},
                          required=["draft_id"]),
        handler=_send_draft,
        confirm=_send_confirm_needed,
        preview=_send_draft_preview,
    ),
    ToolSpec(
        name="respond_to_event",
        description=("Accept/tentative/decline a meeting — this SENDS a response "
                     "to the organizer, so it is two-phase confirmed."),
        side_effect_class="send",
        input_schema=_obj({
            "event_id": _STR,
            "response": {"type": "string", "enum": sorted(_RESPONSE_METHOD)},
            "message": _STR,
        }, required=["event_id", "response"]),
        handler=_respond_to_event,
        confirm=True,
    ),
    ToolSpec(
        name="cancel_event",
        description=("Cancel a meeting you organize — sends cancellations to all "
                     "attendees (destructive, two-phase confirmed)."),
        side_effect_class="destructive",
        input_schema=_obj({"event_id": _STR, "message": _STR},
                          required=["event_id"]),
        handler=_cancel_event,
        confirm=True,
    ),
    ToolSpec(
        name="delete_messages",
        description=(
            "Delete up to 50 messages. disposition: 'trash' (default, "
            "recoverable), 'soft' (dumpster) or 'permanent' (UNRECOVERABLE — "
            "requires two-phase confirm). Class is destructive, so ALL "
            "dispositions need the full tier in v5.0 (conservative)."
        ),
        side_effect_class="destructive",
        input_schema=_obj({
            "ids": _IDS,
            "disposition": {"type": "string",
                            "enum": ["trash", "soft", "permanent"],
                            "default": "trash"},
        }, required=["ids"]),
        handler=_delete_messages,
        confirm=lambda kw: kw.get("disposition") == "permanent",
    ),
    ToolSpec(
        name="set_oof",
        description=(
            "Set out-of-office auto-replies (externally visible → send class, "
            "two-phase confirmed). state: disabled|enabled|scheduled; "
            "scheduled needs start+end. external_reply defaults to "
            "internal_reply when omitted."
        ),
        side_effect_class="send",
        input_schema=_obj({
            "state": {"type": "string", "enum": sorted(_OOF_STATE)},
            "internal_reply": _STR, "external_reply": _STR,
            "start": _STR, "end": _STR,
        }, required=["state"]),
        handler=_set_oof,
        confirm=True,
    ),
]
