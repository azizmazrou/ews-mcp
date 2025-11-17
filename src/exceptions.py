"""Custom exceptions for EWS MCP Server."""


class EWSMCPException(Exception):
    """Base exception for EWS MCP Server."""
    pass


class AuthenticationError(EWSMCPException):
    """Authentication failed."""
    pass


class ConnectionError(EWSMCPException):
    """Connection to Exchange failed."""
    pass


class RateLimitError(EWSMCPException):
    """Rate limit exceeded."""
    pass


class ValidationError(EWSMCPException):
    """Input validation failed."""
    pass


class ToolExecutionError(EWSMCPException):
    """Tool execution failed."""
    pass


class ConfigurationError(EWSMCPException):
    """Configuration error."""
    pass
