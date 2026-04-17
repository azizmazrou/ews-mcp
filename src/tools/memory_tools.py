"""MCP tools that expose the per-mailbox memory store to the agent.

These are intentionally narrow primitives. Higher-level tools (commitments,
approvals, rules) wrap the same store via typed repositories and should be
preferred for structured data — reserve these for ad-hoc agent scratch
state ("Alice prefers morning meetings", "remind me Thursday at 3pm").

Security
--------
* All namespace/key values must match ``[A-Za-z0-9._:-]{1,128}``; enforced
  by :func:`src.memory.store._validate_name`.
* Values are capped at 1 MiB each; namespaces at 50 MiB total.
* The store is rooted at ``EWS_MEMORY_DIR`` (jailed path).
* Namespace ``approval`` and ``rule`` are reserved — writes via the generic
  memory tools would bypass the repositories' validation, so we refuse them.
"""

from typing import Any, Dict

from .base import BaseTool
from ..exceptions import ToolExecutionError, ValidationError
from ..utils import format_success_response


_RESERVED_NAMESPACES = {"approval", "rule", "oof.policy"}


def _reject_reserved(namespace: str) -> None:
    if namespace in _RESERVED_NAMESPACES:
        raise ValidationError(
            f"namespace {namespace!r} is reserved; use the dedicated tool "
            f"(approval_* / rule_* / oof_*) instead."
        )


class MemorySetTool(BaseTool):
    """Agent scratch-write: persist a JSON value against a key."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "memory_set",
            "description": (
                "Persist an arbitrary JSON value under (namespace, key) in the "
                "agent's private per-mailbox memory. Reserved namespaces "
                "(approval, rule, oof.policy) are refused — use the dedicated "
                "tools for those."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Namespace (e.g. 'person.note', 'thread.snooze', 'prefs')",
                    },
                    "key": {"type": "string", "description": "Item key within the namespace"},
                    "value": {
                        "description": "JSON-serialisable value (string, number, object, array, boolean, null)",
                    },
                    "ttl_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional expiry in seconds (omit for no expiry)",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Optional small metadata dict (<= 64 KiB)",
                    },
                },
                "required": ["namespace", "key", "value"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        namespace = kwargs.get("namespace")
        key = kwargs.get("key")
        value = kwargs.get("value")
        ttl_seconds = kwargs.get("ttl_seconds")
        metadata = kwargs.get("metadata")

        if namespace is None or key is None:
            raise ToolExecutionError("namespace and key are required")
        _reject_reserved(namespace)

        store = self.get_memory_store()
        record = store.set(
            namespace=namespace,
            key=key,
            value=value,
            ttl_seconds=ttl_seconds,
            metadata=metadata if isinstance(metadata, dict) else None,
        )
        return format_success_response(
            "Memory entry stored",
            namespace=namespace,
            key=key,
            updated_at=record.updated_at,
            expires_at=record.expires_at,
        )


class MemoryGetTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "memory_get",
            "description": "Fetch a previously stored memory entry by (namespace, key).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "key": {"type": "string"},
                },
                "required": ["namespace", "key"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        namespace = kwargs.get("namespace")
        key = kwargs.get("key")
        if namespace is None or key is None:
            raise ToolExecutionError("namespace and key are required")

        store = self.get_memory_store()
        record = store.get(namespace, key)
        if record is None:
            return format_success_response(
                "Not found",
                namespace=namespace,
                key=key,
                found=False,
            )
        return format_success_response(
            "Memory entry fetched",
            found=True,
            **record.to_dict(),
        )


class MemoryListTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "memory_list",
            "description": (
                "List memory entries in a namespace, optionally filtered by "
                "key prefix. Returns up to 500 most-recently-updated entries."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "prefix": {"type": "string", "description": "Key prefix filter (optional)"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                },
                "required": ["namespace"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        namespace = kwargs.get("namespace")
        prefix = kwargs.get("prefix")
        limit = kwargs.get("limit", 100)
        if namespace is None:
            raise ToolExecutionError("namespace is required")

        store = self.get_memory_store()
        records = store.list(namespace, prefix=prefix, limit=limit)
        return format_success_response(
            f"Found {len(records)} memory entry(ies)",
            namespace=namespace,
            count=len(records),
            entries=[r.to_dict() for r in records],
        )


class MemoryDeleteTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "memory_delete",
            "description": (
                "Delete a memory entry. Reserved namespaces (approval, rule, "
                "oof.policy) are refused — use the dedicated tools."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "key": {"type": "string"},
                },
                "required": ["namespace", "key"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        namespace = kwargs.get("namespace")
        key = kwargs.get("key")
        if namespace is None or key is None:
            raise ToolExecutionError("namespace and key are required")
        _reject_reserved(namespace)

        store = self.get_memory_store()
        deleted = store.delete(namespace, key)
        return format_success_response(
            "Deleted" if deleted else "Nothing to delete",
            namespace=namespace,
            key=key,
            deleted=deleted,
        )
