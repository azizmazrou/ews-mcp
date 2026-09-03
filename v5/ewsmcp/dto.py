"""Token-lean DTO builders (DESIGN.md §DTOs). The model never sees a raw EWS id:
``id`` IS the short alias; the aliaser holds the raw id + changekey."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .bodyclean import clean_body, html_to_text, strip_quoted_history
from .ids import IdAliaser


def fmt_dt(value: Any, tz: str) -> Optional[str]:
    if value is None or not hasattr(value, "astimezone"):
        return None
    try:
        return value.astimezone(ZoneInfo(tz)).isoformat(timespec="minutes")
    except Exception:
        return value.isoformat() if isinstance(value, datetime) else None


_DERIVE = object()  # sentinel: derive next_offset from total_available


def envelope(items: List[Dict[str, Any]], total_available: Optional[int],
             offset: int, next_offset: Any = _DERIVE) -> Dict[str, Any]:
    """Canonical paged envelope: {items, count, total_available, next_offset}.

    ``next_offset`` may be passed explicitly (lookahead pagination knows it
    without knowing the total); by default it derives from total_available.
    """
    out: Dict[str, Any] = {
        "ok": True,
        "items": items,
        "count": len(items),
        "total_available": total_available,
        "next_offset": None,
    }
    if next_offset is not _DERIVE:
        out["next_offset"] = next_offset
    elif total_available is not None and offset + len(items) < total_available:
        out["next_offset"] = offset + len(items)
    return out


def _addr(mailbox: Any) -> str:
    if mailbox is None:
        return ""
    name = getattr(mailbox, "name", None)
    email = getattr(mailbox, "email_address", None) or ""
    return f"{name} <{email}>" if name and name != email else email


def _emails(recipients: Any) -> List[str]:
    return [
        r.email_address for r in (recipients or [])
        if getattr(r, "email_address", None)
    ]


def msg_card(item: Any, aliaser: IdAliaser, tz: str) -> Dict[str, Any]:
    """MsgCard: the ≤~60-token search/list unit."""
    text = getattr(item, "text_body", None) or ""
    try:
        text = strip_quoted_history(text)[0] or text
    except Exception:
        pass
    raw_id = getattr(getattr(item, "id", None), "__str__", lambda: None)() or getattr(item, "id", None)
    imid = getattr(item, "message_id", None)
    card: Dict[str, Any] = {
        "id": aliaser.alias_for(str(raw_id), "m", internet_message_id=imid) if raw_id else None,
        "from": _addr(getattr(item, "sender", None)),
        "subject": getattr(item, "subject", "") or "",
        "date": fmt_dt(getattr(item, "datetime_received", None), tz),
        "snippet": text[:200],
    }
    conv = getattr(item, "conversation_id", None)
    if conv is not None and getattr(conv, "id", None):
        card["thread"] = aliaser.alias_for(conv.id, "t")
    to = _emails(getattr(item, "to_recipients", None))
    if len(to) > 1:
        card["to_count"] = len(to)
    if not getattr(item, "is_read", True):
        card["unread"] = True
    if getattr(item, "has_attachments", False):
        card["attach"] = len(getattr(item, "attachments", None) or []) or True
    importance = str(getattr(item, "importance", "") or "")
    if importance.lower() == "high":
        card["importance"] = "high"
    return card


def msg_full(item: Any, aliaser: IdAliaser, tz: str, body_max_chars: int,
             include_html: bool = False) -> Dict[str, Any]:
    """MsgFull: card + cleaned body + recipients + attachment inventory."""
    full = msg_card(item, aliaser, tz)
    full["to"] = _emails(getattr(item, "to_recipients", None))
    cc = _emails(getattr(item, "cc_recipients", None))
    if cc:
        full["cc"] = cc
    source = getattr(item, "text_body", None) or ""
    raw_html = ""
    body_obj = getattr(item, "body", None)
    if body_obj is not None:
        raw_html = str(body_obj)
    if not source and raw_html:
        source = html_to_text(raw_html)
    cleaned = clean_body(source, max_chars=body_max_chars)
    full["body"] = cleaned["text"]
    if cleaned["quoted_blocks_stripped"]:
        full["quoted_history"] = (
            f"stripped {cleaned['quoted_blocks_stripped']} quoted block(s) — "
            "use get_thread for the full conversation"
        )
    if cleaned["truncated"]:
        full["body_truncated"] = True
    if include_html and raw_html:
        full["body_html"] = raw_html
    attachments = []
    for i, att in enumerate(getattr(item, "attachments", None) or []):
        attachments.append({
            "idx": i,
            "name": getattr(att, "name", f"attachment-{i}"),
            "size_bytes": getattr(att, "size", None),
            "content_type": getattr(att, "content_type", None),
        })
    if attachments:
        full["attachments"] = attachments
    imid = getattr(item, "message_id", None)
    if imid:
        full["internet_message_id"] = imid
    if getattr(item, "flag_status", None) == 1:
        full["flag_complete"] = True
    full.pop("snippet", None)
    return full


def event_card(item: Any, aliaser: IdAliaser, tz: str) -> Dict[str, Any]:
    raw_id = getattr(item, "id", None)
    card: Dict[str, Any] = {
        "id": aliaser.alias_for(str(raw_id), "e") if raw_id else None,
        "subject": getattr(item, "subject", "") or "",
        "start": fmt_dt(getattr(item, "start", None), tz),
        "end": fmt_dt(getattr(item, "end", None), tz),
    }
    location = getattr(item, "location", None)
    if location:
        card["location"] = str(location)
    organizer = getattr(item, "organizer", None)
    if organizer is not None:
        card["organizer"] = _addr(organizer)
    my_response = getattr(item, "my_response_type", None)
    if my_response:
        card["my_response"] = str(my_response)
    if getattr(item, "is_recurring", False) or getattr(item, "recurrence", None):
        card["recurring"] = True
    return card
