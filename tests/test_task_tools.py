"""Tests for task tools."""

import pytest
from unittest.mock import Mock, MagicMock
from datetime import datetime

from src.tools.task_tools import (
    CreateTaskTool,
    GetTasksTool,
    UpdateTaskTool,
    CompleteTaskTool,
    DeleteTaskTool
)


@pytest.mark.asyncio
async def test_create_task_tool(mock_ews_client):
    """Test creating task."""
    tool = CreateTaskTool(mock_ews_client)

    # Mock task creation
    mock_task = MagicMock()
    mock_task.id = "task-123"
    mock_task.subject = "Test Task"

    with pytest.mock.patch('src.tools.task_tools.Task', return_value=mock_task):
        result = await tool.execute(
            subject="Test Task",
            body="Task description",
            due_date="2025-12-31",
            priority="High"
        )

    assert result["success"] is True
    assert "created successfully" in result["message"].lower()


@pytest.mark.asyncio
async def test_get_tasks_tool(mock_ews_client):
    """Test getting tasks."""
    tool = GetTasksTool(mock_ews_client)

    # Mock task list
    mock_task = MagicMock()
    mock_task.id = "task-1"
    mock_task.subject = "Test Task"
    mock_task.status = "NotStarted"
    mock_task.percent_complete = 0
    mock_task.due_date = datetime(2025, 12, 31)

    mock_ews_client.account.tasks.all.return_value.order_by.return_value = [mock_task]

    result = await tool.execute(max_results=10)

    assert result["success"] is True
    assert len(result["tasks"]) > 0


@pytest.mark.asyncio
async def test_update_task_tool(mock_ews_client):
    """Test updating task."""
    tool = UpdateTaskTool(mock_ews_client)

    # Mock task
    mock_task = MagicMock()
    mock_task.id = "task-1"
    mock_ews_client.account.tasks.get.return_value = mock_task

    result = await tool.execute(
        task_id="task-1",
        subject="Updated Task",
        status="InProgress"
    )

    assert result["success"] is True
    mock_task.save.assert_called_once()


@pytest.mark.asyncio
async def test_complete_task_tool(mock_ews_client):
    """Test completing task."""
    tool = CompleteTaskTool(mock_ews_client)

    # Mock task
    mock_task = MagicMock()
    mock_task.id = "task-1"
    mock_ews_client.account.tasks.get.return_value = mock_task

    result = await tool.execute(task_id="task-1")

    assert result["success"] is True
    assert mock_task.status == "Completed"
    assert mock_task.percent_complete == 100


@pytest.mark.asyncio
async def test_delete_task_tool(mock_ews_client):
    """Test deleting task."""
    tool = DeleteTaskTool(mock_ews_client)

    # Mock task
    mock_task = MagicMock()
    mock_ews_client.account.tasks.get.return_value = mock_task

    result = await tool.execute(task_id="task-1")

    assert result["success"] is True
    mock_task.delete.assert_called_once()
