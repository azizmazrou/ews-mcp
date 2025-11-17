"""Tests for Out-of-Office (OOF) tools."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime

from src.tools.oof_tools import (
    SetOOFSettingsTool,
    GetOOFSettingsTool
)
from src.exceptions import ToolExecutionError


@pytest.mark.asyncio
async def test_set_oof_enabled(mock_ews_client):
    """Test enabling OOF with messages."""
    tool = SetOOFSettingsTool(mock_ews_client)

    # Mock OOF settings
    mock_oof = MagicMock()
    mock_ews_client.account.oof_settings = mock_oof

    with patch('src.tools.oof_tools.OofSettings') as mock_oof_settings, \
         patch('src.tools.oof_tools.OofReply') as mock_oof_reply:

        mock_oof_instance = MagicMock()
        mock_oof_settings.return_value = mock_oof_instance

        result = await tool.execute(
            state="Enabled",
            internal_reply="I am out of office",
            external_reply="I am currently unavailable",
            external_audience="Known"
        )

    assert result["success"] is True
    assert "updated to Enabled" in result["message"]
    assert result["settings"]["state"] == "Enabled"
    assert result["settings"]["internal_reply"] == "I am out of office"
    assert result["settings"]["external_reply"] == "I am currently unavailable"
    assert result["settings"]["external_audience"] == "Known"


@pytest.mark.asyncio
async def test_set_oof_scheduled(mock_ews_client):
    """Test scheduling OOF with start and end times."""
    tool = SetOOFSettingsTool(mock_ews_client)

    # Mock OOF settings
    mock_oof = MagicMock()
    mock_ews_client.account.oof_settings = mock_oof

    with patch('src.tools.oof_tools.OofSettings') as mock_oof_settings, \
         patch('src.tools.oof_tools.OofReply') as mock_oof_reply:

        mock_oof_instance = MagicMock()
        mock_oof_settings.return_value = mock_oof_instance

        result = await tool.execute(
            state="Scheduled",
            internal_reply="I will be out",
            external_reply="I will be unavailable",
            start_time="2025-12-20T00:00:00",
            end_time="2025-12-31T23:59:59",
            external_audience="All"
        )

    assert result["success"] is True
    assert "updated to Scheduled" in result["message"]
    assert result["settings"]["state"] == "Scheduled"
    assert "start_time" in result["settings"]
    assert "end_time" in result["settings"]


@pytest.mark.asyncio
async def test_set_oof_disabled(mock_ews_client):
    """Test disabling OOF."""
    tool = SetOOFSettingsTool(mock_ews_client)

    # Mock OOF settings
    mock_oof = MagicMock()
    mock_ews_client.account.oof_settings = mock_oof

    with patch('src.tools.oof_tools.OofSettings') as mock_oof_settings:
        mock_oof_instance = MagicMock()
        mock_oof_settings.return_value = mock_oof_instance

        result = await tool.execute(state="Disabled")

    assert result["success"] is True
    assert "updated to Disabled" in result["message"]
    assert result["settings"]["state"] == "Disabled"


@pytest.mark.asyncio
async def test_set_oof_scheduled_missing_times(mock_ews_client):
    """Test scheduled OOF without required start/end times."""
    tool = SetOOFSettingsTool(mock_ews_client)

    with pytest.raises(ToolExecutionError) as exc_info:
        await tool.execute(
            state="Scheduled",
            internal_reply="I am out"
        )

    assert "start_time and end_time are required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_set_oof_invalid_time_range(mock_ews_client):
    """Test OOF with end time before start time."""
    tool = SetOOFSettingsTool(mock_ews_client)

    with patch('src.tools.oof_tools.OofSettings'):
        with pytest.raises(ToolExecutionError) as exc_info:
            await tool.execute(
                state="Scheduled",
                start_time="2025-12-31T23:59:59",
                end_time="2025-12-20T00:00:00"
            )

        assert "end_time must be after start_time" in str(exc_info.value)


@pytest.mark.asyncio
async def test_set_oof_missing_state(mock_ews_client):
    """Test OOF without required state parameter."""
    tool = SetOOFSettingsTool(mock_ews_client)

    with pytest.raises(ToolExecutionError) as exc_info:
        await tool.execute()

    assert "state is required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_oof_enabled(mock_ews_client):
    """Test getting OOF settings when enabled."""
    tool = GetOOFSettingsTool(mock_ews_client)

    # Mock OOF settings
    mock_oof = MagicMock()
    mock_oof.state = "Enabled"
    mock_oof.external_audience = "Known"

    mock_internal_reply = MagicMock()
    mock_internal_reply.message = "I am out of office"
    mock_oof.internal_reply = mock_internal_reply

    mock_external_reply = MagicMock()
    mock_external_reply.message = "I am unavailable"
    mock_oof.external_reply = mock_external_reply

    mock_ews_client.account.oof_settings = mock_oof

    result = await tool.execute()

    assert result["success"] is True
    assert result["settings"]["state"] == "Enabled"
    assert result["settings"]["internal_reply"] == "I am out of office"
    assert result["settings"]["external_reply"] == "I am unavailable"
    assert result["settings"]["external_audience"] == "Known"
    assert result["settings"]["currently_active"] is True


@pytest.mark.asyncio
async def test_get_oof_scheduled(mock_ews_client):
    """Test getting OOF settings when scheduled."""
    tool = GetOOFSettingsTool(mock_ews_client)

    # Mock OOF settings
    mock_oof = MagicMock()
    mock_oof.state = "Scheduled"
    mock_oof.external_audience = "All"

    # Set schedule in the future
    from exchangelib import EWSDateTime
    from datetime import timedelta
    start_time = EWSDateTime.now() + timedelta(days=1)
    end_time = start_time + timedelta(days=7)
    mock_oof.start = start_time
    mock_oof.end = end_time

    mock_internal_reply = MagicMock()
    mock_internal_reply.message = "I will be out"
    mock_oof.internal_reply = mock_internal_reply

    mock_external_reply = MagicMock()
    mock_external_reply.message = "I will be unavailable"
    mock_oof.external_reply = mock_external_reply

    mock_ews_client.account.oof_settings = mock_oof

    result = await tool.execute()

    assert result["success"] is True
    assert result["settings"]["state"] == "Scheduled"
    assert "start_time" in result["settings"]
    assert "end_time" in result["settings"]
    assert result["settings"]["currently_active"] is False  # Future schedule


@pytest.mark.asyncio
async def test_get_oof_disabled(mock_ews_client):
    """Test getting OOF settings when disabled."""
    tool = GetOOFSettingsTool(mock_ews_client)

    # Mock OOF settings
    mock_oof = MagicMock()
    mock_oof.state = "Disabled"
    mock_oof.external_audience = "None"
    mock_oof.internal_reply = None
    mock_oof.external_reply = None

    mock_ews_client.account.oof_settings = mock_oof

    result = await tool.execute()

    assert result["success"] is True
    assert result["settings"]["state"] == "Disabled"
    assert result["settings"]["internal_reply"] == ""
    assert result["settings"]["external_reply"] == ""
    assert result["settings"]["currently_active"] is False


@pytest.mark.asyncio
async def test_get_oof_no_settings(mock_ews_client):
    """Test getting OOF settings when none configured."""
    tool = GetOOFSettingsTool(mock_ews_client)

    # Mock no OOF settings
    mock_ews_client.account.oof_settings = None

    result = await tool.execute()

    assert result["success"] is True
    assert result["settings"]["state"] == "Disabled"
    assert "No OOF settings configured" in result["message"]
