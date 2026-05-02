"""MCP Tools for EWS operations."""

from .email_tools import SendEmailTool, ReadEmailsTool, SearchEmailsTool, GetEmailDetailsTool, GetEmailsBulkTool, DeleteEmailTool, MoveEmailTool, UpdateEmailTool, CopyEmailTool, ReplyEmailTool, ForwardEmailTool
from .email_tools_draft import CreateDraftTool, CreateReplyDraftTool, CreateForwardDraftTool
from .calendar_tools import CreateAppointmentTool, GetCalendarTool, UpdateAppointmentTool, DeleteAppointmentTool, RespondToMeetingTool, CheckAvailabilityTool, FindMeetingTimesTool
from .contact_tools import CreateContactTool, UpdateContactTool, DeleteContactTool
from .task_tools import CreateTaskTool, GetTasksTool, UpdateTaskTool, CompleteTaskTool, DeleteTaskTool
from .attachment_tools import ListAttachmentsTool, DownloadAttachmentTool, AddAttachmentTool, DeleteAttachmentTool, ReadAttachmentTool, GetEmailMimeTool, AttachEmailToDraftTool
from .search_tools import SearchByConversationTool
from .folder_tools import ListFoldersTool, FindFolderTool, ManageFolderTool
from .oof_tools import OofSettingsTool
from .ai_tools import SemanticSearchEmailsTool
from .contact_intelligence_tools import FindPersonTool, AnalyzeContactsTool

# Agent-secretary tools (memory, commitments, approvals, rules, voice, OOF
# policy, briefing, meeting prep).
from .memory_tools import MemorySetTool, MemoryGetTool, MemoryListTool, MemoryDeleteTool
from .commitment_tools import (
    TrackCommitmentTool,
    ListCommitmentsTool,
    ResolveCommitmentTool,
)
from .approval_tools import (
    SubmitForApprovalTool,
    ListPendingApprovalsTool,
    ApproveTool,
    RejectTool,
    ExecuteApprovedActionTool,
)
from .voice_tools import GetVoiceProfileTool
from .rule_tools import (
    RuleCreateTool,
    RuleListTool,
    RuleDeleteTool,
    RuleSimulateTool,
    EvaluateRulesOnMessageTool,
)
from .oof_policy_tools import (
    ConfigureOOFPolicyTool,
    GetOOFPolicyTool,
    ApplyOOFPolicyTool,
)
from .briefing_tools import GenerateBriefingTool
from .meeting_prep_tools import PrepareMeetingTool

__all__ = [
    # Email tools (11)
    "CreateDraftTool",
    "CreateReplyDraftTool",
    "CreateForwardDraftTool",
    "SendEmailTool",
    "ReadEmailsTool",
    "SearchEmailsTool",
    "GetEmailDetailsTool",
    "GetEmailsBulkTool",
    "DeleteEmailTool",
    "MoveEmailTool",
    "UpdateEmailTool",
    "CopyEmailTool",
    "ReplyEmailTool",
    "ForwardEmailTool",
    # Calendar tools (7)
    "CreateAppointmentTool",
    "GetCalendarTool",
    "UpdateAppointmentTool",
    "DeleteAppointmentTool",
    "RespondToMeetingTool",
    "CheckAvailabilityTool",
    "FindMeetingTimesTool",
    # Contact tools (3)
    "CreateContactTool",
    "UpdateContactTool",
    "DeleteContactTool",
    # Task tools (5)
    "CreateTaskTool",
    "GetTasksTool",
    "UpdateTaskTool",
    "CompleteTaskTool",
    "DeleteTaskTool",
    # Attachment tools (7)
    "ListAttachmentsTool",
    "DownloadAttachmentTool",
    "AddAttachmentTool",
    "DeleteAttachmentTool",
    "ReadAttachmentTool",
    "GetEmailMimeTool",
    "AttachEmailToDraftTool",
    # Search tools (1)
    "SearchByConversationTool",
    # Folder tools (3)
    "ListFoldersTool",
    "FindFolderTool",
    "ManageFolderTool",
    # Out-of-Office tools (1)
    "OofSettingsTool",
    # AI tools (1 in v4 — semantic only; classification/summarisation/
    # reply-suggest moved to skill side)
    "SemanticSearchEmailsTool",
    # Contact Intelligence tools (2)
    "FindPersonTool",
    "AnalyzeContactsTool",
    # --- Agent-secretary tools ---
    # Memory (4)
    "MemorySetTool",
    "MemoryGetTool",
    "MemoryListTool",
    "MemoryDeleteTool",
    # Commitments (3 in v4 — manual CRUD only; auto-extraction moved to skill)
    "TrackCommitmentTool",
    "ListCommitmentsTool",
    "ResolveCommitmentTool",
    # Approvals (5)
    "SubmitForApprovalTool",
    "ListPendingApprovalsTool",
    "ApproveTool",
    "RejectTool",
    "ExecuteApprovedActionTool",
    # Voice profile (1 in v4 — read-only; tone analysis moved to skill side)
    "GetVoiceProfileTool",
    # Rule engine (5)
    "RuleCreateTool",
    "RuleListTool",
    "RuleDeleteTool",
    "RuleSimulateTool",
    "EvaluateRulesOnMessageTool",
    # OOF policy (3)
    "ConfigureOOFPolicyTool",
    "GetOOFPolicyTool",
    "ApplyOOFPolicyTool",
    # Compound tools (2)
    "GenerateBriefingTool",
    "PrepareMeetingTool",
]
