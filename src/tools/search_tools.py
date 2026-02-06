"""Advanced search tools for EWS MCP Server."""

from typing import Any, Dict, List
from datetime import datetime
from exchangelib.queryset import Q

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, safe_get, truncate_text, parse_datetime_tz_aware, find_message_across_folders, ews_id_to_str, format_datetime


class AdvancedSearchTool(BaseTool):
    """Tool for complex multi-criteria searches."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "advanced_search",
            "description": "Perform complex searches across mailbox with multiple criteria and filters. Supports impersonation to search in another user's mailbox",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    },
                    "search_filter": {
                        "type": "object",
                        "description": "Search criteria object with filters",
                        "properties": {
                            "keywords": {
                                "type": "string",
                                "description": "Keywords to search in subject and body"
                            },
                            "from_address": {
                                "type": "string",
                                "description": "Sender email address"
                            },
                            "to_address": {
                                "type": "string",
                                "description": "Recipient email address"
                            },
                            "subject": {
                                "type": "string",
                                "description": "Subject contains text"
                            },
                            "body": {
                                "type": "string",
                                "description": "Body contains text"
                            },
                            "has_attachments": {
                                "type": "boolean",
                                "description": "Filter by attachment presence"
                            },
                            "importance": {
                                "type": "string",
                                "enum": ["Low", "Normal", "High"],
                                "description": "Email importance level"
                            },
                            "categories": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Email categories"
                            },
                            "is_read": {
                                "type": "boolean",
                                "description": "Read status"
                            },
                            "start_date": {
                                "type": "string",
                                "description": "Start date (ISO 8601 format)"
                            },
                            "end_date": {
                                "type": "string",
                                "description": "End date (ISO 8601 format)"
                            }
                        }
                    },
                    "search_scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Folders to search (e.g., ['inbox', 'sent', 'drafts'])",
                        "default": ["inbox"]
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return",
                        "default": 250,
                        "maximum": 1000
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["datetime_received", "datetime_sent", "from", "subject", "importance"],
                        "description": "Sort field",
                        "default": "datetime_received"
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["ascending", "descending"],
                        "description": "Sort order",
                        "default": "descending"
                    }
                },
                "required": ["search_filter", "search_scope"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Perform advanced search."""
        target_mailbox = kwargs.get("target_mailbox")
        search_filter = kwargs.get("search_filter", {})
        search_scope = kwargs.get("search_scope", ["inbox"])
        max_results = kwargs.get("max_results", 250)
        sort_by = kwargs.get("sort_by", "datetime_received")
        sort_order = kwargs.get("sort_order", "descending")

        if not search_filter:
            raise ToolExecutionError("search_filter is required and cannot be empty")

        if not search_scope:
            raise ToolExecutionError("search_scope is required and cannot be empty")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Map folder names to folder objects
            folder_map = {
                "inbox": account.inbox,
                "sent": account.sent,
                "drafts": account.drafts,
                "deleted": account.trash,
                "junk": account.junk
            }

            # Get folders to search
            folders = []
            for folder_name in search_scope:
                folder = folder_map.get(folder_name.lower())
                if folder:
                    folders.append(folder)
                else:
                    self.logger.warning(f"Unknown folder: {folder_name}, skipping")

            if not folders:
                raise ToolExecutionError(f"No valid folders found in search_scope: {search_scope}")

            # Build query filters
            q_filters = []

            # Keywords (search in subject and body)
            if search_filter.get("keywords"):
                keywords = search_filter["keywords"]
                q_filters.append(
                    Q(subject__contains=keywords) | Q(body__contains=keywords)
                )

            # From address
            if search_filter.get("from_address"):
                q_filters.append(Q(sender=search_filter["from_address"]))

            # To address
            if search_filter.get("to_address"):
                q_filters.append(Q(to_recipients__contains=search_filter["to_address"]))

            # Subject
            if search_filter.get("subject"):
                q_filters.append(Q(subject__contains=search_filter["subject"]))

            # Body
            if search_filter.get("body"):
                q_filters.append(Q(body__contains=search_filter["body"]))

            # Has attachments
            if "has_attachments" in search_filter:
                q_filters.append(Q(has_attachments=search_filter["has_attachments"]))

            # Importance
            if search_filter.get("importance"):
                q_filters.append(Q(importance=search_filter["importance"]))

            # Categories
            if search_filter.get("categories"):
                # Categories filter - at least one category matches
                q_filters.append(Q(categories__contains=search_filter["categories"]))

            # Is read
            if "is_read" in search_filter:
                q_filters.append(Q(is_read=search_filter["is_read"]))

            # Date range
            if search_filter.get("start_date"):
                start_date = parse_datetime_tz_aware(search_filter["start_date"])
                if start_date:
                    q_filters.append(Q(datetime_received__gte=start_date))

            if search_filter.get("end_date"):
                end_date = parse_datetime_tz_aware(search_filter["end_date"])
                if end_date:
                    q_filters.append(Q(datetime_received__lte=end_date))

            # Combine filters with AND
            if not q_filters:
                raise ToolExecutionError("No valid search filters provided")

            combined_filter = q_filters[0]
            for q_filter in q_filters[1:]:
                combined_filter &= q_filter

            # Search across all specified folders
            all_results = []
            for folder in folders:
                try:
                    query = folder.filter(combined_filter)

                    # Apply sorting
                    sort_field = sort_by if sort_by.startswith('-') or sort_by.startswith('+') else sort_by
                    if sort_order == "descending" and not sort_field.startswith('-'):
                        sort_field = f"-{sort_field}"

                    query = query.order_by(sort_field)

                    # Fetch results (limit per folder)
                    results_per_folder = max_results // len(folders) if len(folders) > 1 else max_results
                    for email in query[:results_per_folder]:
                        all_results.append({
                            "message_id": safe_get(email, 'id', ''),
                            "subject": safe_get(email, 'subject', ''),
                            "from": safe_get(email, 'sender', {}).email_address if hasattr(safe_get(email, 'sender', {}), 'email_address') else '',
                            "to": [r.email_address for r in safe_get(email, 'to_recipients', []) if hasattr(r, 'email_address')],
                            "received_time": safe_get(email, 'datetime_received', '').isoformat() if safe_get(email, 'datetime_received') else None,
                            "is_read": safe_get(email, 'is_read', False),
                            "has_attachments": safe_get(email, 'has_attachments', False),
                            "importance": safe_get(email, 'importance', 'Normal'),
                            "categories": safe_get(email, 'categories', []),
                            "body_preview": truncate_text(safe_get(email, 'text_body', ''), 200),
                            "folder": folder.name
                        })

                except Exception as e:
                    self.logger.warning(f"Error searching folder {folder.name}: {e}")
                    continue

            # Sort all results if we searched multiple folders
            if len(folders) > 1:
                reverse = (sort_order == "descending")
                if sort_by == "datetime_received":
                    all_results.sort(key=lambda x: x.get("received_time", ""), reverse=reverse)
                elif sort_by == "subject":
                    all_results.sort(key=lambda x: x.get("subject", ""), reverse=reverse)
                elif sort_by == "from":
                    all_results.sort(key=lambda x: x.get("from", ""), reverse=reverse)

            # Limit total results
            all_results = all_results[:max_results]

            self.logger.info(f"Advanced search found {len(all_results)} result(s)")

            return format_success_response(
                f"Found {len(all_results)} result(s)",
                results=all_results,
                count=len(all_results),
                search_filter=search_filter,
                folders_searched=search_scope,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to perform advanced search: {e}")
            raise ToolExecutionError(f"Failed to perform advanced search: {e}")


class SearchByConversationTool(BaseTool):
    """Tool for finding all emails in a conversation thread."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "search_by_conversation",
            "description": "Find all emails in a conversation thread using conversation ID or initial message. Supports impersonation to search in another user's mailbox",
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

            from exchangelib import Q

            # If message_id provided, get conversation_id from it
            if message_id and not conversation_id:
                message = None
                for folder_name in ["inbox", "sent", "drafts"]:
                    folder_map = {
                        "inbox": account.inbox,
                        "sent": account.sent,
                        "drafts": account.drafts
                    }
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

            # Map folder names to folder objects
            folder_map = {
                "inbox": account.inbox,
                "sent": account.sent,
                "drafts": account.drafts,
                "deleted": account.trash,
                "junk": account.junk
            }

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
                    # Filter by conversation ID
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

            # Limit total results
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


class FullTextSearchTool(BaseTool):
    """Tool for full-text search across email content."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "full_text_search",
            "description": "Perform full-text search across subject, body, and attachment names. Supports impersonation to search in another user's mailbox",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query text"
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
                        "default": 50,
                        "minimum": 1,
                        "maximum": 500
                    },
                    "search_in": {
                        "type": "array",
                        "description": "Where to search (subject, body, attachments)",
                        "items": {
                            "type": "string",
                            "enum": ["subject", "body", "attachments"]
                        },
                        "default": ["subject", "body"]
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Case-sensitive search",
                        "default": False
                    },
                    "exact_phrase": {
                        "type": "boolean",
                        "description": "Search for exact phrase",
                        "default": False
                    }
                },
                "required": ["query"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Perform full-text search."""
        target_mailbox = kwargs.get("target_mailbox")
        query = kwargs.get("query")
        search_scope = kwargs.get("search_scope", ["inbox", "sent"])
        max_results = kwargs.get("max_results", 50)
        search_in = kwargs.get("search_in", ["subject", "body"])
        case_sensitive = kwargs.get("case_sensitive", False)
        exact_phrase = kwargs.get("exact_phrase", False)

        if not query:
            raise ToolExecutionError("query is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            from exchangelib import Q

            # Normalize query for case-insensitive search
            search_query = query if case_sensitive else query.lower()

            # Map folder names to folder objects
            folder_map = {
                "inbox": account.inbox,
                "sent": account.sent,
                "drafts": account.drafts,
                "deleted": account.trash,
                "junk": account.junk
            }

            # Build list of folders to search
            folders_to_search = []
            for folder_name in search_scope:
                folder = folder_map.get(folder_name.lower())
                if folder:
                    folders_to_search.append(folder)

            if not folders_to_search:
                raise ToolExecutionError("No valid folders to search")

            # Build search filter based on search_in
            all_results = []

            for folder in folders_to_search:
                try:
                    # Build Q filter
                    q_filters = []

                    if "subject" in search_in:
                        q_filters.append(Q(subject__contains=query))

                    if "body" in search_in:
                        q_filters.append(Q(body__contains=query))

                    # Combine with OR
                    if q_filters:
                        combined_filter = q_filters[0]
                        for f in q_filters[1:]:
                            combined_filter |= f

                        items = folder.filter(combined_filter).order_by('-datetime_received')[:max_results]

                        for item in items:
                            # Additional filtering for attachments and exact phrase
                            item_text = ""
                            if "subject" in search_in:
                                item_text += safe_get(item, 'subject', '').lower() + " "
                            if "body" in search_in:
                                item_text += safe_get(item, 'text_body', '').lower() + " "

                            # Check attachments if requested
                            attachment_match = False
                            if "attachments" in search_in and hasattr(item, 'attachments') and item.attachments:
                                for att in item.attachments:
                                    att_name = safe_get(att, 'name', '')
                                    if not case_sensitive:
                                        att_name = att_name.lower()
                                    if search_query in att_name:
                                        attachment_match = True
                                        break

                            # For exact phrase matching
                            if exact_phrase and search_query not in item_text and not attachment_match:
                                continue

                            result = {
                                "id": ews_id_to_str(safe_get(item, 'id', None)) or '',
                                "subject": safe_get(item, 'subject', ''),
                                "from": safe_get(safe_get(item, 'sender', {}), 'email_address', ''),
                                "to": [r.email_address for r in safe_get(item, 'to_recipients', []) if hasattr(r, 'email_address')],
                                "received": format_datetime(safe_get(item, 'datetime_received', datetime.now())),
                                "is_read": safe_get(item, 'is_read', False),
                                "has_attachments": safe_get(item, 'has_attachments', False),
                                "importance": safe_get(item, 'importance', 'Normal'),
                                "folder": safe_get(folder, 'name', 'Unknown'),
                                "preview": safe_get(item, 'text_body', '')[:200] if "body" in search_in else ""
                            }
                            all_results.append(result)

                except Exception as e:
                    self.logger.warning(f"Error searching folder {safe_get(folder, 'name', 'Unknown')}: {e}")
                    continue

            # Sort by received date
            all_results.sort(key=lambda x: x['received'], reverse=True)

            # Limit total results
            all_results = all_results[:max_results]

            self.logger.info(f"Full-text search for '{query}' found {len(all_results)} results")

            return format_success_response(
                f"Found {len(all_results)} emails matching '{query}'",
                results=all_results,
                query=query,
                total_results=len(all_results),
                searched_folders=search_scope,
                search_options={
                    "case_sensitive": case_sensitive,
                    "exact_phrase": exact_phrase,
                    "search_in": search_in
                },
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to perform full-text search: {e}")
            raise ToolExecutionError(f"Failed to perform full-text search: {e}")
