"""Search tools for EWS MCP Server.

Provides:
- SearchEmailsTool: Unified email search with quick/advanced/full_text modes
- SearchByConversationTool: Find all emails in a conversation thread
"""

from typing import Any, Dict, List
from datetime import datetime
from exchangelib.queryset import Q

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, safe_get, truncate_text, parse_datetime_tz_aware, find_message_across_folders, ews_id_to_str, format_datetime


class SearchByConversationTool(BaseTool):
    """Tool for finding all emails in a conversation thread."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "search_by_conversation",
            "description": "Find all emails in a conversation thread.",
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
                    "search_scope": {
                        "type": "array",
                        "description": "Folders to search",
                        "items": {"type": "string"},
                        "default": ["inbox", "sent"]
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
                        "description": "Include deleted items folder",
                        "default": False
                    }
                },
                "required": []
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Search for emails by conversation."""
        target_mailbox = kwargs.get("target_mailbox")
        conversation_id = kwargs.get("conversation_id")
        message_id = kwargs.get("message_id")
        search_scope = kwargs.get("search_scope", ["inbox", "sent"])
        max_results = kwargs.get("max_results", 100)
        include_deleted = kwargs.get("include_deleted", False)

        if not conversation_id and not message_id:
            raise ToolExecutionError("Either conversation_id or message_id is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Build folder map once outside the loop
            folder_map = {
                "inbox": account.inbox,
                "sent": account.sent,
                "drafts": account.drafts,
                "deleted": account.trash,
                "junk": account.junk
            }

            # If message_id provided, get conversation_id from it
            if message_id and not conversation_id:
                for folder_name in ["inbox", "sent", "drafts"]:
                    folder = folder_map.get(folder_name)
                    try:
                        message = folder.get(id=message_id)
                        if message:
                            conversation_id = safe_get(message, 'conversation_id', None)
                            if conversation_id:
                                break
                    except Exception:
                        continue

                if not conversation_id:
                    raise ToolExecutionError(f"Could not find conversation_id for message: {message_id}")

            # Build list of folders to search
            folders_to_search = []
            for folder_name in search_scope:
                folder = folder_map.get(folder_name.lower())
                if folder:
                    folders_to_search.append(folder)

            if include_deleted and "deleted" not in [s.lower() for s in search_scope]:
                folders_to_search.append(account.trash)

            if not folders_to_search:
                raise ToolExecutionError("No valid folders to search")

            # Search for emails with this conversation ID
            all_results = []
            for folder in folders_to_search:
                try:
                    items = folder.filter(conversation_id=conversation_id).order_by('-datetime_received')[:max_results]

                    for item in items:
                        result = {
                            "id": ews_id_to_str(safe_get(item, 'id', None)) or '',
                            "subject": safe_get(item, 'subject', ''),
                            "from": safe_get(safe_get(item, 'sender', {}), 'email_address', ''),
                            "to": [r.email_address for r in safe_get(item, 'to_recipients', []) if hasattr(r, 'email_address')],
                            "received": format_datetime(safe_get(item, 'datetime_received', datetime.now())),
                            "conversation_id": safe_get(item, 'conversation_id', ''),
                            "is_read": safe_get(item, 'is_read', False),
                            "importance": safe_get(item, 'importance', 'Normal'),
                            "folder": safe_get(folder, 'name', 'Unknown')
                        }
                        all_results.append(result)

                except Exception as e:
                    self.logger.warning(f"Error searching folder {safe_get(folder, 'name', 'Unknown')}: {e}")
                    continue

            # Sort by received date
            all_results.sort(key=lambda x: x['received'], reverse=True)
            all_results = all_results[:max_results]

            self.logger.info(f"Found {len(all_results)} emails in conversation {conversation_id}")

            return format_success_response(
                f"Found {len(all_results)} emails in conversation",
                results=all_results,
                conversation_id=conversation_id,
                total_results=len(all_results),
                searched_folders=search_scope,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to search by conversation: {e}")
            raise ToolExecutionError(f"Failed to search by conversation: {e}")
