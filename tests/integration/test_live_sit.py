"""Live SIT (System Integration Test) — tool-by-tool against a real
Exchange mailbox + the configured AI provider.

Gating
------
* All tests require ``EWS_LIVE_SIT=1``.
* Mutating tests additionally require ``EWS_LIVE_SIT_WRITE=1``.
  The single mutation in this file is a Drafts-folder round-trip
  (create draft → assert visible → delete) — no email is ever sent.
* AI tests additionally require ``ENABLE_AI=true`` and a reachable
  ``AI_BASE_URL`` (skip if the embedding/chat round-trip fails).

Why structural-only assertions
------------------------------
SIT against a live mailbox can return wildly different counts and
contents minute to minute. The assertions are deliberately weak:
*shape* of the response (success flag, expected keys, list types)
rather than *contents*. The unit tests in ``tests/test_*.py`` cover
behavioural correctness against mocks; this file exists to prove the
wires are connected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from .conftest import (
    LIVE_AI_ENABLED,
    LIVE_SIT_ENABLED,
    LIVE_SIT_WRITE_ENABLED,
    SKIP_REASON_AI,
    SKIP_REASON_LIVE,
    SKIP_REASON_WRITE,
    fetch_top_inbox_message,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not LIVE_SIT_ENABLED, reason=SKIP_REASON_LIVE),
]


# --------------------------------------------------------------------- #
# Connection                                                            #
# --------------------------------------------------------------------- #

class TestConnection:
    def test_connection_succeeds(self, live_client):
        assert live_client.test_connection() is True

    def test_account_resolves(self, live_client):
        account = live_client.account
        assert account is not None
        assert account.primary_smtp_address


# --------------------------------------------------------------------- #
# Email — read                                                          #
# --------------------------------------------------------------------- #

class TestEmailRead:
    @pytest.mark.asyncio
    async def test_search_emails_quick(self, live_client):
        from src.tools.email_tools import SearchEmailsTool

        result = await SearchEmailsTool(live_client).execute(
            mode="quick", folder="inbox", max_results=5,
        )
        assert result.get("success") is not False
        items = result.get("items") or result.get("emails") or []
        assert isinstance(items, list)

    @pytest.mark.asyncio
    async def test_search_emails_full_text(self, live_client):
        from src.tools.email_tools import SearchEmailsTool

        result = await SearchEmailsTool(live_client).execute(
            mode="full_text", query="meeting", max_results=5,
        )
        assert result.get("success") is not False

    @pytest.mark.asyncio
    async def test_read_emails(self, live_client):
        from src.tools.email_tools import ReadEmailsTool

        result = await ReadEmailsTool(live_client).execute(
            folder="inbox", max_results=3,
        )
        assert result.get("success") is not False

    @pytest.mark.asyncio
    async def test_get_email_details_on_top_message(self, live_client):
        from src.tools.email_tools import GetEmailDetailsTool

        top = await fetch_top_inbox_message(live_client)
        if top is None:
            pytest.skip("Inbox is empty.")

        msg_id = top.get("message_id") or top.get("id")
        details = await GetEmailDetailsTool(live_client).execute(message_id=msg_id)
        assert details.get("success") is not False
        assert details.get("message_id") or details.get("subject")


# --------------------------------------------------------------------- #
# Email — write (draft round-trip)                                      #
# --------------------------------------------------------------------- #

class TestEmailWrite:
    @pytest.mark.asyncio
    @pytest.mark.skipif(not LIVE_SIT_WRITE_ENABLED, reason=SKIP_REASON_WRITE)
    async def test_create_and_delete_draft(self, live_client):
        """Create a draft, locate it, delete it. Never sends mail."""
        from src.tools.email_tools import DeleteEmailTool, SearchEmailsTool
        from src.tools.email_tools_draft import CreateDraftTool

        marker = f"[SIT-{datetime.utcnow():%Y%m%d%H%M%S}]"
        subject = f"{marker} ews-mcp live SIT draft"

        created = await CreateDraftTool(live_client).execute(
            to=[live_client.config.ews_email],
            subject=subject,
            body="Created by tests/integration/test_live_sit.py — safe to ignore.",
        )
        assert created.get("success") is True
        draft_id = created.get("message_id") or created.get("id")
        assert draft_id, f"CreateDraftTool returned no id: {created}"

        try:
            found = await SearchEmailsTool(live_client).execute(
                mode="quick", folder="drafts", subject_contains=marker, max_results=5,
            )
            items = found.get("items") or found.get("emails") or []
            assert any(
                (it.get("message_id") == draft_id) or (marker in (it.get("subject") or ""))
                for it in items
            ), f"Draft with marker {marker} not found after create"
        finally:
            await DeleteEmailTool(live_client).execute(
                message_id=draft_id, hard_delete=True,
            )


# --------------------------------------------------------------------- #
# Calendar                                                              #
# --------------------------------------------------------------------- #

class TestCalendar:
    @pytest.mark.asyncio
    async def test_get_calendar_today(self, live_client):
        from src.tools.calendar_tools import GetCalendarTool

        now = datetime.now(timezone.utc).replace(microsecond=0)
        result = await GetCalendarTool(live_client).execute(
            start_time=now.isoformat(),
            end_time=(now + timedelta(days=1)).isoformat(),
        )
        assert result.get("success") is not False

    @pytest.mark.asyncio
    async def test_check_availability_self(self, live_client):
        from src.tools.calendar_tools import CheckAvailabilityTool

        now = datetime.now(timezone.utc).replace(microsecond=0)
        result = await CheckAvailabilityTool(live_client).execute(
            attendees=[live_client.config.ews_email],
            start_time=now.isoformat(),
            end_time=(now + timedelta(hours=1)).isoformat(),
        )
        assert result.get("success") is not False

    @pytest.mark.asyncio
    async def test_find_meeting_times_self(self, live_client):
        from src.tools.calendar_tools import FindMeetingTimesTool

        now = datetime.now(timezone.utc).replace(microsecond=0)
        result = await FindMeetingTimesTool(live_client).execute(
            attendees=[live_client.config.ews_email],
            duration_minutes=30,
            start_date=(now + timedelta(days=1)).date().isoformat(),
            end_date=(now + timedelta(days=2)).date().isoformat(),
        )
        assert result.get("success") is not False


# --------------------------------------------------------------------- #
# Folders                                                               #
# --------------------------------------------------------------------- #

class TestFolders:
    @pytest.mark.asyncio
    async def test_list_folders_inbox(self, live_client):
        from src.tools.folder_tools import ListFoldersTool

        result = await ListFoldersTool(live_client).execute(
            folder="inbox", max_depth=1,
        )
        assert result.get("success") is not False
        folders = result.get("folders") or result.get("items") or []
        assert isinstance(folders, list)

    @pytest.mark.asyncio
    async def test_find_folder_inbox(self, live_client):
        from src.tools.folder_tools import FindFolderTool

        result = await FindFolderTool(live_client).execute(query="Inbox")
        assert result.get("success") is not False


# --------------------------------------------------------------------- #
# Search                                                                #
# --------------------------------------------------------------------- #

class TestSearch:
    @pytest.mark.asyncio
    async def test_search_by_conversation_on_top_message(self, live_client):
        from src.tools.search_tools import SearchByConversationTool

        top = await fetch_top_inbox_message(live_client)
        if top is None:
            pytest.skip("Inbox is empty.")

        conv_id = top.get("conversation_id")
        msg_id = top.get("message_id") or top.get("id")
        kwargs = {}
        if conv_id:
            kwargs["conversation_id"] = conv_id
        elif msg_id:
            kwargs["message_id"] = msg_id
        else:
            pytest.skip("Top message has neither conversation_id nor message_id.")

        result = await SearchByConversationTool(live_client).execute(**kwargs)
        assert result.get("success") is not False


# --------------------------------------------------------------------- #
# Contacts                                                              #
# --------------------------------------------------------------------- #

class TestContacts:
    @pytest.mark.asyncio
    async def test_find_person_self(self, live_client):
        from src.tools.contact_intelligence_tools import FindPersonTool

        local_part = live_client.config.ews_email.split("@", 1)[0]
        result = await FindPersonTool(live_client).execute(query=local_part)
        assert result.get("success") is not False


# --------------------------------------------------------------------- #
# Out-of-Office                                                         #
# --------------------------------------------------------------------- #

class TestOOF:
    @pytest.mark.asyncio
    async def test_get_oof_settings(self, live_client):
        from src.tools.oof_tools import OofSettingsTool

        result = await OofSettingsTool(live_client).execute(action="get")
        assert result.get("success") is not False
        assert "state" in result or "oof" in result or "settings" in result


# --------------------------------------------------------------------- #
# Tasks                                                                 #
# --------------------------------------------------------------------- #

class TestTasks:
    @pytest.mark.asyncio
    async def test_get_tasks(self, live_client):
        from src.tools.task_tools import GetTasksTool

        result = await GetTasksTool(live_client).execute()
        assert result.get("success") is not False


# --------------------------------------------------------------------- #
# AI                                                                    #
# --------------------------------------------------------------------- #

class TestAI:
    pytestmark = pytest.mark.skipif(not LIVE_AI_ENABLED, reason=SKIP_REASON_AI)

    @pytest.mark.asyncio
    async def test_semantic_search_emails(self, live_client):
        from src.tools.ai_tools import SemanticSearchEmailsTool

        result = await SemanticSearchEmailsTool(live_client).execute(
            query="any open requests from leadership",
            folder="inbox",
            max_results=5,
            threshold=0.0,
        )
        assert result.get("success") is not False
        items = result.get("items") or result.get("emails") or []
        assert isinstance(items, list)

    @pytest.mark.asyncio
    async def test_classify_email_top_message(self, live_client):
        from src.tools.ai_tools import ClassifyEmailTool

        top = await fetch_top_inbox_message(live_client)
        if top is None:
            pytest.skip("Inbox is empty.")
        msg_id = top.get("message_id") or top.get("id")

        result = await ClassifyEmailTool(live_client).execute(message_id=msg_id)
        assert result.get("success") is not False

    @pytest.mark.asyncio
    async def test_summarize_email_top_message(self, live_client):
        from src.tools.ai_tools import SummarizeEmailTool

        top = await fetch_top_inbox_message(live_client)
        if top is None:
            pytest.skip("Inbox is empty.")
        msg_id = top.get("message_id") or top.get("id")

        result = await SummarizeEmailTool(live_client).execute(message_id=msg_id)
        assert result.get("success") is not False
        assert result.get("summary") or result.get("text") or result.get("message")

    @pytest.mark.asyncio
    async def test_generate_briefing_today(self, live_client):
        from src.tools.briefing_tools import GenerateBriefingTool

        result = await GenerateBriefingTool(live_client).execute(
            scope="today", max_per_section=5,
        )
        assert result.get("success") is not False
        for key in ("inbox_delta", "meetings", "commitments", "overdue_tasks"):
            assert key in result, f"briefing missing section {key!r}: keys={list(result)}"

    @pytest.mark.asyncio
    async def test_extract_commitments_top_message(self, live_client):
        from src.tools.commitment_tools import ExtractCommitmentsTool

        top = await fetch_top_inbox_message(live_client)
        if top is None:
            pytest.skip("Inbox is empty.")
        msg_id = top.get("message_id") or top.get("id")

        result = await ExtractCommitmentsTool(live_client).execute(
            message_id=msg_id, save=False, max_extractions=5,
        )
        assert result.get("success") is not False
        commitments = result.get("commitments") or result.get("items") or []
        assert isinstance(commitments, list)


# --------------------------------------------------------------------- #
# Agent secretary (local memory store — safe even on prod)              #
# --------------------------------------------------------------------- #

class TestAgentSecretaryLive:
    @pytest.mark.asyncio
    async def test_memory_set_get_delete_roundtrip(self, live_client):
        from src.tools.memory_tools import (
            MemoryDeleteTool,
            MemoryGetTool,
            MemorySetTool,
        )

        key = f"sit:{datetime.utcnow():%Y%m%d%H%M%S}"
        try:
            set_result = await MemorySetTool(live_client).execute(
                key=key, value={"hello": "world"}, ttl_seconds=300,
            )
            assert set_result.get("success") is True

            got = await MemoryGetTool(live_client).execute(key=key)
            assert got.get("success") is True
            assert (got.get("value") or {}).get("hello") == "world"
        finally:
            await MemoryDeleteTool(live_client).execute(key=key)

    @pytest.mark.asyncio
    async def test_track_and_resolve_commitment(self, live_client):
        from src.tools.commitment_tools import (
            ListCommitmentsTool,
            ResolveCommitmentTool,
            TrackCommitmentTool,
        )

        track = await TrackCommitmentTool(live_client).execute(
            description="SIT smoke commitment — auto-resolved",
            owner="me",
            counterparty_email="sit@example.invalid",
        )
        assert track.get("success") is True
        cid = (
            track.get("commitment", {}).get("id")
            or track.get("id")
            or track.get("commitment_id")
        )
        assert cid, f"TrackCommitmentTool returned no id: {track}"

        try:
            listed = await ListCommitmentsTool(live_client).execute(status="open")
            assert listed.get("success") is not False
        finally:
            await ResolveCommitmentTool(live_client).execute(
                commitment_id=cid, resolution="completed",
            )

    @pytest.mark.asyncio
    @pytest.mark.skipif(not LIVE_AI_ENABLED, reason=SKIP_REASON_AI)
    async def test_build_voice_profile(self, live_client):
        from src.tools.voice_tools import BuildVoiceProfileTool

        result = await BuildVoiceProfileTool(live_client).execute(max_samples=10)
        assert result.get("success") is not False
