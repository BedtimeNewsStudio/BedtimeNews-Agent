"""Statistics collection for chunks."""

from collections import Counter
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
    chunks_per_doc: Counter[str] = Counter()

    for chunk in chunks:
        tokens = len(encoding.encode(chunk.text))
        total_tokens += tokens
        chunk_token_counts.append(tokens)
        chunks_per_doc[chunk.doc_id] += 1

    stats = {
        "total_documents": len(chunks_per_doc),
        "total_chunks": total_chunks,
        "total_tokens": total_tokens,
        "avg_tokens_per_chunk": (
            total_tokens / total_chunks if total_chunks > 0 else 0
        ),
        "min_tokens": min(chunk_token_counts) if chunk_token_counts else 0,
        "max_tokens": max(chunk_token_counts) if chunk_token_counts else 0,
        "embedding_model": embedding_model,
        # The pipeline embeds each document separately, so batches never span
        # documents.
        "estimated_api_calls": sum(
            -(-count // batch_size) for count in chunks_per_doc.values()
        ),
    }

    return stats
