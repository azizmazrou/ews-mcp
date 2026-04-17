"""Custom exceptions for EWS MCP Server."""


class EWSMCPException(Exception):
    """Base exception for EWS MCP Server."""
    pass


class AuthenticationError(EWSMCPException):
    """Authentication failed."""
    pass


class EWSConnectionError(EWSMCPException):
    """Connection to Exchange failed."""
    pass


# Deprecated alias. `ConnectionError` shadows the Python builtin of the same
# name, so `except ConnectionError` blocks in code that imported this name
# silently stopped matching real OS-level socket/HTTP errors. New code should
# use `EWSConnectionError`; this alias is retained for one release.
ConnectionError = EWSConnectionError  # noqa: A001


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


class EmbeddingError(EWSMCPException):
    """An AI embedding provider returned an error or malformed response.

    Raised by ``OpenAIEmbeddingProvider`` when the upstream endpoint
    (OpenAI, Ollama in OpenAI-compat mode, or any other compatible
    provider) replies with a non-2xx status, an ``{"error": ...}`` body,
    or a 2xx body that is missing the expected ``data`` array.
    """
    pass
