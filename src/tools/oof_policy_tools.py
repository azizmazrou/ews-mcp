"""OOF policy: templates + forward rules applied on demand.

Exchange's native OOF (set via ``oof_settings``) handles static auto-replies.
This module layers on:

* **Templates** — internal / external message stored once, reapplied on
  every "I'm OOF" activation so the user doesn't re-type.
* **Forward rules** — "when OOF, forward messages matching X to Y". Unlike
  native Exchange rules, these are evaluated on demand via
  :class:`ApplyOOFPolicyTool` (and optionally by the main rule engine on
  incoming mail).
* **VIP passthrough** — a marker the agent can honour ("don't suppress
  notifications for VIPs").

Security
--------
* Forward destinations are plain email strings; the tool performs basic
  syntactic validation (``local@domain``). There is no DNS lookup.
* Rules reuse :meth:`RuleRepo.validate_match` so match keys are
  allow-listed.
* Policy is stored per primary mailbox (never the impersonated one).
* Applying forwards creates drafts (side effect) — use ``dry_run=true``
  first to preview.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .base import BaseTool
from ..exceptions import ToolExecutionError, ValidationError
from ..memory import OOFPolicy, OOFPolicyRepo, RuleRepo
from ..utils import format_success_response, find_message_for_account, safe_get


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _validate_email(value: str) -> str:
    if not isinstance(value, str) or not _EMAIL_RE.match(value):
        raise ValidationError(f"Not a valid email address: {value!r}")
    return value


class ConfigureOOFPolicyTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "configure_oof_policy",
            "description": (
                "Store an out-of-office policy: optional internal/external "
                "reply templates, forward rules, and a VIP-passthrough flag. "
                "This does NOT enable OOF on Exchange — pair with "
                "oof_settings(action='set') to do that."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "internal_template": {
                        "type": "string",
                        "description": "Reply text for internal senders (optional)",
                    },
                    "external_template": {
                        "type": "string",
                        "description": "Reply text for external senders (optional)",
                    },
                    "vip_passthrough": {
                        "type": "boolean",
                        "default": True,
                        "description": "Agent may alert user for messages from VIPs even while OOF",
                    },
                    "forward_rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "match": {"type": "object"},
                                "to": {"type": "string", "description": "Email address"},
                                "reason": {"type": "string"},
                            },
                            "required": ["match", "to"],
                        },
                        "description": "List of {match, to, reason?} forward rules",
                    },
                },
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        internal = kwargs.get("internal_template")
        external = kwargs.get("external_template")
        vip_passthrough = bool(kwargs.get("vip_passthrough", True))
        forward_rules = kwargs.get("forward_rules") or []
        if not isinstance(forward_rules, list):
            raise ToolExecutionError("forward_rules must be an array")
        if len(forward_rules) > 20:
            raise ToolExecutionError("at most 20 forward rules")

        cleaned: List[Dict[str, Any]] = []
        for idx, rule in enumerate(forward_rules):
            if not isinstance(rule, dict):
                raise ValidationError(f"forward_rules[{idx}] must be an object")
            match = RuleRepo.validate_match(rule.get("match") or {})
            to_addr = _validate_email(rule.get("to") or "")
            cleaned.append({
                "match": match,
                "to": to_addr,
                "reason": (rule.get("reason") or None),
            })

        # Cap template length.
        for label, value in (("internal_template", internal), ("external_template", external)):
            if value is not None and len(str(value)) > 8000:
                raise ValidationError(f"{label} too long (max 8000 chars)")

        policy = OOFPolicy(
            internal_template=internal,
            external_template=external,
            vip_passthrough=vip_passthrough,
            forward_rules=cleaned,
        )
        OOFPolicyRepo(self.get_memory_store()).save(policy)
        return format_success_response(
            "OOF policy stored",
            policy=policy.to_dict(),
        )


class GetOOFPolicyTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_oof_policy",
            "description": "Return the currently stored OOF policy (if any).",
            "inputSchema": {"type": "object", "properties": {}},
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        policy = OOFPolicyRepo(self.get_memory_store()).get()
        if policy is None:
            return format_success_response("No OOF policy stored", has_policy=False)
        return format_success_response(
            "OOF policy fetched",
            has_policy=True,
            policy=policy.to_dict(),
        )


class ApplyOOFPolicyTool(BaseTool):
    """Evaluate forward rules against one message and optionally create forwards as drafts."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "apply_oof_policy",
            "description": (
                "Evaluate the stored OOF policy's forward rules against a "
                "message. Creates forward DRAFTS (never sends) so the user "
                "can review on return. Set dry_run=true to report what WOULD "
                "happen without creating drafts."
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
        from .rule_tools import _match_one

        message_id = kwargs.get("message_id")
        target_mailbox = kwargs.get("target_mailbox")
        dry_run = bool(kwargs.get("dry_run", False))
        if not message_id:
            raise ToolExecutionError("message_id is required")

        policy = OOFPolicyRepo(self.get_memory_store()).get()
        if policy is None or not policy.forward_rules:
            return format_success_response(
                "No OOF policy with forward rules configured",
                has_policy=policy is not None,
                actions=[],
            )

        account = self.get_account(target_mailbox)
        message = find_message_for_account(account, message_id)

        actions: List[Dict[str, Any]] = []
        for rule in policy.forward_rules:
            if not _match_one(rule.get("match") or {}, message):
                continue
            to_addr = rule.get("to")
            entry: Dict[str, Any] = {
                "matched": True,
                "to": to_addr,
                "reason": rule.get("reason"),
                "dry_run": dry_run,
            }
            if dry_run:
                entry["status"] = "preview"
                actions.append(entry)
                continue

            try:
                from .email_tools import ForwardEmailTool
                # We build a forward via the real tool but pipe it through
                # the Drafts folder by using reply with a "please reply"-style
                # body and explicitly creating a draft. We reuse
                # CreateForwardDraftTool to keep the HTML construction
                # consistent (and safe — it HTML-escapes headers).
                from .email_tools_draft import CreateForwardDraftTool
                draft_tool = CreateForwardDraftTool(self.ews_client)
                result = await draft_tool.safe_execute(
                    message_id=message_id,
                    to=[to_addr],
                    body=(
                        f"<p>Auto-forwarded while I'm out of office"
                        + (f": {rule.get('reason')}" if rule.get("reason") else "")
                        + "</p>"
                    ),
                    target_mailbox=target_mailbox,
                )
                entry["status"] = "draft_created" if result.get("success") else "error"
                entry["draft_id"] = result.get("message_id")
                if not result.get("success"):
                    entry["error"] = result.get("error")
            except Exception as exc:
                entry["status"] = "error"
                entry["error"] = str(exc)
            actions.append(entry)

        return format_success_response(
            f"{len(actions)} forward rule(s) matched",
            dry_run=dry_run,
            message_id=message_id,
            actions=actions,
        )
