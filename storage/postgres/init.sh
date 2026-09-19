#!/bin/bash
# ============================================================================
# Postgres schema bootstrap for the RAG vector store.
#
# ⚠️  The embedding column dimension comes from EMBEDDING_DIM (.env), and MUST
# match the output dimension of the configured embedding model
# (EMBEDDING_PROVIDER / *_EMBEDDING_MODEL). Mismatch => inserts fail with
# "expected N dimensions, not M".
#
# This script runs ONLY when Postgres initializes an EMPTY data volume
# (docker-entrypoint-initdb.d) and every statement is CREATE ... IF NOT EXISTS.
# Changing EMBEDDING_DIM later does NOT alter an existing table. To switch the
# embedding model on an existing database, follow the "Changing the embedding
# model" runbook in indexer/README.md (ALTER column + re-embed).
#
# Only the DIMENSION is parametrized; the column TYPE is intentionally fixed to
# halfvec (not a knob). halfvec is a universal default for every mainstream
# embedding model: it stores any vector up to 4000 dims (vs. 2000 for the full
# float32 `vector` type under HNSW), at half the storage, with negligible
# cosine-similarity recall loss (float16). It works for low-dim models too
# (e.g. OpenAI text-embedding-3-small, 1536) — the old `vector(1536)` schema was
# a choice, not a requirement. The query/insert casts in {agent,indexer}/src/
# vector_db.py also cast to ::halfvec, so the type is consistent end-to-end.
# Only switch types for full float32 precision, >4000 dims, or binary/sparse
# embeddings — which also requires changing the index opclass and those casts.
# ============================================================================
set -euo pipefail

# Embedding dimension = the model's native output size (see .env / EMBEDDING_DIM).
# Default 2560 matches Qwen/Qwen3-Embedding-4B.
EMBEDDING_DIM="${EMBEDDING_DIM:-2560}"

echo "init.sh: creating rag schema with embedding halfvec(${EMBEDDING_DIM})"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	    -- Initialize pgvector extension
	    CREATE EXTENSION IF NOT EXISTS vector;
	    -- Create schema for RAG data
	    CREATE SCHEMA IF NOT EXISTS rag;

	    -- Main table for storing document chunks and their embeddings
	    CREATE TABLE IF NOT EXISTS rag.document_chunks (
	        id SERIAL PRIMARY KEY,
	        chunk_id VARCHAR(255) UNIQUE NOT NULL,
	        -- e.g., "ShuiQianXiaoXi_0501-0600_0588_chunk_000" (the doc_id URI
	        -- without .md, "/" replaced by "_", plus the chunk index)
	        doc_id VARCHAR(255) NOT NULL,
	        -- The transcript's URI: its path relative to contents/ in
	        -- BedtimeNews-Transcripts, INCLUDING the .md suffix, e.g.
	        -- "ShuiQianXiaoXi/0501-0600/0588.md". Byte-for-byte the key used by
	        -- the upstream URI映射.md, and the join key to rag.documents.
	        chunk_index INTEGER NOT NULL,
	        -- 0-based index within document
	        heading TEXT,
	        text TEXT NOT NULL,
	        word_count INTEGER NOT NULL DEFAULT 0,
	        -- Vector embedding; dimension parametrized via EMBEDDING_DIM (see header).
	        embedding halfvec(${EMBEDDING_DIM}),
	        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
	        CONSTRAINT valid_chunk_index CHECK (chunk_index >= 0),
	        CONSTRAINT valid_word_count CHECK (word_count >= 0)
	    );
	    CREATE INDEX IF NOT EXISTS idx_doc_id ON rag.document_chunks(doc_id);
	    CREATE INDEX IF NOT EXISTS idx_chunk_id ON rag.document_chunks(chunk_id);
	    -- HNSW approximate nearest-neighbor index (defaults m=16, ef_construction=64)
	    CREATE INDEX IF NOT EXISTS idx_embedding_hnsw ON rag.document_chunks
	        USING hnsw (embedding halfvec_cosine_ops);

	    GRANT USAGE ON SCHEMA rag TO "$POSTGRES_USER";
	    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA rag TO "$POSTGRES_USER";
	    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA rag TO "$POSTGRES_USER";

	    -- Document titles: URI -> 标准化标题, mirrored from the upstream
	    -- URI映射.md by the indexer. The agent LEFT JOINs this onto retrieved
	    -- chunks so a citation can be labelled "睡前消息588" rather than with its
	    -- raw URI. Kept separate from document_chunks so a title correction is a
	    -- single-row update instead of a rewrite of every chunk.
	    CREATE TABLE IF NOT EXISTS rag.documents (
	        doc_id VARCHAR(255) PRIMARY KEY,
	        -- Transcript URI, including .md (matches document_chunks.doc_id)
	        title VARCHAR(255) NOT NULL,
	        -- 标准化标题, e.g. "睡前消息588"
	        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
	    );

	    -- Indexing history keeps two independent fingerprints. source_hash sees
	    -- every Markdown edit; body_hash covers the exact normalized ## 正文 text
	    -- represented by the stored chunks and embeddings.
	    CREATE TABLE IF NOT EXISTS rag.indexing_history (
	        id SERIAL PRIMARY KEY,
	        file_path VARCHAR(500) UNIQUE NOT NULL,
	        source_hash VARCHAR(64) NOT NULL,
	        body_hash VARCHAR(64) NOT NULL,
	        body_normalization_version SMALLINT NOT NULL,
	        indexed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
	        source_observed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
	    );
	    CREATE INDEX IF NOT EXISTS idx_indexing_history_file_path ON rag.indexing_history(file_path);
	    CREATE INDEX IF NOT EXISTS idx_indexing_history_source_hash ON rag.indexing_history(source_hash);
	    CREATE INDEX IF NOT EXISTS idx_indexing_history_body_hash ON rag.indexing_history(body_hash);

	    -- Safe reader projection rebuilt directly from upstream Markdown.
	    CREATE TABLE IF NOT EXISTS rag.transcripts (
	        doc_id VARCHAR(500) PRIMARY KEY,
	        canonical_title VARCHAR(255) NOT NULL,
	        source_title TEXT NOT NULL,
	        channel VARCHAR(100) NOT NULL,
	        publication_date VARCHAR(100),
	        body_html TEXT NOT NULL,
	        source_hash VARCHAR(64) NOT NULL,
	        projection_version INTEGER NOT NULL DEFAULT 2,
	        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
	    );
	    CREATE INDEX IF NOT EXISTS idx_transcripts_channel ON rag.transcripts(channel);
	    CREATE INDEX IF NOT EXISTS idx_transcripts_source_hash ON rag.transcripts(source_hash);

	    -- File actions distinguish source-only edits from actual RAG rebuilds.
	    CREATE TABLE IF NOT EXISTS rag.file_actions (
	        id SERIAL PRIMARY KEY,
	        file_path VARCHAR(500) NOT NULL,
	        action_type VARCHAR(16) NOT NULL,
	        source_hash VARCHAR(64),
	        body_hash VARCHAR(64),
	        run_timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
	        processed_at TIMESTAMP WITH TIME ZONE,
	        CONSTRAINT valid_action_type CHECK (
	            action_type IN ('ADD', 'MODIFY', 'SOURCE_ONLY', 'DELETE')
	        )
	    );
	    CREATE INDEX IF NOT EXISTS idx_file_actions_file_path ON rag.file_actions(file_path);
	    CREATE INDEX IF NOT EXISTS idx_file_actions_timestamp ON rag.file_actions(run_timestamp);
	    CREATE INDEX IF NOT EXISTS idx_file_actions_type ON rag.file_actions(action_type);
EOSQL

echo "init.sh: schema ready"
