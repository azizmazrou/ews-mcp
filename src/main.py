"""Main MCP Server implementation for Exchange Web Services."""

import asyncio
import logging
import os
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route

from .config import get_settings
from .auth import AuthHandler
from .ews_client import EWSClient
from .middleware.logging import setup_logging, AuditLogger
from .middleware.error_handler import ErrorHandler
from .middleware.rate_limiter import RateLimiter
from .exceptions import EWSMCPException
from .logging_system import get_logger

# Import all tool classes (up to 47 tools total: 43 base + 4 AI)
from .tools import (
    # Email tools (8)
    SendEmailTool, ReadEmailsTool, SearchEmailsTool, GetEmailDetailsTool,
    DeleteEmailTool, MoveEmailTool, UpdateEmailTool, CopyEmailTool,
    # Calendar tools (7)
    CreateAppointmentTool, GetCalendarTool, UpdateAppointmentTool,
    DeleteAppointmentTool, RespondToMeetingTool, CheckAvailabilityTool,
    FindMeetingTimesTool,
    # Contact tools (6)
    CreateContactTool, SearchContactsTool, GetContactsTool,
    UpdateContactTool, DeleteContactTool, ResolveNamesTool,
    # Task tools (5)
    CreateTaskTool, GetTasksTool, UpdateTaskTool,
    CompleteTaskTool, DeleteTaskTool,
    # Attachment tools (5)
    ListAttachmentsTool, DownloadAttachmentTool,
    AddAttachmentTool, DeleteAttachmentTool, ReadAttachmentTool,
    # Search tools (3)
    AdvancedSearchTool, SearchByConversationTool, FullTextSearchTool,
    # Folder tools (5)
    ListFoldersTool, CreateFolderTool, DeleteFolderTool,
    RenameFolderTool, MoveFolderTool,
    # Out-of-Office tools (2)
    SetOOFSettingsTool, GetOOFSettingsTool,
    # AI tools (4 - conditionally enabled)
    SemanticSearchEmailsTool, ClassifyEmailTool,
    SummarizeEmailTool, SuggestRepliesTool,
    # Contact Intelligence tools (3)
    FindPersonTool, GetCommunicationHistoryTool, AnalyzeNetworkTool
)


class EWSMCPServer:
    """MCP Server for Exchange Web Services with comprehensive logging."""

    def __init__(self):
        # Get settings (lazy loading)
        self.settings = get_settings()

        # Set timezone
        os.environ['TZ'] = self.settings.timezone
        try:
            import time
            time.tzset()
        except AttributeError:
            # tzset not available on Windows
            pass

        # Set up logging
        setup_logging(self.settings.log_level)
        self.logger = logging.getLogger(__name__)

        # Initialize comprehensive logging system
        self.log_manager = get_logger()

        # Initialize server
        self.server = Server(self.settings.mcp_server_name)

        # Initialize components
        self.auth_handler = AuthHandler(self.settings)
        self.ews_client = EWSClient(self.settings, self.auth_handler)
        self.error_handler = ErrorHandler()
        self.audit_logger = AuditLogger()

        # Rate limiter (if enabled)
        self.rate_limiter = None
        if self.settings.rate_limit_enabled:
            self.rate_limiter = RateLimiter(self.settings.rate_limit_requests_per_minute)

        # Tool registry
        self.tools = {}

        # Register handlers
        self._register_handlers()

        # Log server initialization
        self.log_manager.log_activity(
            level="INFO",
            module="main",
            action="SERVER_INIT",
            data={
                "version": "2.1.0",
                "user": self.settings.ews_email,
                "auth_type": self.settings.ews_auth_type,
                "server_url": self.settings.ews_server_url or "autodiscover"
            },
            result={"status": "initializing"},
            context={
                "timezone": self.settings.timezone,
                "transport": self.settings.mcp_transport,
                "features": {
                    "email": self.settings.enable_email,
                    "calendar": self.settings.enable_calendar,
                    "contacts": self.settings.enable_contacts,
                    "tasks": self.settings.enable_tasks
                }
            }
        )

    def _register_handlers(self):
        """Register MCP protocol handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """List all available tools."""
            return [
                Tool(
                    name=tool.get_schema()["name"],
                    description=tool.get_schema()["description"],
                    inputSchema=tool.get_schema()["inputSchema"]
                )
                for tool in self.tools.values()
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            """Execute a tool."""
            # Check rate limit
            if self.rate_limiter:
                try:
                    self.rate_limiter.check_and_raise()
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=str(self.error_handler.handle_exception(e, f"Rate limit"))
                    )]

            # Check if tool exists
            if name not in self.tools:
                error_response = {
                    "success": False,
                    "error": f"Unknown tool: {name}",
                    "available_tools": list(self.tools.keys())
                }
                return [TextContent(
                    type="text",
                    text=str(error_response)
                )]

            # Execute tool
            tool = self.tools[name]
            self.logger.info(f"Executing tool: {name}")

            try:
                result = await tool.safe_execute(**arguments)

                # Audit log
                if self.settings.enable_audit_log:
                    self.audit_logger.log_operation(
                        operation=name,
                        user=self.settings.ews_email,
                        success=result.get("success", False),
                        details={"arguments": arguments}
                    )

                return [TextContent(
                    type="text",
                    text=str(result)
                )]

            except Exception as e:
                self.logger.exception(f"Tool execution failed: {name}")
                error_response = self.error_handler.handle_exception(e, f"Tool: {name}")
                return [TextContent(
                    type="text",
                    text=str(error_response)
                )]

    def register_tools(self):
        """Register all enabled tools (43 base tools, up to 47 with AI)."""
        tool_classes = []

        # Email tools (8 tools)
        if self.settings.enable_email:
            tool_classes.extend([
                SendEmailTool,
                ReadEmailsTool,
                SearchEmailsTool,
                GetEmailDetailsTool,
                DeleteEmailTool,
                MoveEmailTool,
                UpdateEmailTool,
                CopyEmailTool
            ])
            self.logger.info("Email tools enabled (8 tools)")

        # Attachment tools (5 tools - email-related)
        if self.settings.enable_email:
            tool_classes.extend([
                ListAttachmentsTool,
                DownloadAttachmentTool,
                AddAttachmentTool,
                DeleteAttachmentTool,
                ReadAttachmentTool
            ])
            self.logger.info("Attachment tools enabled (5 tools)")

        # Calendar tools (7 tools)
        if self.settings.enable_calendar:
            tool_classes.extend([
                CreateAppointmentTool,
                GetCalendarTool,
                UpdateAppointmentTool,
                DeleteAppointmentTool,
                RespondToMeetingTool,
                CheckAvailabilityTool,
                FindMeetingTimesTool
            ])
            self.logger.info("Calendar tools enabled (7 tools)")

        # Contact tools (6 tools)
        if self.settings.enable_contacts:
            tool_classes.extend([
                CreateContactTool,
                SearchContactsTool,
                GetContactsTool,
                UpdateContactTool,
                DeleteContactTool,
                ResolveNamesTool
            ])
            self.logger.info("Contact tools enabled (6 tools)")

        # Contact Intelligence tools (3 tools - always enabled when contacts are enabled)
        if self.settings.enable_contacts:
            tool_classes.extend([
                FindPersonTool,
                GetCommunicationHistoryTool,
                AnalyzeNetworkTool
            ])
            self.logger.info("Contact Intelligence tools enabled (3 tools)")

        # Task tools (5 tools)
        if self.settings.enable_tasks:
            tool_classes.extend([
                CreateTaskTool,
                GetTasksTool,
                UpdateTaskTool,
                CompleteTaskTool,
                DeleteTaskTool
            ])
            self.logger.info("Task tools enabled (5 tools)")

        # Search tools (3 tools - always enabled)
        tool_classes.extend([
            AdvancedSearchTool,
            SearchByConversationTool,
            FullTextSearchTool
        ])
        self.logger.info("Search tools enabled (3 tools)")

        # Folder tools (5 tools - always enabled)
        tool_classes.extend([
            ListFoldersTool,
            CreateFolderTool,
            DeleteFolderTool,
            RenameFolderTool,
            MoveFolderTool
        ])
        self.logger.info("Folder tools enabled (5 tools)")

        # Out-of-Office tools (2 tools - always enabled)
        tool_classes.extend([
            SetOOFSettingsTool,
            GetOOFSettingsTool
        ])
        self.logger.info("Out-of-Office tools enabled (2 tools)")

        # AI tools (4 tools - conditionally enabled)
        if self.settings.enable_ai:
            ai_tools = []
            if self.settings.enable_semantic_search:
                ai_tools.append(SemanticSearchEmailsTool)
            if self.settings.enable_email_classification:
                ai_tools.append(ClassifyEmailTool)
            if self.settings.enable_email_summarization:
                ai_tools.append(SummarizeEmailTool)
            if self.settings.enable_smart_replies:
                ai_tools.append(SuggestRepliesTool)

            tool_classes.extend(ai_tools)
            self.logger.info(f"AI tools enabled ({len(ai_tools)} tools)")

        # Instantiate and register tools
        for tool_class in tool_classes:
            tool = tool_class(self.ews_client)
            schema = tool.get_schema()
            self.tools[schema["name"]] = tool

        self.logger.info(f"Registered {len(self.tools)} tools: {', '.join(self.tools.keys())}")

    async def run(self):
        """Run the MCP server with comprehensive logging."""
        try:
            self.logger.info(f"Starting {self.settings.mcp_server_name}")
            self.logger.info(f"Server: {self.settings.ews_server_url or 'autodiscover'}")
            self.logger.info(f"User: {self.settings.ews_email}")
            self.logger.info(f"Auth: {self.settings.ews_auth_type}")

            # Test connection
            self.logger.info("Testing Exchange connection...")
            if not self.ews_client.test_connection():
                self.logger.error("Failed to connect to Exchange server")
                self.logger.error("Please check your configuration and credentials")

                # Log connection failure
                self.log_manager.log_activity(
                    level="ERROR",
                    module="main",
                    action="CONNECTION_FAILED",
                    data={"server": self.settings.ews_server_url or "autodiscover"},
                    result={"status": "failed"},
                    context={"auth_type": self.settings.ews_auth_type}
                )
                return

            self.logger.info("✓ Successfully connected to Exchange")

            # Log successful connection
            self.log_manager.log_activity(
                level="INFO",
                module="main",
                action="CONNECTION_SUCCESS",
                data={"server": self.settings.ews_server_url or "autodiscover"},
                result={"status": "connected"},
                context={"auth_type": self.settings.ews_auth_type}
            )

            # Register tools
            self.register_tools()

            # Log server ready
            self.log_manager.log_activity(
                level="INFO",
                module="main",
                action="SERVER_READY",
                data={"registered_tools": len(self.tools)},
                result={"status": "ready"},
                context={
                    "tool_list": list(self.tools.keys()),
                    "transport": self.settings.mcp_transport
                }
            )

            # Start server based on transport type
            if self.settings.mcp_transport == "stdio":
                self.logger.info(f"Server ready - listening on stdio")
                async with stdio_server() as (read_stream, write_stream):
                    await self.server.run(
                        read_stream,
                        write_stream,
                        self.server.create_initialization_options()
                    )
            elif self.settings.mcp_transport == "sse":
                self.logger.info(f"Server ready - listening on http://{self.settings.mcp_host}:{self.settings.mcp_port}")
                await self.run_sse()

        except KeyboardInterrupt:
            self.logger.info("Shutting down...")
        except Exception as e:
            self.logger.exception(f"Server error: {e}")
            raise
        finally:
            # Cleanup
            self.ews_client.close()
            self.logger.info("Server stopped")

    async def run_sse(self):
        """Run the MCP server with SSE (HTTP) transport."""
        from starlette.responses import Response
        import uvicorn

        sse = SseServerTransport("/messages")

        async def handle_sse(request):
            """Handle SSE connection endpoint."""
            async with sse.connect_sse(
                request.scope,
                request.receive,
                request._send,
            ) as streams:
                await self.server.run(
                    streams[0],
                    streams[1],
                    self.server.create_initialization_options(),
                )

        async def handle_messages(request):
            """Handle POST messages endpoint."""
            await sse.handle_post_message(request.scope, request.receive, request._send)

        # Create Starlette app
        app = Starlette(
            debug=True,
            routes=[
                Route("/sse", endpoint=handle_sse),
                Route("/messages", endpoint=handle_messages, methods=["POST"]),
            ],
        )

        # Run with uvicorn
        config = uvicorn.Config(
            app,
            host=self.settings.mcp_host,
            port=self.settings.mcp_port,
            log_level=self.settings.log_level.lower(),
        )
        server = uvicorn.Server(config)
        await server.serve()


def main():
    """Entry point."""
    try:
        server = EWSMCPServer()
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
