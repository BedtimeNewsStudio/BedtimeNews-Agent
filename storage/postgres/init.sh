#!/bin/bash
# ============================================================================
# Postgres first-init hook: enable pgvector. Runs ONLY when Postgres
# initializes an EMPTY data volume (docker-entrypoint-initdb.d).
#
# Everything else is created by the indexer at startup and during builds:
#   rag_meta    snapshot registry + indexer run status
#   rag_state   indexer-private audit log
#   rag_s<id>   immutable, versioned RAG snapshots (one schema per build)
#   rag_agent   read-only role the agent connects as
# The vector dimension is no longer fixed here: each snapshot's embedding
# column is sized for the vector space it was built in (EMBEDDING_DIM). See
# "Database Schema" in indexer/README.en.md.
# ============================================================================
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE EXTENSION IF NOT EXISTS vector;
EOSQL

echo "init.sh: pgvector enabled; the indexer creates the RAG schemas"
