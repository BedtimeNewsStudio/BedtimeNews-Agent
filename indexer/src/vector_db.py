"""Read-only inspection queries for the debugger.

Everything here reads the *current* snapshot: the newest published snapshot of
the indexer's current lineage, falling back to the newest published snapshot
of any lineage. Writes happen only in the snapshot builder (builder.py).
"""

from typing import Any

from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from . import catalog
from .db import connection


def _current_schema(cursor) -> str:
    with cursor.connection.cursor() as plain:
        plain.execute("SELECT to_regclass('rag_meta.snapshots') IS NOT NULL")
        if not plain.fetchone()[0]:
            raise RuntimeError("No snapshot registry yet (the indexer has not run)")
        snapshots = catalog.list_snapshots(plain)
    snap = catalog.latest_published(
        snapshots, lineage=catalog.current_lineage()
    ) or catalog.latest_published(snapshots)
    if snap is None:
        raise RuntimeError("No published snapshot")
    return snap.schema_name


def _query(query: str, params: tuple = (), *, one: bool = False) -> Any:
    with connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            schema = sql.Identifier(_current_schema(cursor))
            cursor.execute(sql.SQL(query).format(s=schema), params)
            rows = [dict(row) for row in cursor.fetchall()]
        conn.rollback()
    return (rows[0] if rows else None) if one else rows


def test_connection() -> bool:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version()")
        print(cursor.fetchone()[0])
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cursor.fetchone()
        print(f"pgvector: {row[0] if row else 'NOT INSTALLED'}")
        return row is not None


def get_table_stats() -> dict[str, Any]:
    return _query(
        """
        SELECT COUNT(*) AS total_chunks,
               COUNT(DISTINCT doc_id) AS total_documents,
               (SELECT COUNT(*) FROM {s}.transcripts) AS reader_documents
        FROM {s}.document_chunks
        """,
        one=True,
    )


def get_indexing_history(file_path: str) -> dict[str, Any] | None:
    return _query(
        """
        SELECT file_path, source_hash, body_hash, body_normalization_version,
               indexed_at, source_observed_at
        FROM {s}.index_state WHERE file_path = %s
        """,
        (file_path,),
        one=True,
    )


def get_indexed_files() -> list[str]:
    return [row["file_path"] for row in _query("SELECT file_path FROM {s}.index_state")]


def get_file_chunks(doc_id: str) -> list[dict[str, Any]]:
    return _query(
        """
        SELECT chunk_id, chunk_index, heading, word_count,
               embedding IS NOT NULL AS has_embedding
        FROM {s}.document_chunks WHERE doc_id = %s ORDER BY chunk_index
        """,
        (doc_id,),
    )


def get_recent_file_actions(limit: int = 10) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT file_path, action_type, source_hash, body_hash, processed_at
                FROM rag_state.file_actions
                ORDER BY processed_at DESC NULLS LAST, id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = [dict(row) for row in cursor.fetchall()]
        conn.rollback()
    return rows
