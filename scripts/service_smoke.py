"""Live EWS service smoke test — self-only.

Reads credentials from %APPDATA%/Claude/claude_desktop_config.json (the
same env block the desktop MCP uses) and exercises every tool category
end-to-end against the configured Exchange mailbox.

Hard guardrail: every recipient (to/cc/bcc/attendees) MUST be the
configured user's own SMTP address. Anything else aborts the test.

All mutations register a teardown so drafts/appointments/contacts/tasks/
folders are removed before exit, even if a test in between blows up.

Run:
    python scripts/service_smoke.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_env_from_desktop_config() -> dict[str, str]:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
    cfg_path = Path(appdata) / "Claude" / "claude_desktop_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    env = cfg["mcpServers"]["ews"]["env"]
    for k, v in env.items():
        os.environ.setdefault(k, v)
    return env


_load_env_from_desktop_config()

from src.auth import AuthHandler  # noqa: E402
from src.config import Settings  # noqa: E402
from src.ews_client import EWSClient  # noqa: E402
from src.tools.attachment_tools import (  # noqa: E402
    AddAttachmentTool, DeleteAttachmentTool, GetEmailMimeTool, ListAttachmentsTool,
)
from src.tools.calendar_tools import (  # noqa: E402
    CheckAvailabilityTool, CreateAppointmentTool, DeleteAppointmentTool,
    FindMeetingTimesTool, GetCalendarTool, UpdateAppointmentTool,
)
from src.tools.contact_intelligence_tools import FindPersonTool  # noqa: E402
from src.tools.contact_tools import (  # noqa: E402
    CreateContactTool, DeleteContactTool, UpdateContactTool,
)
from src.tools.email_tools import (  # noqa: E402
    CopyEmailTool, DeleteEmailTool, ForwardEmailTool, GetEmailDetailsTool,
    MoveEmailTool, ReadEmailsTool, ReplyEmailTool, SearchEmailsTool,
    SendEmailTool, UpdateEmailTool,
)
from src.tools.email_tools_draft import (  # noqa: E402
    CreateDraftTool, CreateForwardDraftTool, CreateReplyDraftTool,
)
from src.tools.folder_tools import (  # noqa: E402
    FindFolderTool, ListFoldersTool, ManageFolderTool,
)
from src.tools.oof_tools import OofSettingsTool  # noqa: E402
from src.tools.task_tools import (  # noqa: E402
    CompleteTaskTool, CreateTaskTool, DeleteTaskTool, GetTasksTool, UpdateTaskTool,
)


SETTINGS = Settings()
SELF = SETTINGS.ews_email.lower()
ALLOWED = {SELF}

MARKER = f"[SVC-SMOKE-{datetime.utcnow():%Y%m%d%H%M%S}]"


def _guard_recipients(*lists: list[str] | None) -> None:
    for lst in lists:
        if not lst:
            continue
        for addr in lst:
            if (addr or "").strip().lower() not in ALLOWED:
                raise RuntimeError(
                    f"Recipient guardrail tripped: {addr!r} not in allowlist {ALLOWED}"
                )


class Recorder:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []  # (name, status, detail)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append((name, status, detail))
        bullet = {"PASS": "OK ", "FAIL": "FAIL", "SKIP": "skip"}[status]
        print(f"  [{bullet}] {name}{(' — ' + detail) if detail else ''}", flush=True)

    def summary(self) -> str:
        passed = sum(1 for _, s, _ in self.results if s == "PASS")
        failed = sum(1 for _, s, _ in self.results if s == "FAIL")
        skipped = sum(1 for _, s, _ in self.results if s == "SKIP")
        return f"{passed} passed, {failed} failed, {skipped} skipped"


REC = Recorder()
CLEANUP: list[Callable[[], Any]] = []


def _ok(res: dict | None) -> bool:
    return bool(res) and res.get("success") is not False


def _id(res: dict | None) -> str | None:
    if not res:
        return None
    return res.get("message_id") or res.get("item_id") or res.get("id")


async def _try(name: str, coro):
    try:
        res = await coro
        if _ok(res):
            REC.add(name, "PASS", _short(res))
        else:
            REC.add(name, "FAIL", f"success=False: {_short(res)}")
        return res
    except Exception as e:
        REC.add(name, "FAIL", f"{type(e).__name__}: {e}")
        return None


def _short(res: dict | None) -> str:
    if not isinstance(res, dict):
        return str(res)[:120]
    keys = [k for k in ("message_id", "item_id", "id", "subject", "count",
                        "total_count", "state", "items", "folders", "tasks") if k in res]
    snippet: dict[str, Any] = {}
    for k in keys[:4]:
        v = res[k]
        if isinstance(v, list):
            snippet[k] = f"<{len(v)} items>"
        elif isinstance(v, str) and len(v) > 60:
            snippet[k] = v[:60] + "..."
        else:
            snippet[k] = v
    return json.dumps(snippet, default=str) if snippet else "ok"


# --------------------------------------------------------------------- #
# Test groups                                                           #
# --------------------------------------------------------------------- #

async def test_connection(client: EWSClient) -> None:
    print("\n[1] Connection")
    try:
        ok = client.test_connection()
        REC.add("test_connection", "PASS" if ok else "FAIL", f"ok={ok}")
    except Exception as e:
        REC.add("test_connection", "FAIL", f"{type(e).__name__}: {e}")
    try:
        addr = client.account.primary_smtp_address
        REC.add("account_resolved", "PASS" if addr else "FAIL", f"address={addr}")
    except Exception as e:
        REC.add("account_resolved", "FAIL", f"{type(e).__name__}: {e}")


async def test_email_read(client: EWSClient) -> dict | None:
    print("\n[2] Email — read paths")
    await _try("read_emails inbox max=3",
               ReadEmailsTool(client).execute(folder="inbox", max_results=3))
    await _try("search_emails quick",
               SearchEmailsTool(client).execute(mode="quick", folder="inbox", max_results=5))
    await _try("search_emails full_text",
               SearchEmailsTool(client).execute(mode="full_text", query="meeting", max_results=3))
    top = await SearchEmailsTool(client).execute(mode="quick", folder="inbox", max_results=1)
    items = (top or {}).get("items") or (top or {}).get("emails") or []
    if not items:
        REC.add("email_details_top", "SKIP", "inbox empty")
        return None
    msg_id = items[0].get("message_id") or items[0].get("id")
    await _try("get_email_details top", GetEmailDetailsTool(client).execute(message_id=msg_id))
    await _try("get_email_mime top", GetEmailMimeTool(client).execute(message_id=msg_id))
    return items[0]


async def test_email_write_full(client: EWSClient) -> None:
    print("\n[3] Email — write (draft round-trip + self-send + mutate)")

    # Draft create + verify + delete
    subject = f"{MARKER} draft round-trip"
    _guard_recipients([SELF])
    draft = await _try(
        "create_draft self",
        CreateDraftTool(client).execute(
            to=[SELF], subject=subject,
            body="Self-only test draft. Will be deleted.",
        ),
    )
    draft_id = _id(draft)
    if draft_id:
        async def _drop_draft():
            await DeleteEmailTool(client).execute(message_id=draft_id, permanent=True)
        CLEANUP.append(_drop_draft)
        found = await _try(
            "search drafts for marker",
            SearchEmailsTool(client).execute(
                mode="quick", folder="drafts", subject_contains=MARKER, max_results=5,
            ),
        )
        items = (found or {}).get("items") or (found or {}).get("emails") or []
        REC.add("draft_visible_in_drafts", "PASS" if any(
            (it.get("message_id") == draft_id) or (MARKER in (it.get("subject") or ""))
            for it in items
        ) else "FAIL", f"items={len(items)}")

    # Real send to SELF (allowed)
    send_subject = f"{MARKER} self-send"
    _guard_recipients([SELF])
    sent = await _try(
        "send_email to self",
        SendEmailTool(client).execute(
            to=[SELF], subject=send_subject,
            body="Self-only smoke test. Safe to delete.",
            importance="Normal",
        ),
    )

    # Locate the message in inbox (give Exchange a moment)
    inbox_msg_id: str | None = None
    for attempt in range(6):
        await asyncio.sleep(2)
        found = await SearchEmailsTool(client).execute(
            mode="quick", folder="inbox", subject_contains=MARKER, max_results=5,
        )
        items = (found or {}).get("items") or (found or {}).get("emails") or []
        hit = next((it for it in items if MARKER in (it.get("subject") or "")
                    and "self-send" in (it.get("subject") or "")), None)
        if hit:
            inbox_msg_id = hit.get("message_id") or hit.get("id")
            break
    REC.add("self_send_arrived_in_inbox",
            "PASS" if inbox_msg_id else "FAIL",
            f"id={inbox_msg_id} attempts={attempt + 1}")

    if inbox_msg_id:
        async def _drop_inbox():
            await DeleteEmailTool(client).execute(message_id=inbox_msg_id, permanent=True)
        CLEANUP.append(_drop_inbox)

        # update_email — flag, categorize, mark read
        await _try("update_email mark unread+category",
                   UpdateEmailTool(client).execute(
                       message_id=inbox_msg_id, is_read=False,
                       categories=["svc-smoke"], importance="High"))

        # copy_email to Drafts (safe folder, not a sensitive area)
        copy_res = await _try("copy_email to Drafts",
                              CopyEmailTool(client).execute(
                                  message_id=inbox_msg_id, destination_folder="drafts"))
        copy_id = _id(copy_res)
        if copy_id and copy_id != inbox_msg_id:
            async def _drop_copy():
                await DeleteEmailTool(client).execute(message_id=copy_id, permanent=True)
            CLEANUP.append(_drop_copy)

        # reply_email to a self-message → reply goes back to self (safe)
        reply_subject_marker = f"{MARKER}-reply"
        _guard_recipients([SELF])
        rep = await _try("create_reply_draft (no send)",
                         CreateReplyDraftTool(client).execute(
                             message_id=inbox_msg_id,
                             body=f"{reply_subject_marker} reply draft body",
                             reply_all=False))
        rep_id = _id(rep)
        if rep_id:
            async def _drop_rep():
                await DeleteEmailTool(client).execute(message_id=rep_id, permanent=True)
            CLEANUP.append(_drop_rep)

        # create_forward_draft to self (no send)
        _guard_recipients([SELF])
        fwd = await _try("create_forward_draft to self (no send)",
                         CreateForwardDraftTool(client).execute(
                             message_id=inbox_msg_id, to=[SELF],
                             body=f"{MARKER}-fwd forward draft body"))
        fwd_id = _id(fwd)
        if fwd_id:
            async def _drop_fwd():
                await DeleteEmailTool(client).execute(message_id=fwd_id, permanent=True)
            CLEANUP.append(_drop_fwd)

        # forward_email — to self only (actually sends)
        _guard_recipients([SELF])
        await _try("forward_email to self",
                   ForwardEmailTool(client).execute(
                       message_id=inbox_msg_id, to=[SELF],
                       body=f"{MARKER}-fwd-sent forwarded body"))

        # reply_email — to a self-message → reply heads back to self (safe)
        _guard_recipients([SELF])
        await _try("reply_email (sender=self)",
                   ReplyEmailTool(client).execute(
                       message_id=inbox_msg_id,
                       body=f"{MARKER}-reply-sent reply body",
                       reply_all=False))

        # move_email (to Deleted Items, then delete permanently)
        await _try("move_email to Deleted Items",
                   MoveEmailTool(client).execute(
                       message_id=inbox_msg_id,
                       destination_folder="deleted items"))
    else:
        REC.add("post_send_mutations", "SKIP", "self-send did not land in inbox")


async def test_attachments(client: EWSClient) -> None:
    print("\n[4] Attachments")
    subject = f"{MARKER} attachment round-trip"
    _guard_recipients([SELF])
    draft = await CreateDraftTool(client).execute(
        to=[SELF], subject=subject, body="attachment round-trip",
    )
    draft_id = _id(draft)
    if not draft_id:
        REC.add("create_draft for attachment", "FAIL", _short(draft))
        return

    async def _drop_attach_draft():
        await DeleteEmailTool(client).execute(message_id=draft_id, permanent=True)
    CLEANUP.append(_drop_attach_draft)

    payload = base64.b64encode(b"hello svc-smoke\n").decode()
    add = await _try("add_attachment file_content",
                     AddAttachmentTool(client).execute(
                         message_id=draft_id, file_name="hello.txt",
                         file_content=payload, content_type="text/plain"))
    listed = await _try("list_attachments after add",
                        ListAttachmentsTool(client).execute(message_id=draft_id))
    atts = (listed or {}).get("attachments") or (listed or {}).get("items") or []
    att_id = (add or {}).get("attachment_id") or (atts[0].get("attachment_id") if atts else None)
    if att_id:
        await _try("delete_attachment",
                   DeleteAttachmentTool(client).execute(
                       message_id=draft_id, attachment_id=att_id))
    else:
        REC.add("delete_attachment", "SKIP", "no attachment_id surfaced")


async def test_calendar(client: EWSClient) -> None:
    print("\n[5] Calendar")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    await _try("get_calendar +1d",
               GetCalendarTool(client).execute(
                   start_date=now.isoformat(),
                   end_date=(now + timedelta(days=1)).isoformat()))
    await _try("check_availability self 1h",
               CheckAvailabilityTool(client).execute(
                   email_addresses=[SELF],
                   start_time=now.isoformat(),
                   end_time=(now + timedelta(hours=1)).isoformat()))
    await _try("find_meeting_times self 30m",
               FindMeetingTimesTool(client).execute(
                   attendees=[SELF], duration_minutes=30,
                   date_range_start=(now + timedelta(days=1)).date().isoformat(),
                   date_range_end=(now + timedelta(days=2)).date().isoformat()))

    # Solo appointment (no attendees) — no invite is sent
    start = (now + timedelta(days=2)).replace(hour=9, minute=0, second=0)
    end = start + timedelta(minutes=30)
    appt_subject = f"{MARKER} solo appt"
    appt = await _try("create_appointment solo",
                      CreateAppointmentTool(client).execute(
                          subject=appt_subject,
                          start_time=start.isoformat(),
                          end_time=end.isoformat(),
                          location="self-only test",
                          body="solo, no attendees"))
    appt_id = _id(appt)
    if appt_id:
        async def _drop_appt():
            await DeleteAppointmentTool(client).execute(
                item_id=appt_id, send_cancellation=False)
        CLEANUP.append(_drop_appt)

        await _try("update_appointment subject",
                   UpdateAppointmentTool(client).execute(
                       item_id=appt_id, subject=f"{appt_subject} (renamed)"))

    # Invite-style appointment (attendees=[self]) — a meeting invite is sent to self
    invite_subject = f"{MARKER} self-invite appt"
    _guard_recipients([SELF])
    invite = await _try("create_appointment with attendee=self",
                        CreateAppointmentTool(client).execute(
                            subject=invite_subject,
                            start_time=(start + timedelta(hours=2)).isoformat(),
                            end_time=(end + timedelta(hours=2)).isoformat(),
                            attendees=[SELF],
                            body="self-only invite test"))
    invite_id = _id(invite)
    if invite_id:
        async def _drop_invite():
            await DeleteAppointmentTool(client).execute(
                item_id=invite_id, send_cancellation=True)
        CLEANUP.append(_drop_invite)


async def test_folders(client: EWSClient) -> None:
    print("\n[6] Folders")
    await _try("list_folders depth=1",
               ListFoldersTool(client).execute(depth=1))
    await _try("find_folder Inbox",
               FindFolderTool(client).execute(query="Inbox"))

    fname = f"svc-smoke-{datetime.utcnow():%H%M%S}"
    created = await _try("manage_folder create",
                         ManageFolderTool(client).execute(
                             action="create", folder_name=fname,
                             parent_folder="inbox"))
    fid = (created or {}).get("folder_id") or (created or {}).get("id")
    if not fid:
        REC.add("folder_round_trip", "SKIP", "no folder_id returned by create")
        return

    async def _drop_folder():
        await ManageFolderTool(client).execute(
            action="delete", folder_id=fid, permanent=True)
    CLEANUP.append(_drop_folder)

    await _try("manage_folder rename",
               ManageFolderTool(client).execute(
                   action="rename", folder_id=fid, new_name=fname + "-r"))


async def test_contacts(client: EWSClient) -> None:
    print("\n[7] Contacts")
    await _try("find_person self",
               FindPersonTool(client).execute(
                   query=SELF.split("@", 1)[0], max_results=3))
    contact = await _try("create_contact",
                         CreateContactTool(client).execute(
                             given_name="SVC", surname="Smoke",
                             email_address=SELF,
                             company="self-test", job_title="tester"))
    cid = _id(contact)
    if cid:
        async def _drop_contact():
            await DeleteContactTool(client).execute(item_id=cid)
        CLEANUP.append(_drop_contact)
        await _try("update_contact job_title",
                   UpdateContactTool(client).execute(
                       item_id=cid, job_title="tester-renamed"))


async def test_tasks(client: EWSClient) -> None:
    print("\n[8] Tasks")
    await _try("get_tasks", GetTasksTool(client).execute(max_results=3))
    t = await _try("create_task",
                   CreateTaskTool(client).execute(
                       subject=f"{MARKER} svc-smoke task",
                       body="self-only test task",
                       importance="Normal"))
    tid = _id(t)
    if tid:
        async def _drop_task():
            await DeleteTaskTool(client).execute(item_id=tid)
        CLEANUP.append(_drop_task)
        await _try("update_task percent=50",
                   UpdateTaskTool(client).execute(item_id=tid, percent_complete=50))
        await _try("complete_task",
                   CompleteTaskTool(client).execute(item_id=tid))


async def test_oof(client: EWSClient) -> None:
    print("\n[9] OOF")
    await _try("oof_settings get",
               OofSettingsTool(client).execute(action="get"))


# --------------------------------------------------------------------- #
# Main                                                                  #
# --------------------------------------------------------------------- #

async def main() -> int:
    print(f"User: {SELF}  marker: {MARKER}")
    auth = AuthHandler(SETTINGS)
    client = EWSClient(SETTINGS, auth)

    try:
        await test_connection(client)
        await test_email_read(client)
        await test_email_write_full(client)
        await test_attachments(client)
        await test_calendar(client)
        await test_folders(client)
        await test_contacts(client)
        await test_tasks(client)
        await test_oof(client)
    finally:
        print("\n[teardown] cleaning up created artefacts...")
        # LIFO so children go before parents (e.g. attachment before its draft)
        for fn in reversed(CLEANUP):
            try:
                await fn()
            except Exception as e:
                print(f"  cleanup {fn.__name__}: {type(e).__name__}: {e}")
        # Sweep anything left with our marker (defence in depth)
        for folder in ("inbox", "drafts", "sent", "deleted items"):
            try:
                found = await SearchEmailsTool(client).execute(
                    mode="quick", folder=folder, subject_contains=MARKER,
                    max_results=20)
                items = (found or {}).get("items") or (found or {}).get("emails") or []
                for it in items:
                    mid = it.get("message_id") or it.get("id")
                    if mid:
                        try:
                            await DeleteEmailTool(client).execute(
                                message_id=mid, permanent=True)
                        except Exception:
                            pass
            except Exception:
                pass

    print("\n========================================================")
    print(f" RESULT: {REC.summary()}")
    print("========================================================")
    fails = [(n, d) for n, s, d in REC.results if s == "FAIL"]
    if fails:
        print("\nFailures:")
        for n, d in fails:
            print(f"  - {n}: {d}")
    return 0 if not fails else 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except Exception:
        traceback.print_exc()
        rc = 2
    sys.exit(rc)
