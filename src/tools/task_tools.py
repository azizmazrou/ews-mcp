"""Task operation tools for EWS MCP Server."""

from typing import Any, Dict
from datetime import datetime
from decimal import Decimal
from exchangelib import Task

from .base import BaseTool
from ..models import CreateTaskRequest
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, safe_get, parse_datetime_tz_aware, parse_date_tz_aware, ews_id_to_str


class CreateTaskTool(BaseTool):
    """Tool for creating tasks."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "create_task",
            "description": "Create a new task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Task subject"
                    },
                    "body": {
                        "type": "string",
                        "description": "Task body (optional)"
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date (ISO 8601 format, optional)"
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date (ISO 8601 format, optional)"
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["Low", "Normal", "High"],
                        "description": "Task importance (optional)",
                        "default": "Normal"
                    },
                    "reminder_time": {
                        "type": "string",
                        "description": "Reminder time (ISO 8601 format, optional)"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["subject"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Create task."""
        # Validate input first (Pydantic expects datetime types)
        request = self.validate_input(CreateTaskRequest, **kwargs)
        target_mailbox = kwargs.get("target_mailbox")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Create task
            task = Task(
                account=account,
                folder=account.tasks,
                subject=request.subject
            )

            # Set optional fields
            if request.body:
                task.body = request.body

            # Convert datetime to EWSDate for date-only fields
            if request.due_date:
                task.due_date = parse_date_tz_aware(request.due_date.isoformat())

            if request.start_date:
                task.start_date = parse_date_tz_aware(request.start_date.isoformat())

            task.importance = request.importance.value

            # Convert datetime to EWSDateTime for datetime fields
            if request.reminder_time:
                task.reminder_is_set = True
                task.reminder_due_by = parse_datetime_tz_aware(request.reminder_time.isoformat())

            # Save task
            task.save()

            self.logger.info(f"Created task: {request.subject}")

            return format_success_response(
                "Task created successfully",
                item_id=ews_id_to_str(task.id) if hasattr(task, "id") else None,
                subject=request.subject,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to create task: {e}")
            raise ToolExecutionError(f"Failed to create task: {e}")


class GetTasksTool(BaseTool):
    """Tool for retrieving tasks."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_tasks",
            "description": "Retrieve tasks, optionally filtered by status.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "include_completed": {
                        "type": "boolean",
                        "description": "Include completed tasks",
                        "default": False
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of tasks to retrieve",
                        "default": 50,
                        "maximum": 1000
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                }
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Get tasks."""
        include_completed = kwargs.get("include_completed", False)
        max_results = kwargs.get("max_results", 50)
        target_mailbox = kwargs.get("target_mailbox")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Query tasks. ``account.tasks`` can raise if the folder is
            # unavailable on this mailbox, so guard narrowly.
            tasks_folder = getattr(account, "tasks", None)
            if tasks_folder is None:
                raise ToolExecutionError(
                    "Tasks folder is unavailable on this mailbox"
                )
            items = tasks_folder.all()

            if not include_completed:
                items = items.filter(is_complete=False)

            items = items.order_by('-datetime_created')

            # Format tasks. Wrap each item so one malformed task cannot
            # sink the entire response — previously a single bad
            # ``due_date`` or missing attribute produced an opaque HTTP
            # 500 for the whole call.
            tasks = []
            skipped = 0
            for item in items[:max_results]:
                try:
                    due_date = safe_get(item, "due_date", None)
                    # due_date may be an EWSDate/EWSDateTime, a plain
                    # date, or a string — coerce defensively.
                    if due_date is not None and hasattr(due_date, "isoformat"):
                        due_iso = due_date.isoformat()
                    elif due_date is not None:
                        due_iso = str(due_date)
                    else:
                        due_iso = None

                    task_data = {
                        "item_id": ews_id_to_str(safe_get(item, "id", None)) or "unknown",
                        "subject": safe_get(item, "subject", "") or "",
                        "status": safe_get(item, "status", "NotStarted") or "NotStarted",
                        "percent_complete": safe_get(item, "percent_complete", 0),
                        "is_complete": safe_get(item, "is_complete", False),
                        "due_date": due_iso,
                        "importance": safe_get(item, "importance", "Normal") or "Normal",
                    }
                    tasks.append(task_data)
                except Exception as item_exc:
                    skipped += 1
                    self.logger.warning(
                        "Skipped malformed task id=%r: %s: %s",
                        safe_get(item, "id", None),
                        type(item_exc).__name__,
                        item_exc,
                    )
                    continue

            self.logger.info(
                f"Retrieved {len(tasks)} tasks (skipped {skipped} malformed)"
            )

            return format_success_response(
                f"Retrieved {len(tasks)} tasks",
                tasks=tasks,
                count=len(tasks),
                skipped=skipped,
                mailbox=mailbox,
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            # logger.exception emits the traceback so the operator can see
            # the real upstream cause instead of "Internal Server Error".
            self.logger.exception(
                f"get_tasks failed: {type(e).__name__}: {e}"
            )
            raise ToolExecutionError(f"Failed to get tasks: {type(e).__name__}: {e}")


class UpdateTaskTool(BaseTool):
    """Tool for updating tasks."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "update_task",
            "description": "Update an existing task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Task item ID"
                    },
                    "subject": {
                        "type": "string",
                        "description": "New subject (optional)"
                    },
                    "body": {
                        "type": "string",
                        "description": "New body (optional)"
                    },
                    "due_date": {
                        "type": "string",
                        "description": "New due date (ISO 8601 format, optional)"
                    },
                    "percent_complete": {
                        "type": "integer",
                        "description": "Percent complete (0-100, optional)",
                        "minimum": 0,
                        "maximum": 100
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["Low", "Normal", "High"],
                        "description": "New importance (optional)"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["item_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Update task."""
        item_id = kwargs.get("item_id")
        target_mailbox = kwargs.get("target_mailbox")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get the task
            task = account.tasks.get(id=item_id)

            # Update fields
            if "subject" in kwargs:
                task.subject = kwargs["subject"]

            if "body" in kwargs:
                task.body = kwargs["body"]

            if "due_date" in kwargs:
                # Convert string to EWSDate for date-only field
                due_date_str = kwargs["due_date"]
                if isinstance(due_date_str, str):
                    task.due_date = parse_date_tz_aware(due_date_str)
                else:
                    # If it's already a datetime object from somewhere else
                    task.due_date = parse_date_tz_aware(due_date_str.isoformat())

            if "percent_complete" in kwargs:
                task.percent_complete = Decimal(str(kwargs["percent_complete"]))

            if "importance" in kwargs:
                task.importance = kwargs["importance"]

            # Save changes
            task.save()

            self.logger.info(f"Updated task {item_id}")

            return format_success_response(
                "Task updated successfully",
                item_id=item_id,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to update task: {e}")
            raise ToolExecutionError(f"Failed to update task: {e}")


class CompleteTaskTool(BaseTool):
    """Tool for marking tasks as complete."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "complete_task",
            "description": "Mark a task as complete.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Task item ID to complete"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["item_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Complete task."""
        item_id = kwargs.get("item_id")
        target_mailbox = kwargs.get("target_mailbox")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get and complete the task
            task = account.tasks.get(id=item_id)
            task.percent_complete = Decimal('100')
            task.status = "Completed"
            task.is_complete = True
            task.save()

            self.logger.info(f"Completed task {item_id}")

            return format_success_response(
                "Task marked as complete",
                item_id=item_id,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to complete task: {e}")
            raise ToolExecutionError(f"Failed to complete task: {e}")


class DeleteTaskTool(BaseTool):
    """Tool for deleting tasks."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_task",
            "description": "Delete a task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Task item ID to delete"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["item_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Delete task."""
        item_id = kwargs.get("item_id")
        target_mailbox = kwargs.get("target_mailbox")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get and delete the task
            task = account.tasks.get(id=item_id)
            task.delete()

            self.logger.info(f"Deleted task {item_id}")

            return format_success_response(
                "Task deleted successfully",
                item_id=item_id,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to delete task: {e}")
            raise ToolExecutionError(f"Failed to delete task: {e}")
