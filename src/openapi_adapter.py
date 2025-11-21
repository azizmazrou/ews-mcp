"""OpenAPI adapter for MCP tools - provides REST API compatibility."""

import json
from typing import Any, Dict, List
from datetime import datetime


class OpenAPIAdapter:
    """Converts MCP tools to OpenAPI/REST endpoints."""

    def __init__(self, server, tools: Dict[str, Any]):
        """Initialize OpenAPI adapter.

        Args:
            server: MCP server instance
            tools: Dictionary of tool name -> tool instance
        """
        self.server = server
        self.tools = tools

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
                        "500": {
                            "description": "Internal server error",
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
                        }
                    },
                    "tags": [self._get_tool_category(tool_name)]
                }
            }

        return {
            "openapi": "3.0.0",
            "info": {
                "title": "Exchange Web Services (EWS) MCP API",
                "description": "REST API for Exchange operations via Model Context Protocol",
                "version": "3.0.0",
                "contact": {
                    "name": "EWS MCP Server",
                    "url": "https://github.com/azizmazrou/ews-mcp"
                }
            },
            "servers": [
                {
                    "url": "http://localhost:8000",
                    "description": "Local development server"
                }
            ],
            "paths": paths,
            "tags": [
                {"name": "Email", "description": "Email operations"},
                {"name": "Calendar", "description": "Calendar operations"},
                {"name": "Contacts", "description": "Contact operations"},
                {"name": "Tasks", "description": "Task operations"},
                {"name": "Attachments", "description": "Attachment operations"},
                {"name": "Search", "description": "Search operations"},
                {"name": "Folders", "description": "Folder operations"},
                {"name": "Out-of-Office", "description": "Out-of-office settings"}
            ]
        }

    def _convert_input_schema(self, mcp_schema: Dict[str, Any]) -> Dict[str, Any]:
        """Convert MCP input schema to OpenAPI schema."""
        # MCP schemas are already JSON Schema compatible
        return mcp_schema

    def _get_tool_category(self, tool_name: str) -> str:
        """Determine tool category from tool name."""
        if any(x in tool_name for x in ["email", "send", "read", "search_email", "move_email", "delete_email", "update_email", "copy_email"]):
            return "Email"
        elif any(x in tool_name for x in ["appointment", "calendar", "meeting", "availability"]):
            return "Calendar"
        elif any(x in tool_name for x in ["contact", "person", "communication", "network"]):
            return "Contacts"
        elif any(x in tool_name for x in ["task", "complete"]):
            return "Tasks"
        elif any(x in tool_name for x in ["attachment", "download", "upload"]):
            return "Attachments"
        elif any(x in tool_name for x in ["search", "conversation", "full_text"]):
            return "Search"
        elif any(x in tool_name for x in ["folder", "rename", "move_folder"]):
            return "Folders"
        elif any(x in tool_name for x in ["oof", "out_of_office"]):
            return "Out-of-Office"
        return "Other"

    async def handle_rest_request(self, tool_name: str, body: bytes) -> Dict[str, Any]:
        """Handle REST API request for a tool.

        Args:
            tool_name: Name of the tool to execute
            body: Request body bytes

        Returns:
            Response dictionary
        """
        try:
            # Parse request body
            try:
                arguments = json.loads(body.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                return {
                    "error": f"Invalid JSON in request body: {e}",
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

            return {
                "success": result.get("success", False),
                "data": result,
                "message": result.get("message", ""),
                "status": 200
            }

        except Exception as e:
            return {
                "error": f"Tool execution failed: {str(e)}",
                "tool": tool_name,
                "status": 500
            }
