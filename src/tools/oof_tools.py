"""Out-of-Office (OOF) tools for EWS MCP Server."""

from typing import Any, Dict
from datetime import datetime

from exchangelib import OofSettings

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, parse_datetime_tz_aware, format_datetime


class OofSettingsTool(BaseTool):
    """Unified OOF settings: get or set Out-of-Office configuration.

    Replaces: get_oof_settings, set_oof_settings.
    """

    @staticmethod
    def _reply_to_text(value: Any) -> str:
        if value is None:
            return ""
        return value.message if hasattr(value, "message") else str(value)

    @classmethod
    def _currently_active(cls, oof: Any) -> bool:
        state = getattr(oof, "state", None)
        if state == "Scheduled" and getattr(oof, "start", None) and getattr(oof, "end", None):
            now = datetime.now(oof.start.tzinfo) if getattr(oof, "start", None) else datetime.now()
            return oof.start <= now <= oof.end
        if state == "Enabled":
            return True
        return False

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "oof_settings",
            "description": "Get or set Out-of-Office automatic reply settings.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "set"],
                        "description": "Get current OOF settings or set new ones"
                    },
                    "state": {
                        "type": "string",
                        "enum": ["Enabled", "Scheduled", "Disabled"],
                        "description": "OOF state (required for set action)"
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
                        "description": "Start date/time (ISO 8601) - required for Scheduled state"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End date/time (ISO 8601) - required for Scheduled state"
                    },
                    "external_audience": {
                        "type": "string",
                        "enum": ["None", "Known", "All"],
                        "description": "Who receives external reply",
                        "default": "Known"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["action"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Route to get or set OOF settings."""
        action = kwargs.get("action")
        if action == "get":
            return await self._get_settings(**kwargs)
        elif action == "set":
            return await self._set_settings(**kwargs)
        else:
            raise ToolExecutionError("action must be 'get' or 'set'")

    async def _get_settings(self, **kwargs) -> Dict[str, Any]:
        """Get current OOF settings."""
        target_mailbox = kwargs.get("target_mailbox")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            oof = account.oof_settings

            if not oof:
                return format_success_response(
                    "No OOF settings configured",
                    settings={
                        "state": "Disabled",
                        "internal_reply": "",
                        "external_reply": "",
                        "external_audience": "None"
                    },
                    mailbox=mailbox
                )

            settings = {
                "state": oof.state if hasattr(oof, 'state') else "Unknown",
                "external_audience": oof.external_audience if hasattr(oof, 'external_audience') else "Unknown"
            }

            if hasattr(oof, 'internal_reply') and oof.internal_reply:
                settings["internal_reply"] = self._reply_to_text(oof.internal_reply)
            else:
                settings["internal_reply"] = ""

            if hasattr(oof, 'external_reply') and oof.external_reply:
                settings["external_reply"] = self._reply_to_text(oof.external_reply)
            else:
                settings["external_reply"] = ""

            if hasattr(oof, 'start') and oof.start:
                settings["start_time"] = format_datetime(oof.start)
            if hasattr(oof, 'end') and oof.end:
                settings["end_time"] = format_datetime(oof.end)

            settings["currently_active"] = self._currently_active(oof)

            return format_success_response(
                f"Current OOF state: {settings['state']}",
                settings=settings,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to get OOF settings: {e}")
            raise ToolExecutionError(f"Failed to get OOF settings: {e}")

    async def _set_settings(self, **kwargs) -> Dict[str, Any]:
        """Configure OOF settings."""
        state = kwargs.get("state")
        internal_reply = kwargs.get("internal_reply")
        external_reply = kwargs.get("external_reply")
        start_time_str = kwargs.get("start_time")
        end_time_str = kwargs.get("end_time")
        external_audience = kwargs.get("external_audience")
        target_mailbox = kwargs.get("target_mailbox")

        if not state:
            raise ToolExecutionError("state is required for set action")

        if state == "Scheduled" and (not start_time_str or not end_time_str):
            raise ToolExecutionError("start_time and end_time are required for Scheduled state")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)
            from exchangelib import OofSettings, UTC

            current_oof = account.oof_settings
            current_external_audience = getattr(current_oof, "external_audience", None) if current_oof else None
            start_time = parse_datetime_tz_aware(start_time_str) if start_time_str else None
            end_time = parse_datetime_tz_aware(end_time_str) if end_time_str else None

            if start_time and end_time and end_time <= start_time:
                raise ToolExecutionError("end_time must be after start_time")

            if external_audience is None:
                external_audience = current_external_audience or "Known"

            if state != "Disabled":
                current_internal = self._reply_to_text(getattr(current_oof, "internal_reply", None))
                current_external = self._reply_to_text(getattr(current_oof, "external_reply", None))
                internal_reply = internal_reply if internal_reply is not None else current_internal
                external_reply = external_reply if external_reply is not None else current_external
                if not internal_reply:
                    internal_reply = "I am currently out of the office."
                if not external_reply:
                    external_reply = "I am currently out of the office."
            else:
                internal_reply = internal_reply if internal_reply is not None else self._reply_to_text(getattr(current_oof, "internal_reply", None))
                external_reply = external_reply if external_reply is not None else self._reply_to_text(getattr(current_oof, "external_reply", None))

            oof = OofSettings()
            oof.state = state
            oof.external_audience = external_audience

            # OofSettings.internal_reply / external_reply are MessageField(value_cls=str) in
            # exchangelib >= 5.x — assign plain strings. The legacy OofReply wrapper class was
            # removed; importing it raises ImportError on every set call.
            if internal_reply:
                oof.internal_reply = internal_reply
            if external_reply:
                oof.external_reply = external_reply
            if start_time and end_time:
                # Exchange on-prem accepted scheduled OOF only when the
                # payload timestamps were serialized in UTC.
                oof.start = start_time.astimezone(UTC)
                oof.end = end_time.astimezone(UTC)

            account.oof_settings = oof

            response_data = {
                "state": state,
                "internal_reply": internal_reply,
                "external_reply": external_reply,
                "external_audience": external_audience,
                "currently_active": self._currently_active(oof),
            }
            if start_time:
                response_data["start_time"] = format_datetime(start_time)
            if end_time:
                response_data["end_time"] = format_datetime(end_time)

            return format_success_response(
                f"Out-of-Office settings updated to {state}",
                settings=response_data,
                mailbox=mailbox
            )

        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to set OOF settings: {e}")
            raise ToolExecutionError(f"Failed to set OOF settings: {e}")
