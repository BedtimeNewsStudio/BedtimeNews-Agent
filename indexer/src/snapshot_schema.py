"""Snapshot identity, compatibility fingerprints and per-format table DDL.

A snapshot is one immutable schema ``rag_s<id>`` holding the served tables
(``document_chunks``, ``documents``, ``transcripts``) plus the build provenance
``index_state``. Two dimensions decide compatibility (see
docs/designs/20261007_rag-snapshot-architecture.md, 5.2):

* ``pipeline_fingerprint`` — format, normalization and chunker versions. Only
  snapshots with the same fingerprint can serve as an incremental base.
* ``embedding_space`` — ``<model>@<dimension>`` (or ``embedding.space_id``).
  Vectors are only reused, and snapshots only read, within one space.
"""

import hashlib
from datetime import UTC, datetime

from psycopg2 import sql

from . import chunker, document_loader
from .settings import settings

# Bump whenever the structure or meaning of a table or column the agent reads
# changes. Adding columns the agent does not read, or changing only
# index_state, does not need a bump. Format 1 is the layout of the original
# `rag` schema, which is why adopted legacy data is format 1 too.
FORMAT_VERSION = 1

SCHEMA_PREFIX = "rag_"
LEGACY_SNAPSHOT_ID = "legacy"
LEGACY_SCHEMA = "rag"
LEGACY_FINGERPRINT = "legacy"

# HNSW parameters stay at the pgvector defaults; recorded in index_params.
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64
MAINTENANCE_WORK_MEM = "256MB"
MAX_PARALLEL_MAINTENANCE_WORKERS = 2

# Columns copied from a base snapshot, listed explicitly so a column-order
# difference cannot misalign a copy, and so the SERIAL `id` is regenerated.
COPY_COLUMNS: dict[str, tuple[str, ...]] = {
    "document_chunks": (
        "chunk_id",
        "doc_id",
        "chunk_index",
        "heading",
        "text",
        "word_count",
        "embedding",
        "created_at",
    ),
    "documents": ("doc_id", "title", "updated_at"),
    "transcripts": (
        "doc_id",
        "canonical_title",
        "source_title",
        "channel",
        "publication_date",
        "body_html",
        "source_hash",
        "projection_version",
        "updated_at",
    ),
    "index_state": (
        "file_path",
        "source_hash",
        "body_hash",
        "body_normalization_version",
        "indexed_at",
        "source_observed_at",
    ),
}
SNAPSHOT_TABLES = tuple(COPY_COLUMNS)


def pipeline_fingerprint(
    format_version: int | None = None,
    normalization_version: int | None = None,
    chunker_version: int | None = None,
) -> str:
    """Short stable hash of everything that decides the chunk texts."""
    format_version = FORMAT_VERSION if format_version is None else format_version
    if normalization_version is None:
        normalization_version = document_loader.BODY_NORMALIZATION_VERSION
    if chunker_version is None:
        chunker_version = chunker.CHUNKER_VERSION
    key = f"format={format_version};normalization={normalization_version};chunker={chunker_version}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def embedding_space(model: str | None = None, dim: int | None = None) -> str:
    """The configured vector space: ``embedding.space_id`` or ``model@dim``."""
    if model is None and dim is None and settings.embedding.space_id:
        return settings.embedding.space_id
    model = settings.embedding.model if model is None else model
    dim = settings.embedding_dim if dim is None else dim
    return f"{model}@{dim}"


def new_snapshot_id(source_commit: str | None, now: datetime | None = None) -> str:
    """``s<UTC build time>_<commit[:7]>``: unique, sortable, a valid identifier."""
    now = now or datetime.now(UTC)
    commit = (source_commit or "0000000")[:7].lower()
    return f"s{now.strftime('%Y%m%dt%H%M%Sz')}_{commit}"


def schema_for(snapshot_id: str) -> str:
    return f"{SCHEMA_PREFIX}{snapshot_id}"


def create_tables_sql(schema: str, dim: int) -> sql.Composed:
    """Format-1 DDL for one snapshot schema, without the HNSW index."""
    s = sql.Identifier(schema)
    return sql.SQL(
        """
        CREATE TABLE {s}.document_chunks (
            id SERIAL PRIMARY KEY,
            chunk_id VARCHAR(255) UNIQUE NOT NULL,
            doc_id VARCHAR(255) NOT NULL,
            chunk_index INTEGER NOT NULL,
            heading TEXT,
            text TEXT NOT NULL,
            word_count INTEGER NOT NULL DEFAULT 0,
            embedding halfvec({dim}) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT valid_chunk_index CHECK (chunk_index >= 0),
            CONSTRAINT valid_word_count CHECK (word_count >= 0)
        );
        CREATE INDEX idx_doc_id ON {s}.document_chunks(doc_id);

        CREATE TABLE {s}.documents (
            doc_id VARCHAR(255) PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE {s}.transcripts (
            doc_id VARCHAR(500) PRIMARY KEY,
            canonical_title VARCHAR(255) NOT NULL,
            source_title TEXT NOT NULL,
            channel VARCHAR(100) NOT NULL,
            publication_date VARCHAR(100),
            body_html TEXT NOT NULL,
            source_hash VARCHAR(64) NOT NULL,
            projection_version INTEGER NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_transcripts_channel ON {s}.transcripts(channel);

        CREATE TABLE {s}.index_state (
            id SERIAL PRIMARY KEY,
            file_path VARCHAR(500) UNIQUE NOT NULL,
            source_hash VARCHAR(64) NOT NULL,
            body_hash VARCHAR(64) NOT NULL,
            body_normalization_version SMALLINT NOT NULL,
            indexed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            source_observed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """
    ).format(s=s, dim=sql.Literal(int(dim)))


def create_hnsw_sql(schema: str) -> sql.Composed:
    return sql.SQL(
        "CREATE INDEX idx_embedding_hnsw ON {s}.document_chunks "
        "USING hnsw (embedding halfvec_cosine_ops) WITH (m = {m}, ef_construction = {ef})"
    ).format(
        s=sql.Identifier(schema),
        m=sql.Literal(HNSW_M),
        ef=sql.Literal(HNSW_EF_CONSTRUCTION),
    )


def index_params() -> dict:
    return {
        "type": "hnsw",
        "opclass": "halfvec_cosine_ops",
        "m": HNSW_M,
        "ef_construction": HNSW_EF_CONSTRUCTION,
        "maintenance_work_mem": MAINTENANCE_WORK_MEM,
        "max_parallel_maintenance_workers": MAX_PARALLEL_MAINTENANCE_WORKERS,
    }
