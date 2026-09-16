"""Semantic retrieval service backed by PostgreSQL + pgvector."""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from tenacity import retry, stop_after_attempt, wait_exponential

from .cache import LRUCache, hash_query
from .models import ChunkResult, RetrieveRequest, RetrieveResponse
from .providers import get_provider
from .settings import settings
from .vector_db import search_similar_chunks

logger = logging.getLogger(__name__)


# Initialize embedding provider (module-level singleton).
# Embeddings may use a different backend than chat (settings.embedding_provider).
_embedding_provider = get_provider(settings.embedding_provider)


class _Retriever:
    """
    Internal retriever implementation.

    DO NOT instantiate directly. Use the singleton instance:
        from .retriever import retriever
    """

    def __init__(self) -> None:
        self._embeddings = _embedding_provider.get_embeddings_model(
            model=settings.embedding_model
        )
        self._result_cache = LRUCache(capacity=1000)

    def retrieve(self, request: RetrieveRequest) -> RetrieveResponse:
        """Perform semantic retrieval based on the request."""
        # Check cache first
        cache_key = _cache_key(request)
        cached_result = self._result_cache.get(cache_key)
        if cached_result is not None:
            return cast(RetrieveResponse, cached_result)

        query_embedding = self._generate_embedding(request.query)

        documents = search_similar_chunks(
            query_embedding=query_embedding,
            match_threshold=request.match_threshold,
            match_count=request.match_count,
            doc_id_filter=request.doc_id_filter,
            include_text=request.include_text,
        )

        results = self._map_chunk_results(documents, request)
        response = RetrieveResponse(
            query=request.query,
            match_threshold=request.match_threshold,
            match_count=request.match_count,
            results=results,
        )

        self._result_cache.put(cache_key, response)
        return response

    def retrieve_batch(self, requests: list[RetrieveRequest]) -> list[RetrieveResponse]:
        """
        Perform semantic retrieval for multiple queries efficiently.

        Generates all embeddings in a single batch API call, then runs
        DB searches in parallel using a thread pool. Results are cached
        individually for future single-query cache hits.
        """
        if not requests:
            return []

        # Check cache for each request and identify uncached ones
        cached_responses: list[RetrieveResponse | None] = []
        uncached_indices: list[int] = []
        for i, request in enumerate(requests):
            cached = self._result_cache.get(_cache_key(request))
            if cached is not None:
                cached_responses.append(cast(RetrieveResponse, cached))
            else:
                cached_responses.append(None)
                uncached_indices.append(i)

        if not uncached_indices:
            return cached_responses  # type: ignore[return-value]

        # Generate embeddings only for uncached queries
        uncached_requests = [requests[i] for i in uncached_indices]
        all_queries = [r.query for r in uncached_requests]
        all_embeddings = self._generate_embeddings_batch(all_queries)

        def _search_one(
            request: RetrieveRequest, query_embedding: list[float]
        ) -> RetrieveResponse:
            documents = search_similar_chunks(
                query_embedding=query_embedding,
                match_threshold=request.match_threshold,
                match_count=request.match_count,
                doc_id_filter=request.doc_id_filter,
                include_text=request.include_text,
            )
            chunk_results = self._map_chunk_results(documents, request)
            return RetrieveResponse(
                query=request.query,
                match_threshold=request.match_threshold,
                match_count=request.match_count,
                results=chunk_results,
            )

        # Run uncached searches in parallel
        if len(uncached_requests) == 1:
            new_responses = [_search_one(uncached_requests[0], all_embeddings[0])]
        else:
            with ThreadPoolExecutor(
                max_workers=min(len(uncached_requests), 4)
            ) as executor:
                futures = [
                    executor.submit(_search_one, req, emb)
                    for req, emb in zip(uncached_requests, all_embeddings, strict=False)
                ]
                new_responses = [f.result() for f in futures]

        # Cache new results and merge into response list
        for idx, response in zip(uncached_indices, new_responses, strict=False):
            self._result_cache.put(_cache_key(requests[idx]), response)
            cached_responses[idx] = response

        return cached_responses  # type: ignore[return-value]

    @staticmethod
    def _map_chunk_results(
        documents: list[dict[str, Any]], request: RetrieveRequest
    ) -> list[ChunkResult]:
        """Convert raw DB rows to ChunkResult list."""
        return [
            ChunkResult(
                chunk_id=doc["chunk_id"],
                doc_id=doc["doc_id"],
                title=doc.get("title"),
                chunk_index=doc.get("chunk_index"),
                heading=doc.get("heading") if request.include_heading else None,
                text=doc["text"] if request.include_text else None,
                word_count=doc["word_count"],
                similarity=round(doc["similarity"], 4),
                rank=rank,
            )
            for rank, doc in enumerate(documents, start=1)
        ]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def _generate_embedding(self, text: str) -> list[float]:
        """Generate embedding for a text query with retry logic."""
        return self._embeddings.embed_query(text)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def _generate_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple queries with retry logic."""
        return self._embeddings.embed_documents(texts)


def _cache_key(request: RetrieveRequest) -> str:
    return hash_query(
        request.query,
        request.match_threshold,
        request.match_count,
        request.include_text,
        request.include_heading,
        request.doc_id_filter,
    )


# Public singleton instance
retriever = _Retriever()
