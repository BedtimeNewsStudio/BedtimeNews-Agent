# RAG Snapshot Publishing: Separating Build from Serving

Status: implemented (2026-10-07): phases 0–2 and the code and documentation parts of phase 3. What remains is operational: setting `POSTGRES_AGENT_PASSWORD` in production and, optionally, off-host backups. Section 16 records the decisions; section 18 records where the implementation refines or deviates from this design, and section 13 the phase 0 measurements.

## 1. Purpose

This design has two goals:

1. **Improve the system as a whole.** Remove the direct coupling between the indexer (writer) and the agent (reader) through a shared set of mutable tables, make data updates atomic, reversible and auditable, and lift restrictions such as "changing the embedding dimension requires downtime".
2. **Prepare for blue-green deployment.** Blue-green switching requires the old and new versions to run at the same time. As long as the writer and readers share tables that are modified in place, both versions must follow a permanent "schema changes are additive only" discipline, backed by migration tooling, version gates and maintenance windows. This design turns served data into **immutable, versioned snapshots**, so the old and new versions each read their own snapshot, and that compatibility burden disappears at the root. What remains for blue-green is the application layer only: running multiple instances and switching traffic.

Unchanged: PostgreSQL, pgvector, `halfvec`, the HNSW index, the existing served table structures and the agent's retrieval SQL. This design only changes **how** data is written, published and found by readers.

## 2. Current State and Problems

Current data size (production, measured 2026-10-07): 1,902 transcripts, 13,434 chunks, `embedding halfvec(2560)` (model `Qwen/Qwen3-Embedding-4B`). The agent performs only three kinds of reads: vector top-k (`<=>` + HNSW), listing transcripts by channel, and reading one transcript by `doc_id`. User traffic never writes to the database.

| Part                                                    | Size         |
| ------------------------------------------------------- | ------------ |
| HNSW index on `document_chunks`                         | 127 MB       |
| Vectors (66 MB) and chunk text (27 MB), stored in TOAST | 117 MB       |
| `transcripts` (reader HTML)                             | 42 MB        |
| Other tables and B-tree indexes                         | about 10 MB  |
| Total (database size)                                   | about 314 MB |

Problems:

1. **Readers and the writer share mutable tables.** The indexer runs `INSERT/UPDATE/DELETE` directly against `rag.document_chunks`, `rag.documents` and `rag.transcripts` while the agent is reading them. The two sides are upgraded independently, so the table structure is an implicit contract.
2. **Updates are not atomic.** Within one pipeline run, the RAG sync and the reader-projection sync commit separately; if one fails, the other still lands. A full rebuild (for example after bumping `BODY_NORMALIZATION_VERSION`) takes about 25 minutes, during which users read a mix of old and new data.
3. **The format is baked into the table definition.** `init.sh` fixes the vector dimension as `halfvec(${EMBEDDING_DIM})` when it creates the tables on an empty database. Changing the model or dimension means altering the column type and re-embedding everything, which requires downtime.
4. **Existing databases rely on manual migrations.** `init.sh` only runs on an empty data directory, so existing databases must apply `storage/postgres/migrations/*.sql` by hand, in order.
5. **The writer's private state is mixed with served data.** `indexing_history` and `file_actions` are the indexer's incremental bookkeeping and audit log, yet they live in the same schema as the served tables.
6. **Data cannot be rolled back.** A bad build (for example, the embedding service returning broken vectors) overwrites live data directly.
7. **Existing concurrency hazards.** If `python -m src.pipeline` is run manually while the scheduled run is in progress, two processes each `reset --hard` the same git directory and write to the database at the same time. When a run exceeds the schedule interval, the scheduler fires every missed slot back to back.
8. **The retrieval cache returns stale data.** The agent's retrieval result cache (`retriever.py`, `LRUCache(1000)`) is keyed only on query parameters and never expires, so after a data update the same query keeps returning old results until the entry is evicted.
9. **The agent has full database privileges.** Both the indexer and the agent connect as the superuser created by the Postgres image; the user-facing agent, which is exposed to prompt injection, could in principle rewrite any data.

## 3. Goals and Non-Goals

Goals:

- Served data is published in units of **snapshots**: each build produces one complete, self-consistent schema that is never modified afterwards.
- Publishing is **atomic**: readers see either the old snapshot or the new one, and physically cannot see data that is still being built.
- Readers **choose snapshots according to their own capabilities** (format version, vector space), laying the groundwork for old and new versions running side by side.
- Data updates **do not require restarting** the agent; rolling data back takes a single ops command.
- Changing the embedding model or dimension, or the normalization or chunking rules, happens in a new snapshot without affecting the snapshot being served, and only text that actually changed is sent to the embedding API.
- The agent is **read-only** against the database, enforced by database privileges.
- Self-hosting stays the same (`docker compose up -d`): no new services, and no manual scripts during upgrades.

Non-goals:

- Replacing PostgreSQL or pgvector.
- Running multiple application instances or switching traffic between them (blue-green deployment builds on this design but is out of scope here; see [Application-Layer Blue-Green Deployment](20261008_blue-green-deployment.md)).
- PostgreSQL major-version upgrades.

## 4. Architecture

```
BedtimeNews-Transcripts (git)
          │
          ▼
  indexer (builder, sole writer, superuser)
    take file lock → git sync and pin commit → diff against base snapshot
    Phase A (outside a transaction): chunk; reuse embeddings by text hash,
                                     call the API only for misses
    Phase B (one transaction): create schema → copy base → delete → insert
                               → ANALYZE → build HNSW → self-check
                               → recheck base → register as published → grant → COMMIT
    then: GC, update indexer_status
          │
          ▼
  PostgreSQL (single instance)
    rag_meta    snapshot registry, indexer run status (written only by the indexer)
    rag_state   indexer-private: audit log
    rag_s<id>   read-only snapshots: document_chunks / documents / transcripts / index_state
          │ read-only (role rag_agent)
          ▼
  agent: caches the current snapshot in memory, polls rag_meta every 15 s;
         each request is pinned to one snapshot
          │
          ▼
  web (no direct database access; reads through the agent)
```

## 5. Data Model

### 5.1 Snapshot schema: `rag_s<id>`

- `<id>`: `s` + UTC build time (to the second) + the first 7 characters of the transcripts commit, for example `s20261007t091512z_a1b2c3d`. It is unique (two builds within the same second step to the next second), sortable, and far below Postgres's 63-byte identifier limit.
- Tables:
  - Served data: `document_chunks`, `documents`, `transcripts`, **with the same columns as today's `rag.*`**. The `embedding` column is sized for the build's vector space.
  - Build provenance: `index_state`, with the columns of today's `indexing_history` (`file_path`, `source_hash`, `body_hash`, `body_normalization_version`, and so on). It records which version of which source file each snapshot was built from and serves as the baseline for the next incremental build. The agent does not read it.
- Indexes: the existing B-tree indexes, plus `hnsw (embedding halfvec_cosine_ops)`.
- Table DDL is defined in indexer code per `format_version`, **no longer in `init.sh`**.
- The auto-increment `id` columns (the `SERIAL` primary keys of `document_chunks` and `index_state`) are not stable across snapshots: copies omit that column and the new table generates fresh values. Neither the agent nor anything else may depend on them (verified: the agent only uses `chunk_id` and `doc_id`).
- **Once published, a snapshot is never modified by anyone.**

### 5.2 Two compatibility dimensions

| Dimension                                       | Composition                                                                                                                                                                                                                                        | Role                                                                                                                           |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| **Pipeline fingerprint** `pipeline_fingerprint` | Hash of (`format_version`, `normalization_version`, `chunker_version`)                                                                                                                                                                             | Incremental builds are only possible between snapshots with the same fingerprint; otherwise a full build is done               |
| **Vector space** `embedding_space`              | `<model>@<dimension>` by default, for example `Qwen/Qwen3-Embedding-4B@2560`; can be overridden explicitly with `embedding.space_id` in `config.yml` (for when the same model is served by a different provider and the vectors must not be mixed) | Decides whether vectors can be reused and whether an agent can read the snapshot (query vectors must come from the same space) |

- `chunker_version` is a constant in indexer code. It must be bumped whenever the chunking parameters (currently the defaults in `chunker.py`: target 1000, max 2500, min 50, overlap 150) or the chunking logic change. Likewise, normalization changes bump `normalization_version` (today's `BODY_NORMALIZATION_VERSION`). Both rules live entirely inside the indexer; forgetting a bump mixes old and new chunking in one snapshot, so code review must check for it.
- `format_version` is bumped whenever the structure or meaning of any table or column the agent reads changes. Adding columns the agent does not read, or changing only `index_state`, does not require a bump.

### 5.3 `rag_meta`

```sql
CREATE TABLE rag_meta.snapshots (
    snapshot_id           TEXT PRIMARY KEY,
    schema_name           TEXT NOT NULL UNIQUE,
    format_version        INTEGER NOT NULL,
    pipeline_fingerprint  TEXT NOT NULL,
    embedding_space       TEXT NOT NULL,
    embedding_model       TEXT NOT NULL,
    embedding_dim         INTEGER NOT NULL,
    normalization_version INTEGER,          -- may be NULL for legacy
    chunker_version       INTEGER,          -- NULL for legacy
    source_commit         TEXT,
    base_snapshot_id      TEXT,             -- base of an incremental build; NULL for full builds
    builder_version       TEXT NOT NULL,    -- indexer image version, for traceability
    status                TEXT NOT NULL,    -- published / retired
    pinned                BOOLEAN NOT NULL DEFAULT false,
    published_at          TIMESTAMPTZ NOT NULL,
    retired_at            TIMESTAMPTZ,
    index_params          JSONB,            -- HNSW m, ef_construction, etc.
    stats                 JSONB             -- transcript and chunk counts, new vs reused embeddings, step timings
);

CREATE TABLE rag_meta.indexer_status (      -- exactly one row
    id                    BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    last_run_at           TIMESTAMPTZ,
    last_result           TEXT,             -- published / no_change / skipped_busy / failed
    last_error            TEXT,
    last_published_at     TIMESTAMPTZ,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0
);
```

- Only **committed** snapshots ever appear in `snapshots` (thanks to the single-transaction build in 6.4), so there is no `building` status and no rows left behind by failed builds. Failures are recorded in `indexer_status` and the logs.
- There is no single "current" pointer: each reader applies the selection rule (7.1) to the snapshots it can read and picks the best one. Readers of different versions can therefore select different snapshots at the same time, which is exactly what running old and new versions side by side requires.

### 5.4 `rag_state`: indexer-private

- `file_actions`: the audit log, moved out of `rag`. Append-only, not part of serving, no backup needed.

## 6. Indexer: Build and Publish

### 6.1 Flow of one run

1. **File lock.** Take a non-blocking exclusive `flock` on `${INDEXER_DATA_DIR}/.indexer.lock` and hold it for the whole run. Failing to get it means another run is in progress (the scheduled run in the same container, a manual run via `docker compose exec`, or an accidentally started second container; they all share this host directory). Record `skipped_busy` and exit; do not queue.
2. **Startup bootstrap** (only when the process starts; idempotent): create `rag_meta`, `rag_state` and the role `rag_agent`; adopt legacy data if needed (section 11).
3. **Git sync.** `git fetch` + `git reset --hard origin/main`, and record the commit. The whole build reads files at this version only; transcripts added upstream afterwards are left for the next run. Because every run diffs against the published snapshot, a missed run never loses changes.
4. **Choose the base and diff** (6.2). If nothing changed, record `no_change` and skip to step 7.
5. **GC, then the disk precheck** (6.8, 6.7). On failure, record `failed` and exit.
6. **Phase A** (6.3), then **Phase B** (6.4).
7. **GC again** (after publishing, the oldest snapshot may exceed the retention count; this also runs on no-change runs, to clean up snapshots past their retention period).
8. Update `indexer_status` (reset `consecutive_failures` on success, increment it on failure).

The scheduler changes to compute the next run time from **now**, skipping missed slots instead of firing them back to back.

### 6.2 Base: incremental or full

- **Incremental base**: the most recently published snapshot with `status=published` whose `pipeline_fingerprint` and `embedding_space` both match the current configuration. If one exists, do an incremental build: diff its `index_state` against the current transcripts one by one (reusing the logic in `change_detector.py`, with the data source switched from the mutable `rag.indexing_history` to the snapshot's immutable `index_state`), producing the sets of added, body-changed, metadata-only-changed and deleted transcripts.
- **Full build**: when there is no incremental base (first build, first build after adoption, a bump of the format, normalization or chunker version, or a vector-space change), re-chunk every transcript, while still reusing embeddings per 6.3. The ops command `build --full` forces a full build.
- A failed build advances no state; the next run retries against the same published base.

### 6.3 Phase A: chunking and embedding (outside a transaction)

- Chunk the transcripts that need processing (added and body-changed ones for an incremental build, all of them for a full build).
- **Reuse vectors by text.** The reuse source is the latest published snapshot in the same `embedding_space` (the fingerprint does not have to match). Phase A loads only the set of its `sha256(chunk text)` values to decide hits and misses; the hit vectors themselves never leave the database: Phase B joins them from the reuse source by text hash, server-side, inside the build transaction (see 18).
- Only missed chunks are sent to the embedding API. New vectors are kept in process memory only; if Phase B fails, the next run recomputes them, and since they cover only genuinely new text, the cost is small.
- Result: format upgrades and normalization or chunking changes re-embed only chunks whose text actually changed. Only a vector-space change (new model or dimension) truly requires calling the API for everything (about 25 minutes).

### 6.4 Phase B: build and publish in one transaction

```text
BEGIN;
  SELECT pg_try_advisory_xact_lock(<fixed key>);   -- not acquired: ROLLBACK, record skipped_busy
  SET LOCAL maintenance_work_mem = '256MB';
  SET LOCAL max_parallel_maintenance_workers = 2;
  CREATE SCHEMA rag_s<id>;  create tables per format_version (without HNSW)
  [incremental] copy all four tables from the base, listing columns explicitly and omitting id:
                INSERT INTO rag_s<id>.t (c1, c2, ...) SELECT c1, c2, ... FROM <base>.t;
  [incremental] delete every row of body-changed and deleted transcripts
                (index_state by file_path, the others by doc_id)
  insert Phase A chunks and vectors; update metadata-only transcripts (title, etc.);
  write the reader projection (transcripts)
  ANALYZE all four tables
  CREATE INDEX ... USING hnsw (embedding halfvec_cosine_ops)
  self-check (6.5); on failure: ROLLBACK, record failed
  recheck the base (incremental only): still published and still the newest snapshot
  of its lineage; otherwise ROLLBACK
  INSERT INTO rag_meta.snapshots (..., status='published', published_at=now())
  GRANT USAGE ON SCHEMA rag_s<id> TO rag_agent;
  GRANT SELECT ON ALL TABLES IN SCHEMA rag_s<id> TO rag_agent;
COMMIT;
```

- **Atomicity comes from the transaction itself.** Until commit, the new schema and its registry row are invisible to other sessions. If any step fails, the process is killed or the connection drops, Postgres rolls back automatically: nothing half-built remains and no cleanup logic is needed.
- Copy first, then delete, so modifications and deletions share one code path. Deleted rows are gone before the HNSW index is built, so they never enter it. The few dead tuples left in the tables are reclaimed by autovacuum.
- Columns are listed explicitly in the copy so that a column-order difference between the base and the new table cannot misalign a `SELECT *`, and so that `id` can be skipped.
- Data is loaded and `ANALYZE`d before the HNSW index is built: building an index in bulk is much faster than maintaining it row by row, and a new table without statistics can get bad query plans, so `ANALYZE` must happen before publishing.
- Effect of the long transaction: it holds back the database-wide VACUUM horizon while the build runs. This database has almost no other writes, so the effect is negligible.
- RAG data and the reader projection are **published together in one transaction**; the "one side succeeded, the other failed" intermediate state no longer exists.

### 6.5 Self-check

If any check fails, the whole transaction rolls back and `failed` is recorded:

- **Completeness**: the snapshot is not empty (at least 1 transcript); every transcript has at least 1 chunk; every chunk has a vector whose dimension equals the vector space's dimension and whose norm is non-zero; the `doc_id` sets of `documents`, `transcripts` and `index_state` agree.
- **Queryable**: a sampled vector top-k query returns a full result set.
- **Vectors match the model** (when the build embedded new chunks, or for full builds): pick 5 random chunks, embed them again, and require a cosine similarity of at least 0.99 against the stored vectors. This catches a misbehaving embedding service, a broken reuse mapping, or a configured model that does not match the one actually served. Incremental builds that only change titles skip this check, so an embedding API outage does not block metadata-only updates.

### 6.6 Concurrency and failure

| Situation                                                                           | Handling                                                                                                                                                                                                                                             |
| ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Two runs at once (manual plus scheduled, or a second indexer container)             | The file lock is held for the whole run (6.1), so only the holder touches the git directory; the write transaction also takes a database advisory lock, guarding against two indexers with different data directories connected to the same database |
| A run exceeds the schedule interval                                                 | The scheduler computes the next run from now and does not catch up                                                                                                                                                                                   |
| The base snapshot is retired mid-build (an operator found bad data and rolled back) | The base is rechecked in the same transaction before publishing; if the check fails the build is abandoned, so bad data is not republished                                                                                                           |
| The process is killed, the connection drops, or the host reboots mid-build          | The transaction rolls back automatically; nothing is left behind. On SIGTERM the indexer cancels the running statement and exits; compose sets `stop_grace_period: 30s` for the indexer                                                              |
| New transcripts arrive upstream during a run                                        | Not a race: the build is pinned to the commit taken at the start, and new transcripts are handled by the next run                                                                                                                                    |

Reader-side concurrency is covered by 7.2 (each request is pinned to one snapshot) and 7.5 (read access is granted only at publish time).

### 6.7 Disk precheck

A build writes roughly one snapshot's worth of new data plus a similar amount of WAL (both the copy and the index build are WAL-logged), and both occupy disk until a checkpoint recycles the WAL. **If WAL fills the disk, Postgres PANICs and the entire live database goes down**, so the check must happen before building.

- Run GC (6.8) first to free space, then check.
- Requirement: free space on the filesystem holding the data directory ≥ max(2 GB, 3 × the base snapshot's size). Otherwise, do not build; record `failed` with the reason.
- Method: compose additionally mounts `${POSTGRES_DATA_DIR}` read-only into the indexer (for example at `/pgdata`), and the indexer calls `statvfs` on that mount point. The indexer already connects as the database superuser, so a read-only mount adds no new trust.

### 6.8 Garbage collection (GC)

A "lineage" is the set of snapshots sharing both `pipeline_fingerprint` and `embedding_space`. Retention rules:

| Rule                                                                                                                       | Rationale                                                                                                                            |
| -------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| The current lineage (matching the indexer's current configuration) keeps its latest 2 `published` snapshots                | The current one plus one instant rollback target                                                                                     |
| Every other lineage (including `legacy`) keeps only its latest 1, for 7 days after being superseded by the current lineage | After a format or model change, the previous code version still has a readable snapshot and can be rolled back to within 7 days      |
| `pinned` snapshots are never deleted                                                                                       | Pinned explicitly with the `pin` command, for example when `RAG_SNAPSHOT` pins an agent to a snapshot for a long time                |
| `retired` snapshots are kept for 24 hours                                                                                  | So a mistaken retirement can be undone                                                                                               |
| **Grace period**: no snapshot is deleted within 10 minutes of being superseded or retired                                  | Protects in-flight requests. The agent switches to a new snapshot within 15 s and `/chat` runs at most 240 s, so 10 minutes is ample |

- The 240 s bound on `/chat` that the grace period relies on is enforced by the agent's overall chat time limit (`agent/src/chat.py`, [blue-green design](20261008_blue-green-deployment.md) section 4.3).
- Deletion runs `SET lock_timeout = '5s'`, then `DROP SCHEMA ... CASCADE`, and removes the registry row. Failing to get the lock means a query is still reading the snapshot; it is retried on the next run.
- GC runs inside a write transaction holding the database advisory lock, so it never runs concurrently with a build and cannot delete a base snapshot that is in use.

### 6.9 Ops commands

Run `python -m src.snapshots <command>` inside the indexer container:

| Command                   | Effect                                                                                                |
| ------------------------- | ----------------------------------------------------------------------------------------------------- |
| `list`                    | List snapshots: lineage, status, publish time, size, pinned or not                                    |
| `status`                  | Show `indexer_status`                                                                                 |
| `retire <id>`             | Retire a snapshot (data rollback); the agent falls back to the previous readable snapshot within 15 s |
| `unretire <id>`           | Undo a retirement (within 24 hours)                                                                   |
| `pin <id>` / `unpin <id>` | Pin or unpin; pinned snapshots are never garbage-collected                                            |
| `build [--full]`          | Run a build immediately, protected by the same file lock and advisory lock                            |

## 7. Agent: Reading Snapshots

### 7.1 Selection rule

An agent's read capability is defined by two things:

- The **set** of `format_version`s it supports (a code constant). Usually it has one member. When a release changes the format and supporting the old format too is cheap, the agent can support both, so that in a single-instance deployment an agent upgraded before the indexer still has a snapshot to read.
- Its configured `embedding_space` (derived from the model and dimension in `config.yml`, or overridden by `embedding.space_id`).

Among the snapshots in `rag_meta.snapshots` with `status=published`, a format in the supported set, and a matching vector space, the agent **picks the highest format version first, then the most recently published**. When `RAG_SNAPSHOT=<id>` is set, it uses that snapshot directly (for troubleshooting, long-term pinning, or pinning a particular deployment to a particular snapshot).

**Compatibility before adoption**: if the database has no `rag_meta` yet, the agent treats the old `rag` schema as an implicit snapshot (format 1) and reads the actual dimension of the `embedding` column from the system catalog to check it against its own configuration. Either the agent or the indexer can therefore be upgraded first.

When no readable snapshot exists, the agent is not ready (`/health` returns 503 with the reason) and keeps running, waiting for the next poll.

### 7.2 Each request is pinned to one snapshot

- One `/chat` may retrieve several times. The current snapshot is taken when the request starts and used for the whole request, so the data within one answer is consistent. The reader endpoints (transcript list, single transcript) work the same way.
- Table names in SQL are prefixed with the snapshot schema, composed with `psycopg2.sql.Identifier`, **without using `search_path`**: pooled connections are reused across requests, and leftover session settings are error-prone. The changes are concentrated in `agent/src/vector_db.py`.

### 7.3 Caching and switching the current snapshot

- The "current snapshot" is cached in the agent's **process memory**; request handling reads only this in-memory value and **never queries `rag_meta`**.
- A background task polls `rag_meta.snapshots` every **15 seconds** and reapplies 7.1. When the result changes, it atomically swaps the in-memory value: new requests use the new snapshot, while in-flight requests keep the snapshot they took at the start. No restart is needed.
- When the current snapshot is retired, the next poll falls back to the previous readable snapshot, within 15 s at most.
- With `RAG_SNAPSHOT` set, the agent does not follow changes, but still checks that the snapshot exists and is not retired; otherwise it becomes not ready.
- No `LISTEN/NOTIFY` in the first phase: data changes a few times a week, a 15-second delay is fine, and notifications would need a dedicated long-lived connection, a background thread and reconnect handling, which is not worth the complexity. It can be added later if a real need appears.
- **The retrieval result cache key must include the snapshot ID**, so old entries become unreachable after a switch. This also fixes problem 8 in section 2. The query-embedding cache is unaffected (within one vector space, the same question has the same vector).

### 7.4 Health check and observability

`GET /health` (also suitable as a readiness probe for deployment orchestration):

- **Ready** (200): the database is reachable and a readable snapshot has been selected. Otherwise 503, with the reason (database unreachable, no readable snapshot, pinned snapshot no longer exists, and so on).
- The response body includes the current snapshot ID, its publish time, data age, `source_commit`, and `indexer_status` (last run time, result, error, consecutive failures). The indexer's status **does not affect readiness** (old data can still be served); it lets monitoring judge whether data is being updated normally, for example alerting after 3 consecutive failures or more than 3 hours without a run.
- When the database is unavailable, the agent **does not exit**: it stays not ready and reconnects with backoff. Adoption (7.1) and running versions side by side both rely on this.
- It does not call the LLM.

### 7.5 Read-only role `rag_agent`

- The indexer keeps using the Postgres superuser: it is an internal component that has to create schemas, grant privileges and create roles anyway. A dedicated indexer role would introduce a bootstrap problem (creating roles requires the role to exist first) for little benefit.
- The agent connects as `rag_agent` instead, with only: `USAGE` on `rag_meta` and `SELECT` on its two tables, plus `USAGE` + `SELECT` on published snapshots (including `legacy`). **No write privileges at all.**
- Read access to a snapshot is granted **inside the publish transaction**, so `rag_agent` cannot see uncommitted data by privilege as well (uncommitted data is invisible anyway; this is a second guarantee).
- The indexer ensures the role exists on every start: with `POSTGRES_AGENT_PASSWORD` set, the role gets `LOGIN` and its password is synced (changing the password means editing `.env` and restarting both services); without it, the role is `NOLOGIN`.
- The agent connects as `rag_agent` when `POSTGRES_AGENT_PASSWORD` is set; otherwise it falls back to the superuser and logs a warning. Existing self-hosted deployments therefore keep connecting after an upgrade, and production enables the role by setting the variable.
- Result: even if the agent has a bug or is manipulated by prompt injection, it cannot modify or delete any data.

## 8. How Each Kind of Change Flows

| Change                                    | Build type  | New embedding calls                     | Live service                                                                                                                                                                                                                                                                |
| ----------------------------------------- | ----------- | --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Add, modify or delete transcripts         | Incremental | Only chunks of changed transcripts      | Unaffected; the agent switches within 15 s                                                                                                                                                                                                                                  |
| Change only metadata such as titles       | Incremental | None                                    | Same as above                                                                                                                                                                                                                                                               |
| Bump the normalization or chunker version | Full        | Only chunks whose text actually changed | Same as above                                                                                                                                                                                                                                                               |
| Snapshot format upgrade                   | Full        | Usually none                            | The new-format snapshot is selected only by agents that support it; with a single instance, upgrade the indexer first, or have the new agent support both formats                                                                                                           |
| Change the embedding model or dimension   | Full        | All (about 25 minutes, at low cost)     | Old agents keep serving during the build; the new snapshot is selected only by agents configured with the new model. With a single instance, restarting the agent with the new configuration causes a brief interruption; running old and new agents side by side avoids it |

## 9. Storage Budget

Each snapshot is about 300 MB (see the breakdown in section 2).

| Scenario                                  | Snapshots                             | Approximate size                                           |
| ----------------------------------------- | ------------------------------------- | ---------------------------------------------------------- |
| Steady state                              | 2 in the current lineage              | 600 MB                                                     |
| During a build                            | 2 published + 1 uncommitted, plus WAL | about 1.2 GB (drops back after the build and a checkpoint) |
| Within 7 days of a format or model change | 1 more from another lineage           | about 900 MB                                               |

- On the production host (2026-10-07) about 9.7 GB is free; steady state uses about 6% of that.
- Snapshots are **not deduplicated** against each other: the largest part is each snapshot's own HNSW index, which deduplication could not save, while shared storage would stop snapshots from being self-contained and force reference counting into GC, rollback and backup. Not worth it.
- An improvement over today: in-place inserts, updates and deletes slowly bloat the tables and the HNSW index (dead tuples, index fragmentation) and need VACUUM or REINDEX. Snapshots are built fresh and compact every time, and deleting one is a `DROP SCHEMA` that frees the space immediately.

### 9.1 Snapshots are not backups

All snapshots live in the same Postgres instance on the same disk; disk failure or accidental deletion of the data directory loses them all at once.

- **The worst case can be rebuilt**: all data comes from the transcripts git repository, and a full build on an empty database restores it in about 25 minutes, plus the cost of one full embedding pass.
- **Optional off-host backup** (not in the first phase): periodically export the latest published snapshot to somewhere off the host; to restore, `pg_restore` it and register it as `published`:

  ```bash
  pg_dump -Fc -n rag_s<id> -n rag_meta > rag-<id>.dump   # a few hundred MB
  ```

- The audit log `rag_state.file_actions` is just a record and needs no backup.

## 10. Relation to the Existing Deployment

- **Compose services do not change**: still postgres, indexer, agent and web, so self-hosted `docker compose up -d` is unaffected. Additions: the indexer's read-only mount of `${POSTGRES_DATA_DIR}` (6.7) and `stop_grace_period: 30s`; the optional `POSTGRES_AGENT_PASSWORD`.
- `init.sh` only creates the `vector` extension. `rag_meta`, `rag_state`, the role and the snapshot tables are all created by the indexer at startup and during builds.
- `EMBEDDING_DIM` no longer determines a column type at database creation; it is the dimension of the build's vector space and is recorded in the snapshot registry.
- The manual migrations in `storage/postgres/migrations/` are no longer used after adoption, and no new migrations of that kind will be added.

## 11. Rollout and Adoption

### 11.1 Automatic adoption

On every start, the new indexer checks the following, inside the file lock and a write transaction, idempotently:

1. Create `rag_meta` and `rag_state` if missing; ensure the role `rag_agent` exists (7.5).
2. If the old `rag` schema exists and is not yet registered:
   - Register it as snapshot `legacy` (`schema_name=rag`, `format_version=1`, `status=published`). The vector space is filled in from the actual dimension of the `embedding` column in the system catalog and the configured model. `pipeline_fingerprint` is set to `legacy`, which never matches a new build, so **the first build after adoption is always a full build** (reusing every vector per 6.3, normally with no embedding calls). It produces the first regular snapshot and exercises the full-build path at the same time.
   - Move `rag.file_actions` into `rag_state`, and rename `rag.indexing_history` to `rag.index_state`.
   - Grant `rag_agent` read-only access to `rag`.
3. `legacy` follows the "other lineage" retention rule: it is kept for 7 days after the first regular snapshot supersedes it.

The old agent only reads `document_chunks`, `documents` and `transcripts`, so adoption does not affect it; the new agent works both before and after adoption (7.1). Therefore **there are no manual steps and no required upgrade order**. Self-hosted users upgrade with `docker compose pull && docker compose up -d`.

### 11.2 Rollback

- Data rollback: `retire` the new snapshot; the agent falls back to `legacy` or the previous snapshot (`legacy` is kept for at least 7 days).
- Code rollback: after adoption, **rolling the indexer back to an old version is not supported** (the old version would write to the renamed `rag.indexing_history` and modify `legacy` in place). If a rollback is truly needed, first run a reverse script (move the tables back, restore the names, drop `rag_meta`), then start the old indexer. The agent can be rolled back freely: the old agent only reads the `rag` schema and works until `legacy` is garbage-collected.

## 12. Test Plan

1. **Incremental build**: modify one transcript; the new snapshot calls the embedding API only for that transcript's chunks, the remaining rows are copied from the base, counts match the base, and `id`s do not collide.
2. **Full build with reuse**: after bumping `chunker_version`, only chunks with changed text call the embedding API; the first build after adoption makes 0 (or nearly 0) calls.
3. **Atomicity**: kill the indexer at different points in Phase B; no leftover schema or registry row remains, and the agent keeps reading the old snapshot throughout.
4. **Mutual exclusion**: start a manual run while a scheduled run is in progress; the manual run records `skipped_busy` and exits without touching the git directory or the database.
5. **No catch-up**: simulate a run longer than the schedule interval; no back-to-back catch-up runs follow.
6. **Rollback during a build**: `retire` the base snapshot while a build is running; the build is abandoned before publishing.
7. **Self-check rejection**: builds that are empty or missing chunks or vectors are rejected; changing the configured model so it no longer matches the actual service makes the sampled re-embedding check fail.
8. **Disk precheck**: with free space below the threshold, no build runs and the reason is recorded.
9. **Switching**: publish a new snapshot during a long `/chat`; that request uses the old snapshot throughout and later requests use the new one; no `rag_meta` query appears on the request path.
10. **Retrieval cache**: the same query returns each snapshot's own results before and after a switch.
11. **Compatible selection**: a higher-format snapshot is selected only by agents that support it; snapshots in a different vector space are never selected; an agent supporting two formats prefers the higher one.
12. **GC**: snapshots superseded less than 10 minutes ago are kept; `pinned` snapshots are kept; other lineages are kept for 7 days; when a query holds a lock, `DROP` times out and is retried on the next run.
13. **Privileges**: every write by `rag_agent` is rejected; without `POSTGRES_AGENT_PASSWORD` the agent falls back to the superuser and warns.
14. **Adoption**: start the new indexer on a copy of production data; `legacy` is registered correctly and agent retrieval results are identical to before adoption; both upgrade orders (agent first, indexer first) work.
15. **Self-hosting**: on a fresh, empty database, `docker compose up -d` leaves the agent not ready until the first build finishes, after which it becomes ready automatically.

## 13. To Measure, and Risks

Measured 2026-10-07 on the full corpus (1,902 transcripts, 13,434 chunks, 2560 dimensions) on a local 4-core machine, PostgreSQL 16 + pgvector 0.8.1, `maintenance_work_mem` 256 MB, 2 parallel maintenance workers. Embeddings came from a deterministic local stand-in, so embedding-API time is excluded:

| Measurement                                                    | Result                                                                 |
| -------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Snapshot size (heap + TOAST + indexes)                         | ~240 MB (estimate in section 2: ~300 MB)                               |
| Full build, every vector reused (e.g. first build after adoption) | 36 s end to end; Phase B 19 s (load 4 s, HNSW 12 s, self-check 3 s) |
| Incremental build, one transcript changed                      | 19 s end to end; Phase B 14 s (copy base 1–2 s, HNSW 12 s)             |
| Full build, every vector new                                   | Phase B 49 s before the vector-literal fix in 18, 35 s after (load 20 s, HNSW 12 s); Phase A is dominated by the embedding API |
| WAL written per build                                          | ~205 MB (full or incremental: both copy/write the whole snapshot)      |
| Peak extra disk during a build                                 | ~450 MB (new snapshot + WAL), well within 3 × base (~720 MB)           |
| `SIGTERM` during Phase B                                       | statement cancelled, process exits in 0.2 s, nothing left behind       |
| `SIGKILL` during Phase B                                       | backend notices the dead client and rolls back within ~3 s (18)        |

The HNSW build dominates every build; incremental builds cannot avoid it because each snapshot owns its index. Still open, on production hardware: the effect of a build on live query latency on the shared 4-core host (parallelism may need to drop to 1), and `statvfs` through the read-only mount of a `postgres`-owned mode-700 directory (the mount point itself only needs path lookup, so it is expected to work).

The original phase 0 checklist:

- **Build time**: copying about 300 MB, `ANALYZE`, and the HNSW build, plus the effect of `maintenance_work_mem` (256 MB vs the default 64 MB) and `max_parallel_maintenance_workers`. Expected to be minutes.
- **WAL volume and peak disk usage**: verify that the 3x factor in 6.7 is enough.
- **Impact of a build on live queries**: the production host has 4 cores shared with other services, so index-build parallelism may need to drop to 1.
- **`statvfs` through the read-only mount**: confirm it reports correct free space on a data directory owned by `postgres` with mode 700.

## 14. Phases

Each phase can ship on its own.

| Phase | Scope                                                                                                                                                                                                                                                                                                                 | After shipping                                                                                                         |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| 0     | Measurements from section 13                                                                                                                                                                                                                                                                                          | —                                                                                                                      |
| 1     | Agent: snapshot abstraction (reading the implicit `legacy` before adoption), per-request pinning, schema prefixes, snapshot ID in the retrieval cache key, 15-second polling, `/health`, staying up when the database is unavailable; optional `rag_agent` connection. Indexer: file lock, scheduler without catch-up | Live behavior unchanged, but the stale retrieval cache and the manual-run race are fixed                               |
| 2     | The indexer becomes the snapshot builder: automatic adoption, fingerprints and bases, Phase A vector reuse, single-transaction Phase B, self-check, disk precheck, GC, `indexer_status`, ops commands                                                                                                                 | Data is published as snapshots and the agent switches automatically; the data layer is ready for blue-green deployment |
| 3     | Enable `POSTGRES_AGENT_PASSWORD` in production; slim down `init.sh`; README and self-hosting docs; remove the old migration path; optional off-host backup                                                                                                                                                            | The agent's read-only access is enforced by database privileges                                                        |

## 15. Known Limitations

- When `doc_id_filter` is combined with HNSW, pgvector takes the nearest neighbors first and filters afterwards, so it may return fewer than `match_count` results. This is existing behavior and this design does not change it.
- The chunker and normalization versions must be bumped by hand (5.2); a missed bump mixes old and new behavior in one snapshot. Code review is the safeguard.
- After 7 days, rolling back to an agent version that uses an old format or old model first requires rebuilding a snapshot in that format.

## 16. Decision Record (2026-10-07)

| #   | Question                                | Decision                                                                                                                                                                                                                                                                               |
| --- | --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Reader heartbeats                       | None. GC is protected by the grace period (no deletion within 10 minutes of being superseded) and `pinned`; the agent is therefore fully read-only                                                                                                                                     |
| 2   | Notification mechanism                  | Poll every 15 seconds; no `LISTEN/NOTIFY`                                                                                                                                                                                                                                              |
| 3   | Migration approach                      | The indexer adopts existing data automatically on start; before adoption the agent reads the implicit `legacy`; no required upgrade order                                                                                                                                              |
| 4   | Database roles                          | Add only the read-only `rag_agent`; the indexer keeps the superuser; without a configured password the agent falls back to the superuser and warns                                                                                                                                     |
| 5   | Retention                               | Current lineage keeps 2; other lineages keep their latest 1 for 7 days; `retired` kept 24 hours; 10-minute grace period; `pinned` never deleted                                                                                                                                        |
| 6   | Disk precheck                           | GC first, then require free space ≥ max(2 GB, 3 × base snapshot), via `statvfs` on a read-only mount                                                                                                                                                                                   |
| 7   | Self-check                              | No check for drops in transcript or chunk counts (the transcript library generally does not delete content, so no override for deletions is needed either); only completeness and queryability checks, plus a sampled re-embedding cosine check (≥ 0.99) when new embeddings were made |
| 8   | Format version                          | The agent declares a supported set, usually of one; pick the highest format, then the newest; bump only when agent-visible structure or meaning changes                                                                                                                                |
| 9   | Model identity                          | `vector space = model@dimension`, overridable with `embedding.space_id`                                                                                                                                                                                                                |
| 10  | HNSW parameters                         | Keep pgvector defaults, recorded in `index_params`; `maintenance_work_mem` 256 MB and parallelism 2, pending phase 0 measurements                                                                                                                                                      |
| 11  | Observability                           | `rag_meta.indexer_status`, exposed through the agent's `/health`, not affecting readiness                                                                                                                                                                                              |
| 12  | Build triggers                          | Hourly schedule plus the manual `build` command; no catch-up                                                                                                                                                                                                                           |
| 13  | Metadata-only changes                   | Also produce a new snapshot; no merging                                                                                                                                                                                                                                                |
| 14  | Embedding reuse                         | Implemented in phase 2, keyed by `sha256(chunk text)` from the latest snapshot in the same vector space; no separate cache table                                                                                                                                                       |
| 15  | Build transaction                       | Phase B completes in one transaction; no `building` status                                                                                                                                                                                                                             |
| 16  | Concurrency protection                  | A file lock guards the whole run and the git directory; the write transaction takes an advisory lock                                                                                                                                                                                   |
| 17  | Table references                        | Schema-prefixed identifiers, no `search_path`; copies list columns explicitly and omit `id`                                                                                                                                                                                            |
| 18  | Human-readable snapshot labels          | Not needed. The ID already contains the build time and the transcripts commit, and the registry records `builder_version`                                                                                                                                                              |
| 19  | GC retention count                      | The current lineage keeps 2 (the current one plus an instant rollback target)                                                                                                                                                                                                          |
| 20  | Heartbeats and `LISTEN` on the web side | Not needed. The web service does not access the database directly; it reads through the agent                                                                                                                                                                                          |

## 17. Documentation to Update When Implemented

All items below were updated with the implementation, except `agent-workflow.svg`, whose change was optional and was not made.

Every README exists in Chinese, English and Spanish (`README.md`, `README.en.md`, `README.es-ES.md`); **all three are updated together**. The "Phase" column refers to the phase in section 14 that the update ships with; documentation changes go in the same PR as the code, not as a follow-up.

### 17.1 Diagrams (`docs/diagrams/`)

| Diagram                     | Referenced by                         | Required change                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Phase |
| --------------------------- | ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `system-architecture.svg`   | Main README (all three languages)     | Replace the central "SHARED STATE / PostgreSQL + pgvector / document chunks · vector embeddings · index history" block with: read-only snapshots `rag_s<id>` (chunks, vectors, transcripts), `rag_meta` (snapshot registry, indexer status), `rag_state` (audit log). On the indexer side, change "durable per-file writes" to "build · publish snapshot"; on the agent side, mark "search" as read-only (`rag_agent`) and note that it reads per snapshot. Rewrite the diagram's `<title>`/`<desc>` text to match                                                                                                                                                          | 2     |
| `indexer-pipeline.svg`      | Indexer README (all three languages)  | **Redraw entirely.** It currently shows "five phases, one transaction per file". Change it to: file lock → git sync and pin commit → choose base and diff against the base snapshot's `index_state` (stop if nothing changed) → GC and disk precheck → Phase A (chunking; reuse vectors by text hash, call the API only for misses) → Phase B (one transaction: create schema, copy base, delete, insert, write reader projection, ANALYZE, build HNSW, self-check, recheck base, register as published and grant) → GC → update `indexer_status`. Show both the incremental and full paths, and that "any failure rolls back entirely; published snapshots are unaffected" | 2     |
| `agent-workflow.svg`        | Agent README (all three languages)    | No change needed (retrieval is still pgvector nearest-neighbor search). Optionally add a "current snapshot" line under the "Retrieve" node                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | —     |
| `frontend-architecture.svg` | Frontend README (all three languages) | No change                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | —     |

### 17.2 READMEs

| File                     | Locations to change                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Phase |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| Main `README`            | In the architecture highlights, extend the "Database: PostgreSQL + pgvector" bullet with snapshot publishing. In the setup step that copies `.env`, rewrite the description of `EMBEDDING_DIM` (it no longer sizes the database column) and add the optional `POSTGRES_AGENT_PASSWORD`. Under "Releases", change the note that release notes must call out schema changes and that `init.sh` only runs on a fresh data volume to "the database structure is created and adopted by the indexer automatically; manual migrations are no longer needed". **Replace the whole "Body-hash schema upgrade" section** with a snapshot upgrade section: automatic adoption, `legacy` kept for 7 days, no indexer version rollback after adoption (11.2). In the project structure tree, update `storage/postgres/` (delete `migrations/` or mark it as historical only)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 2, 3  |
| `indexer/README`         | **Features**: incremental processing becomes "diff against the latest snapshot and build a new snapshot"; titles are written to the snapshot's `documents`; add "atomic, reversible publishing" and "vector reuse by chunk text". **Pipeline Phases**: rewrite the text accompanying the diagram (to match the new diagram). **Configuration / Cron Schedule**: note that overrunning runs are not caught up. **Debugging Utilities**: `debugger stats/history/inspect/recent` read from the current snapshot (`history` reads `index_state`, `recent` reads `rag_state.file_actions`); **remove "Clear Data" / `debugger clear`** (replaced by `build --full` and `retire`); "Manual Execution" becomes `python -m src.snapshots build`. **Add an ops commands section** (the `list`, `status`, `retire`, `unretire`, `pin`, `unpin`, `build [--full]` commands from 6.9). **Rewrite the whole "Database Schema" section**: `rag_meta`, `rag_state`, the four tables of `rag_s<id>`, `index_state`, the two compatibility dimensions, the `rag_agent` role, the GC rules. **Rewrite the whole "Changing the Embedding Model" runbook**: no more stopping services, `ALTER`ing the column or clearing `indexing_history`; instead "change the model in `config.yml` and `EMBEDDING_DIM` → the indexer does a full build of a snapshot in the new vector space → update the agent configuration and switch", noting that the old snapshot keeps serving during the build. **Data Backup and Restore**: add the content of 9.1 (snapshots are not backups, data can be rebuilt from git, optional `pg_dump` export of the latest snapshot), keeping the existing volume-archive method. **Project Structure**: add the snapshot builder and ops command modules. **Monitoring / Expected Output**: `rag.document_chunks` and `rag.file_actions` become `rag_s<id>.*` and `rag_state.file_actions`; add `indexer_status` and the data age from the agent's `/health`. **Troubleshooting**: add handling for `skipped_busy` (a run is already in progress), disk precheck failures, self-check failures, and rolling back with `retire` | 1, 2  |
| `agent/README`           | In the module descriptions, note for `retriever.py`, `cache.py` and `vector_db.py` that they read per snapshot and that cache keys include the snapshot ID. In "API Reference", **add `GET /health`** (readiness conditions, snapshot information and `indexer_status` in the response body). In `GET /transcripts`, change "built from `rag.transcripts`" to "from the current snapshot's `transcripts`" and add "returns 503 when the database or snapshot is unavailable". In "Configuration", rewrite the "Embedding dimensions must match the database column" note as "the vector space must match the snapshot" (`model@dimension`, `embedding.space_id`). In "Where citation titles come from", change `rag.documents.title` to the snapshot's `documents`. In "Configuration", add `POSTGRES_AGENT_PASSWORD` and `RAG_SNAPSHOT`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | 1, 3  |
| `frontend/README`        | No change                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | —     |
| `indexer/data/README.md` | Add: the `.indexer.lock` file in this directory is the indexer's run lock; do not delete or create it by hand                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | 1     |

### 17.3 Example configs, compose files and script comments

| File                           | Locations to change                                                                                                                                                                                                                                                                                                                     | Phase |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `.env.example`                 | Change the `EMBEDDING_DIM` comment ("sized by init.sh on first init") to "dimension of the build's vector space"; make the PostgreSQL credentials comment say the superuser is only for postgres and the indexer; add the optional `POSTGRES_AGENT_PASSWORD` with an explanation                                                        | 1, 3  |
| `config.example.yml`           | Change the embedding comments (the dimension sizes the `halfvec(N)` column; switching models requires re-embedding everything) to: the dimension belongs to the vector space, and on a model change the indexer builds a new snapshot while the old one keeps serving; add an optional `embedding.space_id` example with an explanation | 2     |
| `docker-compose.yml`           | Rewrite the comments about `init.sh` and `EMBEDDING_DIM`; add the indexer's read-only `${POSTGRES_DATA_DIR}` mount and `stop_grace_period: 30s` with comments explaining them; add `POSTGRES_AGENT_PASSWORD` (falls back to the superuser when unset) and the optional `RAG_SNAPSHOT` to the agent                                      | 1, 2  |
| `docker-compose.sample.yml`    | Confirm the indexer's read-only mount points at the sample's `POSTGRES_DATA_DIR`; add comments if needed                                                                                                                                                                                                                                | 2     |
| `storage/postgres/init.sh`     | Rewrite the long header comment (dimension, `halfvec` trade-offs, "changing models requires ALTERing the column and re-embedding per the runbook") or move it into the indexer README's "Database Schema" section; reduce the script to creating the `vector` extension only                                                            | 3     |
| `storage/postgres/migrations/` | Unused after adoption: delete it, or add a README stating it is for historical reference only and new deployments do not need it                                                                                                                                                                                                        | 3     |

### 17.4 Usage notes in code

- The usage note at the top of `indexer/src/entrypoint.py` (manual runs with `python -m src.pipeline`) changes to `python -m src.snapshots build`.
- The command help and docstrings in `indexer/src/debugger.py` stay consistent with the debugging section in 17.2; remove `clear`.
- `indexer/src/chunker.py`: next to the chunking parameters, note that "changing the parameters or the logic requires bumping `chunker_version`" (5.2).
- Comments in `agent/src/vector_db.py` that refer to `rag.*` table names (for example the `embedding halfvec(N)` column "sized from EMBEDDING_DIM at DB init") are updated to describe snapshots along with the code.

### 17.5 Release notes

The release notes (GitHub Release) of the version shipping phase 2 must state: existing data is adopted automatically on first start; rolling the indexer back to an older version after adoption is not supported; the indexer has a new read-only mount; `POSTGRES_AGENT_PASSWORD` is optional; the first build after adoption is a full build (normally with no embedding calls, taking a few minutes).

## 18. Implementation Notes (2026-10-07)

Where the implementation refines or deviates from the sections above.

### Indexer

| Topic                          | Implementation                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Modules                        | `pipeline.py` (one run), `builder.py` (plan, disk precheck, Phase A, Phase B, self-check), `catalog.py` (`rag_meta`/`rag_state`, role, adoption, registry, GC), `snapshot_schema.py` (ids, fingerprint, vector space, DDL), `snapshots.py` (ops commands), `run_lock.py`, `db.py`. The old per-file writer (`vector_db.py` write functions, `stats.py`, `debugger clear`) was removed; `vector_db.py` now only holds the debugger's read-only queries |
| Format version                 | New snapshots are format 1, the same layout as the adopted `rag` schema (the served columns did not change). New snapshot tables declare `embedding` `NOT NULL`; the agent's queries are unchanged                                                                                                                                                                                                                   |
| Pipeline fingerprint           | First 16 hex characters of `sha256("format=…;normalization=…;chunker=…")`. `CHUNKER_VERSION` starts at 1 in `chunker.py`                                                                                                                                                                                                                                                                                              |
| Vector reuse                   | Server-side (see 6.3): new chunks go into a temporary table, then one `INSERT … SELECT` takes `COALESCE(new vector, reuse-source vector joined on sha256(text))`. Memory stays proportional to the new vectors only. Identical texts among the misses are embedded once                                                                                                                                                 |
| Vector literals                | Vectors are sent as text with 7 significant digits (lossless for halfvec), a third the size of `repr`; this cut Phase B of an all-new full build from 49 s to 35 s                                                                                                                                                                                                                                                  |
| Reader projections             | Copied from the base like the other tables; re-rendered only when `source_hash` or `projection_version` differs, and canonical titles refreshed in place (as before). A title-only change in `URI映射.md` still produces a new snapshot (decision 13)                                                                                                                                                                 |
| Audit log                      | `rag_state.file_actions` rows are written inside the publish transaction. For full builds they are computed against the newest published snapshot of any lineage, so a full build after a version bump logs only real source changes                                                                                                                                                                                   |
| Self-check details             | Also rejects chunks of transcripts missing from `index_state`. Vector checks use `vector_dims` and `l2_norm`. The query check forces the HNSW path (`enable_seqscan = off`) with k = min(10, chunk count). The model check runs for full builds and whenever new vectors were embedded, and is skipped for metadata-only builds                                                                                          |
| Base recheck                   | `SELECT … FOR UPDATE` on the base's registry row right before registering. A `retire` that commits after this check waits for the build to commit and then retires the base, so that window is milliseconds; a `retire` during the build itself abandons the build (tested)                                                                                                                                             |
| Snapshot id collisions         | Generated after the advisory lock is held; a second build within the same second takes the next second. Without a git checkout the commit part is `0000000`                                                                                                                                                                                                                                                          |
| Cancellation                   | psycopg2 runs queries through `wait_select`, so the Python `SIGTERM` handler runs during a long statement and cancels it immediately (without it the handler waited for the HNSW build to finish). Every indexer connection sets `client_connection_check_interval = 5s`, so a `SIGKILL`ed indexer's backend stops and rolls back within seconds instead of finishing its statement                                        |
| Disk precheck                  | Skipped with a warning when the `/pgdata` mount is absent (older compose files), rather than failing every build                                                                                                                                                                                                                                                                                                      |
| GC "superseded" time           | Within a lineage: the publish time of the next newer published snapshot. For another lineage's latest snapshot: the publish time of the first newer snapshot of the current lineage; with none, it is kept indefinitely. Only schemas named exactly like a snapshot id (or `rag`) are ever dropped — a plain `rag_s` prefix test would also match `rag_state`                                                             |
| Adoption                       | An empty `rag` schema (created by the old `init.sh` on a fresh volume) is not adopted, so a fresh deployment stays not ready until its first real build. Bootstrap takes the advisory lock blocking (it must not be skipped); builds and GC use the non-blocking form                                                                                                                                                      |
| `rag_agent`                    | Also forced `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, with `CREATE` on `public` revoked                                                                                                                                                                                                                                                                                                        |
| `indexer_status`               | `skipped_busy` neither increments nor resets `consecutive_failures`                                                                                                                                                                                                                                                                                                                                                  |
| `EMBEDDING_DIM`                | Now passed by compose to both the indexer (`embedding_dim`, the build's vector space; returned vectors of another size fail the build) and the agent (its vector space)                                                                                                                                                                                                                                             |
| Sample config                  | `index_config.sample.yml` replaced `ChanJingPoBiJi/misc/biz-001.md`, which no longer exists upstream, with `ChanJingPoBiJi/2024-07-25.md`                                                                                                                                                                                                                                                                              |

### Agent

| Topic                    | Implementation                                                                                                                                                                                                                                                       |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Request pinning          | The snapshot is taken once in `agent_query` / `agent_stream_query` and carried in the LangGraph state (`AgentState.snapshot`), so every node of one request reads the same schema; the transcript endpoints take it once per request                                    |
| `/health`                | Served from the poller's last result (≤ 15 s old) and never queries the database itself. Body: `ready`, `reason`, `snapshot` (id, schema, format, vector space, publish time, data age, commit), the agent's vector space and supported formats, `pinned_by_config`, `checked_at`, `indexer_status`. Compose uses it as the agent's container healthcheck |
| Database outage          | A failed poll keeps the last selected snapshot in memory (requests fail on their own) but reports not ready; the connection pool is created lazily (`minconn=1`) so startup never needs the database                                                                     |
| Implicit legacy          | Before `rag_meta` exists, `rag` is readable only if its `embedding` dimension equals `EMBEDDING_DIM` and it holds at least one chunk                                                                                                                                       |
| No snapshot              | `/chat` returns 503 (streaming: an `error` event); `/transcripts*` return 503                                                                                                                                                                                         |
| Eval harnesses           | `eval_agent.py` / `eval_retriever.py` select the current snapshot once at start, since no poller runs outside the server                                                                                                                                              |

### Tests (section 12)

`indexer/tests/test_pipeline.py` and `agent/tests/test_snapshots.py` run against a real PostgreSQL + pgvector (CI starts a `pgvector/pgvector:pg18` service; locally they need `PGTEST_HOST` and are skipped otherwise), with deterministic fake embeddings. They cover items 1, 2, 4 (both the file lock and the advisory lock), 6, 7, 8, 10, 11, 12, 13 and 14; items 5 and the GC retention rules are also covered by unit tests without a database. Items 3 (kill mid-Phase B), 9 (switching while serving) and 15 (fresh database) were verified end to end against a local stack, together with adoption of a legacy schema written by the previous indexer release.
