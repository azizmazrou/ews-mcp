"""Rule engine: declarative "when X, do Y" automations.

Rules are stored per mailbox, evaluated on demand against a specific message
via :class:`EvaluateRulesOnMessageTool`, and produce a list of actions that
have been applied (plus any commitments auto-tracked). No background
watcher yet — callers invoke evaluation explicitly; the design cleanly
accepts a watcher later.

Security
--------
* Match keys are an allow-list (see
  :meth:`src.memory.models.RuleRepo.validate_match`).
* Action types are an allow-list (see
  :data:`src.memory.models._ALLOWED_ACTION_TYPES`). No arbitrary code path.
* Evaluation uses only the message metadata already accessible via
  exchangelib. No external HTTP call from a rule.
* A single rule can contain at most 10 actions; a single evaluation
  applies at most 1 rule's worth of actions per message to avoid amplify
  loops.
"""

from __future__ import annotations

import fnmatch
import re
import time
from typing import Any, Dict, List, Optional

from .base import BaseTool
from ..exceptions import ToolExecutionError, ValidationError
from ..memory import Rule, RuleRepo, Commitment, CommitmentRepo, new_id
from ..utils import find_message_for_account, format_success_response, safe_get


# --- Matching ------------------------------------------------------------


def _get_sender(message) -> str:
    sender = safe_get(message, "sender", None)
    if sender is None:
        return ""
    return safe_get(sender, "email_address", "") or ""


def _get_recipients(message) -> List[str]:
    return [
        safe_get(r, "email_address", "") or ""
        for r in (safe_get(message, "to_recipients", []) or [])
    ]


def _get_categories(message) -> List[str]:
    cats = safe_get(message, "categories", None)
    return list(cats) if cats else []


def _match_one(match: Dict[str, Any], message) -> bool:
    """Return True iff every declared match key is satisfied."""
    # All keys must match (AND semantics). Empty match => universal rule.
    if not match:
        return True

    if "from" in match:
        pattern = str(match["from"]).lower()
        sender = _get_sender(message).lower()
        if not fnmatch.fnmatchcase(sender, pattern):
            return False

    if "to" in match:
        pattern = str(match["to"]).lower()
        recipients = [r.lower() for r in _get_recipients(message)]
        if not any(fnmatch.fnmatchcase(r, pattern) for r in recipients):
            return False

    if "subject_contains" in match:
        needle = str(match["subject_contains"]).lower()
        subject = (safe_get(message, "subject", "") or "").lower()
        if needle not in subject:
            return False

    if "body_contains" in match:
        needle = str(match["body_contains"]).lower()
        body = str(safe_get(message, "text_body", "") or safe_get(message, "body", "") or "").lower()
        if needle not in body:
            return False

    if "has_attachment" in match:
        expected = bool(match["has_attachment"])
        actual = bool(safe_get(message, "has_attachments", False))
        if expected != actual:
            return False

    if "is_unread" in match:
        expected = bool(match["is_unread"])
        actual = not bool(safe_get(message, "is_read", False))
        if expected != actual:
            return False

    if "importance" in match:
        expected = str(match["importance"])
        actual = str(safe_get(message, "importance", "Normal"))
        if expected.lower() != actual.lower():
            return False

    if "categories_any" in match:
        wanted = {str(c).lower() for c in match["categories_any"]}
        have = {str(c).lower() for c in _get_categories(message)}
        if not wanted.intersection(have):
            return False

    if "categories_all" in match:
        wanted = {str(c).lower() for c in match["categories_all"]}
        have = {str(c).lower() for c in _get_categories(message)}
        if not wanted.issubset(have):
            return False

    return True


# --- Action dispatch -----------------------------------------------------


async def _apply_actions(
    tool: BaseTool,
    account,
    message,
    actions: List[Dict[str, Any]],
    *,
    dry_run: bool,
) -> List[Dict[str, Any]]:
    """Apply a list of validated actions to a message. Returns per-action logs."""
    from ..memory import CommitmentRepo

    applied: List[Dict[str, Any]] = []
    for action in actions:
        action_type = action.get("type")
        log_entry: Dict[str, Any] = {"type": action_type, "dry_run": dry_run}
        try:
            if action_type == "flag_importance":
                importance = action.get("importance", "High")
                if importance not in ("Low", "Normal", "High"):
                    raise ValidationError(f"invalid importance: {importance!r}")
                if not dry_run:
                    message.importance = importance
                    message.save(update_fields=["importance"])
                log_entry["importance"] = importance

            elif action_type == "categorize":
                categories = list(action.get("categories") or [])
                if not categories:
                    raise ValidationError("categorize requires non-empty categories list")
                if not dry_run:
                    existing = list(safe_get(message, "categories", []) or [])
                    merged = list(dict.fromkeys(existing + categories))
                    message.categories = merged
                    message.save(update_fields=["categories"])
                log_entry["categories"] = categories

            elif action_type == "move_to_folder":
                destination = str(action.get("destination") or "").strip()
                if not destination:
                    raise ValidationError("move_to_folder requires 'destination'")
                if not dry_run:
                    # Use the existing folder resolver for consistency.
                    from .folder_tools import resolve_folder_for_account
                    folder = resolve_folder_for_account(account, folder_name=destination)
                    message.move(folder)
                log_entry["destination"] = destination

            elif action_type == "mark_read":
                if not dry_run:
                    message.is_read = True
                    message.save(update_fields=["is_read"])
                log_entry["result"] = "marked read"

            elif action_type == "track_commitment":
                description = str(action.get("description") or "").strip()
                if not description:
                    raise ValidationError("track_commitment requires 'description'")
                owner = str(action.get("owner") or "me")
                due_at = action.get("due_at")  # epoch seconds or None
                if not dry_run:
                    repo = CommitmentRepo(tool.get_memory_store())
                    c = CommitmentRepo.new(
                        description=description,
                        owner=owner,
                        due_at=float(due_at) if due_at else None,
                        message_id=safe_get(message, "id", None)
                        if isinstance(safe_get(message, "id", None), str)
                        else None,
                        source="extracted",
                    )
                    repo.save(c)
                log_entry["description"] = description

            elif action_type == "notify_agent":
                # Purely informational: the tool returns the note so the LLM
                # can surface it to the user. No side effects.
                log_entry["note"] = str(action.get("note") or "")

            else:
                raise ValidationError(f"unknown action type: {action_type!r}")

            log_entry["status"] = "ok"
        except Exception as exc:
            log_entry["status"] = "error"
            log_entry["error"] = str(exc)
        applied.append(log_entry)
    return applied


# --- Tools ---------------------------------------------------------------


class RuleCreateTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "rule_create",
            "description": (
                "Create a declarative automation rule. Match keys: from, to, "
                "subject_contains, body_contains, has_attachment, is_unread, "
                "categories_any, categories_all, importance. Action types: "
                "flag_importance, categorize, move_to_folder, mark_read, "
                "track_commitment, notify_agent."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short human-readable name"},
                    "match": {"type": "object"},
                    "actions": {"type": "array", "items": {"type": "object"}},
                    "enabled": {"type": "boolean", "default": True},
                },
                "required": ["name", "match", "actions"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        name = (kwargs.get("name") or "").strip()
        match = kwargs.get("match") or {}
        actions = kwargs.get("actions") or []
        enabled = bool(kwargs.get("enabled", True))
        if not name:
            raise ToolExecutionError("name is required")
        RuleRepo.validate_match(match)
        RuleRepo.validate_actions(actions)

        rule = Rule(
            rule_id=new_id(),
            name=name,
            match=match,
            actions=actions,
            enabled=enabled,
        )
        RuleRepo(self.get_memory_store()).save(rule)
        return format_success_response(
            "Rule created",
            rule=rule.to_dict(),
        )


class RuleListTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "rule_list",
            "description": "List stored rules.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "enabled_only": {"type": "boolean", "default": False},
                },
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        enabled_only = bool(kwargs.get("enabled_only", False))
        rules = RuleRepo(self.get_memory_store()).list(enabled_only=enabled_only)
        return format_success_response(
            f"{len(rules)} rule(s)",
            count=len(rules),
            rules=[r.to_dict() for r in rules],
        )


class RuleDeleteTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "rule_delete",
            "description": "Delete a rule by id.",
            "inputSchema": {
                "type": "object",
                "properties": {"rule_id": {"type": "string"}},
                "required": ["rule_id"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        rule_id = kwargs.get("rule_id")
        if not rule_id:
            raise ToolExecutionError("rule_id is required")
        deleted = RuleRepo(self.get_memory_store()).delete(rule_id)
        return format_success_response(
            "Deleted" if deleted else "Nothing to delete",
            deleted=deleted,
            rule_id=rule_id,
        )


class RuleSimulateTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "rule_simulate",
            "description": (
                "Report which enabled rules would fire against one specific message, "
                "without applying any actions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "target_mailbox": {"type": "string"},
                },
                "required": ["message_id"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        message_id = kwargs.get("message_id")
        target_mailbox = kwargs.get("target_mailbox")
        if not message_id:
            raise ToolExecutionError("message_id is required")

        account = self.get_account(target_mailbox)
        message = find_message_for_account(account, message_id)
        rules = RuleRepo(self.get_memory_store()).list(enabled_only=True)

        matches = []
        for r in rules:
            if _match_one(r.match, message):
                matches.append({
                    "rule_id": r.rule_id,
                    "name": r.name,
                    "actions_preview": r.actions,
                })

        return format_success_response(
            f"{len(matches)} rule(s) would fire",
            message_id=message_id,
            matches=matches,
            mailbox=self.get_mailbox_info(target_mailbox),
        )


class EvaluateRulesOnMessageTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "evaluate_rules_on_message",
            "description": (
                "Run enabled rules against a single message. Side effects "
                "(flag, categorize, move, mark read, track commitment) are "
                "applied in order. Set dry_run=true to report what WOULD "
                "happen without mutating anything."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": False},
                    "target_mailbox": {"type": "string"},
                },
                "required": ["message_id"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        message_id = kwargs.get("message_id")
        target_mailbox = kwargs.get("target_mailbox")
        dry_run = bool(kwargs.get("dry_run", False))
        if not message_id:
            raise ToolExecutionError("message_id is required")

        account = self.get_account(target_mailbox)
        message = find_message_for_account(account, message_id)
        rules = RuleRepo(self.get_memory_store()).list(enabled_only=True)

        applied_rules: List[Dict[str, Any]] = []
        for r in rules:
            if not _match_one(r.match, message):
                continue
            outcomes = await _apply_actions(
                self, account, message, r.actions, dry_run=dry_run
            )
            applied_rules.append({
                "rule_id": r.rule_id,
                "name": r.name,
                "actions_applied": outcomes,
            })

        return format_success_response(
            f"{len(applied_rules)} rule(s) applied",
            dry_run=dry_run,
            message_id=message_id,
            rules_applied=applied_rules,
            mailbox=self.get_mailbox_info(target_mailbox),
        )
