"""Folder management tools for EWS MCP Server."""

from typing import Any, Dict, List
from exchangelib import Folder

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, safe_get, ews_id_to_str


class ListFoldersTool(BaseTool):
    """Tool for listing mailbox folder hierarchy. Supports impersonation to manage folders in another user's mailbox."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "list_folders",
            "description": "Get mailbox folder hierarchy with folder details. Supports impersonation to manage folders in another user's mailbox.",
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

        # Validate depth
        if depth < 1 or depth > 10:
            raise ToolExecutionError("depth must be between 1 and 10")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get the parent folder
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

            # Recursively list folders
            def list_folder_tree(folder, current_depth, max_depth):
                """Recursively list folder tree."""
                if current_depth > max_depth:
                    return None

                # Get folder info - use ews_id_to_str for ID fields to ensure JSON serializability
                folder_info = {
                    "id": ews_id_to_str(safe_get(folder, 'id', None)) or '',
                    "name": safe_get(folder, 'name', ''),
                    "parent_folder_id": ews_id_to_str(safe_get(folder, 'parent_folder_id', None)) or '',
                    "folder_class": safe_get(folder, 'folder_class', ''),
                    "child_folder_count": safe_get(folder, 'child_folder_count', 0)
                }

                # Add counts if requested
                if include_counts:
                    try:
                        folder_info["total_count"] = safe_get(folder, 'total_count', 0)
                        folder_info["unread_count"] = safe_get(folder, 'unread_count', 0)
                    except Exception:
                        folder_info["total_count"] = 0
                        folder_info["unread_count"] = 0

                # Get child folders
                children = []
                try:
                    if hasattr(folder, 'children') and folder.children:
                        for child in folder.children:
                            # Skip system/hidden folders if not requested
                            if not include_hidden:
                                child_name = safe_get(child, 'name', '')
                                child_class = safe_get(child, 'folder_class', '')

                                # System folders to skip (common Exchange system folders)
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

                                # Skip if folder name matches system folders
                                if child_name.lower() in system_folder_names:
                                    continue

                                # Skip folders starting with special characters
                                if child_name.startswith('~') or child_name.startswith('_'):
                                    continue

                                # Skip non-standard folder classes (only keep user-facing types)
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

            # Build folder tree
            folder_tree = list_folder_tree(parent_folder, 1, depth)

            # Count total folders
            def count_folders(tree):
                """Count total folders in tree."""
                count = 1
                if "children" in tree:
                    for child in tree["children"]:
                        count += count_folders(child)
                return count

            total_folders = count_folders(folder_tree) if folder_tree else 0

            self.logger.info(f"Listed {total_folders} folder(s) from {parent_folder_name}")

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


class CreateFolderTool(BaseTool):
    """Tool for creating new mailbox folders. Supports impersonation to manage folders in another user's mailbox."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "create_folder",
            "description": "Create a new folder in the mailbox. Supports impersonation to manage folders in another user's mailbox.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder_name": {
                        "type": "string",
                        "description": "Name of the new folder"
                    },
                    "parent_folder": {
                        "type": "string",
                        "description": "Parent folder location",
                        "default": "inbox",
                        "enum": ["root", "inbox", "sent", "drafts", "deleted", "junk", "calendar", "contacts", "tasks"]
                    },
                    "folder_class": {
                        "type": "string",
                        "description": "Folder class (type of items it will contain)",
                        "default": "IPF.Note",
                        "enum": ["IPF.Note", "IPF.Appointment", "IPF.Contact", "IPF.Task"]
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["folder_name"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Create a new folder."""
        folder_name = kwargs.get("folder_name")
        parent_folder_name = kwargs.get("parent_folder", "inbox").lower()
        folder_class = kwargs.get("folder_class", "IPF.Note")
        target_mailbox = kwargs.get("target_mailbox")

        if not folder_name:
            raise ToolExecutionError("folder_name is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get the parent folder
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

            # Create the new folder
            new_folder = Folder(parent=parent_folder, name=folder_name, folder_class=folder_class)
            new_folder.save()

            self.logger.info(f"Created folder '{folder_name}' in '{parent_folder_name}'")

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
            self.logger.error(f"Failed to create folder: {e}")
            raise ToolExecutionError(f"Failed to create folder: {e}")


class DeleteFolderTool(BaseTool):
    """Tool for deleting mailbox folders. Supports impersonation to manage folders in another user's mailbox."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_folder",
            "description": "Delete a mailbox folder (moves to Deleted Items or permanently deletes). Supports impersonation to manage folders in another user's mailbox.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder_id": {
                        "type": "string",
                        "description": "Folder ID to delete"
                    },
                    "permanent": {
                        "type": "boolean",
                        "description": "Permanently delete (true) or move to Deleted Items (false)",
                        "default": False
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["folder_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Delete a folder."""
        folder_id = kwargs.get("folder_id")
        permanent = kwargs.get("permanent", False)
        target_mailbox = kwargs.get("target_mailbox")

        if not folder_id:
            raise ToolExecutionError("folder_id is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find the folder by ID
            # We need to search through the folder tree
            folder = None

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

            # Search starting from root
            folder = find_folder_by_id(account.root, folder_id)

            if not folder:
                raise ToolExecutionError(f"Folder not found: {folder_id}")

            folder_name = safe_get(folder, 'name', 'Unknown')

            # Delete the folder
            if permanent:
                folder.delete()
                action = "permanently deleted"
            else:
                folder.soft_delete()
                action = "moved to Deleted Items"

            self.logger.info(f"Deleted folder '{folder_name}' (ID: {folder_id})")

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
            self.logger.error(f"Failed to delete folder: {e}")
            raise ToolExecutionError(f"Failed to delete folder: {e}")


class RenameFolderTool(BaseTool):
    """Tool for renaming mailbox folders. Supports impersonation to manage folders in another user's mailbox."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "rename_folder",
            "description": "Rename an existing mailbox folder. Supports impersonation to manage folders in another user's mailbox.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder_id": {
                        "type": "string",
                        "description": "Folder ID to rename"
                    },
                    "new_name": {
                        "type": "string",
                        "description": "New name for the folder"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["folder_id", "new_name"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Rename a folder."""
        folder_id = kwargs.get("folder_id")
        new_name = kwargs.get("new_name")
        target_mailbox = kwargs.get("target_mailbox")

        if not folder_id or not new_name:
            raise ToolExecutionError("folder_id and new_name are required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find the folder by ID
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

            folder = find_folder_by_id(account.root, folder_id)

            if not folder:
                raise ToolExecutionError(f"Folder not found: {folder_id}")

            old_name = safe_get(folder, 'name', 'Unknown')

            # Rename the folder
            folder.name = new_name
            folder.save()

            self.logger.info(f"Renamed folder from '{old_name}' to '{new_name}'")

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
            self.logger.error(f"Failed to rename folder: {e}")
            raise ToolExecutionError(f"Failed to rename folder: {e}")


class MoveFolderTool(BaseTool):
    """Tool for moving folders to a new parent folder. Supports impersonation to manage folders in another user's mailbox."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "move_folder",
            "description": "Move a folder to a new parent folder. Supports impersonation to manage folders in another user's mailbox.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder_id": {
                        "type": "string",
                        "description": "Folder ID to move"
                    },
                    "target_parent_folder": {
                        "type": "string",
                        "description": "Target parent folder location",
                        "enum": ["root", "inbox", "sent", "drafts", "deleted", "junk", "calendar", "contacts", "tasks"]
                    },
                    "target_parent_folder_id": {
                        "type": "string",
                        "description": "Target parent folder ID (alternative to target_parent_folder)"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["folder_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Move a folder to a new parent."""
        folder_id = kwargs.get("folder_id")
        target_parent_name = kwargs.get("target_parent_folder")
        target_parent_id = kwargs.get("target_parent_folder_id")
        target_mailbox = kwargs.get("target_mailbox")

        if not folder_id:
            raise ToolExecutionError("folder_id is required")

        if not target_parent_name and not target_parent_id:
            raise ToolExecutionError("Either target_parent_folder or target_parent_folder_id is required")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Find the folder to move
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

            folder = find_folder_by_id(account.root, folder_id)

            if not folder:
                raise ToolExecutionError(f"Folder not found: {folder_id}")

            folder_name = safe_get(folder, 'name', 'Unknown')

            # Get target parent folder
            if target_parent_name:
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
                target_parent = folder_map.get(target_parent_name.lower())
                if not target_parent:
                    raise ToolExecutionError(f"Unknown target parent folder: {target_parent_name}")
                target_name = target_parent_name
            else:
                target_parent = find_folder_by_id(account.root, target_parent_id)
                if not target_parent:
                    raise ToolExecutionError(f"Target parent folder not found: {target_parent_id}")
                target_name = safe_get(target_parent, 'name', 'Unknown')

            # Move the folder
            folder.parent = target_parent
            folder.save()

            self.logger.info(f"Moved folder '{folder_name}' to '{target_name}'")

            return format_success_response(
                f"Folder '{folder_name}' moved to '{target_name}'",
                folder_id=folder_id,
                folder_name=folder_name,
                target_parent=target_name,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to move folder: {e}")
            raise ToolExecutionError(f"Failed to move folder: {e}")
