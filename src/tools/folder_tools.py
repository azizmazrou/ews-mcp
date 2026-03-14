"""Folder management tools for EWS MCP Server."""

from typing import Any, Dict, List
from exchangelib import Folder

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, safe_get, ews_id_to_str


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
        parent_folder_name = kwargs.get("parent_folder", "root").lower()
        depth = kwargs.get("depth", 2)
        include_hidden = kwargs.get("include_hidden", False)
        include_counts = kwargs.get("include_counts", True)
        target_mailbox = kwargs.get("target_mailbox")

        if depth < 1 or depth > 10:
            raise ToolExecutionError("depth must be between 1 and 10")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            folder_map = {
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

            parent_folder = folder_map.get(parent_folder_name)
            if not parent_folder:
                raise ToolExecutionError(f"Unknown parent folder: {parent_folder_name}")

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
                                child_name = safe_get(child, 'name', '')
                                child_class = safe_get(child, 'folder_class', '')

                                system_folder_names = {
                                    'recoverable items', 'recoverable items deletions',
                                    'recoverable items purges', 'recoverable items versions',
                                    'calendar logging', 'conversation action settings',
                                    'quick step settings', 'suggested contacts',
                                    'sync issues', 'conflicts', 'local failures',
                                    'server failures', 'deletions', 'purges', 'versions',
                                    'audits', 'administrativeaudits', 'conversationhistory',
                                    'mycontacts', 'peopleconnect', 'quickcontacts',
                                    'recipientcache', 'skypetelemetry', 'teamchat',
                                    'workingset', 'companies', 'organizational contacts'
                                }

                                if child_name.lower() in system_folder_names:
                                    continue
                                if child_name.startswith('~') or child_name.startswith('_'):
                                    continue
                                if child_class:
                                    user_facing_classes = ['IPF.Note', 'IPF.Appointment', 'IPF.Contact', 'IPF.Task']
                                    if not any(cls in child_class for cls in user_facing_classes):
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
                parent_folder=parent_folder_name,
                depth=depth,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to list folders: {e}")
            raise ToolExecutionError(f"Failed to list folders: {e}")


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
                        "description": "Target parent folder name or ID (move action)",
                        "enum": ["root", "inbox", "sent", "drafts", "deleted", "junk", "calendar", "contacts", "tasks"]
                    },
                    "permanent": {
                        "type": "boolean",
                        "description": "Permanently delete (true) or soft delete (false)",
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

    def _find_folder_by_id(self, parent, target_id):
        """Recursively search for folder by ID."""
        parent_id = ews_id_to_str(safe_get(parent, 'id', None)) or ''
        if parent_id == target_id:
            return parent
        if hasattr(parent, 'children') and parent.children:
            for child in parent.children:
                result = self._find_folder_by_id(child, target_id)
                if result:
                    return result
        return None

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
        parent_folder_name = kwargs.get("parent_folder", "inbox").lower()
        folder_class = kwargs.get("folder_class", "IPF.Note")
        target_mailbox = kwargs.get("target_mailbox")

        if not folder_name:
            raise ToolExecutionError("folder_name is required for create action")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            folder_map = self._get_folder_map(account)
            parent_folder = folder_map.get(parent_folder_name)
            if not parent_folder:
                raise ToolExecutionError(f"Unknown parent folder: {parent_folder_name}")

            new_folder = Folder(parent=parent_folder, name=folder_name, folder_class=folder_class)
            new_folder.save()

            return format_success_response(
                f"Folder '{folder_name}' created successfully",
                folder_id=ews_id_to_str(new_folder.id),
                folder_name=folder_name,
                parent_folder=parent_folder_name,
                folder_class=folder_class,
                mailbox=mailbox
            )
        except ToolExecutionError:
            raise
        except Exception as e:
            raise ToolExecutionError(f"Failed to create folder: {e}")

    async def _delete(self, **kwargs) -> Dict[str, Any]:
        """Delete a folder."""
        folder_id = kwargs.get("folder_id")
        permanent = kwargs.get("permanent", False)
        target_mailbox = kwargs.get("target_mailbox")

        if not folder_id:
            raise ToolExecutionError("folder_id is required for delete action")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            folder = self._find_folder_by_id(account.root, folder_id)
            if not folder:
                raise ToolExecutionError(f"Folder not found: {folder_id}")

            folder_name = safe_get(folder, 'name', 'Unknown')

            if permanent:
                folder.delete()
                action = "permanently deleted"
            else:
                folder.soft_delete()
                action = "moved to Deleted Items"

            return format_success_response(
                f"Folder '{folder_name}' {action}",
                folder_id=folder_id,
                folder_name=folder_name,
                permanent=permanent,
                mailbox=mailbox
            )
        except ToolExecutionError:
            raise
        except Exception as e:
            raise ToolExecutionError(f"Failed to delete folder: {e}")

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
        """Move a folder to a new parent."""
        folder_id = kwargs.get("folder_id")
        destination = kwargs.get("destination")
        target_mailbox = kwargs.get("target_mailbox")

        if not folder_id:
            raise ToolExecutionError("folder_id is required for move action")
        if not destination:
            raise ToolExecutionError("destination is required for move action")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            folder = self._find_folder_by_id(account.root, folder_id)
            if not folder:
                raise ToolExecutionError(f"Folder not found: {folder_id}")

            folder_name = safe_get(folder, 'name', 'Unknown')

            folder_map = self._get_folder_map(account)
            target_parent = folder_map.get(destination.lower())
            if not target_parent:
                raise ToolExecutionError(f"Unknown destination folder: {destination}")

            folder.parent = target_parent
            folder.save()

            return format_success_response(
                f"Folder '{folder_name}' moved to '{destination}'",
                folder_id=folder_id,
                folder_name=folder_name,
                destination=destination,
                mailbox=mailbox
            )
        except ToolExecutionError:
            raise
        except Exception as e:
            raise ToolExecutionError(f"Failed to move folder: {e}")
