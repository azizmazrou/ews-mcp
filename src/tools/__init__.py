"""MCP Tools for EWS operations."""

from .email_tools import SendEmailTool, ReadEmailsTool, SearchEmailsTool, GetEmailDetailsTool, DeleteEmailTool, MoveEmailTool, UpdateEmailTool, CopyEmailTool, ReplyEmailTool, ForwardEmailTool
from .email_tools_draft import CreateDraftTool, CreateReplyDraftTool
from .calendar_tools import CreateAppointmentTool, GetCalendarTool, UpdateAppointmentTool, DeleteAppointmentTool, RespondToMeetingTool, CheckAvailabilityTool, FindMeetingTimesTool
from .contact_tools import CreateContactTool, UpdateContactTool, DeleteContactTool
from .task_tools import CreateTaskTool, GetTasksTool, UpdateTaskTool, CompleteTaskTool, DeleteTaskTool
from .attachment_tools import ListAttachmentsTool, DownloadAttachmentTool, AddAttachmentTool, DeleteAttachmentTool, ReadAttachmentTool
from .search_tools import SearchByConversationTool
from .folder_tools import ListFoldersTool, FindFolderTool, ManageFolderTool
from .oof_tools import OofSettingsTool
from .ai_tools import SemanticSearchEmailsTool, ClassifyEmailTool, SummarizeEmailTool, SuggestRepliesTool
from .contact_intelligence_tools import FindPersonTool, AnalyzeContactsTool

__all__ = [
    # Email tools (11)
    "CreateDraftTool",
    "CreateReplyDraftTool",
    "SendEmailTool",
    "ReadEmailsTool",
    "SearchEmailsTool",
    "GetEmailDetailsTool",
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
    # Attachment tools (5)
    "ListAttachmentsTool",
    "DownloadAttachmentTool",
    "AddAttachmentTool",
    "DeleteAttachmentTool",
    "ReadAttachmentTool",
    # Search tools (1)
    "SearchByConversationTool",
    # Folder tools (3)
    "ListFoldersTool",
    "FindFolderTool",
    "ManageFolderTool",
    # Out-of-Office tools (1)
    "OofSettingsTool",
    # AI tools (4)
    "SemanticSearchEmailsTool",
    "ClassifyEmailTool",
    "SummarizeEmailTool",
    "SuggestRepliesTool",
    # Contact Intelligence tools (2)
    "FindPersonTool",
    "AnalyzeContactsTool",
]
