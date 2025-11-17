"""Tests for folder management tools."""

import pytest
from unittest.mock import MagicMock

from src.tools.folder_tools import ListFoldersTool


@pytest.mark.asyncio
async def test_list_folders_tool(mock_ews_client):
    """Test listing mailbox folders."""
    tool = ListFoldersTool(mock_ews_client)

    # Mock folder hierarchy
    mock_child_folder = MagicMock()
    mock_child_folder.id = "child-1"
    mock_child_folder.name = "Subfolder"
    mock_child_folder.parent_folder_id = "root-1"
    mock_child_folder.folder_class = "IPF.Note"
    mock_child_folder.child_folder_count = 0
    mock_child_folder.total_count = 5
    mock_child_folder.unread_count = 2
    mock_child_folder.children = []

    mock_root_folder = MagicMock()
    mock_root_folder.id = "root-1"
    mock_root_folder.name = "Inbox"
    mock_root_folder.parent_folder_id = ""
    mock_root_folder.folder_class = "IPF.Note"
    mock_root_folder.child_folder_count = 1
    mock_root_folder.total_count = 10
    mock_root_folder.unread_count = 3
    mock_root_folder.children = [mock_child_folder]

    mock_ews_client.account.inbox = mock_root_folder

    result = await tool.execute(
        parent_folder="inbox",
        depth=2,
        include_hidden=False,
        include_counts=True
    )

    assert result["success"] is True
    assert "folder_tree" in result
    assert result["folder_tree"]["name"] == "Inbox"
    assert result["folder_tree"]["total_count"] == 10
    assert result["folder_tree"]["unread_count"] == 3
    assert len(result["folder_tree"]["children"]) == 1


@pytest.mark.asyncio
async def test_list_folders_from_root(mock_ews_client):
    """Test listing folders from root."""
    tool = ListFoldersTool(mock_ews_client)

    mock_root = MagicMock()
    mock_root.id = "root"
    mock_root.name = "Root"
    mock_root.parent_folder_id = ""
    mock_root.folder_class = "IPF"
    mock_root.child_folder_count = 3
    mock_root.total_count = 0
    mock_root.unread_count = 0
    mock_root.children = []

    mock_ews_client.account.root = mock_root

    result = await tool.execute(
        parent_folder="root",
        depth=1,
        include_counts=True
    )

    assert result["success"] is True
    assert result["folder_tree"]["name"] == "Root"


@pytest.mark.asyncio
async def test_list_folders_without_counts(mock_ews_client):
    """Test listing folders without item counts."""
    tool = ListFoldersTool(mock_ews_client)

    mock_folder = MagicMock()
    mock_folder.id = "folder-1"
    mock_folder.name = "Test Folder"
    mock_folder.parent_folder_id = ""
    mock_folder.folder_class = "IPF.Note"
    mock_folder.child_folder_count = 0
    mock_folder.children = []

    mock_ews_client.account.inbox = mock_folder

    result = await tool.execute(
        parent_folder="inbox",
        depth=1,
        include_counts=False
    )

    assert result["success"] is True
    assert "total_count" not in result["folder_tree"]
    assert "unread_count" not in result["folder_tree"]


@pytest.mark.asyncio
async def test_list_folders_max_depth(mock_ews_client):
    """Test listing folders with depth limit."""
    tool = ListFoldersTool(mock_ews_client)

    # Create nested folder structure
    mock_level3 = MagicMock()
    mock_level3.id = "level3"
    mock_level3.name = "Level 3"
    mock_level3.child_folder_count = 0
    mock_level3.children = []

    mock_level2 = MagicMock()
    mock_level2.id = "level2"
    mock_level2.name = "Level 2"
    mock_level2.child_folder_count = 1
    mock_level2.children = [mock_level3]

    mock_level1 = MagicMock()
    mock_level1.id = "level1"
    mock_level1.name = "Level 1"
    mock_level1.parent_folder_id = ""
    mock_level1.folder_class = "IPF.Note"
    mock_level1.child_folder_count = 1
    mock_level1.children = [mock_level2]

    mock_ews_client.account.inbox = mock_level1

    # Test with depth=2 (should stop at level 2)
    result = await tool.execute(
        parent_folder="inbox",
        depth=2,
        include_counts=False
    )

    assert result["success"] is True
    assert result["folder_tree"]["name"] == "Level 1"
    assert "children" in result["folder_tree"]
    # Should have level 2, but level 3 might not be fully expanded


@pytest.mark.asyncio
async def test_list_folders_invalid_depth(mock_ews_client):
    """Test listing folders with invalid depth."""
    tool = ListFoldersTool(mock_ews_client)

    with pytest.raises(Exception) as exc_info:
        await tool.execute(
            parent_folder="inbox",
            depth=15  # Exceeds maximum of 10
        )

    assert "depth must be between 1 and 10" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_list_folders_unknown_parent(mock_ews_client):
    """Test listing folders with unknown parent folder."""
    tool = ListFoldersTool(mock_ews_client)

    with pytest.raises(Exception) as exc_info:
        await tool.execute(
            parent_folder="nonexistent_folder",
            depth=1
        )

    assert "unknown parent folder" in str(exc_info.value).lower()
