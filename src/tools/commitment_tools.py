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


