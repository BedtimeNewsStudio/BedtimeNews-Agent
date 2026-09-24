"""Caching utilities for query results and embeddings."""

import hashlib
import sys
import threading
from collections import OrderedDict
from typing import Any


def hash_query(
    query: str,
    match_threshold: float,
    match_count: int,
    include_text: bool = True,
    include_heading: bool = True,
    doc_id_filter: list[str] | None = None,
) -> str:
    """
    Generate cache key for query parameters.

    Args:
        query: Search query string
        match_threshold: Similarity threshold
        match_count: Maximum results count
        include_text: Whether results carry full text
        include_heading: Whether results carry headings
        doc_id_filter: Optional doc_id restriction applied to the search

    Returns:
        MD5 hash of the query parameters
    """
    # Every parameter that changes the response payload must be part of the
    # key — otherwise e.g. a text-less or filtered cached response could be
    # served to a caller that requested something broader.
    key_str = (
        f"{query}:{match_threshold}:{match_count}:{include_text}:{include_heading}"
        f":{sorted(doc_id_filter) if doc_id_filter else None}"
    )
    return hashlib.md5(key_str.encode()).hexdigest()


def _estimate_size(obj: Any) -> int:
    """Approximate memory footprint of a cached value in bytes.

    ``sys.getsizeof`` is shallow: a pydantic response holding thirty chunks of
    text measures the same ~100 bytes as an empty one, so it cannot drive a
    memory cap. The serialized length tracks what the value actually holds.
    """
    if hasattr(obj, "model_dump_json"):
        return len(obj.model_dump_json())
    if isinstance(obj, str | bytes):
        return len(obj)
    return sys.getsizeof(obj)


class LRUCache:
    """
    LRU (Least Recently Used) cache using OrderedDict for O(1) eviction.

    Tracks both item count and memory footprint. Evicts when either
    capacity or max_memory is exceeded.

    Thread-safe: accessed concurrently by request handler threads.
    """

    def __init__(self, capacity: int = 1000, max_memory_mb: float = 100):
        """
        Initialize the cache.

        Args:
            capacity: Maximum number of items to store
            max_memory_mb: Maximum memory usage in megabytes (approximate)
        """
        self.capacity = capacity
        self.max_memory = int(max_memory_mb * 1024 * 1024)
        self.cache: OrderedDict[str, Any] = OrderedDict()
        self._sizes: OrderedDict[str, int] = OrderedDict()
        self._total_size = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        """
        Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value if found, None otherwise
        """
        with self._lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                self._sizes.move_to_end(key)
                return self.cache[key]
            return None

    def put(self, key: str, value: Any) -> None:
        """
        Put value into cache.

        Args:
            key: Cache key
            value: Value to cache
        """
        with self._lock:
            if key in self.cache:
                self._total_size -= self._sizes[key]
                size = _estimate_size(value)
                self.cache[key] = value
                self._sizes[key] = size
                self._total_size += size
                self.cache.move_to_end(key)
                self._sizes.move_to_end(key)
            else:
                size = _estimate_size(value)
                while len(self.cache) >= self.capacity or (
                    self._total_size + size > self.max_memory and self.cache
                ):
                    self._evict_one()
                self.cache[key] = value
                self._sizes[key] = size
                self._total_size += size

    def _evict_one(self) -> None:
        """Evict the least recently used item. Caller must hold the lock."""
        _, size = self._sizes.popitem(last=False)
        self.cache.popitem(last=False)
        self._total_size -= size
