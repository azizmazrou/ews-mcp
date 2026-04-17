"""Approval queue for side-effectful actions.

The agent can submit any action on the allow-list to the queue instead of
executing it directly. A human then approves or rejects via the other tools,
and :class:`ExecuteApprovedActionTool` dispatches the stored arguments to
the real tool.

Security
--------
* Only actions in :attr:`ApprovalRepo._ALLOWED_ACTIONS` can be queued.
* Approval IDs are UUID4 (128-bit). They are consumed atomically by
  ``MemoryStore.consume`` with a status-check guard, so a single approval
  cannot be redeemed twice even under concurrent calls.
* Approvals carry a TTL; expired entries are refused by the executor.
* Arguments are stored as-is but sensitive fields (body, tokens) are
  redacted on audit log write by the shared logging middleware.
"""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool
from ..exceptions import ToolExecutionError, ValidationError
from ..memory import ApprovalRepo, Approval
from ..utils import format_success_response


def _summary(action: str, args: Dict[str, Any]) -> str:
    """One-line preview of a pending action (for humans / LLM UI)."""
    if action in ("send_email", "reply_email", "forward_email"):
        to = args.get("to") or []
        subject = args.get("subject") or "(no subject)"
        return f"{action}: to={to} subject={subject!r}"
    if action == "delete_email":
        return f"delete_email: message_id={args.get('message_id')!r}"
    if action == "move_email":
        return f"move_email: {args.get('message_id')!r} -> {args.get('destination_folder')!r}"
    if action in ("create_appointment", "update_appointment"):
        return f"{action}: subject={args.get('subject')!r} start={args.get('start_time') or args.get('start')}"
    if action == "delete_appointment":
        return f"delete_appointment: id={args.get('appointment_id')!r}"
    return f"{action}: args_keys={sorted(args.keys())}"


class SubmitForApprovalTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "submit_for_approval",
            "description": (
                "Queue a side-effectful action for human approval instead of "
                "executing it immediately. Returns an approval_id that a human "
                "can approve or reject. Only a fixed allow-list of tools can "
                "be queued."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Tool name to execute on approval (e.g. 'send_email')",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments to pass to the tool when approved",
                    },
                    "ttl_seconds": {
                        "type": "integer",
                        "minimum": 60,
                        "maximum": 604800,
                        "default": 1800,
                        "description": "How long the approval is valid (seconds). Default 30 min.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional human-readable reason shown alongside the approval",
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Mailbox the action will target (record-keeping only)",
                    },
                },
                "required": ["action", "arguments"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        action = kwargs.get("action")
        arguments = kwargs.get("arguments")
        ttl = kwargs.get("ttl_seconds", 30 * 60)
        target_mailbox = kwargs.get("target_mailbox")
        if not action or arguments is None:
            raise ToolExecutionError("action and arguments are required")
        if not isinstance(arguments, dict):
            raise ToolExecutionError("arguments must be an object")

        repo = ApprovalRepo(self.get_memory_store())
        try:
            approval = repo.submit(
                action=action,
                arguments=arguments,
                ttl_seconds=int(ttl),
                target_mailbox=target_mailbox,
            )
        except ValidationError:
            raise
        return format_success_response(
            "Queued for approval",
            approval_id=approval.approval_id,
            action=approval.action,
            summary=_summary(approval.action, approval.arguments),
            requested_at=approval.requested_at,
            expires_at=approval.expires_at,
            target_mailbox=approval.target_mailbox,
        )


class ListPendingApprovalsTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "list_pending_approvals",
            "description": "List pending (not yet decided, not yet expired) approvals.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                },
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        limit = int(kwargs.get("limit", 50))
        repo = ApprovalRepo(self.get_memory_store())
        pending = repo.list_pending(limit=limit)
        return format_success_response(
            f"{len(pending)} pending approval(s)",
            count=len(pending),
            approvals=[
                {
                    "approval_id": a.approval_id,
                    "action": a.action,
                    "summary": _summary(a.action, a.arguments),
                    "requested_at": a.requested_at,
                    "expires_at": a.expires_at,
                    "target_mailbox": a.target_mailbox,
                    "requested_by": a.requested_by,
                }
                for a in pending
            ],
        )


class ApproveTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "approve",
            "description": (
                "Mark a pending approval as approved. Does NOT execute the "
                "action — call execute_approved_action with the approval_id "
                "to actually run it. This two-step design lets a human review "
                "a second time before side effects occur."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "approval_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["approval_id"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        approval_id = kwargs.get("approval_id")
        reason = kwargs.get("reason")
        if not approval_id:
            raise ToolExecutionError("approval_id is required")

        store = self.get_memory_store()
        repo = ApprovalRepo(store)
        decided = repo.decide(approval_id, approve=True, reason=reason)
        if decided is None:
            raise ToolExecutionError(
                "Approval not found, already decided, or expired"
            )
        # Re-persist under a new key in namespace "approval" with status
        # "approved" so execute_approved_action can pick it up. We use a
        # separate key to avoid conflict with the original pending slot.
        store.set("approval", f"decided.{decided.approval_id}", decided.to_dict(), ttl_seconds=600)
        return format_success_response(
            "Approved",
            approval_id=decided.approval_id,
            action=decided.action,
            decided_at=decided.decided_at,
        )


class RejectTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "reject",
            "description": "Mark a pending approval as rejected (permanently denies execution).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "approval_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["approval_id"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        approval_id = kwargs.get("approval_id")
        reason = kwargs.get("reason")
        if not approval_id:
            raise ToolExecutionError("approval_id is required")

        repo = ApprovalRepo(self.get_memory_store())
        decided = repo.decide(approval_id, approve=False, reason=reason)
        if decided is None:
            raise ToolExecutionError("Approval not found, already decided, or expired")
        return format_success_response(
            "Rejected",
            approval_id=decided.approval_id,
            action=decided.action,
            decided_at=decided.decided_at,
            reason=decided.reason,
        )


class ExecuteApprovedActionTool(BaseTool):
    """Dispatch a previously-approved action to its real tool.

    This tool is the only path by which a queued approval becomes a side
    effect. It:

    1. Fetches the ``decided.<approval_id>`` record from memory.
    2. Refuses anything with status != "approved" or past its 10-minute
       post-decision execution window.
    3. Atomically consumes the record so the same approval cannot be
       replayed.
    4. Looks up the tool by name in the registry shared with MCP.
    5. Delegates execution and returns the tool's result.
    """

    def __init__(self, ews_client, tools_registry: Dict[str, "BaseTool"]):
        super().__init__(ews_client)
        self._tools = tools_registry

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "execute_approved_action",
            "description": (
                "Execute a previously-approved action using its approval_id. "
                "Consumes the approval atomically — each id can be executed "
                "at most once."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "approval_id": {"type": "string"},
                },
                "required": ["approval_id"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        approval_id = kwargs.get("approval_id")
        if not approval_id:
            raise ToolExecutionError("approval_id is required")

        store = self.get_memory_store()
        rec = store.consume(
            "approval",
            f"decided.{approval_id}",
            expect_value_key="status",
            expect_value_equal="approved",
        )
        if rec is None:
            raise ToolExecutionError(
                "No approved-and-unexecuted action found for that approval_id"
            )

        approval = Approval(**rec.value)
        tool = self._tools.get(approval.action)
        if tool is None:
            raise ToolExecutionError(
                f"Tool {approval.action!r} is not registered; cannot execute"
            )

        # Delegate to the real tool. safe_execute gives us the usual error
        # handling, circuit breaker, audit log, etc.
        result = await tool.safe_execute(**approval.arguments)
        return format_success_response(
            "Approved action executed",
            approval_id=approval_id,
            action=approval.action,
            target_mailbox=approval.target_mailbox,
            tool_result=result,
        )
