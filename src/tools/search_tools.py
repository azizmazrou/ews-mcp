"""Search tools for EWS MCP Server.

Provides:
- SearchByConversationTool: Find every message in a conversation thread
  across the entire mailbox (or a caller-specified subset).
"""

from typing import Any, Dict, List, Optional, Set
from datetime import datetime

from .base import BaseTool
from ..exceptions import ToolExecutionError, ValidationError
from ..utils import (
    format_success_response, safe_get, truncate_text, parse_datetime_tz_aware,
    find_message_across_folders, ews_id_to_str, format_datetime,
    project_fields, strip_body_by_default, LIST_DEFAULT_FIELDS,
    ews_call_log,
)


# Public folder names the caller typically supplies. We honour these only
# when ``include_all_folders=False``.
_STANDARD_FOLDER_NAMES = ("inbox", "sent", "drafts", "deleted", "junk", "archive")


class SearchByConversationTool(BaseTool):
    """Tool for finding every message in a conversation thread.

    Default behaviour walks the full mail-folder tree so archive /
    custom-label messages aren't invisible to the tool (Issue 3).
    """

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "search_by_conversation",
            "description": (
                "Find every email sharing a conversation thread. Defaults "
                "to searching ALL mail folders (recursively from root) "
                "so archive / labelled messages are included. Pass "
                "``include_all_folders=false`` + ``search_scope`` to "
                "restrict the scope."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "Conversation ID to search for"
                    },
                    "message_id": {
                        "type": "string",
                        "description": "Message ID to find conversation from (alternative to conversation_id)"
                    },
                    "include_all_folders": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Walk every mail folder under account.root. "
                            "Default true so archive / subfolder messages "
                            "aren't missed; set false and use search_scope "
                            "to restrict."
                        ),
                    },
                    "search_scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Folder names to search when "
                            "include_all_folders=false. Standard names: "
                            + ", ".join(_STANDARD_FOLDER_NAMES)
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results",
                        "default": 100,
                        "minimum": 1,
                        "maximum": 500
                    },
                    "include_deleted": {
                        "type": "boolean",
                        "description": "Also include Deleted Items when include_all_folders=false",
                        "default": False
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Result projection. Default items include "
                            "message_id, subject, from, received_time, "
                            "is_read, has_attachments, snippet. Include "
                            "'body' to opt into the full message text."
                        ),
                    }
                },
                "required": []
            }
        }

    # --- helpers --------------------------------------------------------

    def _collect_mail_folders(
        self, account: Any,
    ) -> (List[Any], List[Dict[str, str]]):
        """Walk the account's folder tree and return every mail-typed folder.

        Returns (folders, skipped) where ``skipped`` carries structured
        entries for folders that couldn't be iterated (permission denied,
        protocol error, etc). Errors are logged at DEBUG — the tool still
        returns the folders we could reach.
        """
        folders: List[Any] = []
        skipped: List[Dict[str, str]] = []
        root = getattr(account, "msg_folder_root", None) or getattr(account, "root", None)
        if root is None:
            return folders, skipped

        try:
            candidates = list(root.walk())
        except Exception as exc:
            self.logger.debug(
                "root.walk() failed: %s: %s", type(exc).__name__, exc,
            )
            return folders, skipped

        for folder in candidates:
            # Skip non-mail folder classes (Contacts, Calendar, Tasks).
            folder_class = (safe_get(folder, "folder_class", "") or "").lower()
            if folder_class and not folder_class.startswith(("ipf.note", "ipf.message")):
                continue
            folders.append(folder)
        return folders, skipped

    def _standard_folder_map(self, account: Any) -> Dict[str, Any]:
        return {
            "inbox": getattr(account, "inbox", None),
            "sent": getattr(account, "sent", None),
            "drafts": getattr(account, "drafts", None),
            "deleted": getattr(account, "trash", None),
            "junk": getattr(account, "junk", None),
            "archive": getattr(account, "archive", None),
        }

    # --- main entrypoint ------------------------------------------------

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Search for emails by conversation."""
        target_mailbox = kwargs.get("target_mailbox")
        conversation_id = kwargs.get("conversation_id")
        message_id = kwargs.get("message_id")
        include_all_folders = bool(kwargs.get("include_all_folders", True))
        search_scope = kwargs.get("search_scope")
        max_results = int(kwargs.get("max_results", 100))
        include_deleted = bool(kwargs.get("include_deleted", False))

        if not conversation_id and not message_id:
            raise ValidationError("Either conversation_id or message_id is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Resolve conversation_id from message_id if needed. Use the
            # existing multi-folder finder (it already walks standard
            # folders + custom subfolders).
            if message_id and not conversation_id:
                try:
                    message = find_message_across_folders(self.ews_client, message_id)
                    conversation_id = safe_get(message, "conversation_id", None)
                    if hasattr(conversation_id, "id"):
                        conversation_id = conversation_id.id
                except Exception as exc:
                    raise ValidationError(
                        f"Could not find conversation_id for message "
                        f"{message_id!r}: {type(exc).__name__}: {exc}"
                    )
                if not conversation_id:
                    raise ValidationError(
                        f"Message {message_id!r} has no conversation_id"
                    )

            # Figure out which folders to iterate.
            folders_to_search: List[Any] = []
            skipped_folders: List[Dict[str, str]] = []
            if include_all_folders:
                # Full tree walk — the default. Archive, custom labels,
                # custom subfolders are all covered here.
                folders_to_search, tree_skipped = self._collect_mail_folders(account)
                skipped_folders.extend(tree_skipped)
            else:
                scope = [s.lower() for s in (search_scope or ["inbox", "sent"])]
                folder_map = self._standard_folder_map(account)
                for name in scope:
                    f = folder_map.get(name)
                    if f is not None:
                        folders_to_search.append(f)
                    else:
                        skipped_folders.append({
                            "folder": name,
                            "reason": "not_a_standard_folder",
                        })
                if include_deleted and folder_map.get("deleted") is not None:
                    if not any(getattr(f, "id", None) == getattr(folder_map["deleted"], "id", object()) for f in folders_to_search):
                        folders_to_search.append(folder_map["deleted"])

            if not folders_to_search:
                raise ToolExecutionError("No mail folders available to search")

            fields = kwargs.get("fields") or list(LIST_DEFAULT_FIELDS)
            wants_body = "body" in fields

            seen_ids: Set[str] = set()
            all_results: List[Dict[str, Any]] = []
            searched_folders: List[str] = []
            start_time = datetime.now()

            for folder in folders_to_search:
                folder_name = safe_get(folder, "name", "Unknown")
                searched_folders.append(folder_name)
                try:
                    items = list(
                        folder.filter(conversation_id=conversation_id)
                        .order_by("-datetime_received")[:max_results]
                    )
                except Exception as exc:
                    # permission_denied vs. real error vs. transient —
                    # we classify for the response without blowing up.
                    reason = "permission_denied" if (
                        "access denied" in str(exc).lower()
                        or "ErrorAccessDenied" in type(exc).__name__
                    ) else "error"
                    skipped_folders.append({
                        "folder": folder_name,
                        "reason": reason,
                        "error_type": type(exc).__name__,
                    })
                    self.logger.debug(
                        "conversation walk on %s failed: %s: %s",
                        folder_name, type(exc).__name__, exc,
                    )
                    continue

                ews_call_log(
                    self.logger, "FindItem",
                    result_count=len(items),
                    folder=folder_name,
                    outcome="ok",
                    extra_fields={
                        "tool": "search_by_conversation",
                        "filter": "conversation_id",
                    },
                )

                for item in items:
                    mid = ews_id_to_str(safe_get(item, "id", None)) or ""
                    if mid and mid in seen_ids:
                        continue
                    if mid:
                        seen_ids.add(mid)
                    text_body = safe_get(item, "text_body", "") or ""
                    record: Dict[str, Any] = {
                        "message_id": mid,
                        "subject": safe_get(item, "subject", "") or "",
                        "from": safe_get(
                            safe_get(item, "sender", None), "email_address", ""
                        ) or "",
                        "to": [
                            r.email_address for r in (safe_get(item, "to_recipients", []) or [])
                            if hasattr(r, "email_address")
                        ],
                        "received_time": format_datetime(
                            safe_get(item, "datetime_received", None)
                        ),
                        "conversation_id": conversation_id,
                        "is_read": safe_get(item, "is_read", False),
                        "has_attachments": safe_get(item, "has_attachments", False),
                        "importance": safe_get(item, "importance", "Normal"),
                        "folder": folder_name,
                        "snippet": truncate_text(text_body, 200),
                    }
                    if wants_body:
                        record["body"] = text_body
                    strip_body_by_default(record, keep_body=wants_body)
                    all_results.append(project_fields(record, fields))

            all_results.sort(
                key=lambda x: x.get("received_time") or "",
                reverse=True,
            )
            all_results = all_results[:max_results]

            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            ews_call_log(
                self.logger, "search_by_conversation",
                duration_ms=duration_ms,
                result_count=len(all_results),
                outcome="ok" if not skipped_folders else "partial",
                extra_fields={
                    "folders_searched": len(searched_folders),
                    "folders_skipped": len(skipped_folders),
                },
            )

            self.logger.info(
                "conversation %s: %d message(s) across %d folder(s), %d skipped",
                conversation_id, len(all_results),
                len(searched_folders), len(skipped_folders),
            )

            return format_success_response(
                f"Found {len(all_results)} emails in conversation",
                items=all_results,
                count=len(all_results),
                conversation_id=conversation_id,
                searched_folders=searched_folders,
                skipped_folders=skipped_folders,
                mailbox=mailbox,
            )

        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.exception(f"Failed to search by conversation: {e}")
            raise ToolExecutionError(
                f"Failed to search by conversation: {type(e).__name__}: {e}"
            )
