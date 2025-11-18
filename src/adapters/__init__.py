"""Adapters for external integrations in EWS MCP v3.0."""

from .gal_adapter import GALAdapter
from .cache_adapter import CacheAdapter

__all__ = ["GALAdapter", "CacheAdapter"]
