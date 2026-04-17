"""Token bucket rate limiter."""

from collections import deque
from time import time
from typing import Optional
import logging
import threading

from ..exceptions import RateLimitError


class RateLimiter:
    """Sliding-window rate limiter for controlling request rates.

    Thread-safe: a single lock guards the request timestamp deque so the
    SSE transport + asyncio.gather paths don't race when mutating the
    window boundaries.
    """

    def __init__(self, requests_per_minute: int):
        self.requests_per_minute = requests_per_minute
        self.requests = deque()
        self.window_seconds = 60
        self.logger = logging.getLogger(__name__)
        self._lock = threading.Lock()

    def is_allowed(self) -> bool:
        """Check if request is allowed under rate limit."""
        now = time()
        window_start = now - self.window_seconds

        with self._lock:
            # Remove old requests outside the window
            while self.requests and self.requests[0] < window_start:
                self.requests.popleft()

            # Check if we're under the limit
            if len(self.requests) < self.requests_per_minute:
                self.requests.append(now)
                return True

            over_limit_count = len(self.requests)

        self.logger.warning(f"Rate limit exceeded: {over_limit_count} requests in last minute")
        return False

    def check_and_raise(self) -> None:
        """Check rate limit and raise exception if exceeded."""
        if not self.is_allowed():
            raise RateLimitError(
                f"Rate limit exceeded: maximum {self.requests_per_minute} requests per minute"
            )

    def get_remaining(self) -> int:
        """Get remaining requests in current window."""
        now = time()
        window_start = now - self.window_seconds

        with self._lock:
            while self.requests and self.requests[0] < window_start:
                self.requests.popleft()
            return max(0, self.requests_per_minute - len(self.requests))

    def reset(self) -> None:
        """Reset the rate limiter."""
        with self._lock:
            self.requests.clear()
        self.logger.info("Rate limiter reset")
