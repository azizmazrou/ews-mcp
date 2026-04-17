"""Commitments: who owes what to whom by when.

Commitments are the secretary's ledger. Four tools:

* :class:`TrackCommitmentTool` — create one manually
* :class:`ListCommitmentsTool` — query open / overdue / done
* :class:`ResolveCommitmentTool` — mark done/cancelled
* :class:`ExtractCommitmentsTool` — AI-assisted detection from a thread

Each commitment stores an optional ``excerpt`` but not the full message
body. Audit logs go through the redaction layer already, so excerpts are
truncated to 2000 chars by the data model.

Security
--------
* All storage goes through :class:`CommitmentRepo`, which lives in the
  per-mailbox memory store — no cross-mailbox leakage.
* ``extract_commitments`` requires the AI layer to be enabled. When it
  isn't, the tool returns a ``success: False`` error rather than silently
  no-op'ing; callers that want a graceful fallback can use
  ``track_commitment`` with an agent-authored description.
* The extraction prompt is built from the EMAIL content only and never
  interpolates user-supplied strings into the system prompt.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from .base import BaseTool
from ..exceptions import ToolExecutionError, ValidationError
from ..memory import Commitment, CommitmentRepo
from ..utils import (
    find_message_for_account,
    format_success_response,
    parse_datetime_tz_aware,
    safe_get,
)


def _iso_to_epoch(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    dt = parse_datetime_tz_aware(value)
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except Exception:
        return None


def _epoch_to_iso(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _commitment_to_response(c: Commitment) -> Dict[str, Any]:
    d = c.to_dict()
    d["due_at_iso"] = _epoch_to_iso(c.due_at)
    d["created_at_iso"] = _epoch_to_iso(c.created_at)
    d["updated_at_iso"] = _epoch_to_iso(c.updated_at)
    d["resolved_at_iso"] = _epoch_to_iso(c.resolved_at)
    return d


class TrackCommitmentTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "track_commitment",
            "description": (
                "Record a commitment — something the user owes someone, or something "
                "someone owes the user. Use for follow-up tracking."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Short human-readable description (max 2000 chars)",
                    },
                    "owner": {
                        "type": "string",
                        "description": "'me' if the user owes; email address if someone else owes",
                    },
                    "counterparty": {
                        "type": "string",
                        "description": "Email of the other party (optional)",
                    },
                    "due_at": {
                        "type": "string",
                        "description": "ISO 8601 datetime the commitment is due (optional)",
                    },
                    "thread_id": {"type": "string", "description": "Related conversation id"},
                    "message_id": {"type": "string", "description": "Related message id"},
                    "excerpt": {
                        "type": "string",
                        "description": "Optional short quote from the source (max 2000 chars)",
                    },
                },
                "required": ["description", "owner"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        description = kwargs.get("description")
        owner = kwargs.get("owner")
        due_at = _iso_to_epoch(kwargs.get("due_at"))

        repo = CommitmentRepo(self.get_memory_store())
        commitment = CommitmentRepo.new(
            description=description,
            owner=owner,
            counterparty=kwargs.get("counterparty"),
            thread_id=kwargs.get("thread_id"),
            message_id=kwargs.get("message_id"),
            due_at=due_at,
            source="manual",
            excerpt=kwargs.get("excerpt"),
        )
        saved = repo.save(commitment)
        return format_success_response(
            "Commitment tracked",
            commitment=_commitment_to_response(saved),
        )


class ListCommitmentsTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "list_commitments",
            "description": (
                "List commitments. Default returns all open commitments, newest-due first."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["open", "overdue", "done", "cancelled", "all"],
                        "default": "open",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Filter by owner ('me' or email). Omit for all.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                },
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        scope = kwargs.get("scope", "open")
        owner = kwargs.get("owner")
        limit = int(kwargs.get("limit", 100))

        repo = CommitmentRepo(self.get_memory_store())
        status = None if scope in ("all", "overdue") else scope
        overdue = scope == "overdue"
        records = repo.list(status=status, owner=owner, overdue=overdue, limit=limit)
        return format_success_response(
            f"Found {len(records)} commitment(s)",
            scope=scope,
            count=len(records),
            commitments=[_commitment_to_response(c) for c in records],
        )


class ResolveCommitmentTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "resolve_commitment",
            "description": "Mark a commitment as done or cancelled.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "commitment_id": {"type": "string"},
                    "outcome": {"type": "string", "enum": ["done", "cancelled"]},
                    "note": {"type": "string", "description": "Optional resolution note"},
                },
                "required": ["commitment_id", "outcome"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        cid = kwargs.get("commitment_id")
        outcome = kwargs.get("outcome")
        note = kwargs.get("note")
        if not cid or outcome not in ("done", "cancelled"):
            raise ToolExecutionError("commitment_id and outcome=done|cancelled are required")

        repo = CommitmentRepo(self.get_memory_store())
        saved = repo.resolve(cid, outcome=outcome, note=note)
        if saved is None:
            raise ToolExecutionError(f"Commitment not found: {cid}")
        return format_success_response(
            "Commitment resolved",
            commitment=_commitment_to_response(saved),
        )


class ExtractCommitmentsTool(BaseTool):
    """AI-assisted commitment extraction from a message.

    Takes a message_id, pulls the thread content, and asks the configured
    AI provider to return a list of commitments in strict JSON. Each
    extracted commitment is saved to the store with ``source="extracted"``
    so humans can distinguish AI-authored items later.
    """

    _SYSTEM_PROMPT = (
        "You extract commitments from email content. A commitment is a "
        "concrete promise that someone (the sender, the recipient, or a "
        "named party) will do something by a specific time, or a concrete "
        "ask that requires a follow-up action.\n\n"
        "Return STRICT JSON with this shape:\n"
        '{"commitments": [\n'
        '  {"description": str, "owner": "me" | "them", '
        '"counterparty_email": str | null, "due_iso": str | null, '
        '"excerpt": str}\n]}\n\n'
        "Rules:\n"
        "- owner='me' if the MAILBOX USER is the one who owes; "
        "owner='them' if the other party owes.\n"
        "- due_iso is an ISO 8601 datetime when one is clearly stated; "
        "otherwise null.\n"
        "- excerpt is the short sentence you based the commitment on "
        "(max 200 chars).\n"
        "- If nothing qualifies, return an empty list.\n"
        "- NEVER invent commitments not plainly stated."
    )

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "extract_commitments",
            "description": (
                "AI-extract commitments from an email. Requires ENABLE_AI=true "
                "and a configured AI provider. Extracted items are saved "
                "with source='extracted' so they can be reviewed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "max_extractions": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 10,
                    },
                    "save": {
                        "type": "boolean",
                        "default": True,
                        "description": "Save extracted commitments to the store",
                    },
                    "target_mailbox": {"type": "string"},
                },
                "required": ["message_id"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        from ..ai import get_ai_provider

        message_id = kwargs.get("message_id")
        max_n = int(kwargs.get("max_extractions", 10))
        save = bool(kwargs.get("save", True))
        target_mailbox = kwargs.get("target_mailbox")
        if not message_id:
            raise ToolExecutionError("message_id is required")

        provider = get_ai_provider(self.ews_client.config)
        if provider is None:
            raise ToolExecutionError(
                "AI provider not configured. Set ENABLE_AI=true and configure "
                "AI_PROVIDER/AI_API_KEY/AI_MODEL to use extract_commitments."
            )

        account = self.get_account(target_mailbox)
        message = find_message_for_account(account, message_id)

        # Build a compact prompt: subject + truncated body + sender/recipients.
        subject = safe_get(message, "subject", "") or ""
        sender = safe_get(safe_get(message, "sender", None), "email_address", "") or ""
        to_emails = [
            safe_get(r, "email_address", "")
            for r in (safe_get(message, "to_recipients", []) or [])
        ]
        body = safe_get(message, "text_body", "") or safe_get(message, "body", "") or ""
        body = str(body)[:6000]  # hard cap on prompt size

        user_prompt = (
            f"Mailbox owner email: {self.ews_client.config.ews_email}\n"
            f"Sender: {sender}\n"
            f"To: {', '.join(filter(None, to_emails))}\n"
            f"Subject: {subject}\n\n"
            f"Body:\n{body}\n\n"
            f"Extract up to {max_n} commitments as strict JSON."
        )

        from ..ai.base import Message as AIMessage
        response = await provider.complete(
            messages=[
                AIMessage(role="system", content=self._SYSTEM_PROMPT),
                AIMessage(role="user", content=user_prompt),
            ],
            max_tokens=1024,
            temperature=0.1,
        )
        raw = getattr(response, "content", "") or ""
        parsed = self._parse_json_payload(raw)

        extracted_raw = parsed.get("commitments", []) if isinstance(parsed, dict) else []
        if not isinstance(extracted_raw, list):
            extracted_raw = []
        extracted_raw = extracted_raw[:max_n]

        saved_out: List[Dict[str, Any]] = []
        repo = CommitmentRepo(self.get_memory_store())
        for item in extracted_raw:
            if not isinstance(item, dict):
                continue
            description = item.get("description")
            owner_raw = (item.get("owner") or "").lower()
            if owner_raw == "them":
                owner = item.get("counterparty_email") or sender or "them"
            else:
                owner = "me"
            try:
                commitment = CommitmentRepo.new(
                    description=description,
                    owner=owner,
                    counterparty=item.get("counterparty_email") or sender,
                    thread_id=safe_get(safe_get(message, "conversation_id", None), "id", None)
                    if hasattr(safe_get(message, "conversation_id", None), "id")
                    else safe_get(message, "conversation_id", None),
                    message_id=message_id,
                    due_at=_iso_to_epoch(item.get("due_iso")),
                    source="extracted",
                    excerpt=item.get("excerpt"),
                )
            except ValidationError as exc:
                self.logger.debug(f"Skipping invalid AI extraction: {exc}")
                continue
            if save:
                commitment = repo.save(commitment)
            saved_out.append(_commitment_to_response(commitment))

        return format_success_response(
            f"Extracted {len(saved_out)} commitment(s)",
            saved=save,
            count=len(saved_out),
            commitments=saved_out,
            message_id=message_id,
        )

    @staticmethod
    def _parse_json_payload(raw: str) -> Dict[str, Any]:
        """Tolerant JSON parser — the model sometimes wraps in ```json."""
        if not raw:
            return {}
        # Strip markdown code fences.
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
        if fence:
            raw = fence.group(1)
        # Find the first {...} block if there's prose around it.
        brace = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        candidate = brace.group(0) if brace else raw
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
