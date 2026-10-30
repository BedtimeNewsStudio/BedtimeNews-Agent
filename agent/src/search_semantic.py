"""Semantic recall and reranking for question searches.

Keyword search finds transcripts that use the query's words; a question such as
为什么年轻人不愿意生孩子 is often answered in other words. For questions, the
chunk embeddings the chat retriever already uses add the transcripts closest in
meaning, fused with the keyword ranking by reciprocal rank. Names and short
topics stay keyword-only: their nearest chunks are rarely about the name.

Both model calls are optional. A slow or failing embedding or rerank endpoint
degrades the search to keyword ranking instead of failing it.
"""

import json
import logging
import urllib.request
from dataclasses import dataclass
from threading import Lock

from langchain_openai import OpenAIEmbeddings

from .settings import settings
from .vector_db import search_similar_chunks

logger = logging.getLogger(__name__)

# Nearest chunks read per question; grouped into at most SEMANTIC_DOCUMENTS.
SEMANTIC_CHUNKS = 100
SEMANTIC_DOCUMENTS = 30
# Keep transcripts within this similarity of the best one (and above the
# configured match_threshold): the nearest neighbours of any text exist.
SEMANTIC_WINDOW = 0.08
# Reciprocal rank fusion constant; 60 is the customary value.
RRF_K = 60
MODEL_TIMEOUT_S = 8


@dataclass(frozen=True)
class SemanticHit:
    doc_id: str
    similarity: float
    heading: str | None
    heading_anchor: str
    text: str


_embeddings: OpenAIEmbeddings | None = None
_lock = Lock()


def _client() -> OpenAIEmbeddings:
    global _embeddings
    with _lock:
        if _embeddings is None:
            _embeddings = OpenAIEmbeddings(
                model=settings.embedding.model,
                check_embedding_ctx_length=False,
                request_timeout=MODEL_TIMEOUT_S,
                max_retries=1,
                **settings.embedding.client_kwargs("embedding"),
            )
        return _embeddings


def semantic_hits(schema: str, format_version: int, query: str) -> list[SemanticHit]:
    """Transcripts whose chunks are nearest the question, best first."""
    if not settings.search_semantic:
        return []
    try:
        vector = _client().embed_query(query)
        rows = search_similar_chunks(
            schema,
            query_embedding=vector,
            match_threshold=settings.match_threshold,
            match_count=SEMANTIC_CHUNKS,
            include_text=True,
            format_version=format_version,
        )
    except Exception:
        logger.warning(
            "Semantic search unavailable; keyword ranking only", exc_info=True
        )
        return []
    hits: dict[str, SemanticHit] = {}
    for row in rows:
        if row["doc_id"] not in hits:
            hits[row["doc_id"]] = SemanticHit(
                doc_id=row["doc_id"],
                similarity=float(row["similarity"]),
                heading=row.get("heading"),
                heading_anchor=row.get("heading_anchor") or "",
                text=row.get("text") or "",
            )
    ranked = sorted(hits.values(), key=lambda h: (-h.similarity, h.doc_id))
    if not ranked:
        return []
    floor = ranked[0].similarity - SEMANTIC_WINDOW
    return [h for h in ranked if h.similarity >= floor][:SEMANTIC_DOCUMENTS]


def fuse(*rankings: list[str]) -> dict[str, float]:
    """Reciprocal rank fusion of ranked doc_id lists."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
    return scores


def rerank_enabled() -> bool:
    return bool(settings.rerank.model and _rerank_key())


def _rerank_key() -> str:
    return settings.rerank.api_key or settings.embedding.api_key


def rerank(query: str, documents: list[str]) -> list[float] | None:
    """Relevance of each document to the query, or None when unavailable."""
    if not documents or not rerank_enabled():
        return None
    base = settings.rerank.base_url or settings.embedding.base_url
    body = {
        "model": settings.rerank.model,
        "query": query,
        "documents": documents,
        "return_documents": False,
    }
    request = urllib.request.Request(
        (base or "https://api.openai.com/v1").rstrip("/") + "/rerank",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {_rerank_key()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT_S) as response:
            results = json.load(response)["results"]
        scores = [0.0] * len(documents)
        for item in results:
            scores[item["index"]] = float(item["relevance_score"])
        return scores
    except Exception:
        logger.warning("Rerank unavailable; fused ranking kept", exc_info=True)
        return None
