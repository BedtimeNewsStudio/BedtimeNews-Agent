# Indexer Service

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

Automated document embedding pipeline for the BedtimeNews archive. Clones the transcript repository, chunks and embeds the transcripts, and publishes the result to PostgreSQL + pgvector as **immutable, versioned snapshots** that the agent reads. The indexer is the only writer to the database.

See the [main README](../README.en.md) for setup instructions, and the [design document](../docs/designs/20261007_rag-snapshot-architecture.md) for the full rationale.

## Features

- **Auto-sync**: Clones/updates from [BedtimeNews-Transcripts](https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts); every build is pinned to the commit taken at its start
- **Snapshot publishing**: each build produces one complete, self-consistent schema `rag_s<id>` that is never modified afterwards. Publishing is atomic (one transaction), reversible (`retire`) and recorded in a registry; the agent switches to a new snapshot within 15 seconds, without a restart
- **Incremental builds**: the transcripts are diffed against the latest snapshot's `index_state`, using separate SHA-256 fingerprints for the complete Markdown source and the exact normalized `## 正文` text sent to embeddings. Unchanged transcripts are copied from the base snapshot; title/date/appendix-only edits make zero embedding calls
- **Vector reuse by chunk text**: vectors are reused by `sha256(chunk text)` from the latest snapshot in the same vector space, so full builds (after a chunking or normalization change, a format upgrade, or adoption) only embed text that actually changed
- **Self-check before publishing**: completeness, a sampled HNSW query, and a sampled re-embedding (cosine ≥ 0.99) guard against broken builds
- **Scheduled execution**: in-process scheduler with a configurable cron expression (default: hourly); runs that overrun their slot are not caught up, and a run lock prevents concurrent runs
- **Body-only indexing**: only the span between `## 正文` and `## 附录` is extracted; the title line, the `**发布日期**` metadata line and the appendix's fact-correction notes are all dropped
- **URI as doc_id**: a transcript's path relative to `contents/` (with `.md`) is its identifier
- **Standardised titles**: the upstream `URI映射.md` is parsed and the URI → title mapping written to the snapshot's `documents` table
- **Smart chunking**: Markdown-aware semantic chunking, one section per sub-heading inside `## 正文` (the text before the first sub-heading is its own section); overlap is carried only within a section, never across a heading, and chunks under 50 words are dropped
- **Read-only agent**: the indexer maintains the `rag_agent` role, which can only read published snapshots
- **Monitoring**: run status in `rag_meta.indexer_status` (exposed by the agent's `/health`), ops commands and a debugger

## Pipeline Phases

![Indexer pipeline](../docs/diagrams/indexer-pipeline.svg)

One run:

1. **Run lock**: a non-blocking `flock` on `/data/.indexer.lock` (the `INDEXER_DATA_DIR` mount). If another run holds it (the scheduled run, a manual `build`, or a second container), the run records `skipped_busy` and exits without touching the git checkout or the database.
2. **Bootstrap** (idempotent): create `rag_meta`, `rag_state` and the `rag_agent` role; adopt an existing pre-snapshot `rag` schema as snapshot `legacy` (see [Upgrading](#upgrading-from-the-pre-snapshot-schema)).
3. **Git sync** and pin the commit.
4. **Plan**: the incremental base is the latest published snapshot of the current lineage (same pipeline fingerprint and vector space). Diff the transcripts against its `index_state`: added, body-modified, source-only (title/date/appendix) and deleted, plus reader projections and titles to refresh. Nothing changed → `no_change`. No base (first build, a version bump, a new vector space) or `build --full` → full build.
5. **GC, then disk precheck**: refuse to build unless the Postgres data filesystem has at least max(2 GB, 3 × the base snapshot's size) free.
6. **Phase A** (no transaction): chunk the transcripts to (re)index; reuse vectors by chunk-text hash, call the embedding API only for misses; render reader projections. New vectors stay in memory only.
7. **Phase B** (one transaction holding the database advisory lock): create `rag_s<id>` → copy the base's four tables (incremental) → delete changed and deleted transcripts → insert chunks (reused vectors are joined server-side) → write `index_state`, titles and reader projections → `ANALYZE` → build the HNSW index → self-check → recheck that the base is still published and still the newest of its lineage → register as `published` → grant `rag_agent` read access → `COMMIT`.
8. **GC again** (a failure here is only logged: the run's result stands), then update `rag_meta.indexer_status`.

Any failure, a killed process or a dropped connection rolls the whole Phase B transaction back: no half-built schema or registry row is ever left, and the published snapshots are untouched. On `SIGTERM` the indexer cancels the running statement and exits (compose allows 30 s).

Measured on the full corpus (1,902 transcripts, 13,434 chunks, 2560 dimensions; local 4-core machine): an incremental build of one changed transcript takes ~19 s (Phase B ~14 s, mostly the HNSW build); a full build reusing every vector ~36 s; one snapshot is ~240 MB and a build writes ~200 MB of WAL.

## Configuration

### Cron Schedule

Set in `config.yml`:

```yaml
indexer_cron_schedule: "0 * * * *"      # Every hour (default)
# indexer_cron_schedule: "*/30 * * * *" # Every 30 minutes
# indexer_cron_schedule: "0 2 * * *"    # Daily at 2 AM
```

The next run is always computed from the current time: a run that takes longer than the interval skips the missed slots instead of firing them back to back. A missed run never loses changes, because every build diffs against the published snapshot.

### Local sample mode

`index_config.sample.yml` names eight deterministic transcripts covering ordinary, fractional, `misc`, and multi-channel URIs. Run it only with `INDEXER_SCOPE=sample`, `INDEX_CONFIG_FILE=/app/index_config.sample.yml`, an isolated data directory, and a `POSTGRES_DB` ending in `_local`; the indexer refuses any other database target. `docker-compose.sample.yml` supplies the service overrides.

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

## Snapshot Operations

Run inside the indexer container:

| Command                                                 | Effect                                                                                                   |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `python -m src.snapshots list`                          | List snapshots: status, pinned, lineage (current/other), publish time, size, vector space               |
| `python -m src.snapshots status`                        | Show `rag_meta.indexer_status` (last run, result, error, consecutive failures)                           |
| `python -m src.snapshots retire <id>`                   | Retire a snapshot (data rollback); agents fall back to the previous readable snapshot within 15 s        |
| `python -m src.snapshots unretire <id>`                 | Undo a retirement (retired snapshots are kept for 24 hours)                                              |
| `python -m src.snapshots pin <id>` / `unpin <id>`       | Pin or unpin; pinned snapshots are never garbage-collected                                               |
| `python -m src.snapshots build [--full]`                | Run a build now, protected by the same run lock and advisory lock as the scheduled run                   |

```bash
docker compose exec indexer python -m src.snapshots list
docker compose exec indexer python -m src.snapshots retire s20261007t091512z_a1b2c3d
```

## Debugging Utilities

`stats`, `history` and `inspect` read the current snapshot (the newest published snapshot of the indexer's current lineage); `history` reads its `index_state`. `recent` reads the audit log `rag_state.file_actions`.

### Test Connection

```bash
docker compose exec indexer python -m src.debugger test
```

### View Statistics

```bash
# Current snapshot stats
docker compose exec indexer python -m src.debugger stats

# Recent file actions
docker compose exec indexer python -m src.debugger recent --limit 20

# Index state: all files in the current snapshot
docker compose exec indexer python -m src.debugger history

# Index state of one file
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
# Build once now (incremental)
docker compose exec indexer python -m src.snapshots build

# Rebuild everything (vectors are still reused by chunk text)
docker compose exec indexer python -m src.snapshots build --full
```

There is no "clear data" command: rebuild with `build --full`, and roll bad data back with `retire`.

## Database Schema

The indexer is the only writer. It creates everything below at startup and during builds; `storage/postgres/init.sh` only enables the `vector` extension.

```plaintext
rag_meta    snapshot registry and indexer run status (written only by the indexer)
rag_state   indexer-private: audit log
rag_s<id>   read-only snapshots: document_chunks / documents / transcripts / index_state
```

### `rag_s<id>`: one snapshot

`<id>` is `s` + the UTC build time + the first 7 characters of the transcripts commit, e.g. `s20261007t091512z_a1b2c3d`. Once published, a snapshot is never modified. Table DDL lives in `src/snapshot_schema.py`, per `format_version`.

**`document_chunks`**: chunks with embeddings

- `chunk_id`: unique identifier, `{slug}_chunk_{index:03d}`, where the slug is the URI without `.md` and with `/` replaced by `_` (e.g. `ShuiQianXiaoXi_0501-0600_0588_chunk_000`)
- `doc_id`: the transcript's URI, **including `.md`**, e.g. `ShuiQianXiaoXi/0501-0600/0588.md` — byte-for-byte the key used by the upstream `URI映射.md`
- `chunk_index`: 0-based index within document
- `heading`: section heading (if any)
- `text`: chunk content
- `word_count`: number of words
- `embedding`: `halfvec(N)`, `N` being the dimension of the snapshot's vector space. The column **type** is intentionally fixed to `halfvec`: it fits any model up to 4000 dims at half the storage with negligible recall loss. An HNSW index (`halfvec_cosine_ops`, pgvector defaults `m=16`, `ef_construction=64`) serves the agent's nearest-neighbour search
- `created_at`: timestamp
- `id`: a `SERIAL` key regenerated in every snapshot; nothing may depend on it

**`documents`**: URI → 标准化标题 (standardised title)

- `doc_id`: transcript URI (primary key, includes `.md`)
- `title`: standardised title, e.g. `睡前消息588`
- `updated_at`: timestamp

Titles come from the upstream `URI映射.md`. A general rule (`{channel}/{bucket}/{number}.md` → `{Chinese channel name}{unpadded number}`) covers the vast majority, but 29 transcripts — the `misc/` specials, officially duplicated episode numbers, the negative-numbered 产经破壁机 issues — cannot be derived by any rule, so that file is authoritative. The agent LEFT JOINs this table at retrieval time to render citations with the title rather than the raw URI. Every build refreshes all titles, since upstream may correct a title without touching the transcript.

**`transcripts`**: rendered transcripts for the in-app reader

- `doc_id`: transcript URI (primary key)
- `canonical_title` / `source_title`: standardised title and the transcript's own `# ` title
- `channel`, `publication_date`: channel name and the `**发布日期**` value
- `body_html`: the rendered page served by the agent's `/transcripts` API
- `source_hash`, `projection_version`: re-render triggers (source edits or a renderer change)

**`index_state`**: build provenance, the baseline for the next incremental build (not read by the agent)

- `file_path`: transcript URI
- `source_hash`: SHA-256 of the complete raw Markdown source
- `body_hash`: SHA-256 of the exact normalized body represented by the chunks and vectors
- `body_normalization_version`: the normalizer version that produced it
- `indexed_at`, `source_observed_at`: last body indexing and last accepted source change

### `rag_meta`

**`snapshots`**: the registry. One row per committed snapshot (`status` `published` or `retired`; failed builds never appear): `snapshot_id`, `schema_name`, `format_version`, `pipeline_fingerprint`, `embedding_space`, `embedding_model`, `embedding_dim`, `normalization_version`, `chunker_version`, `source_commit`, `base_snapshot_id` (incremental builds), `builder_version`, `pinned`, `published_at`, `retired_at`, `index_params` (HNSW parameters) and `stats` (counts, new vs reused embeddings, step timings).

**`indexer_status`**: exactly one row: `last_run_at`, `last_result` (`published` / `no_change` / `skipped_busy` / `failed`), `last_error`, `last_published_at`, `consecutive_failures`.

### `rag_state`

**`file_actions`**: append-only audit log, one row per transcript change a build published: `action_type` (`ADD`, `MODIFY`, `SOURCE_ONLY`, `DELETE`), `source_hash` / `body_hash` (`NULL` for `DELETE`), `run_timestamp` / `processed_at`. Not part of serving; needs no backup.

### Two compatibility dimensions

| Dimension                                | Composition                                                                                                                  | Role                                                                                                    |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| **Pipeline fingerprint**                 | Hash of (`format_version`, `normalization_version` = `BODY_NORMALIZATION_VERSION`, `chunker_version` = `CHUNKER_VERSION`)    | Incremental builds only between snapshots with the same fingerprint; otherwise a full build             |
| **Vector space** `embedding_space`       | `<model>@<dimension>`, e.g. `Qwen/Qwen3-Embedding-4B@2560`; overridable with `embedding.space_id`                            | Vectors are reused, and snapshots read by an agent, only within one vector space                        |

- Bump `CHUNKER_VERSION` (`src/chunker.py`) whenever the chunking parameters or logic change, and `BODY_NORMALIZATION_VERSION` (`src/document_loader.py`) whenever normalization changes. Forgetting a bump mixes old and new chunking in one snapshot; code review must check for it.
- Bump `FORMAT_VERSION` (`src/snapshot_schema.py`) whenever the structure or meaning of a table or column the agent reads changes, and add the new format to the agent's `SUPPORTED_FORMATS`.
- A "lineage" is the set of snapshots sharing both the fingerprint and the vector space.

### The `rag_agent` role

The agent connects as `rag_agent`, which has only `USAGE` on `rag_meta` with `SELECT` on its two tables, plus `USAGE` + `SELECT` on published snapshots — granted inside the publish transaction. It has no write privileges anywhere. The indexer ensures the role on every start: with `POSTGRES_AGENT_PASSWORD` set it gets `LOGIN` and that password; without it the role is `NOLOGIN` and the agent falls back to the superuser (with a warning). The indexer itself keeps the superuser.

### Garbage collection

GC runs before and after every build, each deletion in its own transaction holding the advisory lock (`lock_timeout` 5 s; a snapshot still being read is retried on the next run):

| Rule                                                                                                      | Rationale                                                         |
| --------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| The current lineage keeps its latest 2 published snapshots                                                | The current one plus an instant rollback target                   |
| Every other lineage (including `legacy`) keeps only its latest 1, for 7 days after the current lineage superseded it | The previous code version can still be rolled back to   |
| `pinned` snapshots are never deleted                                                                      | Explicit long-term pinning                                        |
| `retired` snapshots are kept for 24 hours                                                                 | A mistaken retirement can be undone                               |
| Nothing is deleted within 10 minutes of being superseded or retired                                       | Protects in-flight requests (the agent switches within 15 s)      |

## Upgrading from the pre-snapshot schema

Existing deployments are adopted automatically on the first start of the snapshot-aware indexer, with no manual steps and no required upgrade order:

1. The existing `rag` schema is registered as snapshot `legacy` (format 1, vector space from the column's actual dimension and the configured model); `rag.file_actions` moves to `rag_state`, `rag.indexing_history` is renamed to `rag.index_state`, and `rag_agent` gets read access to `rag`.
2. The first build is a full build in a new lineage that reuses every vector from `legacy` (normally zero embedding calls, a few minutes).
3. `legacy` is kept for 7 days after that, so the old agent keeps working and data can be rolled back with `retire`.

Rolling the **indexer** back to a pre-snapshot version after adoption is not supported (it would write to the renamed tables and modify `legacy` in place). The agent can be rolled back freely until `legacy` is garbage-collected. Databases older than v0.3 must first apply `storage/postgres/migrations/` (historical).

## Changing the Embedding Model

Vectors from different models are not comparable, even at the same dimension, and each model emits a fixed dimension (e.g. `Qwen/Qwen3-Embedding-4B` = 2560, `text-embedding-3-small` = 1536, `text-embedding-3-large` = 3072). A model change is therefore a new vector space, built as a new snapshot while the old one keeps serving — no stopped services, no `ALTER TABLE`, no state to clear.

### Runbook

```bash
# 1. Edit config.yml: the embedding group (model / base_url / api_key).
#    Set EMBEDDING_DIM in .env to the new model's output dimension.

# 2. Restart the indexer only. The next build sees a new vector space, finds no
#    incremental base, and does a full build embedding the whole corpus
#    (about 25 minutes). The old snapshot keeps serving the current agent.
docker compose up -d indexer
docker compose exec indexer python -m src.snapshots build   # or wait for the schedule

# 3. Check the new snapshot is published in the new space.
docker compose exec indexer python -m src.snapshots list

# 4. Restart the agent with the same config: it selects the snapshot in its
#    (new) vector space. A single agent instance is briefly unavailable.
docker compose up -d agent
docker compose exec agent curl -s localhost:8000/health
```

The old snapshot stays readable for 7 days, so rolling back is restoring the old configuration and restarting the agent. To keep the same model but stop mixing vectors from a different provider, set `embedding.space_id` (identically for indexer and agent) instead.

**With blue-green deployments** (production; see the [blue-green design](../docs/designs/20261008_blue-green-deployment.md), section 5), step 4 involves no interruption: recreate **only** the indexer in step 2 (`docker compose -p bedtimenews-agent -f compose.data.yml up -d indexer`); the running application instance (blue) keeps serving the old space, because its agent loaded its configuration at startup. Once the new snapshot is published, deploy a new application instance (green), which starts with the new configuration and selects the new snapshot, and switch the proxy to it. Blue's data is frozen from step 2 until the switch (the old lineage gets no new builds), about half an hour.

### Production: the data layer

In production the stack is two layers: `compose.data.yml` (postgres + indexer, project `bedtimenews-agent`, upgraded in place) and one `compose.app.yml` project per application instance. Commands on the indexer or postgres there need `-p bedtimenews-agent -f compose.data.yml`, or rely on the VM's `.env` guard `COMPOSE_FILE=compose.data.yml`, which makes a plain `docker compose ...` act on the data layer only. Never run `down -v` on `bedtimenews-agent`, and never run `docker compose down` there on an application instance's behalf: application instances are retired with `docker compose -p <instance> -f compose.app.yml down -v`. Self-hosters using the umbrella `docker-compose.yml` run every command here as written.

## Data Backup and Restore

### Snapshots are not backups

All snapshots live in the same Postgres instance on the same disk; a disk failure or an accidentally deleted data directory loses them all.

- **The worst case can be rebuilt**: all data comes from the transcripts git repository; a full build on an empty database restores it (one full embedding pass, about 25 minutes).
- **Optional off-host export** of the latest snapshot (a few hundred MB):

  ```bash
  docker compose exec postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
    -n rag_s<id> -n rag_meta > rag-<id>.dump
  ```

  To restore it, `pg_restore` the dump into the database and make sure the snapshot's row in `rag_meta.snapshots` has `status = 'published'`.
- The audit log `rag_state.file_actions` needs no backup.

### Backing up the whole volume

The PostgreSQL data volume is stored in a gitignored directory in the codebase. You can back up and restore the entire database with standard tar archives.

**Prerequisites**: stop all services to ensure data consistency:

```bash
docker compose down
# Production (two layers): stop every application instance first, then the
# data layer — without -v:
#   docker compose -p <instance> -f compose.app.yml stop
#   docker compose -p bedtimenews-agent -f compose.data.yml down
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

The backup includes every snapshot, the registry, the audit log and the database configuration.

**Restore from backup**:

```bash
docker compose down

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
docker compose exec indexer python -m src.snapshots list
docker compose exec indexer python -m src.debugger stats
```

### Notes

- **Local only**: the postgres data directory is in `.gitignore` and not tracked by version control
- **Migration**: backups are portable and can be restored on different machines
- **Disk space**: steady state is two snapshots (~500 MB); a build temporarily needs about one more snapshot plus WAL

## Project Structure

```plaintext
indexer/src/
├── entrypoint.py        # Container entry point (immediate build + scheduler)
├── pipeline.py          # One run: lock, bootstrap, plan, build, GC, status
├── builder.py           # Snapshot builder: plan, disk precheck, Phase A, Phase B, self-check
├── catalog.py           # rag_meta/rag_state, rag_agent role, adoption, registry, GC
├── snapshot_schema.py   # Snapshot ids, fingerprint, vector space, per-format DDL
├── snapshots.py         # Ops commands (list, status, retire, pin, build, ...)
├── run_lock.py          # Whole-run file lock
├── db.py                # Connections, advisory lock, SIGTERM cancellation
├── scheduler.py         # Cron scheduler (no catch-up)
├── git_sync.py          # Repository sync and pinned commit
├── file_scanner.py      # File system scanning
├── document_loader.py   # Markdown processing (BODY_NORMALIZATION_VERSION)
├── change_detector.py   # Source/body hash diff against a snapshot's index_state
├── chunker.py           # Semantic chunking (CHUNKER_VERSION)
├── embeddings.py        # Embedding generation (OpenAI-compatible client)
├── debugger.py          # Read-only debug commands (test, stats, history, recent, inspect, logs)
├── transcript_export.py # Reader projection (Markdown -> HTML)
├── uri_mapping.py       # URI映射.md title table parser
├── models.py            # Data models
├── paths.py             # Path management
└── settings.py          # Configuration
```

## Monitoring

### Check Service Status

```bash
# View logs
docker compose logs -f indexer

# Last run result and consecutive failures
docker compose exec indexer python -m src.snapshots status

# Snapshots and their sizes
docker compose exec indexer python -m src.snapshots list

# Check the unprivileged scheduler process
docker compose top indexer
```

The agent's `GET /health` also reports the snapshot it serves, its data age and `indexer_status`; alert on, for example, 3 consecutive failures or no run for more than 3 hours.

### Expected Output

After the first run, you should see:

- Repository cloned to `indexer/data/BedtimeNews-Transcripts/`
- A published snapshot in `python -m src.snapshots list`, with its chunks in `rag_s<id>.document_chunks`
- File actions logged in `rag_state.file_actions`
- `last_result` = `published` in `python -m src.snapshots status`

Each published build logs its stats (also stored in `rag_meta.snapshots.stats`): transcript and chunk counts, new vs reused embeddings, the reuse source, the change counts and per-step timings.

## Troubleshooting

**No snapshot published:**

```bash
# Last result and error
docker compose exec indexer python -m src.snapshots status

# Check logs for errors
docker compose logs indexer | grep -iE "error|failed"

# Build manually
docker compose exec indexer python -m src.snapshots build

# Verify git clone succeeded
docker compose exec indexer ls -la /data/BedtimeNews-Transcripts/
```

**`skipped_busy`:** another run holds the run lock (the scheduled run, a manual `build`, or a second indexer container sharing the data directory) or the database advisory lock. Wait for it to finish; do not delete `/data/.indexer.lock`.

**Disk precheck failed:** the Postgres data filesystem has less than max(2 GB, 3 × the base snapshot) free. Free disk space; GC already ran before the check. Check that `${POSTGRES_DATA_DIR}` is mounted read-only at `/pgdata` (without the mount the check is skipped with a warning).

**Self-check failed:** nothing was published and the previous snapshot keeps serving. The error names the check:

- *snapshot is empty / transcripts have no chunks / doc_id sets differ*: an upstream or chunking problem; inspect the named transcripts.
- *re-embedded sample disagrees with stored vector*: the embedding service returns different vectors than before, or `embedding.model` does not match the model the endpoint actually serves.
- *sampled top-k query returned … rows*: the HNSW index is unusable; check the Postgres logs.

**Bad data was published:** roll it back with `python -m src.snapshots retire <id>`; agents fall back to the previous snapshot within 15 s. A build in progress on top of the retired snapshot is abandoned.

**Embedding API errors:**

- Check `embedding.api_key` in `config.yml`
- Verify rate limits not exceeded
- Check API usage in the provider's dashboard
- `Embedding service returned N dimensions, expected M`: `EMBEDDING_DIM` does not match the model's output — see [Changing the Embedding Model](#changing-the-embedding-model)

**Database connection failed:**

- Ensure postgres is running: `docker compose ps postgres`
- Check the `POSTGRES_*` credentials in `.env`
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
