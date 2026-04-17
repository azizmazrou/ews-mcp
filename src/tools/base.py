"""Base class for all MCP tools."""

from abc import ABC, abstractmethod
from typing import Any, Dict, Type, Optional
from pydantic import BaseModel, ValidationError as PydanticValidationError
from exchangelib import Account
import logging
import time

from ..ews_client import EWSClient
from ..exceptions import ValidationError, ToolExecutionError
from ..utils import format_error_response
from ..logging_system import get_logger
from ..middleware.circuit_breaker import get_circuit_breaker


class BaseTool(ABC):
    """Base class for all MCP tools with integrated logging."""

    def __init__(self, ews_client: EWSClient):
        self.ews_client = ews_client
        self.logger = logging.getLogger(self.__class__.__name__)
        self.log_manager = get_logger()

    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """Return tool schema for MCP registration."""
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute tool operation."""
        pass

    def validate_input(self, model: Type[BaseModel], **kwargs) -> BaseModel:
        """Validate input using Pydantic model. Returns human-readable errors."""
        try:
            return model(**kwargs)
        except PydanticValidationError as e:
            # Simplify Pydantic errors to single actionable lines
            errors = e.errors()
            messages = []
            for err in errors[:3]:  # Max 3 errors to keep it short
                field = ".".join(str(loc) for loc in err["loc"])
                msg = err["msg"]
                if "Value error, " in msg:
                    msg = msg.replace("Value error, ", "")
                messages.append(f"{field}: {msg}")
            short_msg = "; ".join(messages)
            self.logger.error(f"Validation: {short_msg}")
            raise ValidationError(short_msg)

    def get_account(self, target_mailbox: Optional[str] = None) -> Account:
        """Get Exchange account for operations."""
        return self.ews_client.get_account(target_mailbox)

    def get_mailbox_info(self, target_mailbox: Optional[str] = None) -> str:
        """Get mailbox identifier for response."""
        if target_mailbox and target_mailbox.lower() != self.ews_client.config.ews_email.lower():
            return target_mailbox
        return self.ews_client.config.ews_email

    def get_memory_store(self):
        """Return the persistent memory store for the primary authenticated mailbox.

        Agentic features (commitments, approvals, rules, voice profile, OOF
        policy) are intentionally stored against the *primary* mailbox, not
        the impersonated ``target_mailbox``. The primary mailbox is the
        operator whose server is running; per-target state would either leak
        across service-account boundaries or require a per-target-mailbox DB
        each call, which we don't want yet.
        """
        from ..memory import MemoryStore
        return MemoryStore.for_mailbox(self.ews_client.config.ews_email)

    async def safe_execute(self, **kwargs) -> Dict[str, Any]:
        """Execute with error handling, circuit breaker, and logging."""
        start_time = time.time()
        tool_name = self.get_schema()["name"]
        module_name = self.__class__.__module__.split('.')[-1]

        # Circuit breaker check
        cb = get_circuit_breaker()
        try:
            cb.check()
        except ToolExecutionError as e:
            return format_error_response(e, "")

        # Log attempt
        self.log_manager.log_activity(
            level="INFO",
            module=module_name,
            action=f"{tool_name.upper()}_ATTEMPT",
            data=self._sanitize_kwargs(kwargs),
            result={"status": "attempting"},
            context={"tool": tool_name}
        )

        try:
            result = await self.execute(**kwargs)
            duration_ms = int((time.time() - start_time) * 1000)

            # Record success with circuit breaker
            cb.record_success()

            # Log success
            self.log_manager.log_activity(
                level="INFO",
                module=module_name,
                action=f"{tool_name.upper()}_SUCCESS",
                data=self._sanitize_kwargs(kwargs),
                result={
                    "status": "success",
                    "duration_ms": duration_ms,
                    **result
                },
                context={"tool": tool_name}
            )

            # Log performance
            self.log_manager.log_performance(
                metric="api_call",
                tool=tool_name,
                duration_ms=duration_ms,
                status="success"
            )

            # Audit log
            self.log_manager.log_audit(
                user=self.ews_client.config.ews_email,
                action=tool_name,
                resource=f"{tool_name}_operation",
                result="success",
                details={"duration_ms": duration_ms}
            )

            return result

        except ValidationError as e:
            duration_ms = int((time.time() - start_time) * 1000)
            # Validation errors are user errors — don't trip circuit breaker
            self._log_error("ValidationError", tool_name, module_name, kwargs, e, duration_ms)
            return format_error_response(e, "")

        except ToolExecutionError as e:
            duration_ms = int((time.time() - start_time) * 1000)
            error_str = str(e).lower()
            # Only trip circuit breaker for connectivity/server errors
            if any(kw in error_str for kw in ["connect", "timeout", "unavailable", "transport"]):
                cb.record_failure()
            self._log_error("ToolExecutionError", tool_name, module_name, kwargs, e, duration_ms)
            return format_error_response(e, "")

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            cb.record_failure()
            self.logger.exception("Unexpected error in tool execution")
            self._log_error("UnexpectedError", tool_name, module_name, kwargs, e, duration_ms)
            return format_error_response(e, "Unexpected error")

    def _log_error(self, error_type: str, tool_name: str, module_name: str,
                   kwargs: Dict, error: Exception, duration_ms: int):
        """Log error with full context."""
        self.log_manager.log_activity(
            level="ERROR",
            module=module_name,
            action=f"{tool_name.upper()}_ERROR",
            data=self._sanitize_kwargs(kwargs),
            result={
                "status": "failed",
                "error": str(error),
                "error_type": error_type,
                "duration_ms": duration_ms
            },
            context={"tool": tool_name}
        )

        self.log_manager.log_performance(
            metric="api_call",
            tool=tool_name,
            duration_ms=duration_ms,
            status="failed",
            error_type=error_type
        )

        self.log_manager.log_audit(
            user=self.ews_client.config.ews_email,
            action=tool_name,
            resource=f"{tool_name}_operation",
            result="failed",
            details={"error": str(error), "error_type": error_type}
        )

    def _sanitize_kwargs(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize kwargs for logging (remove sensitive data)."""
        sanitized = {}
        sensitive_keys = ['password', 'secret', 'token', 'api_key', 'body', 'content']

        for key, value in kwargs.items():
            if any(sensitive in key.lower() for sensitive in sensitive_keys):
                if isinstance(value, str) and len(value) > 50:
                    sanitized[key] = f"{value[:50]}... (truncated)"
                else:
                    sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = value

        return sanitized
