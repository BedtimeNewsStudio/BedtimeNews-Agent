"""PostgreSQL and pgvector operations for the indexer."""

import logging
import threading
from functools import wraps
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor, execute_batch
from psycopg2.pool import ThreadedConnectionPool

from .models import Chunk, TranscriptProjection
from .settings import settings

logger = logging.getLogger(__name__)

_connection_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

TRANSIENT_ERRORS = (
    psycopg2.OperationalError,
    psycopg2.InterfaceError,
)
_VALID_ACTIONS = frozenset({"ADD", "MODIFY", "SOURCE_ONLY", "DELETE"})


def retry_on_transient_error(max_attempts: int = 3, delay_seconds: float = 0.1):
    """Retry a database operation after transient connection failures."""
    import time

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except TRANSIENT_ERRORS:
                    if attempt < max_attempts - 1:
                        time.sleep(delay_seconds * (2**attempt))
                    else:
                        raise

        return wrapper

    return decorator


def _get_connection_pool() -> ThreadedConnectionPool:
    """Get or create the connection pool singleton."""
    global _connection_pool
    if _connection_pool is None:
        with _pool_lock:
            if _connection_pool is None:
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
    return _connection_pool


def close_connection_pool() -> None:
    """Close all connections in the pool."""
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
    """Internal transaction-scoped connection context manager."""

    def __init__(self) -> None:
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
            logger.exception("Error finalizing transaction; discarding connection")
            try:
                self._pool.putconn(self._conn, close=True)
            except Exception:
                logger.exception("Error discarding connection")
            if exc_type is None:
                raise
            return

        try:
            self._pool.putconn(self._conn)
        except Exception:
            logger.exception("Error returning connection to pool")

    def cursor(self):
        """Return the cursor while inside the context manager."""
        if self._cursor is None:
            raise RuntimeError("Cursor is only available within the context manager")
        return self._cursor


def _validate_chunk_inputs(
    chunks: list[Chunk], embeddings: list[list[float]] | None
) -> bool:
    has_embeddings = embeddings is not None
    if has_embeddings and len(embeddings) != len(chunks):
        raise ValueError(
            f"Embeddings count ({len(embeddings)}) must match chunks count ({len(chunks)})"
        )
    return has_embeddings


def _insert_chunks_cursor(
    cursor,
    chunks: list[Chunk],
    embeddings: list[list[float]] | None,
    batch_size: int = 100,
) -> int:
    """Insert chunks using an existing transaction cursor."""
    if not chunks:
        return 0

    has_embeddings = _validate_chunk_inputs(chunks, embeddings)
    if has_embeddings:
        query = """
            INSERT INTO rag.document_chunks (
                chunk_id, doc_id, chunk_index, heading, text, word_count, embedding
            ) VALUES (
                %(chunk_id)s, %(doc_id)s, %(chunk_index)s, %(heading)s,
                %(text)s, %(word_count)s, %(embedding)s::halfvec
            );
        """
    else:
        query = """
            INSERT INTO rag.document_chunks (
                chunk_id, doc_id, chunk_index, heading, text, word_count
            ) VALUES (
                %(chunk_id)s, %(doc_id)s, %(chunk_index)s, %(heading)s,
                %(text)s, %(word_count)s
            );
        """

    rows = []
    for index, chunk in enumerate(chunks):
        row = {
            "chunk_id": chunk.id,
            "doc_id": chunk.doc_id,
            "chunk_index": chunk.chunk_index,
            "heading": chunk.heading,
            "text": chunk.text,
            "word_count": chunk.word_count,
        }
        if has_embeddings:
            row["embedding"] = embeddings[index]
        rows.append(row)

    for start in range(0, len(rows), batch_size):
        execute_batch(cursor, query, rows[start : start + batch_size])
    return len(rows)


def _upsert_history_cursor(
    cursor,
    file_path: str,
    source_hash: str,
    body_hash: str,
    body_normalization_version: int,
    *,
    reindexed: bool,
) -> None:
    if reindexed:
        cursor.execute(
            """
            INSERT INTO rag.indexing_history (
                file_path, source_hash, body_hash, body_normalization_version,
                indexed_at, source_observed_at
            ) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (file_path) DO UPDATE
            SET source_hash = EXCLUDED.source_hash,
                body_hash = EXCLUDED.body_hash,
                body_normalization_version = EXCLUDED.body_normalization_version,
                indexed_at = CURRENT_TIMESTAMP,
                source_observed_at = CURRENT_TIMESTAMP;
            """,
            (file_path, source_hash, body_hash, body_normalization_version),
        )
    else:
        cursor.execute(
            """
            UPDATE rag.indexing_history
            SET source_hash = %s,
                source_observed_at = CURRENT_TIMESTAMP
            WHERE file_path = %s
              AND body_hash = %s
              AND body_normalization_version = %s;
            """,
            (source_hash, file_path, body_hash, body_normalization_version),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Indexing history changed while recording source-only update: {file_path}"
            )


def _log_action_cursor(
    cursor,
    file_path: str,
    action_type: str,
    source_hash: str | None,
    body_hash: str | None,
) -> None:
    if action_type not in _VALID_ACTIONS:
        valid = ", ".join(sorted(_VALID_ACTIONS))
        raise ValueError(f"Invalid action_type: {action_type}. Must be one of {valid}")
    cursor.execute(
        """
        INSERT INTO rag.file_actions (
            file_path, action_type, source_hash, body_hash, processed_at
        ) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP);
        """,
        (file_path, action_type, source_hash, body_hash),
    )


@retry_on_transient_error()
def test_connection() -> bool:
    """Test database connectivity and the pgvector extension."""
    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT version();")
        version = cursor.fetchone()
        logger.info(
            f"PostgreSQL version: {version['version'] if version else 'Unknown'}"
        )
        cursor.execute("SELECT * FROM pg_extension WHERE extname = 'vector';")
        result = cursor.fetchone()
        if not result:
            logger.error("pgvector extension not found")
            return False
        logger.info(f"pgvector extension installed (version: {result['extversion']})")
        return True


@retry_on_transient_error()
def get_table_stats() -> dict[str, Any]:
    """Return chunk and indexed-document counts."""
    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) AS total_chunks,
                   COUNT(DISTINCT doc_id) AS total_documents,
                   (SELECT COUNT(*) FROM rag.transcripts) AS reader_documents
            FROM rag.document_chunks;
        """)
        row = cursor.fetchone()
        return dict(row.items()) if row else {}


@retry_on_transient_error()
def clear_all_chunks() -> None:
    with _Connection() as conn:
        conn.cursor().execute("DELETE FROM rag.document_chunks;")


@retry_on_transient_error()
def delete_chunks(doc_id: str) -> int:
    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rag.document_chunks WHERE doc_id = %s;", (doc_id,))
        return cursor.rowcount


@retry_on_transient_error()
def insert_chunks(
    chunks: list[Chunk],
    embeddings: list[list[float]] | None = None,
    batch_size: int = 100,
) -> int:
    """Insert chunks in one transaction (primarily for utilities/tests)."""
    _validate_chunk_inputs(chunks, embeddings)
    with _Connection() as conn:
        return _insert_chunks_cursor(conn.cursor(), chunks, embeddings, batch_size)


@retry_on_transient_error()
def replace_document_index(
    *,
    file_path: str,
    doc_id: str,
    chunks: list[Chunk],
    embeddings: list[list[float]],
    source_hash: str,
    body_hash: str,
    body_normalization_version: int,
    action_type: str,
) -> int:
    """Atomically replace one document's chunks and indexing state.

    Embeddings are generated before this function is called. A provider failure
    therefore cannot delete the currently searchable version, and any database
    failure rolls back the delete, insert, history, and audit row together.
    """
    if action_type not in {"ADD", "MODIFY"}:
        raise ValueError("replace_document_index action must be ADD or MODIFY")
    _validate_chunk_inputs(chunks, embeddings)

    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rag.document_chunks WHERE doc_id = %s;", (doc_id,))
        inserted = _insert_chunks_cursor(cursor, chunks, embeddings)
        _upsert_history_cursor(
            cursor,
            file_path,
            source_hash,
            body_hash,
            body_normalization_version,
            reindexed=True,
        )
        _log_action_cursor(cursor, file_path, action_type, source_hash, body_hash)
        return inserted


@retry_on_transient_error()
def record_source_only_change(
    *,
    file_path: str,
    source_hash: str,
    body_hash: str,
    body_normalization_version: int,
) -> None:
    """Record a non-body edit without touching chunks or indexed_at."""
    with _Connection() as conn:
        cursor = conn.cursor()
        _upsert_history_cursor(
            cursor,
            file_path,
            source_hash,
            body_hash,
            body_normalization_version,
            reindexed=False,
        )
        _log_action_cursor(cursor, file_path, "SOURCE_ONLY", source_hash, body_hash)


@retry_on_transient_error()
def delete_indexed_document(file_path: str, doc_id: str) -> None:
    """Atomically remove one document and record the deletion."""
    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rag.document_chunks WHERE doc_id = %s;", (doc_id,))
        cursor.execute("DELETE FROM rag.documents WHERE doc_id = %s;", (doc_id,))
        cursor.execute(
            "DELETE FROM rag.indexing_history WHERE file_path = %s;", (file_path,)
        )
        _log_action_cursor(cursor, file_path, "DELETE", None, None)


@retry_on_transient_error()
def upsert_document_titles(titles: dict[str, str], batch_size: int = 500) -> int:
    """Upsert URI -> 标准化标题 rows for the complete active corpus."""
    if not titles:
        return 0
    rows = list(titles.items())
    query = """
        INSERT INTO rag.documents (doc_id, title)
        VALUES (%s, %s)
        ON CONFLICT (doc_id) DO UPDATE
        SET title = EXCLUDED.title,
            updated_at = CURRENT_TIMESTAMP;
    """
    with _Connection() as conn:
        cursor = conn.cursor()
        for start in range(0, len(rows), batch_size):
            execute_batch(cursor, query, rows[start : start + batch_size])
    logger.info(f"Upserted {len(rows)} document titles")
    return len(rows)


@retry_on_transient_error()
def delete_document(doc_id: str) -> int:
    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rag.documents WHERE doc_id = %s;", (doc_id,))
        return cursor.rowcount


@retry_on_transient_error()
def clear_document_titles() -> None:
    with _Connection() as conn:
        conn.cursor().execute("DELETE FROM rag.documents;")


@retry_on_transient_error()
def update_indexing_history(
    file_path: str,
    source_hash: str,
    body_hash: str,
    body_normalization_version: int,
) -> None:
    """Upsert a successfully indexed file's explicit fingerprints."""
    with _Connection() as conn:
        _upsert_history_cursor(
            conn.cursor(),
            file_path,
            source_hash,
            body_hash,
            body_normalization_version,
            reindexed=True,
        )


@retry_on_transient_error()
def delete_indexing_history(file_path: str) -> None:
    with _Connection() as conn:
        conn.cursor().execute(
            "DELETE FROM rag.indexing_history WHERE file_path = %s;", (file_path,)
        )


@retry_on_transient_error()
def log_file_action(
    file_path: str,
    action_type: str,
    source_hash: str | None = None,
    body_hash: str | None = None,
) -> None:
    with _Connection() as conn:
        _log_action_cursor(
            conn.cursor(), file_path, action_type, source_hash, body_hash
        )


@retry_on_transient_error()
def get_indexing_history(file_path: str) -> dict[str, Any] | None:
    """Return one file's source/body indexing state."""
    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT file_path, source_hash, body_hash, body_normalization_version,
                   indexed_at, source_observed_at
            FROM rag.indexing_history
            WHERE file_path = %s;
            """,
            (file_path,),
        )
        result = cursor.fetchone()
        return dict(result) if result else None


@retry_on_transient_error()
def get_indexing_histories() -> dict[str, dict[str, Any]]:
    """Return all histories keyed by URI in one round trip."""
    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT file_path, source_hash, body_hash, body_normalization_version,
                   indexed_at, source_observed_at
            FROM rag.indexing_history;
            """
        )
        return {row["file_path"]: dict(row) for row in cursor.fetchall()}


@retry_on_transient_error()
def get_indexed_files() -> list[str]:
    return list(get_indexing_histories())


@retry_on_transient_error()
def get_recent_file_actions(limit: int = 10) -> list[dict[str, Any]]:
    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT file_path, action_type, source_hash, body_hash, processed_at
            FROM rag.file_actions
            ORDER BY processed_at DESC
            LIMIT %s;
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]


@retry_on_transient_error()
def get_file_chunks(doc_id: str) -> list[dict[str, Any]]:
    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT chunk_id, chunk_index, heading, word_count,
                   embedding IS NOT NULL AS has_embedding
            FROM rag.document_chunks
            WHERE doc_id = %s
            ORDER BY chunk_index;
            """,
            (doc_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


@retry_on_transient_error()
def clear_indexing_history() -> None:
    with _Connection() as conn:
        conn.cursor().execute("DELETE FROM rag.indexing_history;")


@retry_on_transient_error()
def clear_file_actions() -> None:
    with _Connection() as conn:
        conn.cursor().execute("DELETE FROM rag.file_actions;")


@retry_on_transient_error()
def get_transcript_states() -> dict[str, tuple[str, int]]:
    """Return source hash and renderer version for every reader projection."""
    with _Connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT doc_id, source_hash, projection_version FROM rag.transcripts;"
        )
        return {
            row["doc_id"]: (row["source_hash"], row["projection_version"])
            for row in cursor.fetchall()
        }


@retry_on_transient_error()
def upsert_transcript_projection(projection: TranscriptProjection) -> None:
    """Atomically publish one sanitized Markdown-derived reader document."""
    with _Connection() as conn:
        conn.cursor().execute(
            """
            INSERT INTO rag.transcripts (
                doc_id, canonical_title, source_title, channel,
                publication_date, body_html, source_hash, projection_version
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (doc_id) DO UPDATE
            SET canonical_title = EXCLUDED.canonical_title,
                source_title = EXCLUDED.source_title,
                channel = EXCLUDED.channel,
                publication_date = EXCLUDED.publication_date,
                body_html = EXCLUDED.body_html,
                source_hash = EXCLUDED.source_hash,
                projection_version = EXCLUDED.projection_version,
                updated_at = CURRENT_TIMESTAMP;
            """,
            (
                projection.doc_id,
                projection.canonical_title,
                projection.source_title,
                projection.channel,
                projection.publication_date,
                projection.body_html,
                projection.source_hash,
                projection.projection_version,
            ),
        )


@retry_on_transient_error()
def delete_transcript_projection(doc_id: str) -> None:
    with _Connection() as conn:
        conn.cursor().execute(
            "DELETE FROM rag.transcripts WHERE doc_id = %s;", (doc_id,)
        )


@retry_on_transient_error()
def sync_transcript_titles(titles: dict[str, str], batch_size: int = 500) -> None:
    """Refresh canonical titles without re-rendering or touching embeddings."""
    if not titles:
        return
    rows = [(title, doc_id) for doc_id, title in titles.items()]
    query = """
        UPDATE rag.transcripts
        SET canonical_title = %s,
            updated_at = CASE
                WHEN canonical_title IS DISTINCT FROM %s THEN CURRENT_TIMESTAMP
                ELSE updated_at
            END
        WHERE doc_id = %s;
    """
    expanded = [(title, title, doc_id) for title, doc_id in rows]
    with _Connection() as conn:
        cursor = conn.cursor()
        for start in range(0, len(expanded), batch_size):
            execute_batch(cursor, query, expanded[start : start + batch_size])


@retry_on_transient_error()
def clear_transcript_projections() -> None:
    with _Connection() as conn:
        conn.cursor().execute("DELETE FROM rag.transcripts;")
