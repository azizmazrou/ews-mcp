"""Out-of-Office (OOF) tools for EWS MCP Server."""

from typing import Any, Dict
from datetime import datetime, timezone

from exchangelib import OofSettings

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, parse_datetime_tz_aware, format_datetime


class OofSettingsTool(BaseTool):
    """Unified OOF settings: get or set Out-of-Office configuration.

    Replaces: get_oof_settings, set_oof_settings.
    """

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
                settings["internal_reply"] = oof.internal_reply.message if hasattr(oof.internal_reply, 'message') else str(oof.internal_reply)
            else:
                settings["internal_reply"] = ""

            if hasattr(oof, 'external_reply') and oof.external_reply:
                settings["external_reply"] = oof.external_reply.message if hasattr(oof.external_reply, 'message') else str(oof.external_reply)
            else:
                settings["external_reply"] = ""

            if hasattr(oof, 'start') and oof.start:
                settings["start_time"] = format_datetime(oof.start)
            if hasattr(oof, 'end') and oof.end:
                settings["end_time"] = format_datetime(oof.end)

            if settings["state"] == "Scheduled" and "start_time" in settings and "end_time" in settings:
                now = datetime.now(oof.start.tzinfo) if hasattr(oof, 'start') and oof.start else datetime.now()
                settings["currently_active"] = oof.start <= now <= oof.end
            elif settings["state"] == "Enabled":
                settings["currently_active"] = True
            else:
                settings["currently_active"] = False

            return format_success_response(
                f"Current OOF state: {settings['state']}",
                settings=settings,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to get OOF settings: {e}")
            raise ToolExecutionError(f"Failed to get OOF settings: {e}")

    async def _set_settings(self, **kwargs) -> Dict[str, Any]:
        """Configure OOF settings.

        Issue #121 — three real problems on the previous implementation:

        1. ``state="Scheduled"`` failed against some Exchange backends with
           ``InvalidScheduledOofDuration`` because ``start``/``end`` were
           sent in the caller's local timezone. Normalise to UTC before
           assignment so EWS sees a SOAP-compliant duration.
        2. Toggling state to Disabled/Enabled without re-supplying the
           reply texts overwrote the existing OOF replies with the default
           "I am currently out of the office." string. Now: when the
           caller omits ``internal_reply`` / ``external_reply`` we read the
           current OofSettings and preserve them instead.
        3. ``currently_active`` was returned by the get path but never by
           the set path, so callers couldn't tell whether a Scheduled
           window had already started without a separate get call.
        """
        state = kwargs.get("state")
        # Sentinel default: distinguish "caller omitted" from "caller passed
        # an empty string" so we can preserve the existing reply text only
        # in the omitted case.
        _OMIT = object()
        internal_reply_in = kwargs.get("internal_reply", _OMIT)
        external_reply_in = kwargs.get("external_reply", _OMIT)
        external_audience_in = kwargs.get("external_audience", _OMIT)
        start_time_str = kwargs.get("start_time")
        end_time_str = kwargs.get("end_time")
        target_mailbox = kwargs.get("target_mailbox")

        if not state:
            raise ToolExecutionError("state is required for set action")

        if state == "Scheduled" and (not start_time_str or not end_time_str):
            raise ToolExecutionError("start_time and end_time are required for Scheduled state")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Read existing settings up front so we can preserve any field
            # the caller omitted (#121).
            existing = account.oof_settings
            existing_internal = (
                getattr(existing, "internal_reply", None) or ""
            )
            existing_external = (
                getattr(existing, "external_reply", None) or ""
            )
            existing_audience = (
                getattr(existing, "external_audience", None) or "Known"
            )

            internal_reply = (
                existing_internal if internal_reply_in is _OMIT else internal_reply_in
            )
            external_reply = (
                existing_external if external_reply_in is _OMIT else external_reply_in
            )
            external_audience = (
                existing_audience if external_audience_in is _OMIT else external_audience_in
            )

            start_time = parse_datetime_tz_aware(start_time_str) if start_time_str else None
            end_time = parse_datetime_tz_aware(end_time_str) if end_time_str else None

            if start_time and end_time and end_time <= start_time:
                raise ToolExecutionError("end_time must be after start_time")

            # #121 — normalise to UTC so EWS gets an unambiguous SOAP
            # duration. parse_datetime_tz_aware already attaches a tz; here
            # we convert to UTC explicitly. PBS Exchange in particular
            # rejects local-tz Scheduled windows with InvalidScheduledOofDuration.
            if start_time is not None and start_time.tzinfo is not None:
                start_time = start_time.astimezone(timezone.utc)
            if end_time is not None and end_time.tzinfo is not None:
                end_time = end_time.astimezone(timezone.utc)

            oof = OofSettings()
            oof.state = state
            oof.external_audience = external_audience
            if internal_reply:
                oof.internal_reply = internal_reply
            if external_reply:
                oof.external_reply = external_reply
            if start_time and end_time:
                oof.start = start_time
                oof.end = end_time

            account.oof_settings = oof

            # #121 — compute currently_active for parity with get path.
            now = datetime.now(timezone.utc)
            if state == "Enabled":
                currently_active = True
            elif state == "Disabled":
                currently_active = False
            elif state == "Scheduled" and start_time and end_time:
                currently_active = start_time <= now <= end_time
            else:
                currently_active = False

            response_data = {
                "state": state,
                "internal_reply": internal_reply,
                "external_reply": external_reply,
                "external_audience": external_audience,
                "currently_active": currently_active,
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
