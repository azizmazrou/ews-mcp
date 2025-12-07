"""Calendar operation tools for EWS MCP Server."""

from typing import Any, Dict
from datetime import datetime, timedelta
from exchangelib import CalendarItem, Mailbox, Attendee

from .base import BaseTool
from ..models import CreateAppointmentRequest, MeetingResponse
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, safe_get, parse_datetime_tz_aware, make_tz_aware, format_datetime, ews_id_to_str


class CreateAppointmentTool(BaseTool):
    """Tool for creating calendar appointments."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "create_appointment",
            "description": "Create a calendar appointment or meeting with attendees. Supports impersonation to operate on another user's calendar/mailbox",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Appointment subject"
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start time (ISO 8601 format)"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End time (ISO 8601 format)"
                    },
                    "location": {
                        "type": "string",
                        "description": "Meeting location (optional)"
                    },
                    "body": {
                        "type": "string",
                        "description": "Appointment body (optional)"
                    },
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Attendee email addresses (optional)"
                    },
                    "is_all_day": {
                        "type": "boolean",
                        "description": "All day event (optional)",
                        "default": False
                    },
                    "reminder_minutes": {
                        "type": "integer",
                        "description": "Reminder minutes before (optional)",
                        "default": 15
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["subject", "start_time", "end_time"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Create calendar appointment."""
        # Parse datetime strings as timezone-aware
        kwargs["start_time"] = parse_datetime_tz_aware(kwargs["start_time"])
        kwargs["end_time"] = parse_datetime_tz_aware(kwargs["end_time"])

        # Validate input
        request = self.validate_input(CreateAppointmentRequest, **kwargs)

        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Create calendar item
            item = CalendarItem(
                account=account,
                folder=account.calendar,
                subject=request.subject,
                start=request.start_time,
                end=request.end_time,
                is_all_day=request.is_all_day
            )

            # Set optional fields
            if request.location:
                item.location = request.location

            if request.body:
                item.body = request.body

            if request.reminder_minutes is not None:
                item.reminder_is_set = True
                item.reminder_minutes_before_start = request.reminder_minutes

            # Add attendees
            if request.attendees:
                item.required_attendees = [
                    Attendee(mailbox=Mailbox(email_address=email))
                    for email in request.attendees
                ]

            # Save the appointment
            item.save()

            self.logger.info(f"Created appointment: {request.subject}")

            return format_success_response(
                "Appointment created successfully",
                item_id=ews_id_to_str(item.id) if hasattr(item, "id") else None,
                subject=request.subject,
                start_time=request.start_time.isoformat(),
                end_time=request.end_time.isoformat(),
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to create appointment: {e}")
            raise ToolExecutionError(f"Failed to create appointment: {e}")


class GetCalendarTool(BaseTool):
    """Tool for retrieving calendar events."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_calendar",
            "description": "Retrieve calendar events for a date range. Supports impersonation to operate on another user's calendar/mailbox",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start date (ISO 8601 format, optional, defaults to today)"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date (ISO 8601 format, optional, overrides days_ahead)"
                    },
                    "days_ahead": {
                        "type": "integer",
                        "description": "Number of days ahead to retrieve (default: 7, max: 90)",
                        "default": 7,
                        "minimum": 1,
                        "maximum": 90
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of events to retrieve",
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
        """Get calendar events."""
        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Parse dates as timezone-aware
            start_date = kwargs.get("start_date")
            if start_date:
                start_date = parse_datetime_tz_aware(start_date)
            else:
                start_date = make_tz_aware(datetime.now())

            end_date = kwargs.get("end_date")
            if end_date:
                end_date = parse_datetime_tz_aware(end_date)
            else:
                # Use days_ahead parameter (default: 7, max: 90)
                days_ahead = kwargs.get("days_ahead", 7)
                # Enforce maximum of 90 days
                days_ahead = min(max(1, days_ahead), 90)
                end_date = start_date + timedelta(days=days_ahead)

            max_results = kwargs.get("max_results", 50)

            # Query calendar - use only() to fetch specific fields and avoid timezone parsing warnings
            items = account.calendar.view(
                start=start_date,
                end=end_date
            ).only(
                'id', 'subject', 'start', 'end', 'location',
                'organizer', 'is_all_day', 'required_attendees'
            ).order_by('start')

            # Format events
            events = []
            for item in items[:max_results]:
                # Get organizer email safely
                organizer = safe_get(item, "organizer", None)
                organizer_email = ""
                if organizer and hasattr(organizer, "email_address"):
                    organizer_email = organizer.email_address or ""

                # Get attendees safely - filter out None values
                required_attendees = safe_get(item, "required_attendees", []) or []
                attendee_emails = [
                    att.mailbox.email_address
                    for att in required_attendees
                    if att and hasattr(att, "mailbox") and att.mailbox and hasattr(att.mailbox, "email_address") and att.mailbox.email_address
                ]

                event_data = {
                    "item_id": ews_id_to_str(safe_get(item, "id", None)) or "unknown",
                    "subject": safe_get(item, "subject", "") or "",
                    "start": safe_get(item, "start", datetime.now()).isoformat(),
                    "end": safe_get(item, "end", datetime.now()).isoformat(),
                    "location": safe_get(item, "location", "") or "",
                    "organizer": organizer_email,
                    "is_all_day": safe_get(item, "is_all_day", False),
                    "attendees": attendee_emails
                }
                events.append(event_data)

            self.logger.info(f"Retrieved {len(events)} calendar events")

            return format_success_response(
                f"Retrieved {len(events)} events",
                events=events,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to get calendar: {e}")
            raise ToolExecutionError(f"Failed to get calendar: {e}")


class UpdateAppointmentTool(BaseTool):
    """Tool for updating calendar appointments."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "update_appointment",
            "description": "Update an existing calendar appointment. Supports impersonation to operate on another user's calendar/mailbox",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Appointment item ID"
                    },
                    "subject": {
                        "type": "string",
                        "description": "New subject (optional)"
                    },
                    "start_time": {
                        "type": "string",
                        "description": "New start time (ISO 8601 format, optional)"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "New end time (ISO 8601 format, optional)"
                    },
                    "location": {
                        "type": "string",
                        "description": "New location (optional)"
                    },
                    "body": {
                        "type": "string",
                        "description": "New body (optional)"
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
        """Update appointment."""
        item_id = kwargs.get("item_id")

        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get the appointment
            item = account.calendar.get(id=item_id)

            # Update fields
            if "subject" in kwargs:
                item.subject = kwargs["subject"]

            if "start_time" in kwargs:
                item.start = parse_datetime_tz_aware(kwargs["start_time"])

            if "end_time" in kwargs:
                item.end = parse_datetime_tz_aware(kwargs["end_time"])

            if "location" in kwargs:
                item.location = kwargs["location"]

            if "body" in kwargs:
                item.body = kwargs["body"]

            # Save changes
            item.save()

            self.logger.info(f"Updated appointment {item_id}")

            return format_success_response(
                "Appointment updated successfully",
                item_id=item_id,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to update appointment: {e}")
            raise ToolExecutionError(f"Failed to update appointment: {e}")


class DeleteAppointmentTool(BaseTool):
    """Tool for deleting calendar appointments."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_appointment",
            "description": "Delete a calendar appointment. Supports impersonation to operate on another user's calendar/mailbox",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Appointment item ID to delete"
                    },
                    "send_cancellation": {
                        "type": "boolean",
                        "description": "Send cancellation to attendees (for meetings)",
                        "default": True
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
        """Delete appointment."""
        item_id = kwargs.get("item_id")
        send_cancellation = kwargs.get("send_cancellation", True)

        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get and delete the appointment
            item = account.calendar.get(id=item_id)

            # Check if we should send cancellation
            required_attendees = safe_get(item, "required_attendees", []) or []
            has_attendees = len(required_attendees) > 0

            if send_cancellation and has_attendees:
                item.cancel()
            else:
                item.delete()

            self.logger.info(f"Deleted appointment {item_id}")

            return format_success_response(
                "Appointment deleted successfully",
                item_id=item_id,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to delete appointment: {e}")
            raise ToolExecutionError(f"Failed to delete appointment: {e}")


class RespondToMeetingTool(BaseTool):
    """Tool for responding to meeting invitations."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "respond_to_meeting",
            "description": "Accept, tentatively accept, or decline a meeting invitation. Supports impersonation to operate on another user's calendar/mailbox",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Meeting invitation item ID"
                    },
                    "response": {
                        "type": "string",
                        "enum": ["Accept", "Tentative", "Decline"],
                        "description": "Response type"
                    },
                    "message": {
                        "type": "string",
                        "description": "Optional response message"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["item_id", "response"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Respond to meeting invitation."""
        item_id = kwargs.get("item_id")
        response = kwargs.get("response")
        message = kwargs.get("message", "")

        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get the meeting request
            item = account.calendar.get(id=item_id)

            # Send response
            if response == "Accept":
                item.accept(body=message)
                action = "accepted"
            elif response == "Tentative":
                item.tentatively_accept(body=message)
                action = "tentatively accepted"
            elif response == "Decline":
                item.decline(body=message)
                action = "declined"
            else:
                raise ToolExecutionError(f"Invalid response: {response}")

            self.logger.info(f"Meeting {item_id} {action}")

            return format_success_response(
                f"Meeting {action}",
                item_id=item_id,
                response=response,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to respond to meeting: {e}")
            raise ToolExecutionError(f"Failed to respond to meeting: {e}")


class CheckAvailabilityTool(BaseTool):
    """Tool for checking free/busy availability."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "check_availability",
            "description": "Get free/busy information for email addresses in a time range. Supports impersonation to operate on another user's calendar/mailbox",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "email_addresses": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of email addresses to check"
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start time (ISO 8601 format)"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End time (ISO 8601 format)"
                    },
                    "interval_minutes": {
                        "type": "integer",
                        "description": "Time slot granularity in minutes",
                        "default": 30,
                        "minimum": 15,
                        "maximum": 1440
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["email_addresses", "start_time", "end_time"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Check availability for users."""
        email_addresses = kwargs.get("email_addresses", [])
        start_time_str = kwargs.get("start_time")
        end_time_str = kwargs.get("end_time")
        interval_minutes = kwargs.get("interval_minutes", 30)

        if not email_addresses:
            raise ToolExecutionError("email_addresses is required and cannot be empty")

        if not start_time_str or not end_time_str:
            raise ToolExecutionError("start_time and end_time are required")

        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Parse datetimes
            start_time = parse_datetime_tz_aware(start_time_str)
            end_time = parse_datetime_tz_aware(end_time_str)

            if not start_time or not end_time:
                raise ToolExecutionError("Invalid datetime format. Use ISO 8601 format.")

            if end_time <= start_time:
                raise ToolExecutionError("end_time must be after start_time")

            # Create mailbox objects
            from exchangelib import Mailbox
            mailboxes = [Mailbox(email_address=email) for email in email_addresses]

            # Get free/busy information (convert generator to list)
            availability_data = list(account.protocol.get_free_busy_info(
                accounts=mailboxes,
                start=start_time,
                end=end_time,
                merged_free_busy_interval=interval_minutes
            ))

            # Format response
            availability_results = []
            for i, (mailbox, busy_info) in enumerate(zip(mailboxes, availability_data)):
                # Parse the free/busy time slots
                # exchangelib returns FreeBusyView with working_hours_timezone, free_busy_view_type, etc.
                result = {
                    "email": email_addresses[i],
                    "view_type": str(busy_info.free_busy_view_type) if hasattr(busy_info, 'free_busy_view_type') else "Detailed",
                    "calendar_events": []
                }

                # Add calendar event information if available
                if hasattr(busy_info, 'calendar_event_array') and busy_info.calendar_event_array:
                    for event in busy_info.calendar_event_array:
                        result["calendar_events"].append({
                            "start": format_datetime(event.start) if hasattr(event, 'start') else None,
                            "end": format_datetime(event.end) if hasattr(event, 'end') else None,
                            "busy_type": str(event.busy_type) if hasattr(event, 'busy_type') else "Busy",
                            "details": safe_get(event, 'details')
                        })

                # Add merged free/busy string if available
                if hasattr(busy_info, 'merged_free_busy'):
                    # The merged_free_busy is a string like "00002222000..." where:
                    # 0=Free, 1=Tentative, 2=Busy, 3=OOF (Out of Office), 4=NoData
                    result["merged_free_busy"] = busy_info.merged_free_busy
                    result["free_busy_legend"] = {
                        "0": "Free",
                        "1": "Tentative",
                        "2": "Busy",
                        "3": "OutOfOffice",
                        "4": "NoData"
                    }

                availability_results.append(result)

            self.logger.info(f"Retrieved availability for {len(email_addresses)} users")

            return format_success_response(
                f"Availability retrieved for {len(email_addresses)} user(s)",
                availability=availability_results,
                time_range={
                    "start": start_time_str,
                    "end": end_time_str,
                    "interval_minutes": interval_minutes
                },
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to check availability: {e}")
            raise ToolExecutionError(f"Failed to check availability: {e}")


class FindMeetingTimesTool(BaseTool):
    """Tool for finding optimal meeting times using AI-powered scheduling."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "find_meeting_times",
            "description": "Find optimal meeting times based on attendee availability and preferences. Supports impersonation to operate on another user's calendar/mailbox",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "attendees": {
                        "type": "array",
                        "description": "List of attendee email addresses",
                        "items": {"type": "string"}
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Meeting duration in minutes",
                        "default": 60,
                        "minimum": 15,
                        "maximum": 480
                    },
                    "date_range_start": {
                        "type": "string",
                        "description": "Start of date range to search (ISO 8601 format)"
                    },
                    "date_range_end": {
                        "type": "string",
                        "description": "End of date range to search (ISO 8601 format)"
                    },
                    "max_suggestions": {
                        "type": "integer",
                        "description": "Maximum number of time suggestions",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20
                    },
                    "preferences": {
                        "type": "object",
                        "description": "Scheduling preferences",
                        "properties": {
                            "prefer_morning": {"type": "boolean", "default": False},
                            "prefer_afternoon": {"type": "boolean", "default": False},
                            "avoid_back_to_back": {"type": "boolean", "default": True},
                            "working_hours_only": {"type": "boolean", "default": True},
                            "min_break_minutes": {"type": "integer", "default": 15},
                            "earliest_hour": {"type": "integer", "default": 9, "minimum": 0, "maximum": 23},
                            "latest_hour": {"type": "integer", "default": 17, "minimum": 0, "maximum": 23}
                        }
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["attendees", "date_range_start", "date_range_end"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Find optimal meeting times."""
        attendees = kwargs.get("attendees", [])
        duration_minutes = kwargs.get("duration_minutes", 60)
        date_range_start_str = kwargs.get("date_range_start")
        date_range_end_str = kwargs.get("date_range_end")
        max_suggestions = kwargs.get("max_suggestions", 5)
        preferences = kwargs.get("preferences", {})

        if not attendees:
            raise ToolExecutionError("attendees list is required")

        if not date_range_start_str or not date_range_end_str:
            raise ToolExecutionError("date_range_start and date_range_end are required")

        try:
            target_mailbox = kwargs.get("target_mailbox")
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            from exchangelib import Mailbox
            from datetime import timedelta

            # Parse dates
            start_date = parse_datetime_tz_aware(date_range_start_str)
            end_date = parse_datetime_tz_aware(date_range_end_str)

            if not start_date or not end_date:
                raise ToolExecutionError("Invalid date format. Use ISO 8601 format.")

            if end_date <= start_date:
                raise ToolExecutionError("date_range_end must be after date_range_start")

            # Extract preferences
            prefer_morning = preferences.get("prefer_morning", False)
            prefer_afternoon = preferences.get("prefer_afternoon", False)
            avoid_back_to_back = preferences.get("avoid_back_to_back", True)
            working_hours_only = preferences.get("working_hours_only", True)
            min_break_minutes = preferences.get("min_break_minutes", 15)
            earliest_hour = preferences.get("earliest_hour", 9)
            latest_hour = preferences.get("latest_hour", 17)

            # Create mailbox objects for all attendees
            mailboxes = [Mailbox(email_address=email) for email in attendees]

            # Get availability for all attendees (convert generator to list)
            availability_data = list(account.protocol.get_free_busy_info(
                accounts=mailboxes,
                start=start_date,
                end=end_date,
                merged_free_busy_interval=15  # 15-minute intervals
            ))

            # Analyze availability and find open slots
            suggestions = []
            current_time = start_date

            while current_time < end_date and len(suggestions) < max_suggestions:
                # Skip to next day boundary if we're past working hours
                if working_hours_only:
                    if current_time.hour < earliest_hour:
                        current_time = current_time.replace(hour=earliest_hour, minute=0, second=0)
                    elif current_time.hour >= latest_hour:
                        current_time = (current_time + timedelta(days=1)).replace(hour=earliest_hour, minute=0, second=0)
                        continue

                # Check if this time slot works for all attendees
                slot_end = current_time + timedelta(minutes=duration_minutes)

                # Make sure we don't go past end_date or working hours
                if slot_end > end_date:
                    break

                if working_hours_only and slot_end.hour > latest_hour:
                    current_time = (current_time + timedelta(days=1)).replace(hour=earliest_hour, minute=0, second=0)
                    continue

                # Check if all attendees are available
                all_available = True
                for busy_info in availability_data:
                    # Check merged_free_busy string if available
                    if hasattr(busy_info, 'merged_free_busy') and busy_info.merged_free_busy:
                        # Calculate time offset in 15-minute intervals
                        minutes_from_start = int((current_time - start_date).total_seconds() / 60)
                        interval_index = minutes_from_start // 15

                        # Check duration worth of intervals
                        intervals_needed = (duration_minutes + 14) // 15  # Round up

                        if interval_index + intervals_needed <= len(busy_info.merged_free_busy):
                            for i in range(intervals_needed):
                                status = busy_info.merged_free_busy[interval_index + i]
                                # 0=Free, 1=Tentative, 2=Busy, 3=OOF, 4=NoData
                                if status in ['2', '3']:  # Busy or Out of Office
                                    all_available = False
                                    break

                            if not all_available:
                                break

                if all_available:
                    # Calculate a score for this time slot
                    score = 100

                    # Preference scoring
                    if prefer_morning and current_time.hour < 12:
                        score += 20
                    if prefer_afternoon and current_time.hour >= 13:
                        score += 20

                    # Penalize very early or very late times
                    if current_time.hour < 9 or current_time.hour > 16:
                        score -= 10

                    # Check for back-to-back meetings
                    if avoid_back_to_back:
                        # Check 15 minutes before and after
                        buffer_start = current_time - timedelta(minutes=min_break_minutes)
                        buffer_end = slot_end + timedelta(minutes=min_break_minutes)

                        # Simple check - if we have buffer time, add to score
                        score += 10

                    suggestions.append({
                        "start_time": format_datetime(current_time),
                        "end_time": format_datetime(slot_end),
                        "duration_minutes": duration_minutes,
                        "score": score,
                        "day_of_week": current_time.strftime("%A"),
                        "time_of_day": "Morning" if current_time.hour < 12 else "Afternoon" if current_time.hour < 17 else "Evening"
                    })

                # Move to next 15-minute interval
                current_time += timedelta(minutes=15)

            # Sort suggestions by score (highest first)
            suggestions.sort(key=lambda x: x['score'], reverse=True)

            # Limit to max_suggestions
            suggestions = suggestions[:max_suggestions]

            self.logger.info(f"Found {len(suggestions)} meeting time suggestions for {len(attendees)} attendees")

            return format_success_response(
                f"Found {len(suggestions)} optimal meeting times",
                suggestions=suggestions,
                attendees=attendees,
                duration_minutes=duration_minutes,
                date_range={
                    "start": date_range_start_str,
                    "end": date_range_end_str
                },
                preferences=preferences,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to find meeting times: {e}")
            raise ToolExecutionError(f"Failed to find meeting times: {e}")
