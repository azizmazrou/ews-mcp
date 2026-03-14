"""Tests for folder management tools."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from exchangelib import Folder

from src.tools.folder_tools import (
    ManageFolderTool
)
from src.exceptions import ToolExecutionError


@pytest.mark.asyncio
async def test_create_folder(mock_ews_client):
    """Test creating a new folder."""
    tool = ManageFolderTool(mock_ews_client)

    # Mock parent folder
    mock_parent = MagicMock()
    mock_ews_client.account.inbox = mock_parent

    with patch('src.tools.folder_tools.Folder') as mock_folder_class:
        mock_folder = MagicMock()
        mock_folder.id = "new-folder-id"
        mock_folder.name = "New Project"
        mock_folder_class.return_value = mock_folder

        result = await tool.execute(
            action="create",
            folder_name="New Project",
            parent_folder="inbox",
            folder_class="IPF.Note"
        )

    assert result["success"] is True
    assert "created successfully" in result["message"]
    assert result["folder_name"] == "New Project"
    assert result["parent_folder"] == "inbox"
    assert result["folder_id"] == "new-folder-id"
    mock_folder.save.assert_called_once()


@pytest.mark.asyncio
async def test_create_folder_in_root(mock_ews_client):
    """Test creating folder in root."""
    tool = ManageFolderTool(mock_ews_client)

    # Mock root folder
    mock_root = MagicMock()
    mock_ews_client.account.root = mock_root

    with patch('src.tools.folder_tools.Folder') as mock_folder_class:
        mock_folder = MagicMock()
        mock_folder.id = "folder-id"
        mock_folder.name = "Archive"
        mock_folder_class.return_value = mock_folder

        result = await tool.execute(
            action="create",
            folder_name="Archive",
            parent_folder="root"
        )

    assert result["success"] is True
    assert result["folder_name"] == "Archive"
    assert result["parent_folder"] == "root"


@pytest.mark.asyncio
async def test_create_folder_invalid_parent(mock_ews_client):
    """Test creating folder with invalid parent."""
    tool = ManageFolderTool(mock_ews_client)

    with pytest.raises(ToolExecutionError) as exc_info:
        await tool.execute(
            action="create",
            folder_name="Test",
            parent_folder="nonexistent"
        )

    assert "Unknown parent folder" in str(exc_info.value)


@pytest.mark.asyncio
async def test_delete_folder_soft(mock_ews_client):
    """Test soft deleting a folder."""
    tool = ManageFolderTool(mock_ews_client)

    # Mock folder
    mock_folder = MagicMock()
    mock_folder.id = "folder-to-delete"
    mock_folder.name = "Old Project"

    with patch.object(tool, '_find_folder_by_id', return_value=mock_folder):
        result = await tool.execute(
            action="delete",
            folder_id="folder-to-delete",
            permanent=False
        )

    assert result["success"] is True
    assert "deleted successfully" in result["message"]
    assert result["folder_id"] == "folder-to-delete"
    assert result["permanent"] is False
    mock_folder.soft_delete.assert_called_once()


@pytest.mark.asyncio
async def test_delete_folder_permanent(mock_ews_client):
    """Test permanently deleting a folder."""
    tool = ManageFolderTool(mock_ews_client)

    # Mock folder
    mock_folder = MagicMock()
    mock_folder.id = "folder-to-delete"
    mock_folder.name = "Temp Folder"

    with patch.object(tool, '_find_folder_by_id', return_value=mock_folder):
        result = await tool.execute(
            action="delete",
            folder_id="folder-to-delete",
            permanent=True
        )

    assert result["success"] is True
    assert result["permanent"] is True
    mock_folder.delete.assert_called_once()


@pytest.mark.asyncio
async def test_delete_folder_not_found(mock_ews_client):
    """Test deleting non-existent folder."""
    tool = ManageFolderTool(mock_ews_client)

    with patch.object(tool, '_find_folder_by_id', return_value=None):
        with pytest.raises(ToolExecutionError) as exc_info:
            await tool.execute(action="delete", folder_id="nonexistent-id")

        assert "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_rename_folder(mock_ews_client):
    """Test renaming a folder."""
    tool = ManageFolderTool(mock_ews_client)

    # Mock folder
    mock_folder = MagicMock()
    mock_folder.id = "folder-id"
    mock_folder.name = "Old Name"

    with patch.object(tool, '_find_folder_by_id', return_value=mock_folder):
        result = await tool.execute(
            action="rename",
            folder_id="folder-id",
            new_name="New Name"
        )

    assert result["success"] is True
    assert "renamed successfully" in result["message"]
    assert result["folder_id"] == "folder-id"
    assert result["old_name"] == "Old Name"
    assert result["new_name"] == "New Name"
    assert mock_folder.name == "New Name"
    mock_folder.save.assert_called_once()


@pytest.mark.asyncio
async def test_rename_folder_not_found(mock_ews_client):
    """Test renaming non-existent folder."""
    tool = ManageFolderTool(mock_ews_client)

    with patch.object(tool, '_find_folder_by_id', return_value=None):
        with pytest.raises(ToolExecutionError) as exc_info:
            await tool.execute(
                action="rename",
                folder_id="nonexistent-id",
                new_name="New Name"
            )

        assert "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_move_folder(mock_ews_client):
    """Test moving a folder."""
    tool = ManageFolderTool(mock_ews_client)

    # Mock folder to move
    mock_folder = MagicMock()
    mock_folder.id = "folder-to-move"
    mock_folder.name = "Project A"

    # Mock destination folder
    mock_dest = MagicMock()
    mock_dest.id = "dest-folder-id"
    mock_dest.name = "Archive"

    with patch.object(tool, '_find_folder_by_id') as mock_find:
        # First call returns folder to move, second call returns destination
        mock_find.side_effect = [mock_folder, mock_dest]

        result = await tool.execute(
            action="move",
            folder_id="folder-to-move",
            destination="dest-folder-id"
        )

    assert result["success"] is True
    assert "moved successfully" in result["message"]
    assert result["folder_id"] == "folder-to-move"
    assert result["folder_name"] == "Project A"
    mock_folder.move.assert_called_once_with(to_folder=mock_dest)


@pytest.mark.asyncio
async def test_move_folder_source_not_found(mock_ews_client):
    """Test moving non-existent folder."""
    tool = ManageFolderTool(mock_ews_client)

    with patch.object(tool, '_find_folder_by_id', return_value=None):
        with pytest.raises(ToolExecutionError) as exc_info:
            await tool.execute(
                action="move",
                folder_id="nonexistent-id",
                destination="dest-id"
            )

        assert "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_move_folder_destination_not_found(mock_ews_client):
    """Test moving folder to non-existent destination."""
    tool = ManageFolderTool(mock_ews_client)

    # Mock source folder
    mock_folder = MagicMock()
    mock_folder.id = "folder-to-move"

    with patch.object(tool, '_find_folder_by_id') as mock_find:
        # First call returns folder to move, second call returns None (destination not found)
        mock_find.side_effect = [mock_folder, None]

        with pytest.raises(ToolExecutionError) as exc_info:
            await tool.execute(
                action="move",
                folder_id="folder-to-move",
                destination="nonexistent-dest"
            )

        assert "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_move_folder_to_parent(mock_ews_client):
    """Test moving folder using parent folder name."""
    tool = ManageFolderTool(mock_ews_client)

    # Mock folder to move
    mock_folder = MagicMock()
    mock_folder.id = "folder-to-move"
    mock_folder.name = "Reports"

    # Mock destination (inbox)
    mock_inbox = MagicMock()
    mock_ews_client.account.inbox = mock_inbox

    with patch.object(tool, '_find_folder_by_id', return_value=mock_folder):
        result = await tool.execute(
            action="move",
            folder_id="folder-to-move",
            destination="inbox"
        )

    assert result["success"] is True
    assert result["folder_name"] == "Reports"
    mock_folder.move.assert_called_once_with(to_folder=mock_inbox)
