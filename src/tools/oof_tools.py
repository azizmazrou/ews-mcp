"""Out-of-Office (OOF) automatic reply tools for EWS MCP Server."""

from typing import Any, Dict
from datetime import datetime

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, parse_datetime_tz_aware, format_datetime


class SetOOFSettingsTool(BaseTool):
    """Tool for configuring Out-of-Office automatic replies."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "set_oof_settings",
            "description": "Configure Out-of-Office automatic reply settings",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "description": "OOF state",
                        "enum": ["Enabled", "Scheduled", "Disabled"]
                    },
                    "internal_reply": {
                        "type": "string",
                        "description": "Auto-reply message for internal senders"
                    },
                    "external_reply": {
                        "type": "string",
                        "description": "Auto-reply message for external senders"
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start date/time (ISO 8601 format) - required for Scheduled state"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End date/time (ISO 8601 format) - required for Scheduled state"
                    },
                    "external_audience": {
                        "type": "string",
                        "description": "Who receives external reply",
                        "enum": ["None", "Known", "All"],
                        "default": "Known"
                    }
                },
                "required": ["state"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Configure OOF settings."""
        state = kwargs.get("state")
        internal_reply = kwargs.get("internal_reply", "I am currently out of the office.")
        external_reply = kwargs.get("external_reply", "I am currently out of the office.")
        start_time_str = kwargs.get("start_time")
        end_time_str = kwargs.get("end_time")
        external_audience = kwargs.get("external_audience", "Known")

        if not state:
            raise ToolExecutionError("state is required")

        # Validate scheduled parameters
        if state == "Scheduled":
            if not start_time_str or not end_time_str:
                raise ToolExecutionError("start_time and end_time are required for Scheduled state")

        try:
            from exchangelib import OofSettings, MailboxData

            # Parse dates if provided
            start_time = None
            end_time = None
            if start_time_str:
                start_time = parse_datetime_tz_aware(start_time_str)
                if not start_time:
                    raise ToolExecutionError("Invalid start_time format. Use ISO 8601 format.")

            if end_time_str:
                end_time = parse_datetime_tz_aware(end_time_str)
                if not end_time:
                    raise ToolExecutionError("Invalid end_time format. Use ISO 8601 format.")

            # Validate time range
            if start_time and end_time and end_time <= start_time:
                raise ToolExecutionError("end_time must be after start_time")

            # Create OOF settings
            oof = OofSettings()
            oof.state = state
            oof.external_audience = external_audience

            # Set internal reply
            if internal_reply:
                from exchangelib import OofReply
                oof.internal_reply = OofReply(message=internal_reply, lang='en')

            # Set external reply
            if external_reply:
                from exchangelib import OofReply
                oof.external_reply = OofReply(message=external_reply, lang='en')

            # Set schedule if provided
            if start_time and end_time:
                oof.start = start_time
                oof.end = end_time

            # Apply settings
            self.ews_client.account.oof_settings = oof

            self.logger.info(f"OOF settings updated: state={state}")

            response_data = {
                "state": state,
                "internal_reply": internal_reply,
                "external_reply": external_reply,
                "external_audience": external_audience
            }

            if start_time:
                response_data["start_time"] = format_datetime(start_time)
            if end_time:
                response_data["end_time"] = format_datetime(end_time)

            return format_success_response(
                f"Out-of-Office settings updated to {state}",
                settings=response_data
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to set OOF settings: {e}")
            raise ToolExecutionError(f"Failed to set OOF settings: {e}")


class GetOOFSettingsTool(BaseTool):
    """Tool for retrieving current Out-of-Office settings."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_oof_settings",
            "description": "Get current Out-of-Office automatic reply settings",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Get current OOF settings."""
        try:
            # Get OOF settings
            oof = self.ews_client.account.oof_settings

            if not oof:
                return format_success_response(
                    "No OOF settings configured",
                    settings={
                        "state": "Disabled",
                        "internal_reply": "",
                        "external_reply": "",
                        "external_audience": "None"
                    }
                )

            # Extract settings
            settings = {
                "state": oof.state if hasattr(oof, 'state') else "Unknown",
                "external_audience": oof.external_audience if hasattr(oof, 'external_audience') else "Unknown"
            }

            # Get internal reply
            if hasattr(oof, 'internal_reply') and oof.internal_reply:
                if hasattr(oof.internal_reply, 'message'):
                    settings["internal_reply"] = oof.internal_reply.message
                else:
                    settings["internal_reply"] = str(oof.internal_reply)
            else:
                settings["internal_reply"] = ""

            # Get external reply
            if hasattr(oof, 'external_reply') and oof.external_reply:
                if hasattr(oof.external_reply, 'message'):
                    settings["external_reply"] = oof.external_reply.message
                else:
                    settings["external_reply"] = str(oof.external_reply)
            else:
                settings["external_reply"] = ""

            # Get schedule if available
            if hasattr(oof, 'start') and oof.start:
                settings["start_time"] = format_datetime(oof.start)
            if hasattr(oof, 'end') and oof.end:
                settings["end_time"] = format_datetime(oof.end)

            # Check if currently active
            if settings["state"] == "Scheduled" and "start_time" in settings and "end_time" in settings:
                now = datetime.now(oof.start.tzinfo) if hasattr(oof, 'start') and oof.start else datetime.now()
                if oof.start <= now <= oof.end:
                    settings["currently_active"] = True
                else:
                    settings["currently_active"] = False
            elif settings["state"] == "Enabled":
                settings["currently_active"] = True
            else:
                settings["currently_active"] = False

            self.logger.info(f"Retrieved OOF settings: state={settings['state']}")

            return format_success_response(
                f"Current OOF state: {settings['state']}",
                settings=settings
            )

        except Exception as e:
            self.logger.error(f"Failed to get OOF settings: {e}")
            raise ToolExecutionError(f"Failed to get OOF settings: {e}")
