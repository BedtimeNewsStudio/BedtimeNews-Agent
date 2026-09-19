"""Vector Database Operations for RAG System.

Provides module-level functions for PostgreSQL + pgvector operations with connection pooling.
Follows the pattern from topicstreams/common/database.py.
"""

import logging
import threading
from functools import wraps
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from .settings import settings

logger = logging.getLogger(__name__)

# Module-level connection pool (singleton), guarded by a lock so concurrent
# first-callers don't each build a pool and orphan each other's connections.
_connection_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

# Transient errors that should trigger retry
TRANSIENT_ERRORS = (
    psycopg2.OperationalError,  # Connection issues, server restart
    psycopg2.InterfaceError,  # Connection lost during operation
)


def retry_on_transient_error(max_attempts: int = 3, delay_seconds: float = 0.1):
    """Decorator to retry database operations on transient failures.

    Args:
        max_attempts: Maximum number of retry attempts (default: 3)
        delay_seconds: Initial delay between retries in seconds (default: 0.1)
    """
    import time

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except TRANSIENT_ERRORS:
                    if attempt < max_attempts - 1:
                        # Exponential backoff
                        sleep_time = delay_seconds * (2**attempt)
                        time.sleep(sleep_time)
                    else:
                        # Final attempt failed, re-raise
                        raise

        return wrapper

    return decorator


def _get_connection_pool() -> ThreadedConnectionPool:
    """
    Get or create the connection pool singleton.

    Returns:
        ThreadedConnectionPool instance
    """
    global _connection_pool
    # Double-checked locking: fast path without the lock once initialized.
    if _connection_pool is None:
        with _pool_lock:
            if _connection_pool is None:
                try:
                    _connection_pool = ThreadedConnectionPool(
                        minconn=5,
                        maxconn=20,
                        host=settings.postgres_host,
                        port=settings.postgres_port,
                        database=settings.postgres_db,
                        user=settings.postgres_user,
                        password=settings.postgres_password,
                        connect_timeout=10,
                        keepalives=1,
                        keepalives_idle=30,
                        keepalives_interval=10,
                        keepalives_count=5,
                    )
                    logger.info(
                        "Connection pool created: minconn=5, maxconn=20 with keepalives"
                    )
                except Exception:
                    logger.exception("Failed to create connection pool")
                    raise
    return _connection_pool


def close_connection_pool() -> None:
    """
    Close all connections in the pool.

    Call this during application shutdown for graceful cleanup.
    """
    global _connection_pool
    with _pool_lock:
        if _connection_pool is not None:
            try:
                _connection_pool.closeall()
                _connection_pool = None
                logger.info("Connection pool closed")
            except Exception:
                logger.exception("Error closing connection pool")


class _Connection:
    """Internal connection context manager."""

    def __init__(self) -> None:
        # Hold the exact pool we borrow from so the connection is always
        # returned to that same pool, even if the global is later swapped.
        self._pool = _get_connection_pool()
        self._conn = self._pool.getconn()
        self._cursor = None

    def __enter__(self):
        self._cursor = self._conn.cursor(cursor_factory=RealDictCursor)
        return self

    def __exit__(self, exc_type, *_):
        try:
            try:
                if exc_type:
                    self._conn.rollback()
                    logger.debug("Transaction rolled back due to exception")
                else:
                    self._conn.commit()
                    logger.debug("Transaction committed")
            finally:
                if self._cursor:
                    self._cursor.close()
        except Exception:
            # Connection is in an unknown state: discard it via putconn(close=True)
            # so the pool frees the slot. Closing it directly would leak the slot
            # and eventually exhaust the pool.
            logger.exception("Error finalizing transaction; discarding connection")
            try:
                self._pool.putconn(self._conn, close=True)
            except Exception:
                logger.exception("Error discarding connection")
            if exc_type is None:
                # Commit failed: propagate so callers don't assume the write
                # succeeded (also lets transient-error retry kick in).
                raise
            return  # original exception from the with-body propagates

        try:
            self._pool.putconn(self._conn)
            logger.debug("Connection returned to pool")
        except Exception:
            logger.exception("Error returning connection to pool")

    def cursor(self):
        """Get the cursor for executing queries."""
        if self._cursor is None:
            raise RuntimeError("Cursor is only available within the context manager")
        return self._cursor


@retry_on_transient_error()
def search_similar_chunks(
    query_embedding: list[float],
    match_threshold: float = 0.7,
    match_count: int = 10,
    doc_id_filter: list[str] | None = None,
    include_text: bool = True,
) -> list[dict[str, Any]]:
    """
    Search for similar chunks using vector similarity.

    Args:
        query_embedding: embedding vector; its dimension must match the
            `embedding halfvec(N)` column (sized from EMBEDDING_DIM at DB init)
        match_threshold: Minimum cosine similarity threshold
        match_count: Maximum number of results to return
        doc_id_filter: Optional list of doc_ids to restrict search
        include_text: Whether to include full text in results (False saves bandwidth for grading)

    Returns:
        List of matching chunks with similarity scores
    """
    # LEFT JOIN, not INNER: a chunk whose title row is missing (a transcript
    # indexed before the title sync caught up) must still be retrievable, with
    # the caller falling back to a rule-derived label.
    query = f"""
        WITH similarities AS (
            SELECT DISTINCT ON (c.chunk_id)
                c.chunk_id,
                c.doc_id,
                d.title,
                c.chunk_index,
                c.heading,
                {"c.text," if include_text else ""}
                c.word_count,
                1 - (c.embedding <=> %s::halfvec) as similarity
            FROM rag.document_chunks c
            LEFT JOIN rag.documents d ON d.doc_id = c.doc_id
            WHERE c.embedding IS NOT NULL
        )
        SELECT * FROM similarities
        WHERE similarity >= %s
    """

    params = [query_embedding, match_threshold]

    if doc_id_filter:
        placeholders = ",".join(["%s"] * len(doc_id_filter))
        query += f" AND doc_id IN ({placeholders})"
        params.extend(doc_id_filter)

    query += """
        ORDER BY similarity DESC, chunk_id
        LIMIT %s
    """
    params.append(match_count)

    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        results = cursor.fetchall()
        return [dict(row) for row in results]


@retry_on_transient_error()
def fetch_chunk_texts(chunk_ids: list[str]) -> dict[str, str]:
    """
    Fetch full text for specific chunks by their IDs.

    Used to lazily load text only for relevant documents after grading.

    Args:
        chunk_ids: List of chunk_id values to fetch text for

    Returns:
        Dict mapping chunk_id to text content
    """
    if not chunk_ids:
        return {}

    placeholders = ",".join(["%s"] * len(chunk_ids))
    query = f"""
        SELECT chunk_id, text
        FROM rag.document_chunks
        WHERE chunk_id IN ({placeholders})
    """

    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, chunk_ids)
        return {row["chunk_id"]: row["text"] for row in cursor.fetchall()}


@retry_on_transient_error()
def list_transcripts() -> list[dict[str, Any]]:
    """Return deterministic reader-navigation metadata for every transcript."""
    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT doc_id, canonical_title, source_title, channel,
                   publication_date, source_hash, updated_at
            FROM rag.transcripts
            -- Newest first within each channel; undated episodes sink to the end.
            ORDER BY channel,
                     publication_date DESC NULLS LAST,
                     doc_id DESC;
            """
        )
        return [dict(row) for row in cursor.fetchall()]


@retry_on_transient_error()
def get_transcript(doc_id: str) -> dict[str, Any] | None:
    """Return one sanitized reader projection by exact canonical URI."""
    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT doc_id, canonical_title, source_title, channel,
                   publication_date, body_html, source_hash, updated_at
            FROM rag.transcripts
            WHERE doc_id = %s;
            """,
            (doc_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
