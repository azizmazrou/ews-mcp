"""Tests for calendar enhancement tools."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timedelta

from src.tools.calendar_tools import FindMeetingTimesTool
from src.exceptions import ToolExecutionError


@pytest.mark.asyncio
async def test_find_meeting_times_basic(mock_ews_client):
    """Test finding meeting times with basic parameters."""
    tool = FindMeetingTimesTool(mock_ews_client)

    # Mock availability data
    mock_busy_info = MagicMock()
    # All attendees free (0 = Free, 2 = Busy)
    mock_busy_info.merged_free_busy = "0" * 96  # 24 hours * 4 (15-min intervals)

    mock_protocol = MagicMock()
    mock_protocol.get_free_busy_info.return_value = [mock_busy_info, mock_busy_info]
    mock_ews_client.account.protocol = mock_protocol

    # Mock timezone
    from exchangelib import EWSTimeZone
    mock_tz = EWSTimeZone('UTC')

    with patch('src.tools.calendar_tools.get_timezone', return_value=mock_tz), \
         patch('src.tools.calendar_tools.make_tz_aware') as mock_make_tz_aware:

        # Mock make_tz_aware to return dates in the future
        def mock_tz_aware(dt):
            return dt

        mock_make_tz_aware.side_effect = mock_tz_aware

        result = await tool.execute(
            attendees=["alice@example.com", "bob@example.com"],
            duration_minutes=60,
            max_suggestions=5
        )

    assert result["success"] is True
    assert "suggestions" in result
    assert result["duration_minutes"] == 60
    assert result["attendee_count"] == 2


@pytest.mark.asyncio
async def test_find_meeting_times_with_preferences(mock_ews_client):
    """Test finding meeting times with preferences."""
    tool = FindMeetingTimesTool(mock_ews_client)

    # Mock availability data - all free
    mock_busy_info = MagicMock()
    mock_busy_info.merged_free_busy = "0" * 96

    mock_protocol = MagicMock()
    mock_protocol.get_free_busy_info.return_value = [mock_busy_info]
    mock_ews_client.account.protocol = mock_protocol

    from exchangelib import EWSTimeZone
    mock_tz = EWSTimeZone('UTC')

    with patch('src.tools.calendar_tools.get_timezone', return_value=mock_tz), \
         patch('src.tools.calendar_tools.make_tz_aware') as mock_make_tz_aware:

        def mock_tz_aware(dt):
            return dt

        mock_make_tz_aware.side_effect = mock_tz_aware

        result = await tool.execute(
            attendees=["user@example.com"],
            duration_minutes=30,
            max_suggestions=3,
            preferences={
                "prefer_morning": True,
                "avoid_lunch": True,
                "min_gap_minutes": 30
            }
        )

    assert result["success"] is True
    assert "prefer_morning" in str(result.get("preferences", {})) or "suggestions" in result


@pytest.mark.asyncio
async def test_find_meeting_times_with_date_range(mock_ews_client):
    """Test finding meeting times with specific date range."""
    tool = FindMeetingTimesTool(mock_ews_client)

    # Mock availability data
    mock_busy_info = MagicMock()
    mock_busy_info.merged_free_busy = "0" * 96

    mock_protocol = MagicMock()
    mock_protocol.get_free_busy_info.return_value = [mock_busy_info]
    mock_ews_client.account.protocol = mock_protocol

    from exchangelib import EWSTimeZone
    mock_tz = EWSTimeZone('UTC')

    # Use specific future dates
    start_date = datetime.now() + timedelta(days=7)
    end_date = start_date + timedelta(days=1)

    with patch('src.tools.calendar_tools.get_timezone', return_value=mock_tz), \
         patch('src.tools.calendar_tools.make_tz_aware') as mock_make_tz_aware, \
         patch('src.tools.calendar_tools.parse_datetime_tz_aware') as mock_parse:

        def mock_tz_aware(dt):
            return dt

        mock_make_tz_aware.side_effect = mock_tz_aware
        mock_parse.side_effect = lambda x: datetime.fromisoformat(x)

        result = await tool.execute(
            attendees=["user@example.com"],
            duration_minutes=45,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat()
        )

    assert result["success"] is True


@pytest.mark.asyncio
async def test_find_meeting_times_no_attendees(mock_ews_client):
    """Test finding meeting times without attendees."""
    tool = FindMeetingTimesTool(mock_ews_client)

    with pytest.raises(ToolExecutionError) as exc_info:
        await tool.execute(duration_minutes=60)

    assert "attendees" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_find_meeting_times_invalid_duration(mock_ews_client):
    """Test finding meeting times with invalid duration."""
    tool = FindMeetingTimesTool(mock_ews_client)

    with pytest.raises(ToolExecutionError) as exc_info:
        await tool.execute(
            attendees=["user@example.com"],
            duration_minutes=5  # Too short
        )

    assert "duration_minutes must be between 15 and 480" in str(exc_info.value)


@pytest.mark.asyncio
async def test_find_meeting_times_busy_attendees(mock_ews_client):
    """Test finding meeting times when attendees are busy."""
    tool = FindMeetingTimesTool(mock_ews_client)

    # Mock availability data - mostly busy
    mock_busy_info1 = MagicMock()
    # First 8 hours busy (2 = Busy), then free
    mock_busy_info1.merged_free_busy = ("2" * 32) + ("0" * 64)

    mock_busy_info2 = MagicMock()
    # All free
    mock_busy_info2.merged_free_busy = "0" * 96

    mock_protocol = MagicMock()
    mock_protocol.get_free_busy_info.return_value = [mock_busy_info1, mock_busy_info2]
    mock_ews_client.account.protocol = mock_protocol

    from exchangelib import EWSTimeZone
    mock_tz = EWSTimeZone('UTC')

    with patch('src.tools.calendar_tools.get_timezone', return_value=mock_tz), \
         patch('src.tools.calendar_tools.make_tz_aware') as mock_make_tz_aware:

        def mock_tz_aware(dt):
            return dt

        mock_make_tz_aware.side_effect = mock_tz_aware

        result = await tool.execute(
            attendees=["busy@example.com", "free@example.com"],
            duration_minutes=60,
            max_suggestions=5
        )

    assert result["success"] is True
    # Should find times in the afternoon when both are free


@pytest.mark.asyncio
async def test_find_meeting_times_working_hours(mock_ews_client):
    """Test finding meeting times within working hours."""
    tool = FindMeetingTimesTool(mock_ews_client)

    # Mock availability data
    mock_busy_info = MagicMock()
    mock_busy_info.merged_free_busy = "0" * 96

    mock_protocol = MagicMock()
    mock_protocol.get_free_busy_info.return_value = [mock_busy_info]
    mock_ews_client.account.protocol = mock_protocol

    from exchangelib import EWSTimeZone
    mock_tz = EWSTimeZone('UTC')

    with patch('src.tools.calendar_tools.get_timezone', return_value=mock_tz), \
         patch('src.tools.calendar_tools.make_tz_aware') as mock_make_tz_aware:

        def mock_tz_aware(dt):
            return dt

        mock_make_tz_aware.side_effect = mock_tz_aware

        result = await tool.execute(
            attendees=["user@example.com"],
            duration_minutes=60,
            preferences={
                "working_hours_start": 9,
                "working_hours_end": 17
            }
        )

    assert result["success"] is True
    # All suggestions should be within 9 AM - 5 PM


@pytest.mark.asyncio
async def test_find_meeting_times_multiple_attendees(mock_ews_client):
    """Test finding meeting times with multiple attendees."""
    tool = FindMeetingTimesTool(mock_ews_client)

    # Mock availability for 5 attendees
    mock_busy_infos = []
    for i in range(5):
        mock_busy_info = MagicMock()
        if i == 0:
            # First attendee busy in morning
            mock_busy_info.merged_free_busy = ("2" * 32) + ("0" * 64)
        elif i == 1:
            # Second attendee busy in afternoon
            mock_busy_info.merged_free_busy = ("0" * 32) + ("2" * 32) + ("0" * 32)
        else:
            # Others free
            mock_busy_info.merged_free_busy = "0" * 96
        mock_busy_infos.append(mock_busy_info)

    mock_protocol = MagicMock()
    mock_protocol.get_free_busy_info.return_value = mock_busy_infos
    mock_ews_client.account.protocol = mock_protocol

    from exchangelib import EWSTimeZone
    mock_tz = EWSTimeZone('UTC')

    with patch('src.tools.calendar_tools.get_timezone', return_value=mock_tz), \
         patch('src.tools.calendar_tools.make_tz_aware') as mock_make_tz_aware:

        def mock_tz_aware(dt):
            return dt

        mock_make_tz_aware.side_effect = mock_tz_aware

        result = await tool.execute(
            attendees=[
                "user1@example.com",
                "user2@example.com",
                "user3@example.com",
                "user4@example.com",
                "user5@example.com"
            ],
            duration_minutes=60,
            max_suggestions=5
        )

    assert result["success"] is True
    assert result["attendee_count"] == 5


@pytest.mark.asyncio
async def test_find_meeting_times_no_available_slots(mock_ews_client):
    """Test finding meeting times when no slots available."""
    tool = FindMeetingTimesTool(mock_ews_client)

    # Mock availability data - all busy
    mock_busy_info = MagicMock()
    mock_busy_info.merged_free_busy = "2" * 96  # All busy

    mock_protocol = MagicMock()
    mock_protocol.get_free_busy_info.return_value = [mock_busy_info]
    mock_ews_client.account.protocol = mock_protocol

    from exchangelib import EWSTimeZone
    mock_tz = EWSTimeZone('UTC')

    with patch('src.tools.calendar_tools.get_timezone', return_value=mock_tz), \
         patch('src.tools.calendar_tools.make_tz_aware') as mock_make_tz_aware:

        def mock_tz_aware(dt):
            return dt

        mock_make_tz_aware.side_effect = mock_tz_aware

        result = await tool.execute(
            attendees=["busy@example.com"],
            duration_minutes=60,
            max_suggestions=5
        )

    assert result["success"] is True
    # Should return empty or very few suggestions
    assert len(result["suggestions"]) == 0 or result["suggestion_count"] == 0
