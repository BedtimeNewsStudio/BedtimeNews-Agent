# Indexer Service

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

Automated document embedding pipeline for the BedtimeNews archive. Clones the repository, processes markdown files, generates embeddings, and stores them in PostgreSQL + pgvector.

See the [main README](../README.en.md) for setup instructions.

## Features

- **Auto-sync**: Clones/updates from [BedtimeNews-Transcripts](https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts)
- **Body-aware incremental processing**: separate SHA-256 fingerprints for the complete Markdown source and the exact normalized `## 正文` text sent to embeddings; title/date/appendix-only edits make zero embedding calls
- **Scheduled execution**: In-process scheduler with a configurable cron
  expression (default: hourly)
- **Body-only indexing**: only the span between `## 正文` and `## 附录` is
  extracted; the title line, the `**发布日期**` metadata line and the appendix's
  fact-correction notes are all dropped
- **URI as doc_id**: a transcript's path relative to `contents/` (with `.md`) is
  its identifier
- **Standardised titles**: the upstream `URI映射.md` is parsed and the
  URI → title mapping written to `rag.documents`
- **Smart chunking**: Markdown-aware semantic chunking
- **Batch embedding**: Efficient batched embedding API usage
- **Monitoring**: Built-in debugger and statistics

## Pipeline Phases

![Indexer pipeline](../docs/diagrams/indexer-pipeline.svg)

Added and body-modified files are loaded and embedded before one transaction replaces their chunks and history, so a provider failure leaves the previous searchable version intact. Source-only edits update history without touching vectors. Deletions remove chunks, title, and history transactionally.

## Configuration

### Cron Schedule

Set in `config.yml`:

```bash
# Every hour (default)
INDEXER_CRON_SCHEDULE="0 * * * *"

# Every 30 minutes
INDEXER_CRON_SCHEDULE="*/30 * * * *"

# Daily at 2 AM
INDEXER_CRON_SCHEDULE="0 2 * * *"
```

### Local sample mode

`index_config.sample.yml` names seven deterministic transcripts covering ordinary, fractional, `misc`, and multi-channel URIs. Run it only with `INDEXER_SCOPE=sample`, `INDEX_CONFIG_FILE=/app/index_config.sample.yml`, an isolated data directory, and a `POSTGRES_DB` ending in `_local`; the indexer refuses any other database target. `docker-compose.sample.yml` supplies the service overrides.

### Document Filters

Edit `index_config.yml`:

```yaml
# Include patterns (processed first)
# Matched against a transcript's URI — its path relative to contents/,
# including the .md suffix.
include:
  # 睡前消息
  - "ShuiQianXiaoXi/*/*.md"

  # 参考信息
  - "CanKaoXinXi/*/*.md"

  # 高见
  - "GaoJian/*/*.md"

  # 讲点黑话
  - "JiangDianHeiHua/*/*.md"

  # 产经破壁机 (date-named since 2026-09; files sit at the section root)
  - "ChanJingPoBiJi/*.md"
  # 产经破壁机 (legacy numbered issues and misc/ specials, two-level paths)
  - "ChanJingPoBiJi/*/*.md"

# Exclude patterns (processed after include)
exclude:
  # Per-channel navigation pages, not transcripts
  - "*/INDEX.md"

# File validation rules
validation:
  # Minimum file size in bytes (skip empty or tiny files)
  min_file_size: 100

  # Maximum file size in bytes (skip extremely large files)
  max_file_size: 10485760 # 10 MB
```

## Debugging Utilities

### Test Connection

```bash
docker compose exec indexer python -m src.debugger test
```

### View Statistics

```bash
# Database stats
docker compose exec indexer python -m src.debugger stats

# Recent file actions
docker compose exec indexer python -m src.debugger recent --limit 20

# Indexing history for all files
docker compose exec indexer python -m src.debugger history

# History for specific file
docker compose exec indexer python -m src.debugger history ShuiQianXiaoXi/0901-1000/0960.md
```

### Inspect Documents

```bash
# View chunks for a document
docker compose exec indexer python -m src.debugger inspect ShuiQianXiaoXi/0901-1000/0960.md
```

### View Logs

```bash
# Recent scheduled-run logs
docker compose exec indexer python -m src.debugger logs

# Last 100 lines
docker compose exec indexer python -m src.debugger logs --lines 100

# All logs
docker compose exec indexer python -m src.debugger logs --all
```

### Manual Execution

```bash
# Run pipeline manually (one-time)
docker compose exec indexer python -m src.pipeline
```

### Clear Data

```bash
# DANGER: Clear all indexed data
docker compose exec indexer python -m src.debugger clear
```

## Database Schema

The indexer manages four tables in the `rag` schema:

**`rag.document_chunks`**: Stores chunks with embeddings

- `chunk_id`: Unique identifier (`{doc_id}:{chunk_index}`)
- `doc_id`: The transcript's URI, **including `.md`**, e.g.
  `ShuiQianXiaoXi/0501-0600/0588.md` — byte-for-byte the key used by the
  upstream `URI映射.md`
- `chunk_index`: 0-based index within document
- `heading`: Section heading (if any)
- `text`: Chunk content
- `word_count`: Number of words
- `embedding`: `halfvec(N)` vector — `N` comes from `EMBEDDING_DIM` (`.env`), applied
  by `storage/postgres/init.sh` on first DB init, and **must equal the embedding
  model's output dimension** (default `2560` for `Qwen/Qwen3-Embedding-4B`).
  See [Changing the Embedding Model](#changing-the-embedding-model). The column
  **type** is intentionally fixed to `halfvec` (not configurable): it fits any
  model up to 4000 dims — incl. the lower-dim OpenAI models — at half the storage
  with negligible recall loss, and the insert/query casts in
  `{agent,indexer}/src/vector_db.py` also use `::halfvec`. Switch types only for
  full float32 precision, >4000 dims, or binary/sparse embeddings (also requires
  changing the index opclass and those casts).
- `created_at`: Timestamp

**`rag.documents`**: URI → 标准化标题 (standardised title)

- `doc_id`: transcript URI (primary key, includes `.md`)
- `title`: standardised title, e.g. `睡前消息588`
- `updated_at`: timestamp

Titles come from the upstream `URI映射.md`. A general rule
(`{channel}/{bucket}/{number}.md` → `{Chinese channel name}{unpadded number}`)
covers the vast majority, but 29 transcripts — the `misc/` specials, officially
duplicated episode numbers, the negative-numbered 产经破壁机 issues — cannot be
derived by any rule, so that file is authoritative. The agent LEFT JOINs this
table at retrieval time to render citations with the title rather than the raw
URI. Every pipeline run refreshes all titles, since upstream may correct a title
without touching the transcript.

**`rag.indexing_history`**: Tracks the representation currently indexed

- `file_path`: transcript URI
- `source_hash`: SHA-256 of the complete raw Markdown source
- `body_hash`: SHA-256 of the exact normalized body represented by chunks/vectors
- `body_normalization_version`: forces re-indexing after an intentional normalizer change
- `indexed_at`: last successful body indexing time
- `source_observed_at`: last accepted source-only or body update

**`rag.file_actions`**: Audit log

- `action_type`: `ADD`, `MODIFY`, `SOURCE_ONLY`, or `DELETE`
- `source_hash` / `body_hash`: explicit fingerprints (`NULL` for `DELETE`)
- `run_timestamp` / `processed_at`: record and completion times

Existing v0.2 volumes must apply `storage/postgres/migrations/001_body_hashes.sql` before the new indexer starts. Legacy rows have a null `body_hash` and are conservatively re-indexed once. For a guaranteed clean upgrade, back up PostgreSQL, apply the migration, clear all four RAG tables, and repopulate the corpus. Never run the old indexer after rows use the new schema.

## Changing the Embedding Model

Changing `embedding.model` (and its endpoint) in `config.yml` is **not** a drop-in
swap. You must re-embed the entire corpus, because:

- Vectors from different models are **not comparable**, even at the same dimension
  — so a model change always requires re-embedding.
- Each model emits a **fixed dimension** (e.g. `Qwen/Qwen3-Embedding-4B` = 2560,
  `text-embedding-3-small` = 1536, `text-embedding-3-large` = 3072,
  `text-embedding-004` = 768). The `embedding halfvec(N)` column is sized from
  `EMBEDDING_DIM` (`.env`) by `storage/postgres/init.sh`. If the new model's
  dimension differs, the **column type itself must change**, or inserts fail with
  `expected N dimensions, not M`.
- `init.sh` runs **only when Postgres initializes an empty data volume**, and uses
  `CREATE TABLE IF NOT EXISTS`. Changing `EMBEDDING_DIM` afterward does **not**
  alter an existing database.

### Runbook

```bash
# 1. Stop services
docker compose down

# 2. Edit config.yml: update the embedding group (model / base_url / api_key).

# 3. Look up the new model's output dimension (provider docs), call it N.

# 4. Set EMBEDDING_DIM=N in .env (init.sh sizes the column from it on fresh init).
```

Then apply the dimension to the database — pick one:

**Option A — recreate the volume (simplest; wipes the DB so `init.sh` re-runs):**

The Postgres data lives in a **bind mount** (`./storage/postgres/volume`), so
`docker compose down -v` does NOT clear it — you must remove the directory's
contents yourself. Move it aside (reversible) rather than deleting outright:

```bash
docker compose down
mv storage/postgres/volume storage/postgres/volume.bak   # reversible rollback point
docker compose up -d postgres   # init.sh runs fresh, sizing the column from EMBEDDING_DIM
# DB is now empty, so change detection treats every file as new (step 6 optional).
# Once the re-embed is verified, delete the backup: rm -rf storage/postgres/volume.bak
```

**Option B — alter the existing table in place (keeps other tables):**

```bash
docker compose up -d postgres
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
  DROP INDEX IF EXISTS rag.idx_embedding_hnsw;
  TRUNCATE rag.document_chunks;
  ALTER TABLE rag.document_chunks ALTER COLUMN embedding TYPE halfvec(N);
  CREATE INDEX idx_embedding_hnsw ON rag.document_chunks
    USING hnsw (embedding halfvec_cosine_ops);
"
```

Finish by re-embedding:

```bash
# 6. Reset change-detection state so the indexer re-embeds everything.
#    REQUIRED after Option B (indexing_history still holds old hashes, or the
#    indexer will see "no changes" and skip). Harmless after Option A.
docker compose up -d indexer
docker compose exec indexer python -m src.debugger clear --force

# 7. Re-embed the full corpus
docker compose exec indexer python -m src.pipeline

# 8. Verify dimension + row count
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "\d rag.document_chunks" | grep embedding
docker compose exec indexer python -m src.debugger stats
```

> `debugger clear` truncates `document_chunks`, `indexing_history`, and
> `file_actions`, and deletes the local content clone (re-cloned on the next run).

## Data Backup and Restore

The PostgreSQL data volume is stored in a gitignored directory in the codebase. You can back up and restore the entire database using standard tar archives.

### Backup PostgreSQL Data

**Prerequisites**: Stop all services to ensure data consistency:

```bash
docker compose down
```

**(Optional) Check volume size (uncompressed)**:

```bash
du -h -d 0 storage/postgres/volume
```

**Create backup**:

```bash
# Create timestamped backup archive
tar czf /path/to/backup/postgres-volume-$(date +%F).tar.gz storage/postgres/volume/

# Example output: /path/to/backup/postgres-volume-2025-11-27.tar.gz
```

The backup includes:

- All indexed document chunks and embeddings
- Indexing history and file actions
- Database configuration and metadata

### Restore PostgreSQL Data

**Prerequisites**: Stop all services:

```bash
docker compose down
```

**Restore from backup**:

```bash
# Remove existing data (if any)
rm -rf storage/postgres/volume

# Extract backup to the postgres data directory (run in project root directory)
tar xzf /path/to/backup/postgres-volume-2025-11-27.tar.gz -C .
# Verify files extracted as ./storage/postgres/volume/18/docker/...

# Start services
docker compose up -d
```

**Verify restoration**:

```bash
# Check database statistics
docker compose exec indexer python -m src.debugger stats

# View recent file actions
docker compose exec indexer python -m src.debugger recent --limit 10
```

### Notes

- **Local only**: The postgres data directory is in `.gitignore` and not tracked by version control
- **Migration**: Backups are portable and can be restored on different machines
- **Disk space**: Each backup typically ranges from 100MB to several GB depending on indexed content

## Project Structure

```plaintext
indexer/src/
├── entrypoint.py        # Main entry point
├── pipeline.py          # Indexing pipeline orchestration
├── scheduler.py         # Cron scheduler
├── git_sync.py          # Repository sync
├── file_scanner.py      # File system scanning
├── document_loader.py   # Markdown processing
├── change_detector.py   # Content hash comparison
├── chunker.py           # Semantic chunking
├── embeddings.py        # Embedding generation (provider abstraction)
├── vector_db.py         # Database operations
├── debugger.py          # Debug utilities
├── stats.py             # Statistics calculation
├── models.py            # Data models
├── paths.py             # Path management
└── settings.py          # Configuration
```

## Monitoring

### Check Service Status

```bash
# View logs
docker compose logs -f indexer

# Check the unprivileged scheduler process
docker compose top indexer

# Verify database has documents
docker compose exec indexer python -m src.debugger stats
```

### Expected Output

After first run, you should see:

- Repository cloned to `indexer/data/BedtimeNews-Transcripts/`
- Chunks in `rag.document_chunks`
- File actions logged in `rag.file_actions`

### Performance Metrics

Pipeline outputs after each run:

- Total documents processed
- Total chunks created
- Total tokens processed
- Average tokens per chunk
- Estimated embedding API calls

## Troubleshooting

**No documents indexed:**

```bash
# Check logs for errors
docker compose logs indexer | grep -i error

# Manually run pipeline
docker compose exec indexer python -m src.pipeline

# Verify git clone succeeded
docker compose exec indexer ls -la data/BedtimeNews-Transcripts/
```

**Embedding API errors:**

- Check `embedding.api_key` in `config.yml`
- Verify rate limits not exceeded
- Check API usage in the provider's dashboard
- `expected N dimensions, not M`: the model's output dimension doesn't match the
  `embedding halfvec(N)` column (sized from `EMBEDDING_DIM`) — see
  [Changing the Embedding Model](#changing-the-embedding-model)

**Database connection failed:**

- Ensure postgres is running: `docker compose ps postgres`
- Check credentials in `config.yml`
- Test connection: `docker compose exec indexer python -m src.debugger test`

**Scheduler not running:**

```bash
# Check the scheduler process
docker compose top indexer

# View scheduled-run logs
docker compose exec indexer python -m src.debugger logs

# Restart service
docker compose restart indexer
```
