"""Simple in-memory cache adapter for EWS MCP v3.0."""

import logging
from typing import Any, Optional, Dict, Callable
from datetime import datetime, timedelta
import asyncio


class CacheAdapter:
    """
    Simple in-memory cache with TTL support.

    Reduces load on Exchange servers by caching frequent queries.
    """

    # Default cache durations (seconds)
    CACHE_DURATIONS = {
        'gal_search': 3600,      # 1 hour - GAL doesn't change often
        'person_details': 1800,   # 30 min
        'folder_list': 300,       # 5 min
        'email_search': 60,       # 1 min
        'contacts': 1800,         # 30 min
    }

    def __init__(self):
        """Initialize cache."""
        self.cache: Dict[str, tuple[Any, datetime]] = {}
        self.logger = logging.getLogger(__name__)
        self.hit_count = 0
        self.miss_count = 0

    def get(self, key: str) -> Optional[Any]:
        """
        Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value if exists and not expired, None otherwise
        """
        if key not in self.cache:
            self.miss_count += 1
            return None

        value, expires_at = self.cache[key]

        # Check if expired
        if datetime.now() >= expires_at:
            del self.cache[key]
            self.miss_count += 1
            return None

        self.hit_count += 1
        self.logger.debug(f"Cache HIT: {key}")
        return value

    def set(
        self,
        key: str,
        value: Any,
        duration: Optional[int] = None
    ) -> None:
        """
        Set value in cache with TTL.

        Args:
            key: Cache key
            value: Value to cache
            duration: TTL in seconds (default: 300)
        """
        duration = duration or 300
        expires_at = datetime.now() + timedelta(seconds=duration)
        self.cache[key] = (value, expires_at)
        self.logger.debug(f"Cache SET: {key} (TTL: {duration}s)")

    def delete(self, key: str) -> None:
        """Delete key from cache."""
        if key in self.cache:
            del self.cache[key]
            self.logger.debug(f"Cache DELETE: {key}")

    def clear(self) -> None:
        """Clear all cache."""
        self.cache.clear()
        self.hit_count = 0
        self.miss_count = 0
        self.logger.info("Cache cleared")

    async def get_or_fetch(
        self,
        key: str,
        fetch_func: Callable,
        duration: Optional[int] = None
    ) -> Any:
        """
        Get from cache or fetch and cache.

        Args:
            key: Cache key
            fetch_func: Async function to fetch value if not cached
            duration: TTL in seconds

        Returns:
            Cached or fetched value
        """
        # Check cache
        cached = self.get(key)
        if cached is not None:
            return cached

        # Fetch
        try:
            if asyncio.iscoroutinefunction(fetch_func):
                value = await fetch_func()
            else:
                value = fetch_func()

            # Cache
            self.set(key, value, duration)

            return value

        except Exception as e:
            self.logger.error(f"Failed to fetch value for key '{key}': {e}")
            raise

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total_requests = self.hit_count + self.miss_count
        hit_rate = (self.hit_count / total_requests * 100) if total_requests > 0 else 0

        return {
            'size': len(self.cache),
            'hit_count': self.hit_count,
            'miss_count': self.miss_count,
            'total_requests': total_requests,
            'hit_rate_percent': round(hit_rate, 2),
        }

    def cleanup_expired(self) -> int:
        """
        Remove expired entries from cache.

        Returns:
            Number of expired entries removed
        """
        now = datetime.now()
        expired_keys = [
            key for key, (_, expires_at) in self.cache.items()
            if now >= expires_at
        ]

        for key in expired_keys:
            del self.cache[key]

        if expired_keys:
            self.logger.info(f"Cleaned up {len(expired_keys)} expired cache entries")

        return len(expired_keys)


# Global cache instance
_cache_instance: Optional[CacheAdapter] = None


def get_cache() -> CacheAdapter:
    """Get or create global cache instance."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = CacheAdapter()
    return _cache_instance
