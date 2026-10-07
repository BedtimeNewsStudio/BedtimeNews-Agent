"""Snapshot registry (``rag_meta``), indexer-private state (``rag_state``),
the read-only ``rag_agent`` role, legacy adoption, and garbage collection.

Only the indexer writes here. Readers (the agent) only SELECT from
``rag_meta.snapshots`` / ``rag_meta.indexer_status`` and published snapshots.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg2 import sql
from psycopg2.extensions import connection as Connection
from psycopg2.extensions import cursor as Cursor
from psycopg2.extras import Json, RealDictCursor

from .db import ADVISORY_LOCK_KEY, transaction, try_advisory_xact_lock
from .settings import settings
from .snapshot_schema import (
    FORMAT_VERSION,
    LEGACY_FINGERPRINT,
    LEGACY_SCHEMA,
    LEGACY_SNAPSHOT_ID,
    SCHEMA_PREFIX,
    embedding_space,
    pipeline_fingerprint,
)

logger = logging.getLogger(__name__)

AGENT_ROLE = "rag_agent"

# Retention (design 6.8).
CURRENT_LINEAGE_KEEP = 2
OTHER_LINEAGE_RETENTION = timedelta(days=7)
RETIRED_RETENTION = timedelta(hours=24)
GRACE_PERIOD = timedelta(minutes=10)
GC_LOCK_TIMEOUT = "5s"

SNAPSHOT_SCHEMA_RE = re.compile(re.escape(SCHEMA_PREFIX) + r"s\d{8}t\d{6}z_[0-9a-z]{7}")

STATUS_PUBLISHED = "published"
STATUS_RETIRED = "retired"

_BOOTSTRAP_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS rag_meta;
CREATE SCHEMA IF NOT EXISTS rag_state;

CREATE TABLE IF NOT EXISTS rag_meta.snapshots (
    snapshot_id           TEXT PRIMARY KEY,
    schema_name           TEXT NOT NULL UNIQUE,
    format_version        INTEGER NOT NULL,
    pipeline_fingerprint  TEXT NOT NULL,
    embedding_space       TEXT NOT NULL,
    embedding_model       TEXT NOT NULL,
    embedding_dim         INTEGER NOT NULL,
    normalization_version INTEGER,
    chunker_version       INTEGER,
    source_commit         TEXT,
    base_snapshot_id      TEXT,
    builder_version       TEXT NOT NULL,
    status                TEXT NOT NULL,
    pinned                BOOLEAN NOT NULL DEFAULT false,
    published_at          TIMESTAMPTZ NOT NULL,
    retired_at            TIMESTAMPTZ,
    index_params          JSONB,
    stats                 JSONB,
    CONSTRAINT valid_status CHECK (status IN ('published', 'retired'))
);

CREATE TABLE IF NOT EXISTS rag_meta.indexer_status (
    id                    BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    last_run_at           TIMESTAMPTZ,
    last_result           TEXT,
    last_error            TEXT,
    last_published_at     TIMESTAMPTZ,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0
);
INSERT INTO rag_meta.indexer_status (id) VALUES (true) ON CONFLICT DO NOTHING;
"""

_FILE_ACTIONS_SQL = """
CREATE TABLE IF NOT EXISTS rag_state.file_actions (
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
CREATE INDEX IF NOT EXISTS idx_file_actions_file_path ON rag_state.file_actions(file_path);
CREATE INDEX IF NOT EXISTS idx_file_actions_timestamp ON rag_state.file_actions(run_timestamp);
"""


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: str
    schema_name: str
    format_version: int
    pipeline_fingerprint: str
    embedding_space: str
    embedding_model: str
    embedding_dim: int
    status: str
    pinned: bool
    published_at: datetime
    retired_at: datetime | None
    source_commit: str | None
    base_snapshot_id: str | None
    stats: dict | None

    @property
    def lineage(self) -> tuple[str, str]:
        return (self.pipeline_fingerprint, self.embedding_space)


_SNAPSHOT_COLUMNS = (
    "snapshot_id, schema_name, format_version, pipeline_fingerprint, "
    "embedding_space, embedding_model, embedding_dim, status, pinned, "
    "published_at, retired_at, source_commit, base_snapshot_id, stats"
)


def current_lineage() -> tuple[str, str]:
    return (pipeline_fingerprint(), embedding_space())


# ---------------------------------------------------------------------------
# Bootstrap and adoption
# ---------------------------------------------------------------------------


def bootstrap(conn: Connection) -> None:
    """Idempotently create rag_meta / rag_state / rag_agent and adopt `rag`.

    Runs on every indexer start, inside the run's file lock and one write
    transaction holding the database advisory lock.
    """
    with transaction(conn) as cursor:
        # Blocking here is fine: whoever holds it finishes a short transaction
        # or a build, and bootstrap must not be skipped.
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
        cursor.execute(_BOOTSTRAP_SQL)
        _ensure_agent_role(cursor)
        _adopt_legacy(cursor)
        cursor.execute(_FILE_ACTIONS_SQL)
        cursor.execute(
            sql.SQL(
                "GRANT USAGE ON SCHEMA rag_meta TO {r}; "
                "GRANT SELECT ON rag_meta.snapshots, rag_meta.indexer_status TO {r};"
            ).format(r=sql.Identifier(AGENT_ROLE))
        )


def _ensure_agent_role(cursor: Cursor) -> None:
    role = sql.Identifier(AGENT_ROLE)
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (AGENT_ROLE,))
    if cursor.fetchone() is None:
        cursor.execute(sql.SQL("CREATE ROLE {r} NOLOGIN").format(r=role))
        logger.info("Created role %s", AGENT_ROLE)
    # Never a superuser, never able to create anything: read-only by privilege.
    cursor.execute(
        sql.SQL(
            "ALTER ROLE {r} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        ).format(r=role)
    )
    if settings.postgres_agent_password:
        cursor.execute(
            sql.SQL("ALTER ROLE {r} LOGIN PASSWORD %s").format(r=role),
            (settings.postgres_agent_password,),
        )
    else:
        cursor.execute(sql.SQL("ALTER ROLE {r} NOLOGIN").format(r=role))
    # The agent must not create objects anywhere it can reach.
    cursor.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {r}").format(r=role))


def _relation_exists(cursor: Cursor, name: str) -> bool:
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    return bool(cursor.fetchone()[0])


def legacy_embedding_dim(cursor: Cursor) -> int | None:
    """Actual dimension of rag.document_chunks.embedding, from the catalog."""
    cursor.execute(
        """
        SELECT a.atttypmod FROM pg_attribute a
        WHERE a.attrelid = to_regclass('rag.document_chunks')
          AND a.attname = 'embedding' AND NOT a.attisdropped
        """
    )
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] and row[0] > 0 else None


def _adopt_legacy(cursor: Cursor) -> None:
    """Register an existing `rag` schema as snapshot `legacy` (design 11.1)."""
    if not _relation_exists(cursor, "rag.document_chunks"):
        return

    # Move the indexer-private audit log out of the served schema, and give
    # indexing_history its snapshot name. Both idempotent.
    if _relation_exists(cursor, "rag.file_actions") and not _relation_exists(
        cursor, "rag_state.file_actions"
    ):
        cursor.execute("ALTER TABLE rag.file_actions SET SCHEMA rag_state")
        logger.info("Moved rag.file_actions -> rag_state.file_actions")
    if _relation_exists(cursor, "rag.indexing_history") and not _relation_exists(
        cursor, "rag.index_state"
    ):
        cursor.execute("ALTER TABLE rag.indexing_history RENAME TO index_state")
        logger.info("Renamed rag.indexing_history -> rag.index_state")

    cursor.execute(
        "SELECT 1 FROM rag_meta.snapshots WHERE schema_name = %s", (LEGACY_SCHEMA,)
    )
    if cursor.fetchone() is not None:
        return

    # A freshly initialised database (old init.sh) has an empty `rag`: nothing
    # worth serving, so leave it unregistered and let the first build publish.
    cursor.execute("SELECT EXISTS (SELECT 1 FROM rag.document_chunks)")
    if not cursor.fetchone()[0]:
        logger.info("Schema rag is empty; not adopting it as a snapshot")
        return

    dim = legacy_embedding_dim(cursor)
    if dim is None:
        logger.warning("Cannot determine rag embedding dimension; not adopting")
        return
    model = settings.embedding.model
    space = (
        settings.embedding.space_id
        if settings.embedding.space_id and dim == settings.embedding_dim
        else embedding_space(model, dim)
    )
    cursor.execute("SELECT count(*), count(DISTINCT doc_id) FROM rag.document_chunks")
    chunks, docs = cursor.fetchone()
    cursor.execute(
        """
        INSERT INTO rag_meta.snapshots (
            snapshot_id, schema_name, format_version, pipeline_fingerprint,
            embedding_space, embedding_model, embedding_dim,
            normalization_version, chunker_version, source_commit,
            base_snapshot_id, builder_version, status, published_at, stats
        ) VALUES (%s, %s, 1, %s, %s, %s, %s, NULL, NULL, NULL, NULL, %s,
                  'published', now(), %s)
        """,
        (
            LEGACY_SNAPSHOT_ID,
            LEGACY_SCHEMA,
            LEGACY_FINGERPRINT,
            space,
            model,
            dim,
            f"adopted-by-{builder_version()}",
            Json({"chunks": chunks, "transcripts": docs, "adopted": True}),
        ),
    )
    grant_read(cursor, LEGACY_SCHEMA)
    logger.info("Adopted schema rag as snapshot legacy (%s, %d chunks)", space, chunks)


def grant_read(cursor: Cursor, schema: str) -> None:
    s, r = sql.Identifier(schema), sql.Identifier(AGENT_ROLE)
    cursor.execute(
        sql.SQL(
            "GRANT USAGE ON SCHEMA {s} TO {r}; "
            "GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO {r};"
        ).format(s=s, r=r)
    )


def builder_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("bedtimenews-indexer")
    except PackageNotFoundError:
        return "unknown"


# ---------------------------------------------------------------------------
# Registry queries
# ---------------------------------------------------------------------------


def list_snapshots(cursor: Cursor) -> list[SnapshotInfo]:
    cursor.execute(
        f"SELECT {_SNAPSHOT_COLUMNS} FROM rag_meta.snapshots "
        "ORDER BY published_at DESC, snapshot_id DESC"
    )
    return [SnapshotInfo(*row) for row in cursor.fetchall()]


def get_snapshot(cursor: Cursor, snapshot_id: str) -> SnapshotInfo | None:
    cursor.execute(
        f"SELECT {_SNAPSHOT_COLUMNS} FROM rag_meta.snapshots WHERE snapshot_id = %s",
        (snapshot_id,),
    )
    row = cursor.fetchone()
    return SnapshotInfo(*row) if row else None


def latest_published(
    snapshots: list[SnapshotInfo],
    *,
    lineage: tuple[str, str] | None = None,
    space: str | None = None,
) -> SnapshotInfo | None:
    for snap in snapshots:  # newest first
        if snap.status != STATUS_PUBLISHED:
            continue
        if lineage is not None and snap.lineage != lineage:
            continue
        if space is not None and snap.embedding_space != space:
            continue
        return snap
    return None


def snapshot_sizes(cursor: Cursor) -> dict[str, int]:
    """Total on-disk size (heap, TOAST, indexes) of every schema, in bytes."""
    cursor.execute(
        """
        SELECT n.nspname, COALESCE(sum(pg_total_relation_size(c.oid)), 0)::bigint
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'm')
        GROUP BY n.nspname
        """
    )
    return {name: int(size) for name, size in cursor.fetchall()}


def set_status(conn: Connection, snapshot_id: str, status: str) -> SnapshotInfo:
    if status not in (STATUS_PUBLISHED, STATUS_RETIRED):
        raise ValueError(status)
    with transaction(conn) as cursor:
        snap = get_snapshot(cursor, snapshot_id)
        if snap is None:
            raise LookupError(f"No such snapshot: {snapshot_id}")
        cursor.execute(
            """
            UPDATE rag_meta.snapshots
            SET status = %s,
                retired_at = CASE WHEN %s = 'retired' THEN now() ELSE NULL END
            WHERE snapshot_id = %s
            """,
            (status, status, snapshot_id),
        )
        return get_snapshot(cursor, snapshot_id)


def set_pinned(conn: Connection, snapshot_id: str, pinned: bool) -> SnapshotInfo:
    with transaction(conn) as cursor:
        cursor.execute(
            "UPDATE rag_meta.snapshots SET pinned = %s WHERE snapshot_id = %s",
            (pinned, snapshot_id),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"No such snapshot: {snapshot_id}")
        return get_snapshot(cursor, snapshot_id)


# ---------------------------------------------------------------------------
# Indexer status
# ---------------------------------------------------------------------------


def record_run(
    conn: Connection,
    result: str,
    error: str | None = None,
    *,
    published: bool = False,
) -> None:
    """Update the single indexer_status row after a run."""
    failed = result == "failed"
    counts = result in ("published", "no_change", "failed")
    with transaction(conn) as cursor:
        cursor.execute(
            """
            UPDATE rag_meta.indexer_status
            SET last_run_at = now(),
                last_result = %s,
                last_error = %s,
                last_published_at = CASE WHEN %s THEN now() ELSE last_published_at END,
                consecutive_failures = CASE
                    WHEN %s THEN consecutive_failures + 1
                    WHEN %s THEN 0
                    ELSE consecutive_failures END
            """,
            (result, (error or "")[:2000] or None, published, failed, counts),
        )


def get_status(cursor: Cursor) -> dict[str, Any]:
    cur = cursor.connection.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM rag_meta.indexer_status")
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Garbage collection (design 6.8)
# ---------------------------------------------------------------------------


def gc_candidates(
    snapshots: list[SnapshotInfo],
    lineage: tuple[str, str],
    now: datetime,
) -> list[tuple[SnapshotInfo, str]]:
    """Return (snapshot, reason) pairs that the retention rules allow deleting.

    Pure function over the registry so the rules are unit-testable.
    """
    doomed: list[tuple[SnapshotInfo, str]] = []
    published = [s for s in snapshots if s.status == STATUS_PUBLISHED]
    published.sort(key=lambda s: (s.published_at, s.snapshot_id), reverse=True)
    current = [s for s in published if s.lineage == lineage]

    def superseded_at(snap: SnapshotInfo, pool: list[SnapshotInfo]) -> datetime | None:
        newer = [s.published_at for s in pool if s.published_at > snap.published_at]
        return min(newer) if newer else None

    for snap in snapshots:
        if snap.pinned:
            continue
        if snap.status == STATUS_RETIRED:
            retired_at = snap.retired_at or snap.published_at
            if now - retired_at >= max(RETIRED_RETENTION, GRACE_PERIOD):
                doomed.append((snap, "retired more than 24 h ago"))
            continue

        if snap.lineage == lineage:
            if snap in current[:CURRENT_LINEAGE_KEEP]:
                continue
            when = superseded_at(snap, current)
            if when is not None and now - when >= GRACE_PERIOD:
                doomed.append((snap, "beyond the current lineage's retention count"))
            continue

        same = [s for s in published if s.lineage == snap.lineage]
        if same and same[0] is not snap:
            when = superseded_at(snap, same)
            if when is not None and now - when >= GRACE_PERIOD:
                doomed.append((snap, "superseded within its own lineage"))
            continue
        # Latest of another lineage: kept 7 days after the current lineage
        # superseded it; kept indefinitely while nothing newer exists there.
        when = superseded_at(snap, current)
        if when is not None and now - when >= max(
            OTHER_LINEAGE_RETENTION, GRACE_PERIOD
        ):
            doomed.append((snap, "other lineage, superseded more than 7 days ago"))
    return doomed


def collect_garbage(conn: Connection) -> list[str]:
    """Drop snapshots past retention, one transaction each. Returns dropped ids."""
    with transaction(conn) as cursor:
        cursor.execute("SELECT now()")
        now = cursor.fetchone()[0]
        doomed = gc_candidates(list_snapshots(cursor), current_lineage(), now)

    dropped: list[str] = []
    for snap, reason in doomed:
        if not _is_snapshot_schema(snap.schema_name):
            logger.error("Refusing to drop unexpected schema %s", snap.schema_name)
            continue
        try:
            with transaction(conn) as cursor:
                if not try_advisory_xact_lock(cursor):
                    logger.info("GC skipped: another writer holds the lock")
                    return dropped
                cursor.execute(f"SET LOCAL lock_timeout = '{GC_LOCK_TIMEOUT}'")
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {s} CASCADE").format(
                        s=sql.Identifier(snap.schema_name)
                    )
                )
                cursor.execute(
                    "DELETE FROM rag_meta.snapshots WHERE snapshot_id = %s",
                    (snap.snapshot_id,),
                )
            dropped.append(snap.snapshot_id)
            logger.info("GC dropped snapshot %s (%s)", snap.snapshot_id, reason)
        except Exception as exc:
            # Typically a lock timeout: a query is still reading it. Next run.
            logger.warning("GC could not drop %s yet: %s", snap.snapshot_id, exc)
    return dropped


def _is_snapshot_schema(schema: str) -> bool:
    # Exact id shape: a bare "rag_s" prefix would also match rag_state.
    return schema == LEGACY_SCHEMA or SNAPSHOT_SCHEMA_RE.fullmatch(schema) is not None


__all__ = [
    "AGENT_ROLE",
    "FORMAT_VERSION",
    "SnapshotInfo",
    "bootstrap",
    "collect_garbage",
    "current_lineage",
    "gc_candidates",
    "get_snapshot",
    "get_status",
    "grant_read",
    "latest_published",
    "list_snapshots",
    "record_run",
    "set_pinned",
    "set_status",
    "snapshot_sizes",
]
