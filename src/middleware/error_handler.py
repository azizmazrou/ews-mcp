"""Centralized error handling."""

import logging
from typing import Any, Dict
from ..exceptions import (
    EWSMCPException,
    AuthenticationError,
    ConnectionError,
    RateLimitError,
    ValidationError,
    ToolExecutionError
)


class ErrorHandler:
    """Centralized error handling and response formatting."""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def handle_exception(self, e: Exception, context: str = "") -> Dict[str, Any]:
        """Convert exceptions to error responses."""
        error_msg = f"{context}: {str(e)}" if context else str(e)

        # Log based on error type
        if isinstance(e, (AuthenticationError, ConnectionError)):
            self.logger.error(f"Critical error: {error_msg}")
        elif isinstance(e, (ValidationError, RateLimitError)):
            self.logger.warning(f"User error: {error_msg}")
        elif isinstance(e, ToolExecutionError):
            self.logger.error(f"Tool execution error: {error_msg}")
        else:
            self.logger.exception(f"Unexpected error: {error_msg}")

        # Map to error response
        return {
            "success": False,
            "error": error_msg,
            "error_type": type(e).__name__,
            "is_retryable": self._is_retryable(e)
        }

    def _is_retryable(self, e: Exception) -> bool:
        """Determine if error is retryable."""
        retryable_types = (ConnectionError, RateLimitError)
        return isinstance(e, retryable_types)
