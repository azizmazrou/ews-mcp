"""Folder management tools for EWS MCP Server."""

from difflib import SequenceMatcher
from typing import Any, Dict
from exchangelib import Folder

from .base import BaseTool
from ..exceptions import ToolExecutionError, ValidationError
from ..utils import format_success_response, safe_get, ews_id_to_str


def get_standard_folder_map(account):
    """Get standard folder name to object mapping."""
    return {
        "root": account.root,
        "inbox": account.inbox,
        "sent": account.sent,
        "drafts": account.drafts,
        "deleted": account.trash,
        "junk": account.junk,
        "calendar": account.calendar,
        "contacts": account.contacts,
        "tasks": account.tasks
    }


def find_folder_by_id(parent, target_id):
    """Recursively search for folder by ID."""
    parent_id = ews_id_to_str(safe_get(parent, 'id', None)) or ''
    if parent_id == target_id:
        return parent
    if hasattr(parent, 'children') and parent.children:
        for child in parent.children:
            result = find_folder_by_id(child, target_id)
            if result:
                return result
    return None


def resolve_parent_folder(account, parent_folder=None, parent_folder_id=None, default_name="root"):
    """Resolve parent folder from ID or standard folder name."""
    if parent_folder_id:
        folder = find_folder_by_id(account.root, parent_folder_id)
        if not folder:
            raise ToolExecutionError(f"Parent folder not found: {parent_folder_id}")
        return folder, safe_get(folder, "name", parent_folder_id)

    parent_folder_name = (parent_folder or default_name).lower()
    folder = get_standard_folder_map(account).get(parent_folder_name)
    if not folder:
        raise ToolExecutionError(f"Unknown parent folder: {parent_folder_name}")
    return folder, parent_folder_name


def is_user_visible_folder(folder) -> bool:
    """Check whether a folder should be visible in user-facing listings."""
    folder_name = safe_get(folder, "name", "")
    folder_class = safe_get(folder, "folder_class", "")

    system_folder_names = {
        "recoverable items", "recoverable items deletions",
        "recoverable items purges", "recoverable items versions",
        "calendar logging", "conversation action settings",
        "quick step settings", "suggested contacts",
        "sync issues", "conflicts", "local failures",
        "server failures", "deletions", "purges", "versions",
        "audits", "administrativeaudits", "conversationhistory",
        "mycontacts", "peopleconnect", "quickcontacts",
        "recipientcache", "skypetelemetry", "teamchat",
        "workingset", "companies", "organizational contacts"
    }

    if folder_name.lower() in system_folder_names:
        return False
    if folder_name.startswith("~") or folder_name.startswith("_"):
        return False
    if folder_class:
        user_facing_classes = ["IPF.Note", "IPF.Appointment", "IPF.Contact", "IPF.Task"]
        if not any(cls in folder_class for cls in user_facing_classes):
            return False

    return True


class ListFoldersTool(BaseTool):
    """Tool for listing mailbox folder hierarchy."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "list_folders",
            "description": "List mailbox folder hierarchy.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "parent_folder": {
                        "type": "string",
                        "description": "Parent folder to start from (default: root)",
                        "default": "root",
                        "enum": ["root", "inbox", "sent", "drafts", "deleted", "junk", "calendar", "contacts", "tasks"]
                    },
                    "parent_folder_id": {
                        "type": "string",
                        "description": "Parent folder ID (alternative to parent_folder)"
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Maximum depth to traverse (1-10)",
                        "default": 2,
                        "minimum": 1,
                        "maximum": 10
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "description": "Include hidden folders",
                        "default": False
                    },
                    "include_counts": {
                        "type": "boolean",
                        "description": "Include item counts for each folder",
                        "default": True
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                }
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """List folders recursively."""
        parent_folder_name = kwargs.get("parent_folder")
        parent_folder_id = kwargs.get("parent_folder_id")
        depth = kwargs.get("depth", 2)
        include_hidden = kwargs.get("include_hidden", False)
        include_counts = kwargs.get("include_counts", True)
        target_mailbox = kwargs.get("target_mailbox")

        if depth < 1 or depth > 10:
            raise ToolExecutionError("depth must be between 1 and 10")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            parent_folder, resolved_parent = resolve_parent_folder(
                account,
                parent_folder=parent_folder_name,
                parent_folder_id=parent_folder_id,
                default_name="root"
            )

            def list_folder_tree(folder, current_depth, max_depth):
                if current_depth > max_depth:
                    return None

                folder_info = {
                    "id": ews_id_to_str(safe_get(folder, 'id', None)) or '',
                    "name": safe_get(folder, 'name', ''),
                    "parent_folder_id": ews_id_to_str(safe_get(folder, 'parent_folder_id', None)) or '',
                    "folder_class": safe_get(folder, 'folder_class', ''),
                    "child_folder_count": safe_get(folder, 'child_folder_count', 0)
                }

                if include_counts:
                    try:
                        folder_info["total_count"] = safe_get(folder, 'total_count', 0)
                        folder_info["unread_count"] = safe_get(folder, 'unread_count', 0)
                    except Exception:
                        folder_info["total_count"] = 0
                        folder_info["unread_count"] = 0

                children = []
                try:
                    if hasattr(folder, 'children') and folder.children:
                        for child in folder.children:
                            if not include_hidden:
                                if not is_user_visible_folder(child):
                                    continue

                            child_info = list_folder_tree(child, current_depth + 1, max_depth)
                            if child_info:
                                children.append(child_info)
                except Exception as e:
                    self.logger.warning(f"Error listing children of folder {folder_info['name']}: {e}")

                if children:
                    folder_info["children"] = children

                return folder_info

            folder_tree = list_folder_tree(parent_folder, 1, depth)

            def count_folders(tree):
                count = 1
                if "children" in tree:
                    for child in tree["children"]:
                        count += count_folders(child)
                return count

            total_folders = count_folders(folder_tree) if folder_tree else 0

            return format_success_response(
                f"Listed {total_folders} folder(s)",
                folder_tree=folder_tree,
                total_folders=total_folders,
                parent_folder=resolved_parent,
                depth=depth,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to list folders: {e}")
            raise ToolExecutionError(f"Failed to list folders: {e}")


class FindFolderTool(BaseTool):
    """Tool for discovering and searching mailbox folders."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "find_folder",
            "description": "Find folder candidates by name/path with exact, prefix, contains, or fuzzy matching.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Folder name or path query. If omitted, returns folders in scope."
                    },
                    "parent_folder": {
                        "type": "string",
                        "description": "Parent folder scope (default: root)",
                        "default": "root",
                        "enum": ["root", "inbox", "sent", "drafts", "deleted", "junk", "calendar", "contacts", "tasks"]
                    },
                    "parent_folder_id": {
                        "type": "string",
                        "description": "Parent folder ID scope (alternative to parent_folder)"
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Maximum depth to traverse (1-10)",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 10
                    },
                    "match_mode": {
                        "type": "string",
                        "description": "Matching strategy for query",
                        "default": "auto",
                        "enum": ["auto", "exact", "prefix", "contains", "fuzzy"]
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of candidates to return (1-100)",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 100
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "description": "Include hidden/system folders in search",
                        "default": False
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                }
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Find folder candidates in a folder tree scope."""
        query = (kwargs.get("query") or "").strip()
        parent_folder_name = kwargs.get("parent_folder")
        parent_folder_id = kwargs.get("parent_folder_id")
        depth = kwargs.get("depth", 5)
        match_mode = kwargs.get("match_mode", "auto")
        max_results = kwargs.get("max_results", 20)
        include_hidden = kwargs.get("include_hidden", False)
        target_mailbox = kwargs.get("target_mailbox")

        if depth < 1 or depth > 10:
            raise ToolExecutionError("depth must be between 1 and 10")
        if max_results < 1 or max_results > 100:
            raise ToolExecutionError("max_results must be between 1 and 100")
        if match_mode not in {"auto", "exact", "prefix", "contains", "fuzzy"}:
            raise ToolExecutionError("match_mode must be one of: auto, exact, prefix, contains, fuzzy")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            parent_folder, resolved_parent = resolve_parent_folder(
                account,
                parent_folder=parent_folder_name,
                parent_folder_id=parent_folder_id,
                default_name="root"
            )

            all_entries = []

            def walk_folders(folder, current_depth, current_path):
                if current_depth > depth:
                    return

                folder_name = safe_get(folder, "name", "")
                folder_path = f"{current_path}/{folder_name}" if current_path else folder_name
                all_entries.append({
                    "id": ews_id_to_str(safe_get(folder, "id", None)) or "",
                    "name": folder_name,
                    "path": folder_path,
                    "parent_folder_id": ews_id_to_str(safe_get(folder, "parent_folder_id", None)) or "",
                    "folder_class": safe_get(folder, "folder_class", ""),
                    "child_folder_count": safe_get(folder, "child_folder_count", 0)
                })

                if not hasattr(folder, "children") or not folder.children:
                    return

                for child in folder.children:
                    if not include_hidden and not is_user_visible_folder(child):
                        continue
                    walk_folders(child, current_depth + 1, folder_path)

            walk_folders(parent_folder, 1, "")

            query_lower = query.lower()

            def match_entry(entry):
                if not query:
                    return "all", 1.0

                name_value = entry["name"].lower()
                path_value = entry["path"].lower()

                if match_mode in {"auto", "exact"}:
                    if name_value == query_lower or path_value == query_lower:
                        return "exact", 1.0

                if match_mode in {"auto", "prefix"}:
                    if name_value.startswith(query_lower) or path_value.startswith(query_lower):
                        return "prefix", 0.9

                if match_mode in {"auto", "contains"}:
                    if query_lower in name_value or query_lower in path_value:
                        return "contains", 0.8

                if match_mode in {"auto", "fuzzy"}:
                    name_ratio = SequenceMatcher(None, query_lower, name_value).ratio()
                    path_ratio = SequenceMatcher(None, query_lower, path_value).ratio()
                    best_ratio = max(name_ratio, path_ratio)
                    threshold = 0.6 if match_mode == "fuzzy" else 0.72
                    if best_ratio >= threshold:
                        return "fuzzy", round(best_ratio, 3)

                return None, 0.0

            matches = []
            for entry in all_entries:
                match_type, score = match_entry(entry)
                if match_type:
                    matches.append({
                        **entry,
                        "match_type": match_type,
                        "score": score
                    })

            match_rank = {"exact": 4, "prefix": 3, "contains": 2, "fuzzy": 1, "all": 0}
            matches.sort(
                key=lambda item: (
                    match_rank.get(item["match_type"], 0),
                    item["score"],
                    -len(item["path"])
                ),
                reverse=True
            )
            matches = matches[:max_results]

            return format_success_response(
                f"Found {len(matches)} folder candidate(s)",
                query=query,
                match_mode=match_mode,
                parent_folder=resolved_parent,
                depth=depth,
                total_scanned=len(all_entries),
                total_matches=len(matches),
                matches=matches,
                mailbox=mailbox
            )
        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to find folders: {e}")
            raise ToolExecutionError(f"Failed to find folders: {e}")


class ManageFolderTool(BaseTool):
    """Unified folder management: create, delete, rename, move.

    Replaces: create_folder, delete_folder, rename_folder, move_folder.
    """

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "manage_folder",
            "description": "Create, delete, rename, or move a mailbox folder.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "delete", "rename", "move"],
                        "description": "Folder operation to perform"
                    },
                    "folder_name": {
                        "type": "string",
                        "description": "Name for new folder (create action)"
                    },
                    "folder_id": {
                        "type": "string",
                        "description": "Folder ID (required for delete, rename, move)"
                    },
                    "parent_folder": {
                        "type": "string",
                        "description": "Parent folder for create action",
                        "default": "inbox",
                        "enum": ["root", "inbox", "sent", "drafts", "deleted", "junk", "calendar", "contacts", "tasks"]
                    },
                    "parent_folder_id": {
                        "type": "string",
                        "description": "Parent folder ID for create action (alternative to parent_folder)"
                    },
                    "folder_class": {
                        "type": "string",
                        "description": "Folder class for create (type of items)",
                        "default": "IPF.Note",
                        "enum": ["IPF.Note", "IPF.Appointment", "IPF.Contact", "IPF.Task"]
                    },
                    "new_name": {
                        "type": "string",
                        "description": "New name (rename action)"
                    },
                    "destination": {
                        "type": "string",
                        "description": "Target parent folder — standard name (move action). Prefer destination_folder_id for custom folders.",
                        "enum": ["root", "inbox", "sent", "drafts", "deleted", "junk", "calendar", "contacts", "tasks"]
                    },
                    "destination_folder_id": {
                        "type": "string",
                        "description": "Target parent folder id (move action). Same shape as move_email's destination_folder_id."
                    },
                    "permanent": {
                        "type": "boolean",
                        "description": "Permanently delete (true) or soft delete (false). Alias: hard_delete.",
                        "default": False
                    },
                    "hard_delete": {
                        "type": "boolean",
                        "description": "Alias for 'permanent' for callers matching delete_email shape.",
                        "default": False
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["action"]
            }
        }

    def _get_folder_map(self, account):
        """Get standard folder name to object mapping."""
        return get_standard_folder_map(account)

    def _find_folder_by_id(self, parent, target_id):
        """Recursively search for folder by ID."""
        return find_folder_by_id(parent, target_id)

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Route to appropriate folder action."""
        action = kwargs.get("action")
        if not action:
            raise ToolExecutionError("action is required")

        if action == "create":
            return await self._create(**kwargs)
        elif action == "delete":
            return await self._delete(**kwargs)
        elif action == "rename":
            return await self._rename(**kwargs)
        elif action == "move":
            return await self._move(**kwargs)
        else:
            raise ToolExecutionError(f"Unknown action: {action}")

    async def _create(self, **kwargs) -> Dict[str, Any]:
        """Create a new folder."""
        folder_name = kwargs.get("folder_name")
        parent_folder_name = kwargs.get("parent_folder")
        parent_folder_id = kwargs.get("parent_folder_id")
        folder_class = kwargs.get("folder_class", "IPF.Note")
        target_mailbox = kwargs.get("target_mailbox")

        if not folder_name:
            raise ToolExecutionError("folder_name is required for create action")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            parent_folder, resolved_parent = resolve_parent_folder(
                account,
                parent_folder=parent_folder_name,
                parent_folder_id=parent_folder_id,
                default_name="inbox"
            )

            new_folder = Folder(parent=parent_folder, name=folder_name, folder_class=folder_class)
            new_folder.save()

            return format_success_response(
                f"Folder '{folder_name}' created successfully",
                folder_id=ews_id_to_str(new_folder.id),
                folder_name=folder_name,
                parent_folder=resolved_parent,
                folder_class=folder_class,
                mailbox=mailbox
            )
        except ToolExecutionError:
            raise
        except Exception as e:
            raise ToolExecutionError(f"Failed to create folder: {e}")

    async def _delete(self, **kwargs) -> Dict[str, Any]:
        """Delete a folder.

        Accepts either ``folder_id`` or ``folder_name`` (the latter as a
        fallback so the tool is consistent with ``_create``). Accepts
        ``permanent`` and the callers'-common alias ``hard_delete``.
        """
        folder_id = kwargs.get("folder_id")
        folder_name_input = kwargs.get("folder_name")
        parent_folder_name = kwargs.get("parent_folder")
        # ``permanent`` is the canonical param; ``hard_delete`` is an
        # alias many callers use (matching delete_email). Accept both.
        permanent = bool(kwargs.get("permanent", kwargs.get("hard_delete", False)))
        target_mailbox = kwargs.get("target_mailbox")

        if not folder_id and not folder_name_input:
            raise ValidationError(
                "delete requires folder_id (or folder_name as fallback)"
            )

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            folder = None
            if folder_id:
                folder = self._find_folder_by_id(account.root, folder_id)

            # Fallback: resolve by name. Useful when a caller pasted a
            # folder name into the folder_id field (common confusion)
            # or is using the create-style shape.
            if folder is None and folder_name_input:
                try:
                    from ..utils import resolve_folder_for_account
                    # resolve_folder_for_account is async in this file.
                    folder = await resolve_folder_for_account(
                        account, folder_name_input
                    )
                except Exception as resolve_exc:
                    self.logger.debug(
                        f"folder name resolve failed for {folder_name_input!r}: {resolve_exc}"
                    )
                    folder = None

            if folder is None:
                # Caller error — not a server crash. Return 400-mapped
                # ValidationError so the operator sees a clear message.
                ident = folder_id or folder_name_input
                raise ValidationError(f"Folder not found: {ident!r}")

            resolved_folder_name = safe_get(folder, 'name', 'Unknown')

            try:
                if permanent:
                    folder.delete()
                    action = "permanently deleted"
                else:
                    folder.soft_delete()
                    action = "moved to Deleted Items"
            except Exception as del_exc:
                # Exchange returns an informative error for common cases
                # ("folder not empty", "system folder cannot be deleted",
                # "insufficient permissions") — bubble it up verbatim so
                # the operator sees the real cause instead of a 500.
                self.logger.warning(
                    "folder.%s failed for %r: %s: %s",
                    "delete" if permanent else "soft_delete",
                    resolved_folder_name, type(del_exc).__name__, del_exc,
                )
                raise ToolExecutionError(
                    f"Failed to delete folder {resolved_folder_name!r}: "
                    f"{type(del_exc).__name__}: {del_exc}"
                )

            return format_success_response(
                f"Folder '{resolved_folder_name}' {action}",
                folder_id=folder_id or ews_id_to_str(safe_get(folder, 'id', None)),
                folder_name=resolved_folder_name,
                permanent=permanent,
                hard_delete=permanent,
                mailbox=mailbox,
            )
        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            # logger.exception emits the full traceback so operators can
            # diagnose the real upstream cause.
            self.logger.exception(
                f"manage_folder(action=delete) failed: {type(e).__name__}: {e}"
            )
            raise ToolExecutionError(
                f"Failed to delete folder: {type(e).__name__}: {e}"
            )

    async def _rename(self, **kwargs) -> Dict[str, Any]:
        """Rename a folder."""
        folder_id = kwargs.get("folder_id")
        new_name = kwargs.get("new_name")
        target_mailbox = kwargs.get("target_mailbox")

        if not folder_id or not new_name:
            raise ToolExecutionError("folder_id and new_name are required for rename action")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            folder = self._find_folder_by_id(account.root, folder_id)
            if not folder:
                raise ToolExecutionError(f"Folder not found: {folder_id}")

            old_name = safe_get(folder, 'name', 'Unknown')
            folder.name = new_name
            folder.save()

            return format_success_response(
                f"Folder renamed from '{old_name}' to '{new_name}'",
                folder_id=folder_id,
                old_name=old_name,
                new_name=new_name,
                mailbox=mailbox
            )
        except ToolExecutionError:
            raise
        except Exception as e:
            raise ToolExecutionError(f"Failed to rename folder: {e}")

    async def _move(self, **kwargs) -> Dict[str, Any]:
        """Move a folder to a new parent.

        Destination accepted in three forms (in order of preference):

        * ``destination_folder_id`` — same name as ``move_email`` uses
          (canonical).
        * ``destination`` — legacy alias for either a standard folder
          name (``"archive"``) OR a folder id string.
        * No param is rejected with ValidationError (HTTP 400).
        """
        folder_id = kwargs.get("folder_id")
        # Bug #5: accept destination_folder_id so callers can use the same
        # shape as move_email / copy_email.
        destination_id = kwargs.get("destination_folder_id")
        destination = kwargs.get("destination")
        target_mailbox = kwargs.get("target_mailbox")

        if not folder_id:
            raise ValidationError("folder_id is required for move action")
        if not destination and not destination_id:
            raise ValidationError(
                "move requires 'destination' (standard folder name) or "
                "'destination_folder_id' (explicit folder id)"
            )

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            folder = self._find_folder_by_id(account.root, folder_id)
            if not folder:
                raise ValidationError(f"Folder not found: {folder_id}")

            folder_name = safe_get(folder, 'name', 'Unknown')

            # Resolve the target parent. First try the explicit id; then
            # try ``destination`` as a standard name; finally try
            # ``destination`` as an id (some callers conflate the fields).
            target_parent = None
            resolved_destination = None
            if destination_id:
                target_parent = self._find_folder_by_id(account.root, destination_id)
                resolved_destination = destination_id
            if target_parent is None and destination:
                folder_map = self._get_folder_map(account)
                target_parent = folder_map.get(str(destination).lower())
                if target_parent is not None:
                    resolved_destination = destination
                else:
                    # Fallback: maybe ``destination`` is an id.
                    target_parent = self._find_folder_by_id(account.root, destination)
                    if target_parent is not None:
                        resolved_destination = destination

            if target_parent is None:
                raise ValidationError(
                    f"Unknown destination: "
                    f"{destination_id or destination!r}"
                )

            folder.parent = target_parent
            folder.save()

            return format_success_response(
                f"Folder '{folder_name}' moved",
                folder_id=folder_id,
                folder_name=folder_name,
                destination=resolved_destination,
                destination_folder_id=ews_id_to_str(
                    safe_get(target_parent, "id", None)
                ),
                mailbox=mailbox,
            )
        except (ValidationError, ToolExecutionError):
            raise
        except Exception as e:
            self.logger.exception(
                f"manage_folder(action=move) failed: {type(e).__name__}: {e}"
            )
            raise ToolExecutionError(
                f"Failed to move folder: {type(e).__name__}: {e}"
            )
