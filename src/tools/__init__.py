"""MCP Tools for EWS operations."""

from .email_tools import SendEmailTool, ReadEmailsTool, SearchEmailsTool, GetEmailDetailsTool, DeleteEmailTool, MoveEmailTool, UpdateEmailTool, CopyEmailTool, ReplyEmailTool, ForwardEmailTool
from .calendar_tools import CreateAppointmentTool, GetCalendarTool, UpdateAppointmentTool, DeleteAppointmentTool, RespondToMeetingTool, CheckAvailabilityTool, FindMeetingTimesTool
from .contact_tools import CreateContactTool, SearchContactsTool, GetContactsTool, UpdateContactTool, DeleteContactTool, ResolveNamesTool
from .task_tools import CreateTaskTool, GetTasksTool, UpdateTaskTool, CompleteTaskTool, DeleteTaskTool
from .attachment_tools import ListAttachmentsTool, DownloadAttachmentTool, AddAttachmentTool, DeleteAttachmentTool, ReadAttachmentTool
from .search_tools import AdvancedSearchTool, SearchByConversationTool, FullTextSearchTool
from .folder_tools import ListFoldersTool, CreateFolderTool, DeleteFolderTool, RenameFolderTool, MoveFolderTool
from .oof_tools import SetOOFSettingsTool, GetOOFSettingsTool
from .ai_tools import SemanticSearchEmailsTool, ClassifyEmailTool, SummarizeEmailTool, SuggestRepliesTool
from .contact_intelligence_tools import FindPersonTool, GetCommunicationHistoryTool, AnalyzeNetworkTool

__all__ = [
    # Email tools (10)
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
    # Contact tools (6)
    "CreateContactTool",
    "SearchContactsTool",
    "GetContactsTool",
    "UpdateContactTool",
    "DeleteContactTool",
    "ResolveNamesTool",
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
    # Search tools (3)
    "AdvancedSearchTool",
    "SearchByConversationTool",
    "FullTextSearchTool",
    # Folder tools (5)
    "ListFoldersTool",
    "CreateFolderTool",
    "DeleteFolderTool",
    "RenameFolderTool",
    "MoveFolderTool",
    # Out-of-Office tools (2)
    "SetOOFSettingsTool",
    "GetOOFSettingsTool",
    # AI tools (4)
    "SemanticSearchEmailsTool",
    "ClassifyEmailTool",
    "SummarizeEmailTool",
    "SuggestRepliesTool",
    # Contact Intelligence tools (3)
    "FindPersonTool",
    "GetCommunicationHistoryTool",
    "AnalyzeNetworkTool",
]
