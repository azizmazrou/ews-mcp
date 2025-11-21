"""OpenAPI adapter for MCP tools - provides REST API compatibility."""

import json
from typing import Any, Dict, List, Optional
from datetime import datetime


class OpenAPIAdapter:
    """Converts MCP tools to OpenAPI/REST endpoints."""

    def __init__(self, server, tools: Dict[str, Any], settings: Optional[Any] = None):
        """Initialize OpenAPI adapter.

        Args:
            server: MCP server instance
            tools: Dictionary of tool name -> tool instance
            settings: Application settings (optional, for URL configuration)
        """
        self.server = server
        self.tools = tools
        self.settings = settings

    def generate_openapi_schema(self) -> Dict[str, Any]:
        """Generate OpenAPI 3.0 schema from MCP tools."""
        paths = {}

        for tool_name, tool in self.tools.items():
            schema = tool.get_schema()

            # Convert MCP tool schema to OpenAPI path
            path = f"/api/tools/{tool_name}"
            paths[path] = {
                "post": {
                    "operationId": tool_name,
                    "summary": schema["description"],
                    "description": schema["description"],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": self._convert_input_schema(schema["inputSchema"])
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Successful response",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "success": {"type": "boolean"},
                                            "data": {"type": "object"},
                                            "message": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        },
                        "400": {
                            "description": "Bad request",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "error": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        },
                        "404": {
                            "description": "Tool not found",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "error": {"type": "string"},
                                            "available_tools": {"type": "array", "items": {"type": "string"}}
                                        }
                                    }
                                }
                            }
                        },
                        "500": {
                            "description": "Internal server error",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "error": {"type": "string"},
                                            "tool": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "tags": [self._get_tool_category(tool_name)]
                }
            }

        # Get server URLs from settings or use defaults
        if self.settings:
            servers = self.settings.get_api_base_urls()
            api_title = self.settings.api_title
            api_description = self.settings.api_description
            api_version = self.settings.api_version
        else:
            # Fallback defaults if no settings provided
            servers = [
                {
                    "url": "http://localhost:8000",
                    "description": "Local development server"
                },
                {
                    "url": "http://ews-mcp:8000",
                    "description": "Docker container (internal network)"
                }
            ]
            api_title = "Exchange Web Services (EWS) MCP API"
            api_description = "REST API for Exchange operations via Model Context Protocol"
            api_version = "3.0.0"

        return {
            "openapi": "3.0.0",
            "info": {
                "title": api_title,
                "description": f"{api_description}. "
                              "This API exposes all EWS MCP tools as REST endpoints, eliminating "
                              "the need for external OpenAPI adapters like MCPO.",
                "version": api_version,
                "contact": {
                    "name": "EWS MCP Server",
                    "url": "https://github.com/azizmazrou/ews-mcp"
                }
            },
            "servers": servers,
            "paths": paths,
            "tags": [
                {"name": "Email", "description": "Email operations - send, read, search, update, delete"},
                {"name": "Calendar", "description": "Calendar operations - appointments, meetings, availability"},
                {"name": "Contacts", "description": "Contact management - create, search, update"},
                {"name": "Tasks", "description": "Task operations - create, update, complete"},
                {"name": "Attachments", "description": "File attachment operations"},
                {"name": "Search", "description": "Advanced search and full-text search"},
                {"name": "Folders", "description": "Folder management operations"},
                {"name": "Out-of-Office", "description": "Out-of-office automatic replies"}
            ],
            "components": {
                "securitySchemes": {
                    "basicAuth": {
                        "type": "http",
                        "scheme": "basic",
                        "description": "Exchange credentials passed via environment variables"
                    }
                }
            },
            "security": [
                {"basicAuth": []}
            ]
        }

    def _convert_input_schema(self, mcp_schema: Dict[str, Any]) -> Dict[str, Any]:
        """Convert MCP input schema to OpenAPI schema.

        Args:
            mcp_schema: MCP JSON Schema input schema

        Returns:
            OpenAPI-compatible schema
        """
        # MCP schemas are already JSON Schema compatible
        # Just ensure we have the right structure
        if not mcp_schema:
            return {"type": "object", "properties": {}}
        return mcp_schema

    def _get_tool_category(self, tool_name: str) -> str:
        """Determine tool category from tool name.

        Args:
            tool_name: Name of the tool

        Returns:
            Category name for grouping in OpenAPI docs
        """
        # Email tools
        if any(x in tool_name for x in ["email", "send", "read", "search_emails", "move_email", "delete_email", "update_email", "copy_email", "get_email_details"]):
            return "Email"
        # Calendar tools
        elif any(x in tool_name for x in ["appointment", "calendar", "meeting", "availability", "respond_to_meeting", "find_meeting_times"]):
            return "Calendar"
        # Contact tools
        elif any(x in tool_name for x in ["contact", "person", "resolve_names", "search_contacts", "get_contacts"]):
            return "Contacts"
        # Task tools
        elif any(x in tool_name for x in ["task", "complete"]):
            return "Tasks"
        # Attachment tools
        elif any(x in tool_name for x in ["attachment", "download", "upload", "read_attachment"]):
            return "Attachments"
        # Search tools
        elif any(x in tool_name for x in ["advanced_search", "conversation", "full_text"]):
            return "Search"
        # Folder tools
        elif any(x in tool_name for x in ["folder", "rename", "move_folder", "list_folders"]):
            return "Folders"
        # Out-of-Office tools
        elif any(x in tool_name for x in ["oof", "out_of_office"]):
            return "Out-of-Office"
        return "Other"

    async def handle_rest_request(self, tool_name: str, body: bytes) -> Dict[str, Any]:
        """Handle REST API request for a tool.

        Args:
            tool_name: Name of the tool to execute
            body: Request body bytes

        Returns:
            Response dictionary with status code
        """
        try:
            # Parse request body
            try:
                if body:
                    arguments = json.loads(body.decode('utf-8'))
                else:
                    arguments = {}
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                return {
                    "error": f"Invalid JSON in request body: {str(e)}",
                    "status": 400
                }

            # Check if tool exists
            if tool_name not in self.tools:
                return {
                    "error": f"Tool '{tool_name}' not found",
                    "available_tools": list(self.tools.keys()),
                    "status": 404
                }

            # Execute tool
            tool = self.tools[tool_name]
            result = await tool.safe_execute(**arguments)

            # Return successful response
            return {
                "success": result.get("success", False),
                "data": result,
                "message": result.get("message", ""),
                "status": 200
            }

        except Exception as e:
            # Return error response
            return {
                "error": f"Tool execution failed: {str(e)}",
                "tool": tool_name,
                "status": 500
            }
