"""Tool pack: rules — server-side Exchange Inbox rules (list, save, delete).

Safety invariants (DESIGN.md §Tools, §Safety):
- Writable actions are strictly allow-listed to mailbox-internal metadata and
  filing operations (``move_to_folder``, ``copy_to_folder``,
  ``assign_categories``, ``mark_as_read``, ``mark_importance``,
  ``stop_processing_rules``). Nothing that sends, forwards, redirects, replies,
  alerts, or deletes can ever be constructed from tool arguments.
- Move and copy targets are verified fail-closed against Deleted Items and Junk
  Email (by class, distinguished id, well-known name, raw id, and live
  parent-folder ancestry up to ``msg_folder_root``), and always send a real
  ``FolderId``.
- Existing rules that use non-writable actions, exceptions, read-only
  conditions, present-but-unreadable XML elements, unnameable folder targets,
  or ``is_not_supported`` are reported by ``list_rules`` with
  ``editable: false`` and ``unsupported`` (plus ``sends_mail`` and
  ``recipients`` when they send mail), and ``save_rule`` refuses to replace
  them so unsupported fields are never silently dropped.
- Both ``save_rule`` and ``delete_rule`` are class ``write`` (draft tier),
  serialised in-process via ``_rules_lock``, and ALWAYS two-phase confirmed
  with a content-bound preview carrying catch-all and ``OutlookRuleBlobExists``
  warnings, raw target folder ids, and a SHA-256 XML fingerprint of any
  replaced or deleted rule.
"""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field as dc_field
import threading
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from exchangelib import Account
from exchangelib.errors import UnauthorizedError
from exchangelib.properties import (
    Actions,
    Address,
    Conditions,
    CopyToFolder,
    Exceptions,
    FolderId,
    MoveToFolder,
    Rule,
)

from ..dto import envelope
from ..errors import ToolError
from ..gateway import rules as gateway_rules
from ..gateway.client import WELL_KNOWN
from .base import Context, ToolSpec, _normalise_recipient, preview_content_hash

MAX_RULE_STR_LEN = 255

WRITABLE_ACTION_KEYS = frozenset({
    "move_to_folder",
    "copy_to_folder",
    "assign_categories",
    "mark_as_read",
    "mark_importance",
    "stop_processing_rules",
})

_STRING_LIST_CONDITIONS: Dict[str, str] = {
    "subject_contains": "contains_subject_strings",
    "body_contains": "contains_body_strings",
    "subject_or_body_contains": "contains_subject_or_body_strings",
    "sender_contains": "contains_sender_strings",
    "recipient_contains": "contains_recipient_strings",
}
_EWS_TO_DTO_STRING_CONDITIONS: Dict[str, str] = {
    ews_name: dto_name for dto_name, ews_name in _STRING_LIST_CONDITIONS.items()
}

_ADDRESS_LIST_CONDITIONS = ("from_addresses", "sent_to_addresses")

_BOOL_CONDITIONS = (
    "has_attachments",
    "sent_to_me",
    "sent_cc_me",
    "sent_only_to_me",
    "sent_to_or_cc_me",
    "not_sent_to_me",
)

WRITABLE_CONDITION_KEYS = frozenset({
    *_ADDRESS_LIST_CONDITIONS,
    *_STRING_LIST_CONDITIONS.keys(),
    "importance",
    *_BOOL_CONDITIONS,
})

_SAVE_RULE_TOP_KEYS = frozenset({
    "name",
    "actions",
    "conditions",
    "priority",
    "enabled",
    "rule_id",
})
_DELETE_RULE_TOP_KEYS = frozenset({"rule_id"})
_DIFFABLE_RULE_FIELDS = ("name", "priority", "enabled", "conditions", "actions")

_IMPORTANCE_TO_EWS = {"low": "Low", "normal": "Normal", "high": "High"}

_RECIPIENT_SENDING_ACTIONS = frozenset({
    "forward_to_recipients",
    "forward_as_attachment_to_recipients",
    "redirect_to_recipients",
    "send_sms_alert_to_recipients",
})

_ALL_MATCH_WARNING = "Rule has no conditions: matches every incoming message."
_OUTLOOK_BLOB_TRUE_WARNING = (
    "Outlook-only rules exist on this mailbox (outlook_only_rules=true): "
    "rules disabled in Outlook and client-side send rules will be lost if you proceed."
)
_OUTLOOK_BLOB_UNKNOWN_WARNING = (
    "Could not verify Outlook rule blob status (outlook_only_rules=null): "
    "rules disabled in Outlook and client-side send rules will be lost if you proceed."
)
_STALE_RULE_HINT = (
    "The rule or arguments changed since the preview — call again without "
    "confirm_token for a fresh preview."
)


# --- in-process rule write serialisation (same pattern as _draft_lock) -------


@dataclass
class _RuleLockState:
    locked: bool = False
    waiters: deque[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = dc_field(
        default_factory=deque
    )


_RULE_LOCK_STATE = _RuleLockState()
_RULE_LOCK_GUARD = threading.Lock()


def reset_rules_lock() -> None:
    """Tests only."""
    global _RULE_LOCK_STATE
    with _RULE_LOCK_GUARD:
        _RULE_LOCK_STATE = _RuleLockState()


def _wake_rule_waiter(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)


def _handoff_rule_lock(state: _RuleLockState) -> None:
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    while state.waiters:
        w_loop, w_fut = state.waiters.popleft()
        if w_fut.done() or w_loop.is_closed():
            continue
        if w_loop is running:
            w_fut.set_result(None)
            return
        try:
            w_loop.call_soon_threadsafe(_wake_rule_waiter, w_fut)
            return
        except RuntimeError:
            continue
    state.locked = False


@asynccontextmanager
async def _rules_lock():
    """Serialise ``save_rule`` and ``delete_rule`` mutations in-process."""
    loop = asyncio.get_running_loop()
    waiter: Optional[asyncio.Future[None]] = None
    with _RULE_LOCK_GUARD:
        state = _RULE_LOCK_STATE
        if not state.locked and not state.waiters:
            state.locked = True
        else:
            waiter = loop.create_future()
            state.waiters.append((loop, waiter))

    if waiter is not None:
        try:
            await waiter
        except BaseException:
            with _RULE_LOCK_GUARD:
                if _RULE_LOCK_STATE is state:
                    try:
                        state.waiters.remove((loop, waiter))
                    except ValueError:
                        pass
                    if waiter.done() and not waiter.cancelled():
                        _handoff_rule_lock(state)
            raise

    try:
        yield
    finally:
        with _RULE_LOCK_GUARD:
            if _RULE_LOCK_STATE is state:
                _handoff_rule_lock(state)


# --- validation & normalization helpers --------------------------------------


def _reject_unknown_top_args(
    kwargs: Dict[str, Any], allowed: FrozenSet[str], tool_name: str
) -> None:
    unknown = sorted(str(k) for k in kwargs if k not in allowed)
    if unknown:
        raise ToolError(
            "validation",
            f"{tool_name}: unknown argument(s): {', '.join(unknown)}.",
            hint="Check the argument names and types against the tool schema.",
        )


def _resolve_rule_id(ctx: Context, rule_id: Any) -> str:
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ToolError("validation", "rule_id must be a non-empty string")
    clean = rule_id.strip()
    try:
        return ctx.aliaser.resolve(clean)
    except KeyError:
        raise ToolError(
            "not_found",
            f"No rule matches {clean!r}.",
            hint="Call list_rules to get current rule ids.",
        ) from None


MAX_RULE_PRIORITY = 2_147_483_647  # xs:int, the type of <t:Priority>


def _require_plain_text(text: str, label: str) -> None:
    """Refuse text that cannot be sent in an XML request (control characters,
    unpaired surrogates). Checked before a confirm token is issued, so that a
    token is never consumed by a call that then fails on its own input."""
    for ch in text:
        code = ord(ch)
        if code < 32 or code == 127 or 0xD800 <= code <= 0xDFFF:
            raise ToolError(
                "validation",
                f"{label} contains a character that cannot be used in a rule "
                f"(U+{code:04X}).",
            )


def _require_non_empty_str(
    value: Any, label: str, *, max_len: Optional[int] = None
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("validation", f"{label} must be a non-empty string")
    cleaned = value.strip()
    _require_plain_text(cleaned, label)
    if max_len is not None and len(cleaned) > max_len:
        raise ToolError(
            "validation",
            f"{label} must be at most {max_len} characters (got {len(cleaned)})",
        )
    return cleaned


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ToolError("validation", f"{label} must be a boolean")
    return value


def _parse_importance(value: Any, label: str) -> Tuple[str, str]:
    if not isinstance(value, str):
        raise ToolError("validation", f"{label} must be 'low', 'normal', or 'high'")
    key = value.strip().lower()
    ews_val = _IMPORTANCE_TO_EWS.get(key)
    if ews_val is None:
        raise ToolError(
            "validation",
            f"{label} must be 'low', 'normal', or 'high', got {value!r}",
        )
    return ews_val, key


def _parse_string_list(
    value: Any,
    label: str,
    *,
    strip: bool = False,
    max_len: int = MAX_RULE_STR_LEN,
) -> List[str]:
    if not isinstance(value, list) or not value:
        raise ToolError("validation", f"{label} must be a non-empty list of strings")
    out: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ToolError(
                "validation",
                f"{label} entries must be non-empty strings",
            )
        cleaned = item.strip() if strip else item
        _require_plain_text(cleaned, f"{label} entries")
        if len(cleaned) > max_len:
            raise ToolError(
                "validation",
                f"{label} entries must be at most {max_len} characters "
                f"(got {len(cleaned)})",
            )
        if cleaned not in out:
            out.append(cleaned)
    return out


def _parse_address_list(value: Any, label: str) -> List[str]:
    if not isinstance(value, list) or not value:
        raise ToolError(
            "validation",
            f"{label} must be a non-empty list of email addresses",
        )
    out: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ToolError(
                "validation",
                f"invalid address in {label}: expected string",
            )
        addr = _normalise_recipient(item)
        if addr is None:
            raise ToolError(
                "validation",
                f"invalid email address in {label}: {item!r}",
            )
        if len(addr) > MAX_RULE_STR_LEN:
            raise ToolError(
                "validation",
                f"{label} entries must be at most {MAX_RULE_STR_LEN} characters",
            )
        if addr not in out:
            out.append(addr)
    return out


def _validate_save_inputs(
    *,
    name: Any,
    actions: Any,
    conditions: Any,
    priority: Any = None,
    enabled: Any = None,
) -> Tuple[str, Optional[int], Optional[bool], Dict[str, Any], Dict[str, Any]]:
    """Validate and normalize ``save_rule`` arguments before touching EWS."""
    clean_name = _require_non_empty_str(name, "name", max_len=MAX_RULE_STR_LEN)

    clean_priority: Optional[int] = None
    if priority is not None:
        if (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or priority < 1
            or priority > MAX_RULE_PRIORITY
        ):
            raise ToolError(
                "validation",
                f"priority must be an integer from 1 to {MAX_RULE_PRIORITY}",
            )
        clean_priority = priority

    clean_enabled: Optional[bool] = None
    if enabled is not None:
        clean_enabled = _require_bool(enabled, "enabled")

    if not isinstance(actions, dict) or not actions:
        raise ToolError("validation", "actions must be a non-empty object")
    unknown_actions = sorted(str(k) for k in actions if k not in WRITABLE_ACTION_KEYS)
    if unknown_actions:
        raise ToolError(
            "validation",
            f"Unsupported or forbidden action field(s): {', '.join(unknown_actions)}. "
            "Inbox rules may only set move_to_folder, copy_to_folder, "
            "assign_categories, mark_as_read, mark_importance, or "
            "stop_processing_rules (sending, forwarding, redirecting, replying, "
            "and deleting are never writable).",
        )

    norm_actions: Dict[str, Any] = {}
    if actions.get("move_to_folder") is not None:
        norm_actions["move_to_folder"] = _require_non_empty_str(
            actions["move_to_folder"], "actions.move_to_folder"
        )
    if actions.get("copy_to_folder") is not None:
        norm_actions["copy_to_folder"] = _require_non_empty_str(
            actions["copy_to_folder"], "actions.copy_to_folder"
        )
    if actions.get("assign_categories") is not None:
        norm_actions["assign_categories"] = _parse_string_list(
            actions["assign_categories"],
            "actions.assign_categories",
            strip=True,
            max_len=MAX_RULE_STR_LEN,
        )
    if actions.get("mark_as_read") is not None:
        if _require_bool(actions["mark_as_read"], "actions.mark_as_read"):
            norm_actions["mark_as_read"] = True
    if actions.get("mark_importance") is not None:
        _, imp_dto = _parse_importance(
            actions["mark_importance"], "actions.mark_importance"
        )
        norm_actions["mark_importance"] = imp_dto
    if actions.get("stop_processing_rules") is not None:
        if _require_bool(
            actions["stop_processing_rules"], "actions.stop_processing_rules"
        ):
            norm_actions["stop_processing_rules"] = True
    if not norm_actions:
        raise ToolError("validation", "actions must enable at least one action")

    if conditions is not None and not isinstance(conditions, dict):
        raise ToolError("validation", "conditions must be an object when provided")
    raw_conds = conditions or {}
    unknown_conds = sorted(
        str(k) for k in raw_conds if k not in WRITABLE_CONDITION_KEYS
    )
    if unknown_conds:
        raise ToolError(
            "validation",
            f"Unsupported condition field(s): {', '.join(unknown_conds)}.",
        )

    norm_conds: Dict[str, Any] = {}
    for addr_key in _ADDRESS_LIST_CONDITIONS:
        if raw_conds.get(addr_key) is not None:
            norm_conds[addr_key] = _parse_address_list(
                raw_conds[addr_key], f"conditions.{addr_key}"
            )
    for dto_key in _STRING_LIST_CONDITIONS:
        if raw_conds.get(dto_key) is not None:
            norm_conds[dto_key] = _parse_string_list(
                raw_conds[dto_key],
                f"conditions.{dto_key}",
                strip=False,
                max_len=MAX_RULE_STR_LEN,
            )
    if raw_conds.get("importance") is not None:
        _, imp_dto = _parse_importance(
            raw_conds["importance"], "conditions.importance"
        )
        norm_conds["importance"] = imp_dto
    for bool_key in _BOOL_CONDITIONS:
        if raw_conds.get(bool_key) is not None:
            bval = _require_bool(raw_conds[bool_key], f"conditions.{bool_key}")
            if not bval:
                raise ToolError(
                    "validation",
                    f"conditions.{bool_key} only supports true (EWS rule "
                    f"predicates cannot be negated with false; omit {bool_key} "
                    "when unset).",
                )
            norm_conds[bool_key] = True

    return clean_name, clean_priority, clean_enabled, norm_actions, norm_conds


# --- folder helpers ----------------------------------------------------------


def _folder_path(folder: Any, aliaser: Any) -> Optional[str]:
    explicit = getattr(folder, "path", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    try:
        parts = list(getattr(folder, "parts", None) or [])
        if len(parts) > 2:
            joined = "/".join(
                p.name for p in parts[2:] if getattr(p, "name", None)
            )
            if joined:
                return joined
    except UnauthorizedError:
        raise
    except Exception:
        pass
    name = getattr(folder, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    raw_id = getattr(folder, "id", None)
    if isinstance(raw_id, str) and raw_id.strip():
        return aliaser.alias_for(raw_id.strip(), "f")
    return None


def _folder_to_target(folder: Any, target_cls: Any, ref: str) -> Any:
    raw_id = getattr(folder, "id", None)
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ToolError(
            "validation",
            f"Target folder {ref!r} has no folder id; cannot verify the folder's location.",
        )
    raw_ck = getattr(folder, "changekey", None)
    return target_cls(
        folder_id=FolderId(
            id=raw_id,
            changekey=str(raw_ck) if raw_ck else None,
        )
    )


def _target_to_folder_label(
    ctx: Context,
    account: Any,
    target: Any,
    *,
    folder_cache: Optional[Dict[Tuple[str, str], Optional[str]]] = None,
) -> Optional[str]:
    dfid = getattr(target, "distinguished_folder_id", None)
    dfid_val = getattr(dfid, "id", None) if dfid is not None else None
    if isinstance(dfid_val, str) and dfid_val.strip():
        ref = dfid_val.strip()
        cache_key = ("dfid", ref.lower())
        if folder_cache is not None and cache_key in folder_cache:
            return folder_cache[cache_key]
        label: Optional[str] = None
        if hasattr(ctx.gateway, "resolve_folder"):
            try:
                folder = ctx.gateway.resolve_folder(account, ref, ctx.aliaser)
                label = _folder_path(folder, ctx.aliaser)
            except UnauthorizedError:
                raise
            except Exception:
                label = None
        if not label:
            low = ref.lower()
            if low in WELL_KNOWN:
                label = low
            elif f"f:{low}" in WELL_KNOWN:
                label = f"f:{low}"
        if folder_cache is not None:
            folder_cache[cache_key] = label
        return label

    fid = getattr(target, "folder_id", None)
    raw_id = getattr(fid, "id", None) if fid is not None else None
    if isinstance(raw_id, str) and raw_id.strip():
        ref = raw_id.strip()
        cache_key = ("fid", ref)
        if folder_cache is not None and cache_key in folder_cache:
            return folder_cache[cache_key]
        label = None
        if hasattr(ctx.gateway, "resolve_folder"):
            try:
                folder = ctx.gateway.resolve_folder(account, ref, ctx.aliaser)
                label = _folder_path(folder, ctx.aliaser)
            except UnauthorizedError:
                raise
            except Exception:
                label = None
        if not label:
            label = ctx.aliaser.alias_for(ref, "f")
        if folder_cache is not None:
            folder_cache[cache_key] = label
        return label

    return None


# --- bidirectional mapping (Rule <-> DTO) ------------------------------------


def build_rule(
    ctx: Context,
    account: Any,
    *,
    name: str,
    actions: Dict[str, Any],
    conditions: Optional[Dict[str, Any]] = None,
    priority: int = 1,
    enabled: bool = True,
    raw_rule_id: Optional[str] = None,
    mint_rule_alias: bool = True,
) -> Tuple[Rule, Dict[str, Any], Dict[str, str]]:
    """Build a real ``exchangelib.properties.Rule`` from allow-listed inputs.

    Returns ``(rule_obj, planned_dto, target_folder_ids)``.
    """
    (
        clean_name,
        clean_priority,
        clean_enabled,
        norm_actions,
        norm_conds,
    ) = _validate_save_inputs(
        name=name,
        actions=actions,
        conditions=conditions,
        priority=priority,
        enabled=enabled,
    )
    eff_priority = clean_priority if clean_priority is not None else 1
    eff_enabled = clean_enabled if clean_enabled is not None else True

    discard_ids: Optional[Set[str]] = None
    target_folder_ids: Dict[str, str] = {}

    def _target_folder(ref: str, label: str) -> Tuple[Any, str]:
        nonlocal discard_ids
        if gateway_rules.is_well_known_discard_ref(ref):
            raise gateway_rules._discard_target_error(label)
        resolved = ctx.gateway.resolve_folder(account, ref, ctx.aliaser)
        fresh, discard_ids = gateway_rules.verify_rule_target_folder(
            account,
            resolved,
            ref=ref,
            label=label,
            discard_ids=discard_ids,
        )
        display_label = _folder_path(fresh, ctx.aliaser) or _folder_path(
            resolved, ctx.aliaser
        )
        if not display_label:
            raise ToolError(
                "validation",
                f"{label}: cannot determine a display label for folder {ref!r}.",
            )
        return fresh, display_label

    action_kwargs: Dict[str, Any] = {}
    actions_dto: Dict[str, Any] = {}
    if "move_to_folder" in norm_actions:
        ref = norm_actions["move_to_folder"]
        fresh_folder, display_label = _target_folder(
            ref, "actions.move_to_folder"
        )
        action_kwargs["move_to_folder"] = _folder_to_target(
            fresh_folder, MoveToFolder, ref
        )
        actions_dto["move_to_folder"] = display_label
        target_folder_ids["move_to_folder"] = str(fresh_folder.id)
    if "copy_to_folder" in norm_actions:
        ref = norm_actions["copy_to_folder"]
        fresh_folder, display_label = _target_folder(
            ref, "actions.copy_to_folder"
        )
        action_kwargs["copy_to_folder"] = _folder_to_target(
            fresh_folder, CopyToFolder, ref
        )
        actions_dto["copy_to_folder"] = display_label
        target_folder_ids["copy_to_folder"] = str(fresh_folder.id)
    if "assign_categories" in norm_actions:
        cats = list(norm_actions["assign_categories"])
        action_kwargs["assign_categories"] = cats
        actions_dto["assign_categories"] = list(cats)
    if norm_actions.get("mark_as_read") is True:
        action_kwargs["mark_as_read"] = True
        actions_dto["mark_as_read"] = True
    if "mark_importance" in norm_actions:
        imp = norm_actions["mark_importance"]
        action_kwargs["mark_importance"] = _IMPORTANCE_TO_EWS[imp]
        actions_dto["mark_importance"] = imp
    if norm_actions.get("stop_processing_rules") is True:
        action_kwargs["stop_processing_rules"] = True
        actions_dto["stop_processing_rules"] = True

    actions_obj = Actions(**action_kwargs)

    cond_kwargs: Dict[str, Any] = {}
    conds_dto: Dict[str, Any] = {}
    for addr_key in _ADDRESS_LIST_CONDITIONS:
        if addr_key in norm_conds:
            addrs = list(norm_conds[addr_key])
            cond_kwargs[addr_key] = [Address(email_address=a) for a in addrs]
            conds_dto[addr_key] = addrs
    for dto_key, ews_key in _STRING_LIST_CONDITIONS.items():
        if dto_key in norm_conds:
            vals = list(norm_conds[dto_key])
            cond_kwargs[ews_key] = vals
            conds_dto[dto_key] = vals
    if "importance" in norm_conds:
        imp = norm_conds["importance"]
        cond_kwargs["importance"] = _IMPORTANCE_TO_EWS[imp]
        conds_dto["importance"] = imp
    for bool_key in _BOOL_CONDITIONS:
        if norm_conds.get(bool_key) is True:
            cond_kwargs[bool_key] = True
            conds_dto[bool_key] = True

    conditions_obj = Conditions(**cond_kwargs) if cond_kwargs else None

    rule_kwargs: Dict[str, Any] = {
        "display_name": clean_name,
        "priority": eff_priority,
        "is_enabled": eff_enabled,
        "conditions": conditions_obj,
        "actions": actions_obj,
    }
    if raw_rule_id is not None:
        rule_kwargs["id"] = raw_rule_id
    if isinstance(account, Account):
        rule_kwargs["account"] = account

    planned_dto: Dict[str, Any] = {
        "name": clean_name,
        "priority": eff_priority,
        "enabled": eff_enabled,
        "conditions": conds_dto,
        "actions": actions_dto,
        "editable": True,
    }
    if raw_rule_id is not None and mint_rule_alias:
        planned_dto["id"] = ctx.aliaser.alias_for(raw_rule_id, "r")

    return Rule(**rule_kwargs), planned_dto, target_folder_ids


def _is_field_active(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value is True
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    if isinstance(value, str):
        return len(value) > 0
    # Any non-None EWSElement instance (MoveToFolder, CopyToFolder, ItemId,
    # WithinDateRange, WithinSizeRange, ...) means the XML tag was present.
    return True


def _extract_condition_addresses(items: Any) -> Tuple[List[str], bool]:
    emails: List[str] = []
    unrepresentable = False
    for entry in items or []:
        raw_email = (
            entry if isinstance(entry, str) else getattr(entry, "email_address", None)
        )
        norm = _normalise_recipient(raw_email) if isinstance(raw_email, str) else None
        if norm is not None:
            if norm not in emails:
                emails.append(norm)
        else:
            unrepresentable = True
            fallback = getattr(entry, "name", None) or raw_email
            if isinstance(fallback, str) and fallback.strip():
                clean_fb = fallback.strip()
                if clean_fb not in emails:
                    emails.append(clean_fb)
    return emails, unrepresentable


def _extract_display_addresses(items: Any) -> List[str]:
    out: List[str] = []
    for entry in items or []:
        if isinstance(entry, str):
            cand = entry.strip()
        else:
            cand = (
                getattr(entry, "email_address", None)
                or getattr(entry, "name", None)
                or ""
            )
            cand = str(cand).strip()
        if cand and cand not in out:
            out.append(cand)
    return out


def rule_to_dto(
    ctx: Context,
    account: Any,
    rule: Rule,
    *,
    folder_cache: Optional[Dict[Tuple[str, str], Optional[str]]] = None,
) -> Dict[str, Any]:
    """Project an ``exchangelib.properties.Rule`` to its public DTO."""
    raw_id = getattr(rule, "id", None)
    alias = ctx.aliaser.alias_for(str(raw_id), "r") if raw_id else None

    unsupported: List[str] = []

    def _add_unsupported(label: str) -> None:
        if label not in unsupported:
            unsupported.append(label)

    if getattr(rule, "is_not_supported", None) is True:
        _add_unsupported("is_not_supported")

    xml_extra_unsupported, xml_extra_sends = gateway_rules.rule_xml_issues(rule)
    for item in xml_extra_unsupported:
        _add_unsupported(item)

    cond_dto: Dict[str, Any] = {}
    cond_obj = getattr(rule, "conditions", None)
    if cond_obj is not None:
        for field in Conditions.FIELDS:
            fname = field.name
            val = getattr(cond_obj, fname, None)
            if not _is_field_active(val):
                continue
            if fname in _ADDRESS_LIST_CONDITIONS:
                addrs, bad = _extract_condition_addresses(val)
                if addrs:
                    cond_dto[fname] = addrs
                if bad or not addrs:
                    _add_unsupported(f"conditions.{fname}")
            elif fname in _EWS_TO_DTO_STRING_CONDITIONS:
                cond_dto[_EWS_TO_DTO_STRING_CONDITIONS[fname]] = [
                    str(s) for s in val
                ]
            elif fname == "importance":
                cond_dto["importance"] = str(val).lower()
            elif fname in _BOOL_CONDITIONS:
                cond_dto[fname] = True
            else:
                _add_unsupported(f"conditions.{fname}")

    exc_obj = getattr(rule, "exceptions", None)
    if exc_obj is not None:
        for field in Exceptions.FIELDS:
            val = getattr(exc_obj, field.name, None)
            if _is_field_active(val):
                _add_unsupported(f"exceptions.{field.name}")

    act_dto: Dict[str, Any] = {}
    sends_mail = bool(xml_extra_sends)
    recipients: List[str] = []
    act_obj = getattr(rule, "actions", None)
    if act_obj is not None:
        for field in Actions.FIELDS:
            fname = field.name
            val = getattr(act_obj, fname, None)
            if not _is_field_active(val):
                continue
            if fname in ("move_to_folder", "copy_to_folder"):
                label = _target_to_folder_label(
                    ctx, account, val, folder_cache=folder_cache
                )
                if label:
                    act_dto[fname] = label
                else:
                    _add_unsupported(f"actions.{fname}")
            elif fname == "assign_categories":
                act_dto["assign_categories"] = [str(c) for c in val]
            elif fname == "mark_as_read":
                act_dto["mark_as_read"] = True
            elif fname == "mark_importance":
                act_dto["mark_importance"] = str(val).lower()
            elif fname == "stop_processing_rules":
                act_dto["stop_processing_rules"] = True
            else:
                _add_unsupported(f"actions.{fname}")
                if fname in _RECIPIENT_SENDING_ACTIONS:
                    sends_mail = True
                    for addr in _extract_display_addresses(val):
                        if addr not in recipients:
                            recipients.append(addr)
                elif fname == "server_reply_with_message":
                    sends_mail = True

    if (
        not act_dto
        and not any(u.startswith("actions.") for u in unsupported)
        and "is_not_supported" not in unsupported
    ):
        _add_unsupported("actions.unknown")

    raw_prio = getattr(rule, "priority", None)
    raw_enabled = getattr(rule, "is_enabled", None)
    dto: Dict[str, Any] = {
        "id": alias,
        "name": getattr(rule, "display_name", None) or "",
        "priority": int(raw_prio) if raw_prio is not None else 1,
        "enabled": True if raw_enabled is None else bool(raw_enabled),
        "conditions": cond_dto,
        "actions": act_dto,
        "editable": len(unsupported) == 0,
    }
    if unsupported:
        dto["unsupported"] = unsupported
    if sends_mail:
        dto["sends_mail"] = True
        dto["recipients"] = recipients
    if getattr(rule, "is_in_error", None) is True:
        dto["in_error"] = True
    return dto


# --- preview & warning builders ----------------------------------------------


def _blob_warnings(blob_exists: Optional[bool]) -> List[str]:
    if blob_exists is True:
        return [_OUTLOOK_BLOB_TRUE_WARNING]
    if blob_exists is None:
        return [_OUTLOOK_BLOB_UNKNOWN_WARNING]
    return []


def _prepare_save_state(
    ctx: Context,
    account: Any,
    *,
    name: str,
    actions: Dict[str, Any],
    conditions: Optional[Dict[str, Any]],
    priority: Optional[int],
    enabled: Optional[bool],
    rule_id: Optional[str],
    raw_rule_id: Optional[str],
) -> Tuple[List[Rule], Optional[bool], Rule, Dict[str, Any]]:
    (
        clean_name,
        clean_priority,
        clean_enabled,
        _,
        _,
    ) = _validate_save_inputs(
        name=name,
        actions=actions,
        conditions=conditions,
        priority=priority,
        enabled=enabled,
    )
    existing_rules, blob_exists = gateway_rules.fetch_rules(account)

    current_dto: Optional[Dict[str, Any]] = None
    target_fingerprint: Optional[str] = None
    if raw_rule_id is None:
        for existing in existing_rules:
            if (existing.display_name or "") == clean_name:
                raise ToolError(
                    "validation",
                    f"A rule named {clean_name!r} already exists; "
                    "pass rule_id to replace an existing rule or choose a "
                    "distinct name.",
                    hint="Call list_rules to inspect existing rules and their ids.",
                )
        eff_priority = clean_priority if clean_priority is not None else 1
        eff_enabled = clean_enabled if clean_enabled is not None else True
    else:
        target = gateway_rules.find_rule(
            existing_rules, raw_rule_id, display_ref=rule_id
        )
        current_dto = rule_to_dto(ctx, account, target)
        if not current_dto["editable"]:
            bad_fields = ", ".join(current_dto.get("unsupported") or [])
            raise ToolError(
                "validation",
                f"Rule {rule_id!r} ({current_dto['name']!r}) is not editable "
                f"because it uses unsupported fields ({bad_fields}); replacing "
                "it would drop them.",
                hint=(
                    "Edit this rule in Outlook, or delete it with delete_rule "
                    "and create a new rule."
                ),
            )
        target_fingerprint = gateway_rules.rule_fingerprint(target, account)
        eff_priority = (
            clean_priority
            if clean_priority is not None
            else current_dto["priority"]
        )
        eff_enabled = (
            clean_enabled
            if clean_enabled is not None
            else current_dto["enabled"]
        )

    rule_obj, planned_dto, target_folder_ids = build_rule(
        ctx,
        account,
        name=clean_name,
        actions=actions,
        conditions=conditions,
        priority=eff_priority,
        enabled=eff_enabled,
        raw_rule_id=raw_rule_id,
        mint_rule_alias=raw_rule_id is not None,
    )

    warnings: List[str] = []
    if not planned_dto["conditions"]:
        warnings.append(_ALL_MATCH_WARNING)
    warnings.extend(_blob_warnings(blob_exists))

    content: Dict[str, Any] = {
        "operation": "replace" if raw_rule_id is not None else "create",
        "rule": planned_dto,
        "outlook_only_rules": blob_exists,
    }
    if current_dto is not None:
        changes = [
            fname
            for fname in _DIFFABLE_RULE_FIELDS
            if planned_dto.get(fname) != current_dto.get(fname)
        ]
        content["current"] = current_dto
        content["changes"] = changes
    if warnings:
        content["warnings"] = warnings
    content["_attachments"] = [{
        "op": "save_rule",
        "rule_id": raw_rule_id,
        "rule": planned_dto,
        "current": current_dto,
        "target_fingerprint": target_fingerprint,
        "target_folder_ids": target_folder_ids,
        "outlook_only_rules": blob_exists,
    }]
    return existing_rules, blob_exists, rule_obj, content


def _prepare_delete_state(
    ctx: Context,
    account: Any,
    *,
    rule_id: str,
    raw_rule_id: str,
) -> Tuple[List[Rule], Dict[str, Any]]:
    existing_rules, blob_exists = gateway_rules.fetch_rules(account)
    target = gateway_rules.find_rule(
        existing_rules, raw_rule_id, display_ref=rule_id
    )
    target_dto = rule_to_dto(ctx, account, target)
    target_fingerprint = gateway_rules.rule_fingerprint(target, account)
    warnings = _blob_warnings(blob_exists)

    content: Dict[str, Any] = {
        "rule": target_dto,
        "outlook_only_rules": blob_exists,
    }
    if warnings:
        content["warnings"] = warnings
    content["_attachments"] = [{
        "op": "delete_rule",
        "rule_id": raw_rule_id,
        "rule": target_dto,
        "target_fingerprint": target_fingerprint,
        "outlook_only_rules": blob_exists,
    }]
    return existing_rules, content


# --- tool handlers & preview hooks -------------------------------------------


async def _list_rules(ctx: Context, **extra: Any) -> Dict[str, Any]:
    _reject_unknown_top_args(extra, frozenset(), "list_rules")

    def work(account: Any) -> Tuple[List[Dict[str, Any]], Optional[bool]]:
        rules_list, blob_exists = gateway_rules.fetch_rules(account)
        folder_cache: Dict[Tuple[str, str], Optional[str]] = {}
        dtos = [
            rule_to_dto(ctx, account, item, folder_cache=folder_cache)
            for item in rules_list
        ]
        return dtos, blob_exists

    items, blob_exists = await ctx.gateway.call(work)
    out = envelope(items, total_available=len(items), offset=0)
    out["source"] = "live"
    out["outlook_only_rules"] = blob_exists
    return out


async def _save_rule_preview(
    ctx: Context, kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    _reject_unknown_top_args(kwargs, _SAVE_RULE_TOP_KEYS, "save_rule")
    raw_rule_id: Optional[str] = None
    rule_id_arg = kwargs.get("rule_id")
    if rule_id_arg is not None:
        raw_rule_id = _resolve_rule_id(ctx, rule_id_arg)
    _validate_save_inputs(
        name=kwargs.get("name"),
        actions=kwargs.get("actions"),
        conditions=kwargs.get("conditions"),
        priority=kwargs.get("priority"),
        enabled=kwargs.get("enabled"),
    )

    def work(account: Any) -> Dict[str, Any]:
        _, _, _, content = _prepare_save_state(
            ctx,
            account,
            name=kwargs["name"],
            actions=kwargs["actions"],
            conditions=kwargs.get("conditions"),
            priority=kwargs.get("priority"),
            enabled=kwargs.get("enabled"),
            rule_id=rule_id_arg,
            raw_rule_id=raw_rule_id,
        )
        return content

    return await ctx.gateway.call(work)


async def _save_rule(
    ctx: Context,
    *,
    name: str,
    actions: Dict[str, Any],
    conditions: Optional[Dict[str, Any]] = None,
    priority: Optional[int] = None,
    enabled: Optional[bool] = None,
    rule_id: Optional[str] = None,
    _verified_content_hash: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    _reject_unknown_top_args(extra, frozenset(), "save_rule")
    raw_rule_id = _resolve_rule_id(ctx, rule_id) if rule_id is not None else None
    _validate_save_inputs(
        name=name,
        actions=actions,
        conditions=conditions,
        priority=priority,
        enabled=enabled,
    )
    if not isinstance(_verified_content_hash, str) or not _verified_content_hash:
        raise ToolError(
            "confirm_invalid",
            "confirm_token rejected (stale): rule preview state was not verified",
            hint=_STALE_RULE_HINT,
        )

    def work(account: Any) -> Dict[str, Any]:
        existing_rules, _, rule_obj, content = _prepare_save_state(
            ctx,
            account,
            name=name,
            actions=actions,
            conditions=conditions,
            priority=priority,
            enabled=enabled,
            rule_id=rule_id,
            raw_rule_id=raw_rule_id,
        )
        if preview_content_hash(content) != _verified_content_hash:
            raise ToolError(
                "confirm_invalid",
                "confirm_token rejected (stale): mailbox rules changed after preview",
                hint=_STALE_RULE_HINT,
            )
        planned_dto = content["rule"]
        if raw_rule_id is None:
            stored, verified = gateway_rules.create_rule(
                account, rule_obj, before_rules=existing_rules
            )
            if not verified or stored is None:
                return {
                    "ok": True,
                    "verified": False,
                    "id": None,
                    "rule": {"id": None, **planned_dto},
                    "replaced": False,
                    "note": (
                        "Rule was created on Exchange, but read-back failed; "
                        "call list_rules to inspect current rules and ids."
                    ),
                }
        else:
            stored, verified = gateway_rules.replace_rule(
                account, rule_obj, display_ref=planned_dto.get("id") or rule_id
            )
            if not verified or stored is None:
                return {
                    "ok": True,
                    "verified": False,
                    "id": planned_dto["id"],
                    "rule": dict(planned_dto),
                    "replaced": True,
                    "note": (
                        "Rule was updated on Exchange, but read-back failed; "
                        "call list_rules to verify the current rule state."
                    ),
                }
        stored_dto = rule_to_dto(ctx, account, stored)
        return {
            "ok": True,
            "verified": True,
            "id": stored_dto["id"],
            "rule": stored_dto,
            "replaced": raw_rule_id is not None,
        }

    async with _rules_lock():
        return await ctx.gateway.call(work)


async def _delete_rule_preview(
    ctx: Context, kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    _reject_unknown_top_args(kwargs, _DELETE_RULE_TOP_KEYS, "delete_rule")
    rule_id_arg = kwargs.get("rule_id")
    raw_rule_id = _resolve_rule_id(ctx, rule_id_arg)

    def work(account: Any) -> Dict[str, Any]:
        _, content = _prepare_delete_state(
            ctx,
            account,
            rule_id=rule_id_arg,
            raw_rule_id=raw_rule_id,
        )
        return content

    return await ctx.gateway.call(work)


async def _delete_rule(
    ctx: Context,
    *,
    rule_id: str,
    _verified_content_hash: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    _reject_unknown_top_args(extra, frozenset(), "delete_rule")
    raw_rule_id = _resolve_rule_id(ctx, rule_id)
    if not isinstance(_verified_content_hash, str) or not _verified_content_hash:
        raise ToolError(
            "confirm_invalid",
            "confirm_token rejected (stale): rule preview state was not verified",
            hint=_STALE_RULE_HINT,
        )

    def work(account: Any) -> Dict[str, Any]:
        existing_rules, content = _prepare_delete_state(
            ctx,
            account,
            rule_id=rule_id,
            raw_rule_id=raw_rule_id,
        )
        if preview_content_hash(content) != _verified_content_hash:
            raise ToolError(
                "confirm_invalid",
                "confirm_token rejected (stale): rule changed after preview",
                hint=_STALE_RULE_HINT,
            )
        rule_handle = content["rule"].get("id") or rule_id
        deleted_raw_id, deleted_name = gateway_rules.delete_rule(
            account,
            raw_rule_id,
            rules=existing_rules,
            display_ref=rule_handle,
        )
        return {
            "ok": True,
            "id": ctx.aliaser.alias_for(deleted_raw_id, "r"),
            "name": deleted_name,
        }

    async with _rules_lock():
        return await ctx.gateway.call(work)


# --- JSON schemas ------------------------------------------------------------


def _obj(
    props: Dict[str, Any], required: Optional[List[str]] = None
) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_NAMED_STR = {"type": "string", "maxLength": MAX_RULE_STR_LEN}
_BOOL = {"type": "boolean"}
_BOUNDED_STR_LIST = {
    "type": "array",
    "items": {"type": "string", "maxLength": MAX_RULE_STR_LEN},
    "minItems": 1,
}
_IMPORTANCE = {"type": "string", "enum": ["low", "normal", "high"]}

_ACTIONS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "Mailbox-internal rule actions (at least one must be enabled). "
        "Sending, forwarding, redirecting, server replies, and deleting are "
        "never writable."
    ),
    "properties": {
        "move_to_folder": {
            "type": "string",
            "description": "Target folder (well-known name, f-alias, or path).",
        },
        "copy_to_folder": {
            "type": "string",
            "description": "Target folder (well-known name, f-alias, or path).",
        },
        "assign_categories": _BOUNDED_STR_LIST,
        "mark_as_read": _BOOL,
        "mark_importance": _IMPORTANCE,
        "stop_processing_rules": _BOOL,
    },
    "additionalProperties": False,
}

_CONDITIONS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "Predicates an incoming message must match. Omitting conditions or "
        "passing {} matches every incoming message (warned in preview)."
    ),
    "properties": {
        "from_addresses": _BOUNDED_STR_LIST,
        "sent_to_addresses": _BOUNDED_STR_LIST,
        "subject_contains": _BOUNDED_STR_LIST,
        "body_contains": _BOUNDED_STR_LIST,
        "subject_or_body_contains": _BOUNDED_STR_LIST,
        "sender_contains": _BOUNDED_STR_LIST,
        "recipient_contains": _BOUNDED_STR_LIST,
        "has_attachments": _BOOL,
        "importance": _IMPORTANCE,
        "sent_to_me": _BOOL,
        "sent_cc_me": _BOOL,
        "sent_only_to_me": _BOOL,
        "sent_to_or_cc_me": _BOOL,
        "not_sent_to_me": _BOOL,
    },
    "additionalProperties": False,
}


# --- the pack ----------------------------------------------------------------


TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="list_rules",
        description=(
            "List server-side Exchange Inbox rules. Reports every rule with an "
            "r-alias id, conditions, actions, and editable=true|false (plus "
            "unsupported fields, sends_mail/recipients for rules that send or "
            "forward mail, and in_error when Exchange flagged the rule). Top-"
            "level outlook_only_rules (true|false|null) indicates whether "
            "Outlook-only client rules exist that any EWS rule write would "
            "remove."
        ),
        side_effect_class="read",
        input_schema=_obj({}),
        handler=_list_rules,
        confirm=False,
    ),
    ToolSpec(
        name="save_rule",
        description=(
            "Create a server-side Inbox rule or wholesale-replace an existing "
            "editable rule (pass rule_id to replace). Only mailbox-internal "
            "actions are allowed (move_to_folder, copy_to_folder, "
            "assign_categories, mark_as_read, mark_importance, "
            "stop_processing_rules); sending/forwarding/deleting actions and "
            "Deleted Items/Junk targets are refused, and rules with "
            "editable=false cannot be replaced. Always two-phase confirmed; "
            "warns when conditions are empty or Outlook-only rules would be lost."
        ),
        side_effect_class="write",
        input_schema=_obj(
            {
                "name": _NAMED_STR,
                "actions": _ACTIONS_SCHEMA,
                "conditions": _CONDITIONS_SCHEMA,
                "priority": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Execution order (1 runs first; defaults to 1 on "
                        "create, or keeps the current priority on replace)."
                    ),
                },
                "enabled": {
                    "type": "boolean",
                    "description": (
                        "Whether the rule is active (defaults to true on "
                        "create, or keeps the current state on replace)."
                    ),
                },
                "rule_id": {
                    "type": "string",
                    "description": "Existing r-alias to wholesale-replace; omit to create.",
                },
            },
            required=["name", "actions"],
        ),
        handler=_save_rule,
        confirm=True,
        preview=_save_rule_preview,
    ),
    ToolSpec(
        name="delete_rule",
        description=(
            "Delete a server-side Inbox rule by rule_id (including rules "
            "reported with editable=false). Always two-phase confirmed; warns "
            "when Outlook-only rules exist on the mailbox."
        ),
        side_effect_class="write",
        input_schema=_obj({"rule_id": _STR}, required=["rule_id"]),
        handler=_delete_rule,
        confirm=True,
        preview=_delete_rule_preview,
    ),
]
