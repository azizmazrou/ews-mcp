"""Tool pack: writes — the 12 mutating tools (DESIGN.md §Tools, §Safety).

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
- Follow-up flag completion IS available, via ExtendedProperty
  registration (PidTagFlagStatus, tag 0x1090) - see ../flag_property.py
  for the live-verified read/write/filter proof. update_messages'
  ``flag_complete`` param uses it; this superseded an earlier assumption
  here that it was unavailable.
"""

import html as _html
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from exchangelib import CalendarItem, HTMLBody, Message, OofSettings
from exchangelib.items import (
    SEND_TO_ALL_AND_SAVE_COPY,
    SEND_TO_CHANGED_AND_SAVE_COPY,
    SEND_TO_NONE,
)

from ..flag_property import ensure_registered as ensure_flag_status_registered

ensure_flag_status_registered()

from ..dates import parse_when
from ..errors import ToolError
from .base import Context, ToolSpec

logger = logging.getLogger(__name__)

MAX_BULK_IDS = 50


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


def _prefixed(prefix: str, original_subject: Any) -> str:
    base = original_subject if isinstance(original_subject, str) else ""
    if base.lower().startswith(prefix.strip().lower()):
        return base
    return f"{prefix}{base}"


def _fetch_one(account: Any, raw_id: str) -> Any:
    """account.fetch yields items in input order; missing ids surface as
    in-line exception instances (exchangelib bulk semantics)."""
    fetched = list(account.fetch([(raw_id, None)]))
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


# --- WRITE class (reversible; tier ≥ draft) ----------------------------------


async def _create_draft(ctx: Context, *, body: str, mode: str = "new",
                        reply_to: Optional[str] = None,
                        to: Optional[List[str]] = None,
                        cc: Optional[List[str]] = None,
                        bcc: Optional[List[str]] = None,
                        subject: Optional[str] = None,
                        importance: Optional[str] = None) -> Dict[str, Any]:
    if mode not in _DRAFT_MODES:
        raise ToolError("validation", f"mode must be one of {_DRAFT_MODES}, got {mode!r}")
    if mode != "new" and not reply_to:
        raise ToolError("validation", f"reply_to (a message id) is required for mode={mode!r}")
    if mode in ("new", "forward") and not to:
        raise ToolError("validation", f"to is required for mode={mode!r}")
    if importance not in (None, "normal", "high"):
        raise ToolError("validation", "importance must be 'normal' or 'high'")
    html_body = _wrap_html(body)

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
            return msg.id, getattr(msg, "changekey", None), list(to), list(cc or []), subject or ""
        original = _fetch_one(account, reply_to)
        extra: Dict[str, Any] = {}
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
        else:  # forward — create_forward REQUIRES to_recipients (5.0.3)
            final_subject = subject or _prefixed("Fwd: ", getattr(original, "subject", None))
            reply = original.create_forward(
                final_subject, html_body, list(to),
                cc_recipients=list(cc) if cc else None,
                bcc_recipients=list(bcc) if bcc else None,
            )
        # Exchange quotes the original server-side, keeping threading headers.
        saved = reply.save(account.drafts)
        prev_to = _email_list(getattr(reply, "to_recipients", None)) or list(to or [])
        prev_cc = _email_list(getattr(reply, "cc_recipients", None)) or list(cc or [])
        return saved.id, getattr(saved, "changekey", None), prev_to, prev_cc, final_subject

    raw_id, changekey, prev_to, prev_cc, prev_subject = await ctx.gateway.call(work)
    alias = ctx.aliaser.alias_for(str(raw_id), "d", changekey=changekey)
    return {
        "ok": True,
        "draft_id": alias,
        "folder": "f:drafts",
        "preview": _draft_preview(prev_to, prev_cc, prev_subject, body),
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
        msg = _fetch_one(account, draft_id)
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

    preview = await ctx.gateway.call(work)
    return {"ok": True, "draft_id": ctx.aliaser.alias_for(draft_id, "d"),
            "preview": preview}


async def _delete_draft(ctx: Context, *, draft_id: str) -> Dict[str, Any]:
    def work(account: Any) -> Dict[str, Any]:
        msg = _fetch_one(account, draft_id)
        _require_in_drafts(msg, account, "delete_draft")
        msg.move_to_trash()  # reversible — hence write class, no confirm
        return {"deleted": True}

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
                if flag_complete is not None:
                    # 1 = flagged and marked Complete; None = not flagged.
                    # Deliberately doesn't produce state 2 (flagged, still
                    # open) - this tool only marks/unmarks completion, it
                    # doesn't raise a new follow-up flag.
                    item.flag_status = 1 if flag_complete else None
                    changed.append("flag_status")
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


async def _create_event(ctx: Context, *, subject: str, start: str, end: str,
                        attendees: Optional[List[str]] = None,
                        location: Optional[str] = None,
                        body: Optional[str] = None,
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
            hint="Save the event without invitations, or have the operator "
                 "flip SEND_ENABLED.",
        )
    start_dt = parse_when(start, "start", ctx.settings.ews_tz)
    end_dt = parse_when(end, "end", ctx.settings.ews_tz)
    if end_dt <= start_dt:
        raise ToolError("validation", "end must be after start")

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
        item.save(send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY
                  if send_invitations else SEND_TO_NONE)
        return str(item.id)

    raw_id = await ctx.gateway.call(work)
    return {"ok": True, "event_id": ctx.aliaser.alias_for(raw_id, "e"),
            "invitations_sent": bool(send_invitations)}


async def _update_event(ctx: Context, *, event_id: str,
                        subject: Optional[str] = None,
                        start: Optional[str] = None,
                        end: Optional[str] = None,
                        location: Optional[str] = None,
                        body: Optional[str] = None,
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
    if all(v is None for v in (subject, start, end, location, body)):
        raise ToolError("validation",
                        "nothing to update: pass subject/start/end/location/body")
    start_dt = parse_when(start, "start", ctx.settings.ews_tz) if start else None
    end_dt = parse_when(end, "end", ctx.settings.ews_tz) if end else None

    def work(account: Any) -> Dict[str, Any]:
        item = _fetch_one(account, event_id)
        changed: List[str] = []
        for value, field in ((subject, "subject"), (start_dt, "start"),
                             (end_dt, "end"), (location, "location")):
            if value is not None:
                setattr(item, field, value)
                changed.append(field)
        if body is not None:
            item.body = _wrap_html(body)
            changed.append("body")
        item.save(update_fields=changed,
                  send_meeting_invitations=SEND_TO_CHANGED_AND_SAVE_COPY
                  if notify_attendees else SEND_TO_NONE)
        return {"updated_fields": changed, "attendees_notified": bool(notify_attendees)}

    return await ctx.gateway.call(work)


# --- SEND class (leaves the mailbox; tier full, two-phase, kill-switch) ------


def _draft_content(msg: Any) -> Dict[str, Any]:
    """The draft fields the confirm token binds and the preview shows.

    Recipients are sorted so reordering doesn't invalidate a token, but
    adding/removing one does (a content change worth re-previewing).
    """
    subject = getattr(msg, "subject", None)
    body_text = getattr(msg, "text_body", None)
    if not isinstance(body_text, str):
        body = getattr(msg, "body", None)
        body_text = str(body) if body is not None else ""
    return {
        "subject": subject if isinstance(subject, str) else "",
        "to": sorted(_email_list(getattr(msg, "to_recipients", None))),
        "cc": sorted(_email_list(getattr(msg, "cc_recipients", None))),
        "bcc": sorted(_email_list(getattr(msg, "bcc_recipients", None))),
        "body_text": body_text,
        "attachment_count": len(getattr(msg, "attachments", None) or []),
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
        msg = _fetch_one(account, raw_id)
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


async def _send_draft(ctx: Context, *, draft_id: str,
                      idempotency_key: Optional[str] = None) -> Dict[str, Any]:
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

    def work(account: Any) -> Dict[str, Any]:
        msg = _fetch_one(account, draft_id)
        _require_in_drafts(msg, account, "send_draft")
        # Draft with an id → SendItem; Exchange moves it from Drafts to Sent.
        msg.send(save_copy=True)
        imid = getattr(msg, "message_id", None)
        return {"sent": True,
                "internet_message_id": imid if isinstance(imid, str) else None}

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
    if state == "scheduled" and not (start and end):
        raise ToolError("validation", "start and end are required when state='scheduled'")
    start_dt = parse_when(start, "start", ctx.settings.ews_tz) if start else None
    end_dt = parse_when(end, "end", ctx.settings.ews_tz) if end else None

    def work(account: Any) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"state": target_state}
        if internal_reply is not None:
            # 5.0.3: plain strings — the OofReply wrapper class is gone.
            kwargs["internal_reply"] = internal_reply
            # OofSettings.clean requires BOTH replies when state != disabled.
            kwargs["external_reply"] = external_reply or internal_reply
        if start_dt is not None:
            kwargs["start"] = start_dt
        if end_dt is not None:
            kwargs["end"] = end_dt
        account.oof_settings = OofSettings(**kwargs)
        return {"state": state}

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
_IDS = {"type": "array", "items": {"type": "string"},
        "minItems": 1, "maxItems": MAX_BULK_IDS}


# --- the pack -----------------------------------------------------------------

TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="create_draft",
        description=(
            "Author mail as a DRAFT (never sends). mode=new needs to+body; "
            "reply/reply_all/forward need reply_to (the original message id) "
            "and quote the original server-side; forward also needs to. "
            "body is plain text (the server wraps it in minimal HTML). "
            "importance applies to mode=new only. Use send_draft to send."
        ),
        side_effect_class="write",
        input_schema=_obj({
            "mode": {"type": "string", "enum": list(_DRAFT_MODES), "default": "new"},
            "reply_to": _STR, "to": _EMAILS, "cc": _EMAILS, "bcc": _EMAILS,
            "subject": _STR, "body": _STR,
            "importance": {"type": "string", "enum": ["normal", "high"]},
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
            "Create a calendar event. Default send_invitations=false saves a "
            "placeholder WITHOUT emailing attendees (no confirm needed). With "
            "send_invitations=true, invitations leave the org: two-phase "
            "confirm + send kill-switch apply."
        ),
        side_effect_class="write",  # tier: draft; invite path confirm-gated below
        input_schema=_obj({
            "subject": _STR, "start": _STR, "end": _STR,
            "attendees": _EMAILS, "location": _STR, "body": _STR,
            "send_invitations": {"type": "boolean", "default": False},
        }, required=["subject", "start", "end"]),
        handler=_create_event,
        confirm=lambda kw: bool(kw.get("send_invitations")),
    ),
    ToolSpec(
        name="update_event",
        description=(
            "Update event fields (subject/start/end/location/body). Default "
            "notify_attendees=false is a silent metadata edit; true notifies "
            "changed attendees → two-phase confirm + send kill-switch apply."
        ),
        side_effect_class="write",
        input_schema=_obj({
            "event_id": _STR, "subject": _STR, "start": _STR, "end": _STR,
            "location": _STR, "body": _STR,
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
            "recipients/subject/body as a preview + confirm_token bound to "
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
