"""Daily / weekly briefing: the morning one-pager.

``generate_briefing`` is a *compound* tool — it composes data from several
primitives and returns a single structured JSON the LLM renders for the
user. Composition happens here (not in the LLM) so the output is
deterministic and audit-friendly.

Scope
-----
A briefing includes (each toggled independently):

* **inbox_delta**: unread messages since ``since`` (ISO) or last N hours
* **meetings**: calendar view for the briefing window
* **commitments**: open commitments due inside the window + overdue
* **overdue_tasks**: Exchange tasks with due_date < now and status != Completed
* **vip_activity**: emails in the window from senders present in
  ``analyze_contacts(analysis_type='vip')`` results

Security
--------
* Read-only. Never mutates mailboxes, commitments, or memory.
* Body/content fields are truncated before return (no full payloads).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..memory import CommitmentRepo
from ..utils import (
    format_success_response,
    make_tz_aware,
    parse_datetime_tz_aware,
    safe_get,
    truncate_text,
)


_DEFAULT_INCLUDE = ["inbox_delta", "meetings", "commitments", "overdue_tasks", "vip_activity"]


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return None


class GenerateBriefingTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "generate_briefing",
            "description": (
                "Produce a structured briefing covering recent inbox, upcoming "
                "meetings, open commitments, overdue tasks, and VIP activity. "
                "Deterministic composition — an LLM can render it to prose."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["today", "since_last_check", "weekly"],
                        "default": "today",
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO 8601 datetime override (takes precedence over 'scope').",
                    },
                    "include": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": _DEFAULT_INCLUDE,
                        },
                        "description": f"Sections to include. Default: {_DEFAULT_INCLUDE}",
                    },
                    "max_per_section": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    },
                    "target_mailbox": {"type": "string"},
                },
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        scope = kwargs.get("scope", "today")
        since_override = kwargs.get("since")
        include = kwargs.get("include") or _DEFAULT_INCLUDE
        max_per = int(kwargs.get("max_per_section", 20))
        target_mailbox = kwargs.get("target_mailbox")

        # Validate include list.
        for section in include:
            if section not in _DEFAULT_INCLUDE:
                raise ToolExecutionError(
                    f"unknown briefing section: {section!r} (expected one of {_DEFAULT_INCLUDE})"
                )

        account = self.get_account(target_mailbox)
        mailbox = self.get_mailbox_info(target_mailbox)

        # Compute the window.
        now = make_tz_aware(datetime.now())
        if since_override:
            since = parse_datetime_tz_aware(since_override)
            if since is None:
                raise ToolExecutionError(f"invalid 'since' datetime: {since_override!r}")
        elif scope == "today":
            since = make_tz_aware(datetime.combine(now.date(), datetime.min.time()))
        elif scope == "since_last_check":
            since = now - timedelta(hours=12)
        elif scope == "weekly":
            since = now - timedelta(days=7)
        else:
            raise ToolExecutionError(f"unknown scope: {scope!r}")

        window_end = now if scope != "weekly" else now + timedelta(days=1)
        meetings_end = (
            make_tz_aware(datetime.combine(now.date() + timedelta(days=1), datetime.min.time()))
            if scope == "today"
            else now + timedelta(days=7)
        )

        briefing: Dict[str, Any] = {
            "scope": scope,
            "since": _iso(since),
            "now": _iso(now),
            "mailbox": mailbox,
        }

        # --- Inbox delta -------------------------------------------------
        if "inbox_delta" in include:
            briefing["inbox_delta"] = self._collect_inbox_delta(account, since, max_per)

        # --- Meetings ----------------------------------------------------
        if "meetings" in include:
            briefing["meetings"] = self._collect_meetings(account, now, meetings_end, max_per)

        # --- Commitments -------------------------------------------------
        if "commitments" in include:
            briefing["commitments"] = self._collect_commitments(window_end, max_per)

        # --- Overdue tasks ----------------------------------------------
        if "overdue_tasks" in include:
            briefing["overdue_tasks"] = self._collect_overdue_tasks(account, now, max_per)

        # --- VIP activity ------------------------------------------------
        if "vip_activity" in include:
            briefing["vip_activity"] = self._collect_vip_activity(account, since, max_per)

        return format_success_response(
            "Briefing generated",
            briefing=briefing,
        )

    # --- Collection helpers -----------------------------------------------

    def _collect_inbox_delta(self, account, since, limit: int) -> List[Dict[str, Any]]:
        try:
            query = account.inbox.filter(datetime_received__gte=since).order_by("-datetime_received")
            items = list(query[:limit])
        except Exception as exc:
            self.logger.warning(f"briefing: inbox_delta query failed: {exc}")
            return []
        return [
            {
                "message_id": self._str_id(safe_get(msg, "id", None)),
                "subject": safe_get(msg, "subject", ""),
                "from": safe_get(safe_get(msg, "sender", None), "email_address", ""),
                "received_at": _iso(safe_get(msg, "datetime_received", None)),
                "is_read": bool(safe_get(msg, "is_read", False)),
                "has_attachments": bool(safe_get(msg, "has_attachments", False)),
                "importance": safe_get(msg, "importance", "Normal"),
                "preview": truncate_text(
                    str(safe_get(msg, "text_body", "") or ""), max_length=200
                ),
            }
            for msg in items
        ]

    def _collect_meetings(self, account, start, end, limit: int) -> List[Dict[str, Any]]:
        try:
            view = account.calendar.view(start=start, end=end)
            items = list(view[:limit])
        except Exception as exc:
            self.logger.warning(f"briefing: calendar view failed: {exc}")
            return []
        out: List[Dict[str, Any]] = []
        for item in items:
            attendees: List[str] = []
            try:
                for holder in [safe_get(item, "required_attendees", []) or [],
                               safe_get(item, "optional_attendees", []) or []]:
                    for att in holder:
                        mbx = safe_get(att, "mailbox", None)
                        email = safe_get(mbx, "email_address", None) if mbx else None
                        if email:
                            attendees.append(email)
            except Exception:
                pass
            out.append({
                "appointment_id": self._str_id(safe_get(item, "id", None)),
                "subject": safe_get(item, "subject", ""),
                "start": _iso(safe_get(item, "start", None)),
                "end": _iso(safe_get(item, "end", None)),
                "location": str(safe_get(item, "location", "") or "")[:200],
                "organizer": safe_get(
                    safe_get(item, "organizer", None), "email_address", None
                ),
                "attendees": attendees[:20],
                "is_cancelled": bool(safe_get(item, "is_cancelled", False)),
            })
        return out

    def _collect_commitments(self, window_end, limit: int) -> Dict[str, Any]:
        try:
            repo = CommitmentRepo(self.get_memory_store())
            overdue = repo.list(status="open", overdue=True, limit=limit)
            open_items = repo.list(status="open", limit=limit)
            end_epoch = window_end.timestamp() if hasattr(window_end, "timestamp") else None
            due_in_window = [
                c for c in open_items
                if c.due_at is not None and end_epoch is not None and c.due_at <= end_epoch
            ][:limit]
        except Exception as exc:
            self.logger.warning(f"briefing: commitments collection failed: {exc}")
            return {"overdue": [], "due_in_window": []}
        return {
            "overdue": [c.to_dict() for c in overdue],
            "due_in_window": [c.to_dict() for c in due_in_window],
        }

    def _collect_overdue_tasks(self, account, now, limit: int) -> List[Dict[str, Any]]:
        tasks = getattr(account, "tasks", None)
        if tasks is None:
            return []
        try:
            query = tasks.filter(due_date__lt=now.date()).order_by("due_date")
            items = list(query[: limit * 2])
        except Exception as exc:
            self.logger.warning(f"briefing: overdue_tasks query failed: {exc}")
            return []
        out: List[Dict[str, Any]] = []
        for task in items:
            status = str(safe_get(task, "status", "") or "")
            if status.lower() == "completed":
                continue
            out.append({
                "task_id": self._str_id(safe_get(task, "id", None)),
                "subject": safe_get(task, "subject", ""),
                "due_date": str(safe_get(task, "due_date", "") or ""),
                "status": status,
                "importance": safe_get(task, "importance", "Normal"),
            })
            if len(out) >= limit:
                break
        return out

    def _collect_vip_activity(self, account, since, limit: int) -> List[Dict[str, Any]]:
        # Lazy import to avoid circulars.
        try:
            from ..services.person_service import PersonService
        except Exception:
            return []
        try:
            person_service = PersonService(self.ews_client)
            # Best-effort: pull a small list of people the user emails often
            # in the last 30 days, treat them as VIP for digest purposes.
            # This matches the behaviour of analyze_contacts(vip) without the
            # heavier full-network scan.
            top_senders = []
            since_30 = make_tz_aware(datetime.now() - timedelta(days=30))
            query = account.inbox.filter(datetime_received__gte=since_30)
            seen: Dict[str, int] = {}
            for msg in query[:500]:
                email = safe_get(safe_get(msg, "sender", None), "email_address", None)
                if email:
                    seen[email] = seen.get(email, 0) + 1
            top_senders = [
                email for email, _ in sorted(seen.items(), key=lambda kv: -kv[1])[:15]
            ]
        except Exception:
            top_senders = []

        if not top_senders:
            return []

        try:
            recent = account.inbox.filter(datetime_received__gte=since).order_by("-datetime_received")
            items = list(recent[:200])
        except Exception:
            return []
        vip_set = {e.lower() for e in top_senders}
        out: List[Dict[str, Any]] = []
        for msg in items:
            email = safe_get(safe_get(msg, "sender", None), "email_address", None)
            if not email or email.lower() not in vip_set:
                continue
            out.append({
                "message_id": self._str_id(safe_get(msg, "id", None)),
                "subject": safe_get(msg, "subject", ""),
                "from": email,
                "received_at": _iso(safe_get(msg, "datetime_received", None)),
                "is_read": bool(safe_get(msg, "is_read", False)),
                "preview": truncate_text(str(safe_get(msg, "text_body", "") or ""), max_length=200),
            })
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _str_id(value) -> Optional[str]:
        if value is None:
            return None
        if hasattr(value, "id"):
            return str(value.id)
        return str(value)
