"""Tests for calendar tools."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

from src.tools.calendar_tools import (
    CreateAppointmentTool,
    GetCalendarTool,
    UpdateAppointmentTool,
    DeleteAppointmentTool,
    RespondToMeetingTool,
    CheckAvailabilityTool
)


@pytest.mark.asyncio
async def test_create_appointment_tool(mock_ews_client, sample_appointment):
    """Test creating appointment."""
    tool = CreateAppointmentTool(mock_ews_client)

    with patch('src.tools.calendar_tools.CalendarItem') as mock_calendar:
        mock_item = MagicMock()
        mock_item.id = "appointment-id"
        mock_calendar.return_value = mock_item

        result = await tool.execute(**sample_appointment)

        assert result["success"] is True
        assert "created successfully" in result["message"].lower()
        mock_item.save.assert_called_once()


@pytest.mark.asyncio
async def test_get_calendar_tool(mock_ews_client):
    """Test getting calendar events."""
    tool = GetCalendarTool(mock_ews_client)

    # Mock calendar items
    mock_event = MagicMock()
    mock_event.id = "event-1"
    mock_event.subject = "Team Meeting"
    mock_event.start = datetime(2025, 1, 15, 10, 0)
    mock_event.end = datetime(2025, 1, 15, 11, 0)
    mock_event.location = "Room A"
    mock_event.is_all_day = False

    mock_view = MagicMock()
    mock_view.order_by.return_value = [mock_event]
    mock_ews_client.account.calendar.view.return_value = mock_view

    result = await tool.execute()

    assert result["success"] is True
    assert len(result["events"]) > 0


@pytest.mark.asyncio
async def test_update_appointment_tool(mock_ews_client):
    """Test updating appointment."""
    tool = UpdateAppointmentTool(mock_ews_client)

    # Mock appointment
    mock_appointment = MagicMock()
    mock_ews_client.account.calendar.get.return_value = mock_appointment

    result = await tool.execute(
        item_id="test-id",
        subject="Updated Meeting"
    )

    assert result["success"] is True
    assert mock_appointment.subject == "Updated Meeting"
    mock_appointment.save.assert_called_once()


@pytest.mark.asyncio
async def test_delete_appointment_tool(mock_ews_client):
    """Test deleting appointment."""
    tool = DeleteAppointmentTool(mock_ews_client)

    # Mock appointment
    mock_appointment = MagicMock()
    mock_appointment.required_attendees = []
    mock_ews_client.account.calendar.get.return_value = mock_appointment

    result = await tool.execute(item_id="test-id")

    assert result["success"] is True
    mock_appointment.delete.assert_called_once()


@pytest.mark.asyncio
async def test_respond_to_meeting_tool(mock_ews_client):
    """Test responding to meeting."""
    tool = RespondToMeetingTool(mock_ews_client)

    # Mock meeting
    mock_meeting = MagicMock()
    mock_ews_client.account.calendar.get.return_value = mock_meeting

    result = await tool.execute(
        item_id="test-id",
        response="Accept",
        message="I'll be there"
    )

    assert result["success"] is True
    mock_meeting.accept.assert_called_once()



@pytest.mark.asyncio
async def test_check_availability_tool(mock_ews_client):
    """Test checking availability for users."""
    tool = CheckAvailabilityTool(mock_ews_client)

    # Mock availability data
    mock_availability = MagicMock()
    mock_availability.free_busy_view_type = "Detailed"
    mock_availability.merged_free_busy = "00002222000"
    mock_availability.calendar_event_array = []

    mock_ews_client.account.protocol.get_free_busy_info.return_value = [mock_availability]

    result = await tool.execute(
        email_addresses=["user1@example.com"],
        start_time="2025-01-15T09:00:00+00:00",
        end_time="2025-01-15T17:00:00+00:00",
        interval_minutes=30
    )

    assert result["success"] is True
    assert len(result["availability"]) == 1
    assert result["availability"][0]["email"] == "user1@example.com"
    assert "merged_free_busy" in result["availability"][0]
    mock_ews_client.account.protocol.get_free_busy_info.assert_called_once()


@pytest.mark.asyncio
async def test_check_availability_invalid_time_range(mock_ews_client):
    """Test checking availability with invalid time range."""
    tool = CheckAvailabilityTool(mock_ews_client)

    with pytest.raises(Exception) as exc_info:
        await tool.execute(
            email_addresses=["user@example.com"],
            start_time="2025-01-15T17:00:00+00:00",
            end_time="2025-01-15T09:00:00+00:00",  # End before start
            interval_minutes=30
        )

    assert "after start_time" in str(exc_info.value).lower()

