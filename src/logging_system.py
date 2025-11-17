"""
Comprehensive logging system for EWS MCP Server.
Supports multiple log formats and destinations for debugging, monitoring, and AI analysis.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
import uuid


class LogManager:
    """Central logging management for EWS MCP Server."""

    def __init__(self, log_dir: Path = Path("/app/logs")):
        """Initialize the logging system.

        Args:
            log_dir: Directory for log files (default: /app/logs)
        """
        self.log_dir = log_dir
        self.session_id = f"sess_{uuid.uuid4().hex[:8]}"
        self.setup_logging()

    def setup_logging(self):
        """Initialize all log files and handlers."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            logging.warning(f"Cannot create log directory {self.log_dir}: {e}")
            # Fall back to /tmp if /app/logs is not writable
            self.log_dir = Path("/tmp/ews_mcp_logs")
            self.log_dir.mkdir(parents=True, exist_ok=True)
            logging.info(f"Using fallback log directory: {self.log_dir}")

        # Create multiple log files
        self.activity_log = self.log_dir / "ews_mcp_activity.log"
        self.performance_log = self.log_dir / "ews_mcp_performance.log"
        self.error_log = self.log_dir / "ews_mcp_errors.log"
        self.audit_log = self.log_dir / "ews_mcp_audit.log"
        self.test_log = self.log_dir / "ews_mcp_test_results.log"

        # Conversation context (JSON, updated in real-time)
        self.context_file = self.log_dir / "analysis" / "conversation_context.json"
        try:
            self.context_file.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            logging.warning(f"Cannot create analysis directory {self.context_file.parent}: {e}")
            # Disable context file if we can't create the directory
            self.context_file = None

        # Initialize context
        self.conversation_context = {
            "session_id": self.session_id,
            "started_at": datetime.now().isoformat(),
            "interactions": [],
            "current_context": {},
            "people_mentioned": {},
            "user_patterns": {}
        }

        # Save initial context
        self._save_context()

    def _save_context(self):
        """Save conversation context to file."""
        if self.context_file is None:
            # Context file is disabled due to permissions
            return

        try:
            with open(self.context_file, 'w') as f:
                json.dump(self.conversation_context, f, indent=2)
        except Exception as e:
            # Don't let context saving errors crash the app
            logging.error(f"Failed to save context: {e}")

    def log_activity(self,
                    level: str,
                    module: str,
                    action: str,
                    data: Dict[str, Any],
                    result: Dict[str, Any],
                    context: Optional[Dict[str, Any]] = None):
        """Log activity in JSON Lines format.

        Args:
            level: Log level (INFO, WARNING, ERROR, CRITICAL)
            module: Module name (e.g., 'email_tools')
            action: Action being performed (e.g., 'SEND_EMAIL')
            data: Input data/parameters
            result: Result of the action
            context: Additional context information
        """
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "module": module,
            "session_id": self.session_id,
            "action": action,
            "data": self._sanitize_data(data),
            "result": result,
            "context": context or {}
        }

        # Write to activity log
        try:
            with open(self.activity_log, 'a') as f:
                f.write(json.dumps(log_entry) + '\n')
        except Exception as e:
            logging.error(f"Failed to write activity log: {e}")

        # Also write errors to dedicated error log
        if level in ["ERROR", "CRITICAL"]:
            try:
                with open(self.error_log, 'a') as f:
                    f.write(json.dumps(log_entry) + '\n')
            except Exception as e:
                logging.error(f"Failed to write error log: {e}")

    def log_performance(self, metric: str, **kwargs):
        """Log performance metrics.

        Args:
            metric: Metric name (e.g., 'api_call', 'database_query')
            **kwargs: Metric data (duration_ms, status, etc.)
        """
        perf_entry = {
            "timestamp": datetime.now().isoformat(),
            "metric": metric,
            "session_id": self.session_id,
            **kwargs
        }

        try:
            with open(self.performance_log, 'a') as f:
                f.write(json.dumps(perf_entry) + '\n')
        except Exception as e:
            logging.error(f"Failed to write performance log: {e}")

    def log_test_result(self,
                       test_suite: str,
                       test_case: str,
                       status: str,
                       duration_ms: int,
                       assertions: Dict[str, bool],
                       error: Optional[str] = None):
        """Log test execution results.

        Args:
            test_suite: Test suite name
            test_case: Test case identifier
            status: Test status (PASSED, FAILED, ERROR, SKIPPED)
            duration_ms: Test duration in milliseconds
            assertions: Dictionary of assertion results
            error: Error message if test failed
        """
        test_entry = {
            "timestamp": datetime.now().isoformat(),
            "test_suite": test_suite,
            "test_case": test_case,
            "status": status,
            "duration_ms": duration_ms,
            "assertions": assertions
        }

        if error:
            test_entry["error"] = error

        try:
            with open(self.test_log, 'a') as f:
                f.write(json.dumps(test_entry) + '\n')
        except Exception as e:
            logging.error(f"Failed to write test log: {e}")

    def update_conversation_context(self,
                                    user_input: str,
                                    agent_action: str,
                                    parameters: Dict[str, Any],
                                    result: Dict[str, Any],
                                    duration_ms: int):
        """Update conversation context for AI analysis.

        Args:
            user_input: User's request/input
            agent_action: Action performed by the agent
            parameters: Parameters used
            result: Result of the action
            duration_ms: Duration in milliseconds
        """
        interaction = {
            "timestamp": datetime.now().isoformat(),
            "user_input": user_input,
            "agent_action": agent_action,
            "parameters": self._sanitize_data(parameters),
            "result": result,
            "duration_ms": duration_ms
        }

        self.conversation_context["interactions"].append(interaction)
        self.conversation_context["last_activity"] = datetime.now().isoformat()

        # Keep only last 100 interactions to prevent file from growing too large
        if len(self.conversation_context["interactions"]) > 100:
            self.conversation_context["interactions"] = self.conversation_context["interactions"][-100:]

        # Save to file
        self._save_context()

    def log_audit(self,
                  user: str,
                  action: str,
                  resource: str,
                  result: str,
                  details: Optional[Dict] = None):
        """Log for compliance and audit purposes.

        Args:
            user: User performing the action
            action: Action performed
            resource: Resource affected
            result: Result (success, failed, denied)
            details: Additional details
        """
        audit_entry = {
            "timestamp": datetime.now().isoformat(),
            "session_id": self.session_id,
            "user": user,
            "action": action,
            "resource": resource,
            "result": result,
            "details": self._sanitize_data(details or {}),
            "ip_address": "internal",  # Can be extended to get actual IP
            "user_agent": "MCP_Client"
        }

        try:
            with open(self.audit_log, 'a') as f:
                f.write(json.dumps(audit_entry) + '\n')
        except Exception as e:
            logging.error(f"Failed to write audit log: {e}")

    def _sanitize_data(self, data: Any) -> Any:
        """Sanitize data to remove sensitive information before logging.

        Args:
            data: Data to sanitize

        Returns:
            Sanitized data
        """
        if not isinstance(data, dict):
            return data

        sanitized = {}
        sensitive_keys = ['password', 'secret', 'token', 'api_key', 'credential']

        for key, value in data.items():
            if any(sensitive in key.lower() for sensitive in sensitive_keys):
                sanitized[key] = "***REDACTED***"
            elif isinstance(value, dict):
                sanitized[key] = self._sanitize_data(value)
            else:
                sanitized[key] = value

        return sanitized


# Global logger instance
_log_manager = None


def get_logger() -> LogManager:
    """Get or create global logger instance.

    Returns:
        Global LogManager instance
    """
    global _log_manager
    if _log_manager is None:
        _log_manager = LogManager()
    return _log_manager


def reset_logger():
    """Reset the global logger (mainly for testing)."""
    global _log_manager
    _log_manager = None
