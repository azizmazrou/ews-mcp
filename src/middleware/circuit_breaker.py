"""Circuit breaker for Exchange connectivity.

Prevents cascade of failed retries when Exchange is down.
Trips after N consecutive failures, resets after timeout.
"""

import time
import logging
from enum import Enum
from typing import Optional

from ..exceptions import ToolExecutionError


class CircuitState(str, Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject fast
    HALF_OPEN = "half_open"  # Testing if recovered


class CircuitBreaker:
    """Simple circuit breaker for EWS operations."""

    def __init__(
        self,
        failure_threshold: int = 3,
        reset_timeout: int = 60,
        name: str = "ews"
    ):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.name = name
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.logger = logging.getLogger(__name__)

    def check(self) -> None:
        """Check if requests are allowed. Raises if circuit is open."""
        if self.state == CircuitState.CLOSED:
            return

        if self.state == CircuitState.OPEN:
            elapsed = time.time() - (self.last_failure_time or 0)
            if elapsed >= self.reset_timeout:
                self.state = CircuitState.HALF_OPEN
                self.logger.info(f"Circuit '{self.name}' half-open, allowing probe request")
                return
            raise ToolExecutionError(
                f"Exchange unavailable (circuit open). Retry in {int(self.reset_timeout - elapsed)}s."
            )

        # HALF_OPEN: allow one probe request
        return

    def record_success(self) -> None:
        """Record a successful request."""
        if self.state == CircuitState.HALF_OPEN:
            self.logger.info(f"Circuit '{self.name}' recovered, closing")
        self.state = CircuitState.CLOSED
        self.failure_count = 0

    def record_failure(self) -> None:
        """Record a failed request."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self.logger.warning(f"Circuit '{self.name}' re-opened after probe failure")
        elif self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.logger.warning(
                f"Circuit '{self.name}' opened after {self.failure_count} consecutive failures"
            )


# Global instance
_circuit_breaker: Optional[CircuitBreaker] = None


def get_circuit_breaker() -> CircuitBreaker:
    """Get or create global circuit breaker."""
    global _circuit_breaker
    if _circuit_breaker is None:
        _circuit_breaker = CircuitBreaker()
    return _circuit_breaker
