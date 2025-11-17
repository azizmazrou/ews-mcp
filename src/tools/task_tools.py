"""Task operation tools for EWS MCP Server."""

from typing import Any, Dict
from datetime import datetime
from exchangelib import Task

from .base import BaseTool
from ..models import CreateTaskRequest
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, safe_get, parse_datetime_tz_aware, parse_date_tz_aware


class CreateTaskTool(BaseTool):
    """Tool for creating tasks."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "create_task",
            "description": "Create a new task in Exchange",
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
                    }
                },
                "required": ["subject"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Create task."""
        # Validate input first (Pydantic expects datetime types)
        request = self.validate_input(CreateTaskRequest, **kwargs)

        try:
            # Create task
            task = Task(
                account=self.ews_client.account,
                folder=self.ews_client.account.tasks,
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
                item_id=task.id if hasattr(task, "id") else None,
                subject=request.subject
            )

        except Exception as e:
            self.logger.error(f"Failed to create task: {e}")
            raise ToolExecutionError(f"Failed to create task: {e}")


class GetTasksTool(BaseTool):
    """Tool for retrieving tasks."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_tasks",
            "description": "Retrieve tasks, optionally filtered by status",
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
                    }
                }
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Get tasks."""
        include_completed = kwargs.get("include_completed", False)
        max_results = kwargs.get("max_results", 50)

        try:
            # Query tasks
            items = self.ews_client.account.tasks.all()

            if not include_completed:
                items = items.filter(is_complete=False)

            items = items.order_by('-datetime_created')

            # Format tasks
            tasks = []
            for item in items[:max_results]:
                task_data = {
                    "item_id": safe_get(item, "id", "unknown"),
                    "subject": safe_get(item, "subject", "") or "",
                    "status": safe_get(item, "status", "NotStarted") or "NotStarted",
                    "percent_complete": safe_get(item, "percent_complete", 0),
                    "is_complete": safe_get(item, "is_complete", False),
                    "due_date": safe_get(item, "due_date", None),
                    "importance": safe_get(item, "importance", "Normal") or "Normal"
                }

                # Format due date
                if task_data["due_date"]:
                    task_data["due_date"] = task_data["due_date"].isoformat()

                tasks.append(task_data)

            self.logger.info(f"Retrieved {len(tasks)} tasks")

            return format_success_response(
                f"Retrieved {len(tasks)} tasks",
                tasks=tasks
            )

        except Exception as e:
            self.logger.error(f"Failed to get tasks: {e}")
            raise ToolExecutionError(f"Failed to get tasks: {e}")


class UpdateTaskTool(BaseTool):
    """Tool for updating tasks."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "update_task",
            "description": "Update an existing task",
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
                    }
                },
                "required": ["item_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Update task."""
        item_id = kwargs.get("item_id")

        try:
            # Get the task
            task = self.ews_client.account.tasks.get(id=item_id)

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
                task.percent_complete = kwargs["percent_complete"]

            if "importance" in kwargs:
                task.importance = kwargs["importance"]

            # Save changes
            task.save()

            self.logger.info(f"Updated task {item_id}")

            return format_success_response(
                "Task updated successfully",
                item_id=item_id
            )

        except Exception as e:
            self.logger.error(f"Failed to update task: {e}")
            raise ToolExecutionError(f"Failed to update task: {e}")


class CompleteTaskTool(BaseTool):
    """Tool for marking tasks as complete."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "complete_task",
            "description": "Mark a task as complete",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Task item ID to complete"
                    }
                },
                "required": ["item_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Complete task."""
        item_id = kwargs.get("item_id")

        try:
            # Get and complete the task
            task = self.ews_client.account.tasks.get(id=item_id)
            task.percent_complete = 100
            task.status = "Completed"
            task.is_complete = True
            task.save()

            self.logger.info(f"Completed task {item_id}")

            return format_success_response(
                "Task marked as complete",
                item_id=item_id
            )

        except Exception as e:
            self.logger.error(f"Failed to complete task: {e}")
            raise ToolExecutionError(f"Failed to complete task: {e}")


class DeleteTaskTool(BaseTool):
    """Tool for deleting tasks."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_task",
            "description": "Delete a task",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Task item ID to delete"
                    }
                },
                "required": ["item_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Delete task."""
        item_id = kwargs.get("item_id")

        try:
            # Get and delete the task
            task = self.ews_client.account.tasks.get(id=item_id)
            task.delete()

            self.logger.info(f"Deleted task {item_id}")

            return format_success_response(
                "Task deleted successfully",
                item_id=item_id
            )

        except Exception as e:
            self.logger.error(f"Failed to delete task: {e}")
            raise ToolExecutionError(f"Failed to delete task: {e}")
