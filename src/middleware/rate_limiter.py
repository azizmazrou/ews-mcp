"""Token bucket rate limiter."""

from collections import deque
from time import time
from typing import Optional
import logging

from ..exceptions import RateLimitError


class RateLimiter:
    """Token bucket rate limiter for controlling request rates."""

    def __init__(self, requests_per_minute: int):
        self.requests_per_minute = requests_per_minute
        self.requests = deque()
        self.window_seconds = 60
        self.logger = logging.getLogger(__name__)

    def is_allowed(self) -> bool:
        """Check if request is allowed under rate limit."""
        now = time()
        window_start = now - self.window_seconds

        # Remove old requests outside the window
        while self.requests and self.requests[0] < window_start:
            self.requests.popleft()

        # Check if we're under the limit
        if len(self.requests) < self.requests_per_minute:
            self.requests.append(now)
            return True

        self.logger.warning(f"Rate limit exceeded: {len(self.requests)} requests in last minute")
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

        # Remove old requests
        while self.requests and self.requests[0] < window_start:
            self.requests.popleft()

        return max(0, self.requests_per_minute - len(self.requests))

    def reset(self) -> None:
        """Reset the rate limiter."""
        self.requests.clear()
        self.logger.info("Rate limiter reset")
