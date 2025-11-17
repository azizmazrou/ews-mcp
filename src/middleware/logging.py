"""Structured logging configuration."""

import logging
import sys
from typing import Any, Dict


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structured logging.

    IMPORTANT: All logs go to stderr to keep stdout clean for JSON-RPC protocol.
    MCP protocol requires stdout to be reserved for JSON-RPC messages only.
    """
    # Set up root logger - MUST use stderr for MCP compatibility
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stderr)  # CRITICAL: Use stderr, not stdout
        ]
    )

    # Enable exchangelib debug logging to see SOAP requests (temporary for debugging)
    # TODO: Change back to WARNING after debugging Arabic GAL search issue
    logging.getLogger("exchangelib").setLevel(logging.DEBUG)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("requests_ntlm").setLevel(logging.WARNING)


class AuditLogger:
    """Audit logger for tracking operations."""

    def __init__(self):
        self.logger = logging.getLogger("audit")

    def log_operation(
        self,
        operation: str,
        user: str,
        success: bool,
        details: Dict[str, Any] = None
    ) -> None:
        """Log an operation for audit trail."""
        message = f"Operation: {operation} | User: {user} | Success: {success}"
        if details:
            message += f" | Details: {details}"

        if success:
            self.logger.info(message)
        else:
            self.logger.warning(message)
