"""Tests for circuit breaker middleware."""

import pytest
import time
from unittest.mock import patch

from src.middleware.circuit_breaker import CircuitBreaker, CircuitState, get_circuit_breaker
from src.exceptions import ToolExecutionError


class TestCircuitBreaker:
    """Test circuit breaker state transitions."""

    def test_starts_closed(self):
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=60)
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_allows_requests_when_closed(self):
        cb = CircuitBreaker()
        cb.check()  # Should not raise

    def test_stays_closed_below_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 2
        cb.check()

    def test_opens_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 3

    def test_rejects_when_open(self):
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        with pytest.raises(ToolExecutionError) as exc_info:
            cb.check()
        assert "Exchange unavailable" in str(exc_info.value)
        assert "Retry in" in str(exc_info.value)

    def test_half_open_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=1)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(1.1)
        cb.check()
        assert cb.state == CircuitState.HALF_OPEN

    def test_closes_on_success_from_half_open(self):
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=1)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        time.sleep(1.1)
        cb.check()
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_reopens_on_failure_from_half_open(self):
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=1)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        time.sleep(1.1)
        cb.check()
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.failure_count == 2
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED

    def test_custom_thresholds(self):
        cb = CircuitBreaker(failure_threshold=5, reset_timeout=120)
        for _ in range(4):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_error_message_includes_retry_time(self):
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=30)
        cb.record_failure()
        with pytest.raises(ToolExecutionError) as exc_info:
            cb.check()
        assert "Retry in" in str(exc_info.value)


class TestGetCircuitBreaker:
    def test_returns_same_instance(self):
        import src.middleware.circuit_breaker as mod
        mod._circuit_breaker = None
        cb1 = get_circuit_breaker()
        cb2 = get_circuit_breaker()
        assert cb1 is cb2

    def test_default_config(self):
        import src.middleware.circuit_breaker as mod
        mod._circuit_breaker = None
        cb = get_circuit_breaker()
        assert cb.failure_threshold == 3
        assert cb.reset_timeout == 60
        assert cb.state == CircuitState.CLOSED
