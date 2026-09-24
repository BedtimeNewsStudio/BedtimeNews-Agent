"""Statistics collection for chunks."""

from typing import Any

import tiktoken

from .models import Chunk
from .settings import settings


def collect_stats(chunks: list[Chunk]) -> dict[str, Any]:
    """Collect statistics about chunks.

    Args:
        chunks: List of Chunk objects

    Returns:
        Dictionary with statistics
    """
    embedding_model = settings.embedding.model
    batch_size = settings.embedding_batch_size

    try:
        encoding = tiktoken.encoding_for_model(embedding_model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")

    total_chunks = len(chunks)
    total_tokens = 0
    chunk_token_counts = []
    doc_ids_set = set()

    for chunk in chunks:
        text = chunk.text
        tokens = len(encoding.encode(text))
        total_tokens += tokens
        chunk_token_counts.append(tokens)
        doc_ids_set.add(chunk.doc_id)

    stats = {
        "total_documents": len(doc_ids_set),
        "total_chunks": total_chunks,
        "total_tokens": total_tokens,
        "avg_tokens_per_chunk": (
            total_tokens / total_chunks if total_chunks > 0 else 0
        ),
        "min_tokens": min(chunk_token_counts) if chunk_token_counts else 0,
        "max_tokens": max(chunk_token_counts) if chunk_token_counts else 0,
        "embedding_model": embedding_model,
        "estimated_api_calls": (total_chunks + batch_size - 1) // batch_size,
    }

    return stats
