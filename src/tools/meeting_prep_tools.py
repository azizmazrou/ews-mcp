"""Meeting prep: ``prepare_meeting`` composes a one-pager for a given appointment.

Given an appointment_id, the tool returns a single JSON payload containing:

* Meeting metadata (subject, time, location, attendees, organizer)
* For each attendee — last N emails with that person, open commitments
  involving them, and any ``person.note`` entries from memory
* Related thread — search_emails(subject_contains=meeting.subject,
  mode="quick") limited to a short horizon
* Attachments on the invite — name + extracted text snippet (PDF/DOCX/XLSX)

Read-only. Does not mutate anything.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..memory import CommitmentRepo, NS
from ..utils import (
    format_success_response,
    make_tz_aware,
    safe_get,
    truncate_text,
)


def _str_id(value) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "id"):
        return str(value.id)
    return str(value)


class PrepareMeetingTool(BaseTool):
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "prepare_meeting",
            "description": (
                "Build a deterministic meeting brief for a given appointment_id: "
                "attendees + last emails with each + related thread + stored "
                "notes + attachment previews. Read-only."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "depth": {
                        "type": "string",
                        "enum": ["quick", "deep"],
                        "default": "quick",
                    },
                    "history_per_attendee": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                    "extract_attachment_text": {
                        "type": "boolean",
                        "default": False,
                        "description": "When true, extract text from PDF/DOCX/XLSX attachments (slow)",
                    },
                    "target_mailbox": {"type": "string"},
                },
                "required": ["appointment_id"],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        appointment_id = kwargs.get("appointment_id")
        depth = kwargs.get("depth", "quick")
        history_per = int(kwargs.get("history_per_attendee", 5))
        extract_text = bool(kwargs.get("extract_attachment_text", False))
        target_mailbox = kwargs.get("target_mailbox")
        if not appointment_id:
            raise ToolExecutionError("appointment_id is required")

        account = self.get_account(target_mailbox)
        mailbox = self.get_mailbox_info(target_mailbox)

        # 1. Fetch appointment.
        try:
            item = account.calendar.get(id=appointment_id)
        except Exception as exc:
            raise ToolExecutionError(f"Appointment not found: {exc}")

        attendees = self._extract_attendees(item)
        organizer = safe_get(safe_get(item, "organizer", None), "email_address", None)

        meeting_info = {
            "appointment_id": _str_id(safe_get(item, "id", None)),
            "subject": safe_get(item, "subject", ""),
            "start": self._iso(safe_get(item, "start", None)),
            "end": self._iso(safe_get(item, "end", None)),
            "location": str(safe_get(item, "location", "") or "")[:200],
            "organizer": organizer,
            "attendees": attendees,
            "is_online_meeting": bool(safe_get(item, "is_online_meeting", False)),
            "body_preview": truncate_text(str(safe_get(item, "text_body", "") or ""), 500),
        }

        # 2. Per-attendee history + notes + commitments.
        store = self.get_memory_store()
        commit_repo = CommitmentRepo(store)
        all_commitments = commit_repo.list(status=None, limit=500)  # open+done+cancelled

        attendee_briefs: List[Dict[str, Any]] = []
        for email in attendees:
            history = self._recent_history_with(account, email, limit=history_per)
            note_rec = store.get(NS.PERSON_NOTE, self._sanitize_key(email))
            commitments_with = [
                c.to_dict()
                for c in all_commitments
                if (c.counterparty and c.counterparty.lower() == email.lower())
            ]
            attendee_briefs.append({
                "email": email,
                "note": note_rec.value if note_rec else None,
                "recent_history": history,
                "open_commitments": [c for c in commitments_with if c["status"] == "open"],
                "past_commitments": [c for c in commitments_with if c["status"] != "open"][:5],
            })

        # 3. Related thread — quick search on subject with recent horizon.
        related_thread = self._find_related_thread(account, meeting_info["subject"])

        # 4. Attachment previews.
        attachments_summary = self._summarise_attachments(item, extract_text=extract_text)

        brief: Dict[str, Any] = {
            "meeting": meeting_info,
            "mailbox": mailbox,
            "attendees": attendee_briefs,
            "related_thread": related_thread,
            "attachments": attachments_summary,
            "depth": depth,
        }
        return format_success_response("Meeting prep ready", brief=brief)

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _iso(value) -> Optional[str]:
        if value is None:
            return None
        try:
            return value.isoformat()
        except Exception:
            return str(value)

    @staticmethod
    def _sanitize_key(email: str) -> str:
        # Normalise to lowercase and strip characters outside the memory key
        # alphabet. Collisions are unlikely because emails are unique and we
        # keep the local-part + domain structure.
        import re
        cleaned = re.sub(r"[^A-Za-z0-9._:\-]+", ".", email.lower())
        return cleaned[:128] or "anonymous"

    def _extract_attendees(self, item) -> List[str]:
        out: List[str] = []
        seen = set()
        for group in (safe_get(item, "required_attendees", []) or [],
                      safe_get(item, "optional_attendees", []) or []):
            for att in group:
                mbx = safe_get(att, "mailbox", None)
                email = safe_get(mbx, "email_address", None) if mbx else None
                if not email:
                    continue
                key = email.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(email)
        return out

    def _recent_history_with(self, account, email: str, limit: int) -> List[Dict[str, Any]]:
        """Return a merged list of recent messages to or from this email."""
        since = make_tz_aware(__import__("datetime").datetime.now() - timedelta(days=180))
        results: List[Dict[str, Any]] = []
        seen = set()
        try:
            inbox_q = account.inbox.filter(
                datetime_received__gte=since,
                sender__email_address=email,
            ).order_by("-datetime_received")[:limit]
            for msg in inbox_q:
                rid = _str_id(safe_get(msg, "id", None))
                if rid in seen:
                    continue
                seen.add(rid)
                results.append({
                    "message_id": rid,
                    "direction": "inbound",
                    "subject": safe_get(msg, "subject", ""),
                    "received_at": self._iso(safe_get(msg, "datetime_received", None)),
                    "preview": truncate_text(str(safe_get(msg, "text_body", "") or ""), 200),
                })
        except Exception as exc:
            self.logger.debug(f"meeting_prep: inbox scan for {email} failed: {exc}")

        try:
            sent = getattr(account, "sent", None)
            if sent is not None:
                sent_q = sent.filter(datetime_sent__gte=since).order_by("-datetime_sent")[:50]
                collected = 0
                for msg in sent_q:
                    to_emails = [
                        (safe_get(r, "email_address", "") or "").lower()
                        for r in (safe_get(msg, "to_recipients", []) or [])
                    ]
                    if email.lower() not in to_emails:
                        continue
                    rid = _str_id(safe_get(msg, "id", None))
                    if rid in seen:
                        continue
                    seen.add(rid)
                    results.append({
                        "message_id": rid,
                        "direction": "outbound",
                        "subject": safe_get(msg, "subject", ""),
                        "sent_at": self._iso(safe_get(msg, "datetime_sent", None)),
                        "preview": truncate_text(str(safe_get(msg, "text_body", "") or ""), 200),
                    })
                    collected += 1
                    if collected >= limit:
                        break
        except Exception as exc:
            self.logger.debug(f"meeting_prep: sent scan for {email} failed: {exc}")

        # Keep the newest N across both directions.
        results.sort(
            key=lambda r: r.get("received_at") or r.get("sent_at") or "",
            reverse=True,
        )
        return results[:limit]

    def _find_related_thread(self, account, subject: str) -> List[Dict[str, Any]]:
        if not subject or len(subject) < 3:
            return []
        since = make_tz_aware(__import__("datetime").datetime.now() - timedelta(days=60))
        # Strip RE: / FW: so the subject-match is less brittle.
        normalised = subject
        for prefix in ("RE:", "Re:", "FW:", "Fw:", "FWD:", "Fwd:"):
            if normalised.startswith(prefix):
                normalised = normalised[len(prefix):].strip()
        if len(normalised) < 3:
            return []
        try:
            q = account.inbox.filter(
                subject__icontains=normalised, datetime_received__gte=since
            ).order_by("-datetime_received")[:20]
        except Exception as exc:
            self.logger.debug(f"meeting_prep: related_thread search failed: {exc}")
            return []
        return [
            {
                "message_id": _str_id(safe_get(msg, "id", None)),
                "subject": safe_get(msg, "subject", ""),
                "from": safe_get(safe_get(msg, "sender", None), "email_address", ""),
                "received_at": self._iso(safe_get(msg, "datetime_received", None)),
                "preview": truncate_text(str(safe_get(msg, "text_body", "") or ""), 200),
            }
            for msg in q
        ]

    def _summarise_attachments(self, item, *, extract_text: bool) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            attachments = list(safe_get(item, "attachments", []) or [])
        except Exception:
            return []
        if not attachments:
            return []
        for att in attachments[:10]:
            info: Dict[str, Any] = {
                "name": safe_get(att, "name", "attachment"),
                "size": safe_get(att, "size", None),
                "content_type": safe_get(att, "content_type", None),
                "is_inline": bool(safe_get(att, "is_inline", False)),
            }
            if extract_text:
                info["excerpt"] = self._try_extract(att)
            out.append(info)
        return out

    def _try_extract(self, att) -> Optional[str]:
        """Best-effort text extraction for PDF/DOCX/XLSX attachments.

        Silently returns None on any failure — this is a convenience feature,
        not a correctness-critical path.
        """
        try:
            from .attachment_tools import ReadAttachmentTool
        except Exception:
            return None
        name = safe_get(att, "name", "") or ""
        ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
        if ext not in ("pdf", "docx", "xlsx", "xls"):
            return None
        try:
            content = safe_get(att, "content", b"") or b""
            if not content:
                return None
            reader = ReadAttachmentTool(self.ews_client)
            if ext == "pdf":
                text = reader._read_pdf(content, extract_tables=False, max_pages=2)
            elif ext == "docx":
                text = reader._read_docx(content, extract_tables=False)
            else:
                text = reader._read_excel(content)
            return truncate_text(text, 500)
        except Exception:
            return None
