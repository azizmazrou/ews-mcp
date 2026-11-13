"""Tool pack: rules — server-side Exchange Inbox rules (list, save, delete).

Safety invariants (DESIGN.md §Tools, §Safety):
- Writable actions are strictly allow-listed to mailbox-internal metadata and
  filing operations (``move_to_folder``, ``copy_to_folder``,
  ``assign_categories``, ``mark_as_read``, ``mark_importance``,
  ``stop_processing_rules``). Nothing that sends, forwards, redirects, replies,
  alerts, or deletes can ever be constructed from tool arguments.
- Existing rules that use non-writable actions, exceptions, read-only
  conditions, or ``is_not_supported`` are reported by ``list_rules`` with
  ``editable: false`` and ``unsupported`` (plus ``sends_mail`` and
  ``recipients`` when they send mail), and ``save_rule`` refuses to replace
  them so unsupported fields are never silently dropped.
- Both ``save_rule`` and ``delete_rule`` are class ``write`` (draft tier) and
  ALWAYS two-phase confirmed with a content-bound preview carrying catch-all
  and ``OutlookRuleBlobExists`` warnings.
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from exchangelib import Account
from exchangelib.errors import UnauthorizedError
from exchangelib.folders import DistinguishedFolderId, Folder
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
from .base import Context, ToolSpec, _normalise_recipient, preview_content_hash

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


# --- validation & normalization helpers --------------------------------------


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


def _require_non_empty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("validation", f"{label} must be a non-empty string")
    return value.strip()


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
        if addr not in out:
            out.append(addr)
    return out


def _validate_save_inputs(
    *,
    name: Any,
    actions: Any,
    conditions: Any,
    priority: Any,
    enabled: Any,
) -> Tuple[str, int, bool, Dict[str, Any], Dict[str, Any]]:
    """Validate and normalize ``save_rule`` arguments before touching EWS."""
    clean_name = _require_non_empty_str(name, "name")
    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 1:
        raise ToolError("validation", "priority must be an integer >= 1")
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
            actions["assign_categories"], "actions.assign_categories", strip=True
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
                raw_conds[dto_key], f"conditions.{dto_key}", strip=False
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

    return clean_name, priority, clean_enabled, norm_actions, norm_conds


# --- folder helpers ----------------------------------------------------------


def _folder_path(folder: Any) -> str:
    explicit = getattr(folder, "path", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    try:
        parts = list(getattr(folder, "parts", None) or [])
        if len(parts) > 2:
            joined = "/".join(
                p.name for p in parts[2:] if getattr(p, "name", None)
            )
            if joined:
                return joined
    except Exception:
        pass
    name = getattr(folder, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(getattr(folder, "id", "") or "")


def _folder_to_target(folder: Any, target_cls: Any, ref: str) -> Any:
    if isinstance(folder, Folder):
        fid = folder.to_id()
        if isinstance(fid, DistinguishedFolderId):
            return target_cls(distinguished_folder_id=fid)
        return target_cls(folder_id=fid)
    raw_id = getattr(folder, "id", None)
    if not raw_id:
        raise ToolError("validation", f"Resolved folder {ref!r} has no folder id.")
    raw_ck = getattr(folder, "changekey", None)
    return target_cls(
        folder_id=FolderId(
            id=str(raw_id),
            changekey=str(raw_ck) if raw_ck else None,
        )
    )


def _target_to_folder_label(ctx: Context, account: Any, target: Any) -> str:
    dfid = getattr(target, "distinguished_folder_id", None)
    if dfid is not None and getattr(dfid, "id", None):
        ref = str(dfid.id)
        if hasattr(ctx.gateway, "resolve_folder"):
            try:
                folder = ctx.gateway.resolve_folder(account, ref, ctx.aliaser)
                path = _folder_path(folder)
                if path:
                    return path
            except UnauthorizedError:
                raise
            except Exception:
                pass
        return ref

    fid = getattr(target, "folder_id", None)
    raw_id = getattr(fid, "id", None) if fid is not None else None
    if raw_id:
        ref = str(raw_id)
        if hasattr(ctx.gateway, "resolve_folder"):
            try:
                folder = ctx.gateway.resolve_folder(account, ref, ctx.aliaser)
                path = _folder_path(folder)
                if path:
                    return path
            except UnauthorizedError:
                raise
            except Exception:
                pass
        return ctx.aliaser.alias_for(ref, "f")
    return ""


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
) -> Tuple[Rule, Dict[str, Any]]:
    """Build a real ``exchangelib.properties.Rule`` from allow-listed inputs."""
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

    discard_ids: Optional[Set[str]] = None

    def _target_folder(ref: str, label: str) -> Any:
        # Filing every match into Deleted Items or Junk is a deleting rule
        # under another name, and deleting is not writable here.
        nonlocal discard_ids
        resolved = ctx.gateway.resolve_folder(account, ref, ctx.aliaser)
        if discard_ids is None:
            discard_ids = gateway_rules.discard_folder_ids(account)
        if gateway_rules.is_discard_target(resolved, discard_ids):
            raise ToolError(
                "validation",
                f"{label}: a rule may not file mail into Deleted Items or Junk "
                "Email, or a folder below them (that would be a deleting rule).",
                hint="Choose another folder. A rule that discards mail can "
                     "only be made in Outlook.",
            )
        return resolved

    action_kwargs: Dict[str, Any] = {}
    actions_dto: Dict[str, Any] = {}
    if "move_to_folder" in norm_actions:
        ref = norm_actions["move_to_folder"]
        folder = _target_folder(ref, "actions.move_to_folder")
        action_kwargs["move_to_folder"] = _folder_to_target(
            folder, MoveToFolder, ref
        )
        actions_dto["move_to_folder"] = _folder_path(folder)
    if "copy_to_folder" in norm_actions:
        ref = norm_actions["copy_to_folder"]
        folder = _target_folder(ref, "actions.copy_to_folder")
        action_kwargs["copy_to_folder"] = _folder_to_target(
            folder, CopyToFolder, ref
        )
        actions_dto["copy_to_folder"] = _folder_path(folder)
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
        "priority": clean_priority,
        "is_enabled": clean_enabled,
        "conditions": conditions_obj,
        "actions": actions_obj,
    }
    if raw_rule_id is not None:
        rule_kwargs["id"] = raw_rule_id
    if isinstance(account, Account):
        rule_kwargs["account"] = account

    planned_dto: Dict[str, Any] = {
        "name": clean_name,
        "priority": clean_priority,
        "enabled": clean_enabled,
        "conditions": conds_dto,
        "actions": actions_dto,
        "editable": True,
    }
    if raw_rule_id is not None:
        planned_dto["id"] = ctx.aliaser.alias_for(raw_rule_id, "r")

    return Rule(**rule_kwargs), planned_dto


def _is_field_active(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value is True
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    if isinstance(value, str):
        return len(value) > 0
    if isinstance(value, CopyToFolder):
        return (
            getattr(value, "folder_id", None) is not None
            or getattr(value, "distinguished_folder_id", None) is not None
        )
    fields = getattr(value, "FIELDS", None)
    if fields is not None:
        return any(getattr(value, f.name, None) is not None for f in fields)
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


def rule_to_dto(ctx: Context, account: Any, rule: Rule) -> Dict[str, Any]:
    """Project an ``exchangelib.properties.Rule`` to its public DTO."""
    raw_id = getattr(rule, "id", None)
    alias = ctx.aliaser.alias_for(str(raw_id), "r") if raw_id else None

    unsupported: List[str] = []
    if getattr(rule, "is_not_supported", None) is True:
        unsupported.append("is_not_supported")

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
                if bad:
                    unsupported.append(f"conditions.{fname}")
            elif fname in _EWS_TO_DTO_STRING_CONDITIONS:
                cond_dto[_EWS_TO_DTO_STRING_CONDITIONS[fname]] = [
                    str(s) for s in val
                ]
            elif fname == "importance":
                cond_dto["importance"] = str(val).lower()
            elif fname in _BOOL_CONDITIONS:
                cond_dto[fname] = True
            else:
                unsupported.append(f"conditions.{fname}")

    exc_obj = getattr(rule, "exceptions", None)
    if exc_obj is not None:
        for field in Exceptions.FIELDS:
            val = getattr(exc_obj, field.name, None)
            if _is_field_active(val):
                unsupported.append(f"exceptions.{field.name}")

    act_dto: Dict[str, Any] = {}
    sends_mail = False
    recipients: List[str] = []
    act_obj = getattr(rule, "actions", None)
    if act_obj is not None:
        for field in Actions.FIELDS:
            fname = field.name
            val = getattr(act_obj, fname, None)
            if not _is_field_active(val):
                continue
            if fname == "move_to_folder":
                act_dto["move_to_folder"] = _target_to_folder_label(
                    ctx, account, val
                )
            elif fname == "copy_to_folder":
                act_dto["copy_to_folder"] = _target_to_folder_label(
                    ctx, account, val
                )
            elif fname == "assign_categories":
                act_dto["assign_categories"] = [str(c) for c in val]
            elif fname == "mark_as_read":
                act_dto["mark_as_read"] = True
            elif fname == "mark_importance":
                act_dto["mark_importance"] = str(val).lower()
            elif fname == "stop_processing_rules":
                act_dto["stop_processing_rules"] = True
            else:
                unsupported.append(f"actions.{fname}")
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
        unsupported.append("actions.unknown")

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
    priority: int,
    enabled: bool,
    rule_id: Optional[str],
    raw_rule_id: Optional[str],
) -> Tuple[List[Rule], Optional[bool], Rule, Dict[str, Any]]:
    existing_rules, blob_exists = gateway_rules.fetch_rules(account)
    rule_obj, planned_dto = build_rule(
        ctx,
        account,
        name=name,
        actions=actions,
        conditions=conditions,
        priority=priority,
        enabled=enabled,
        raw_rule_id=raw_rule_id,
    )

    current_dto: Optional[Dict[str, Any]] = None
    if raw_rule_id is None:
        for existing in existing_rules:
            if (existing.display_name or "") == planned_dto["name"]:
                raise ToolError(
                    "validation",
                    f"A rule named {planned_dto['name']!r} already exists; "
                    "pass rule_id to replace an existing rule or choose a "
                    "distinct name.",
                    hint="Call list_rules to inspect existing rules and their ids.",
                )
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
        content["current"] = current_dto
    if warnings:
        content["warnings"] = warnings
    content["_attachments"] = [{
        "op": "save_rule",
        "rule_id": raw_rule_id,
        "rule": planned_dto,
        "current": current_dto,
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
        "outlook_only_rules": blob_exists,
    }]
    return existing_rules, content


# --- tool handlers & preview hooks -------------------------------------------


async def _list_rules(ctx: Context) -> Dict[str, Any]:
    def work(account: Any) -> Tuple[List[Dict[str, Any]], Optional[bool]]:
        rules_list, blob_exists = gateway_rules.fetch_rules(account)
        dtos = [rule_to_dto(ctx, account, item) for item in rules_list]
        return dtos, blob_exists

    items, blob_exists = await ctx.gateway.call(work)
    out = envelope(items, total_available=len(items), offset=0)
    out["outlook_only_rules"] = blob_exists
    return out


async def _save_rule_preview(
    ctx: Context, kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    raw_rule_id: Optional[str] = None
    rule_id_arg = kwargs.get("rule_id")
    if rule_id_arg is not None:
        raw_rule_id = _resolve_rule_id(ctx, rule_id_arg)
    _validate_save_inputs(
        name=kwargs.get("name"),
        actions=kwargs.get("actions"),
        conditions=kwargs.get("conditions"),
        priority=kwargs.get("priority", 1),
        enabled=kwargs.get("enabled", True),
    )

    def work(account: Any) -> Dict[str, Any]:
        _, _, _, content = _prepare_save_state(
            ctx,
            account,
            name=kwargs["name"],
            actions=kwargs["actions"],
            conditions=kwargs.get("conditions"),
            priority=kwargs.get("priority", 1),
            enabled=kwargs.get("enabled", True),
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
    priority: int = 1,
    enabled: bool = True,
    rule_id: Optional[str] = None,
    _verified_content_hash: Optional[str] = None,
) -> Dict[str, Any]:
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
        if raw_rule_id is None:
            stored = gateway_rules.create_rule(
                account, rule_obj, before_rules=existing_rules
            )
        else:
            stored = gateway_rules.replace_rule(account, rule_obj)
        stored_dto = rule_to_dto(ctx, account, stored)
        return {
            "ok": True,
            "id": stored_dto["id"],
            "rule": stored_dto,
            "replaced": raw_rule_id is not None,
        }

    return await ctx.gateway.call(work)


async def _delete_rule_preview(
    ctx: Context, kwargs: Dict[str, Any]
) -> Dict[str, Any]:
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
) -> Dict[str, Any]:
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
        deleted_raw_id, deleted_name = gateway_rules.delete_rule(
            account,
            raw_rule_id,
            rules=existing_rules,
            display_ref=rule_id,
        )
        return {
            "ok": True,
            "id": ctx.aliaser.alias_for(deleted_raw_id, "r"),
            "name": deleted_name,
        }

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
_BOOL = {"type": "boolean"}
_STR_LIST = {"type": "array", "items": {"type": "string"}, "minItems": 1}
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
        "assign_categories": _STR_LIST,
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
        "from_addresses": _STR_LIST,
        "sent_to_addresses": _STR_LIST,
        "subject_contains": _STR_LIST,
        "body_contains": _STR_LIST,
        "subject_or_body_contains": _STR_LIST,
        "sender_contains": _STR_LIST,
        "recipient_contains": _STR_LIST,
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
            "stop_processing_rules); sending/forwarding/deleting actions are "
            "refused, and rules with editable=false cannot be replaced. Always "
            "two-phase confirmed; warns when conditions are empty or "
            "Outlook-only rules would be lost."
        ),
        side_effect_class="write",
        input_schema=_obj(
            {
                "name": _STR,
                "actions": _ACTIONS_SCHEMA,
                "conditions": _CONDITIONS_SCHEMA,
                "priority": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "Execution order (1 runs first).",
                },
                "enabled": {"type": "boolean", "default": True},
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
