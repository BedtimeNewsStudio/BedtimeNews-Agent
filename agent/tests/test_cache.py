"""Unit tests for the query cache (hash_query) and LRUCache."""

from pydantic import BaseModel
from src.cache import LRUCache, hash_query


class _Payload(BaseModel):
    text: str


class TestHashQuery:
    def test_deterministic(self):
        a = hash_query("q", 0.5, 10)
        b = hash_query("q", 0.5, 10)
        assert a == b

    def test_each_parameter_affects_key(self):
        base = hash_query("q", 0.5, 10, include_text=True, include_heading=True)
        assert hash_query("other", 0.5, 10) != base
        assert hash_query("q", 0.6, 10) != base
        assert hash_query("q", 0.5, 11) != base
        assert hash_query("q", 0.5, 10, include_text=False) != base
        assert hash_query("q", 0.5, 10, include_heading=False) != base
        assert hash_query("q", 0.5, 10, doc_id_filter=["main/1"]) != base

    def test_doc_id_filter_order_insensitive(self):
        a = hash_query("q", 0.5, 10, doc_id_filter=["a", "b"])
        b = hash_query("q", 0.5, 10, doc_id_filter=["b", "a"])
        assert a == b


class TestLRUCache:
    def test_put_get_roundtrip(self):
        cache = LRUCache(capacity=10)
        cache.put("k", {"v": 1})
        assert cache.get("k") == {"v": 1}

    def test_get_missing_returns_none(self):
        assert LRUCache().get("absent") is None

    def test_size_reflects_count(self):
        cache = LRUCache(capacity=10)
        cache.put("a", 1)
        cache.put("b", 2)
        assert len(cache.cache) == 2

    def test_capacity_eviction_drops_least_recently_used(self):
        cache = LRUCache(capacity=2, max_memory_mb=100)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)  # exceeds capacity -> evicts "a"
        assert len(cache.cache) == 2
        assert cache.get("a") is None
        assert cache.get("b") == 2
        assert cache.get("c") == 3

    def test_get_refreshes_recency(self):
        cache = LRUCache(capacity=2, max_memory_mb=100)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.get("a")  # "a" is now most-recently-used
        cache.put("c", 3)  # evicts the LRU, which is now "b"
        assert cache.get("b") is None
        assert cache.get("a") == 1
        assert cache.get("c") == 3

    def test_updating_existing_key_does_not_grow_size(self):
        cache = LRUCache(capacity=2)
        cache.put("a", 1)
        cache.put("a", 2)
        assert len(cache.cache) == 1
        assert cache.get("a") == 2

    def test_memory_cap_eviction(self):
        # ~1 KB cap. The value is a model, like the retriever's cached responses,
        # so this fails if its size is measured shallowly.
        cache = LRUCache(capacity=1000, max_memory_mb=0.001)
        big = _Payload(text="x" * 2000)
        cache.put("a", big)
        cache.put("b", big)
        assert len(cache.cache) == 1
        assert cache.get("b") == big
        assert cache.get("a") is None
