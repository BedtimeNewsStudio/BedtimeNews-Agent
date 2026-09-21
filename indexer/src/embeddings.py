"""Embedding generation against the configured OpenAI-compatible endpoint."""

import logging

import tiktoken
from openai import OpenAI as OpenAIClient
from tenacity import retry, stop_after_attempt, wait_exponential

from .settings import settings

logger = logging.getLogger(__name__)

# Module-level client singleton, pointed at the embedding endpoint from config.yml
_client = OpenAIClient(**settings.embedding.client_kwargs())

# Conservative per-input token limit for the embedding API
MAX_TOKENS_PER_INPUT = 8191


def generate_embeddings(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for texts using configured provider with token validation."""
    if not texts:
        return []

    model = settings.embedding.model
    batch_size = settings.embedding_batch_size

    # Validate and split oversized texts
    validated_texts, original_indices = _validate_and_split_texts(texts, model)

    logger.info(
        f"Generating embeddings: {len(texts)} texts ({len(validated_texts)} after splitting), "
        f"model={model}, batch_size={batch_size}"
    )

    all_embeddings = []
    total_batches = (len(validated_texts) + batch_size - 1) // batch_size

    for i in range(0, len(validated_texts), batch_size):
        batch = validated_texts[i : i + batch_size]
        batch_num = i // batch_size + 1

        try:
            embeddings = _generate_batch(batch)
            all_embeddings.extend(embeddings)
            logger.debug(f"Batch {batch_num}/{total_batches} completed")
        except Exception:
            logger.exception(f"Batch {batch_num} failed")
            raise

    # Merge embeddings for split texts (average them)
    return _merge_split_embeddings(all_embeddings, original_indices, len(texts))


def _validate_and_split_texts(
    texts: list[str], model: str
) -> tuple[list[str], list[int]]:
    """Validate token counts and split oversized texts.

    Args:
        texts: List of input texts
        model: Embedding model name

    Returns:
        Tuple of (validated_texts, original_indices)
        - validated_texts: List with oversized texts split
        - original_indices: List mapping each validated text to original index
    """
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        # Fallback for unknown models
        encoding = tiktoken.get_encoding("cl100k_base")

    validated_texts = []
    original_indices = []

    for idx, text in enumerate(texts):
        tokens = encoding.encode(text)
        token_count = len(tokens)

        if token_count <= MAX_TOKENS_PER_INPUT:
            validated_texts.append(text)
            original_indices.append(idx)
        else:
            # Text exceeds limit, split it
            logger.warning(
                f"Text {idx} has {token_count} tokens (limit: {MAX_TOKENS_PER_INPUT}). "
                f"Splitting into multiple parts."
            )

            # Split into chunks that fit
            split_texts = _split_by_tokens(text, tokens, encoding, MAX_TOKENS_PER_INPUT)

            # Add all split parts with same original index
            validated_texts.extend(split_texts)
            original_indices.extend([idx] * len(split_texts))

            logger.info(f"  Split into {len(split_texts)} parts")

    return validated_texts, original_indices


def _split_by_tokens(
    text: str, tokens: list[int], encoding, max_tokens: int
) -> list[str]:
    """Split text into chunks that fit within token limit.

    Args:
        text: Original text
        tokens: Token list from encoding
        encoding: Tiktoken encoding
        max_tokens: Maximum tokens per chunk

    Returns:
        List of text chunks
    """
    chunks = []
    chunk_size = max_tokens - 100  # Leave buffer for safety

    for i in range(0, len(tokens), chunk_size):
        chunk_tokens = tokens[i : i + chunk_size]
        chunk_text = encoding.decode(chunk_tokens)
        chunks.append(chunk_text)

    return chunks


def _merge_split_embeddings(
    embeddings: list[list[float]], original_indices: list[int], num_original: int
) -> list[list[float]]:
    """Merge embeddings for texts that were split.

    Args:
        embeddings: All embeddings (including split ones)
        original_indices: List mapping each embedding to its original text index
        num_original: Number of original texts

    Returns:
        List of embeddings with splits merged by averaging
    """
    if len(embeddings) == num_original:
        # No splits occurred
        return embeddings

    # Group embeddings by original index
    grouped = {}
    for emb, orig_idx in zip(embeddings, original_indices, strict=False):
        if orig_idx not in grouped:
            grouped[orig_idx] = []
        grouped[orig_idx].append(emb)

    # Merge by averaging
    merged = []
    for idx in range(num_original):
        embs = grouped[idx]
        if len(embs) == 1:
            # No split, use as-is
            merged.append(embs[0])
        else:
            # Multiple parts, average them
            avg_emb = [sum(vals) / len(vals) for vals in zip(*embs, strict=False)]
            merged.append(avg_emb)

    return merged


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def _generate_batch(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a batch with retry logic."""
    response = _client.embeddings.create(input=texts, model=settings.embedding.model)
    return [item.embedding for item in response.data]
