"""Snapshot builder: diff against a base snapshot, then build and publish.

Flow of one build (design 6.2-6.7), run by ``pipeline.run_once`` inside the
run's file lock:

* **plan** — choose the incremental base (latest published snapshot of the
  current lineage) or do a full build, and diff the pinned transcripts against
  the base's immutable ``index_state``.
* **disk precheck** — refuse to build without room for a snapshot plus WAL.
* **Phase A** (no transaction) — chunk, reuse vectors by ``sha256(chunk
  text)`` from the latest snapshot in the same vector space, call the
  embedding API only for misses, render reader projections.
* **Phase B** (one transaction) — create ``rag_s<id>``, copy the base, delete,
  insert, ANALYZE, build HNSW, self-check, recheck the base, register as
  published and grant read access. Any failure rolls everything back.
"""

import hashlib
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg2 import sql
from psycopg2.extensions import connection as Connection
from psycopg2.extensions import cursor as Cursor
from psycopg2.extras import Json, execute_values

from . import catalog, chunker, document_loader
from .catalog import SnapshotInfo
from .change_detector import ChangeSet, detect_changes
from .chunker import chunk_document
from .db import SHUTDOWN, transaction, try_advisory_xact_lock
from .document_loader import load_indexable_source
from .file_scanner import scan_files
from .models import Chunk, LoadedIndexableSource, TranscriptProjection
from .paths import CONTENTS_DIR
from .settings import settings
from .snapshot_schema import (
    COPY_COLUMNS,
    FORMAT_VERSION,
    MAINTENANCE_WORK_MEM,
    MAX_PARALLEL_MAINTENANCE_WORKERS,
    SNAPSHOT_TABLES,
    create_hnsw_sql,
    create_tables_sql,
    embedding_space,
    index_params,
    new_snapshot_id,
    pipeline_fingerprint,
    schema_for,
)
from .transcript_export import TRANSCRIPT_PROJECTION_VERSION, project_transcript
from .uri_mapping import load_uri_titles, resolve_title

logger = logging.getLogger(__name__)

# Self-check parameters (design 6.5).
QUERY_CHECK_K = 10
MODEL_CHECK_SAMPLES = 5
MODEL_CHECK_MIN_COSINE = 0.99
# Texts sent to the embedding API per call of generate_embeddings; between
# calls the builder checks for a shutdown request.
EMBED_GROUP = 200


class BuildBusy(Exception):
    """Another writer holds the database advisory lock."""


class BuildCancelled(Exception):
    """Shutdown was requested while building."""


class SelfCheckFailed(Exception):
    """The new snapshot failed a completeness/queryability/model check."""


@dataclass
class BuildPlan:
    full: bool
    base: SnapshotInfo | None
    reference: SnapshotInfo | None
    current_files: set[str]
    changes: ChangeSet
    titles: dict[str, str]
    source_commit: str | None
    # Files whose chunks are (re)built in this snapshot.
    reindex: set[str] = field(default_factory=set)
    # Reader projections to (re)render / remove, relative to the base.
    render: set[str] = field(default_factory=set)
    transcripts_removed: set[str] = field(default_factory=set)
    titles_changed: bool = False

    @property
    def has_changes(self) -> bool:
        return (
            self.full
            or self.changes.has_changes
            or bool(self.render)
            or bool(self.transcripts_removed)
            or self.titles_changed
        )


@dataclass
class PreparedChunk:
    chunk: Chunk
    text_hash: str
    embedding: list[float] | None  # None: reused from the reuse source


@dataclass
class Prepared:
    chunks: list[PreparedChunk]
    projections: dict[str, TranscriptProjection]
    reuse_source: SnapshotInfo | None
    embedded: int
    reused: int


def _check_cancel() -> None:
    if SHUTDOWN.is_set():
        raise BuildCancelled("shutdown requested")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def _fetch_histories(cursor: Cursor, schema: str) -> dict[str, dict[str, Any]]:
    cursor.execute(
        sql.SQL(
            "SELECT file_path, source_hash, body_hash, body_normalization_version "
            "FROM {s}.index_state"
        ).format(s=sql.Identifier(schema))
    )
    return {
        row[0]: {
            "source_hash": row[1],
            "body_hash": row[2],
            "body_normalization_version": row[3],
        }
        for row in cursor.fetchall()
    }


def _has_table(cursor: Cursor, schema: str, table: str) -> bool:
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (f'"{schema}".{table}',))
    return bool(cursor.fetchone()[0])


def plan_build(conn: Connection, *, full: bool, source_commit: str | None) -> BuildPlan:
    """Choose the base and diff the pinned transcripts against it."""
    current_files = scan_files()
    mapped = load_uri_titles()
    titles = {uri: resolve_title(uri, mapped) for uri in sorted(current_files)}

    with transaction(conn) as cursor:
        snapshots = catalog.list_snapshots(cursor)
        base = (
            None
            if full
            else catalog.latest_published(snapshots, lineage=catalog.current_lineage())
        )
        # The diff reference: the base, or for a full build the newest
        # published snapshot of any lineage (used for the audit log only).
        reference = base or catalog.latest_published(snapshots)
        histories: dict[str, dict[str, Any]] = {}
        if reference and _has_table(cursor, reference.schema_name, "index_state"):
            histories = _fetch_histories(cursor, reference.schema_name)

        base_titles: dict[str, str] = {}
        base_transcripts: dict[str, tuple[str, int, str]] = {}
        if base:
            s = sql.Identifier(base.schema_name)
            cursor.execute(
                sql.SQL("SELECT doc_id, title FROM {s}.documents").format(s=s)
            )
            base_titles = dict(cursor.fetchall())
            cursor.execute(
                sql.SQL(
                    "SELECT doc_id, source_hash, projection_version, canonical_title "
                    "FROM {s}.transcripts"
                ).format(s=s)
            )
            base_transcripts = {r[0]: (r[1], r[2], r[3]) for r in cursor.fetchall()}

    changes = detect_changes(current_files, histories)
    plan = BuildPlan(
        full=base is None,
        base=base,
        reference=reference,
        current_files=current_files,
        changes=changes,
        titles=titles,
        source_commit=source_commit,
    )

    if plan.full:
        plan.reindex = set(current_files)
        plan.render = set(current_files)
        return plan

    plan.reindex = changes.reindex_files
    plan.transcripts_removed = set(base_transcripts) - current_files
    for uri in current_files:
        stored = base_transcripts.get(uri)
        if stored is None or (stored[0], stored[1]) != (
            changes.current_source_hashes[uri],
            TRANSCRIPT_PROJECTION_VERSION,
        ):
            plan.render.add(uri)
        elif stored[2] != titles[uri]:
            plan.titles_changed = True
    plan.titles_changed = plan.titles_changed or base_titles != titles
    return plan


# ---------------------------------------------------------------------------
# Disk precheck (design 6.7)
# ---------------------------------------------------------------------------


def disk_precheck(conn: Connection, plan: BuildPlan) -> None:
    """Raise if the Postgres data filesystem lacks room for a build."""
    path = settings.pgdata_path
    if not path or not os.path.isdir(path):
        logger.warning(
            "Disk precheck skipped: Postgres data mount %r not present", path
        )
        return
    with transaction(conn) as cursor:
        sizes = catalog.snapshot_sizes(cursor)
    sized = plan.base or plan.reference
    base_size = sizes.get(sized.schema_name, 0) if sized else 0
    required = max(settings.build_min_free_bytes, 3 * base_size)
    stats = os.statvfs(path)
    free = stats.f_bavail * stats.f_frsize
    logger.info(
        "Disk precheck: free %.1f GB, required %.1f GB (base snapshot %.0f MB)",
        free / 1024**3,
        required / 1024**3,
        base_size / 1024**2,
    )
    if free < required:
        raise RuntimeError(
            f"Disk precheck failed: {free / 1024**3:.2f} GB free on {path}, "
            f"need {required / 1024**3:.2f} GB"
        )


# ---------------------------------------------------------------------------
# Phase A: chunk, reuse, embed, render (outside any transaction)
# ---------------------------------------------------------------------------


def _reuse_hashes(conn: Connection, source: SnapshotInfo) -> set[str]:
    with transaction(conn) as cursor:
        cursor.execute(
            sql.SQL(
                "SELECT DISTINCT encode(sha256(convert_to(text, 'UTF8')), 'hex') "
                "FROM {s}.document_chunks WHERE embedding IS NOT NULL"
            ).format(s=sql.Identifier(source.schema_name))
        )
        return {row[0] for row in cursor.fetchall()}


def _embed(texts: list[str]) -> list[list[float]]:
    from .embeddings import generate_embeddings

    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_GROUP):
        _check_cancel()
        vectors.extend(generate_embeddings(texts[start : start + EMBED_GROUP]))
        logger.info(
            "  embedded %d/%d", min(start + EMBED_GROUP, len(texts)), len(texts)
        )
    for vector in vectors:
        if len(vector) != settings.embedding_dim:
            raise RuntimeError(
                f"Embedding service returned {len(vector)} dimensions, "
                f"expected {settings.embedding_dim} (EMBEDDING_DIM)"
            )
    return vectors


def prepare(conn: Connection, plan: BuildPlan) -> Prepared:
    """Phase A. New vectors live only in memory until Phase B commits."""
    with transaction(conn) as cursor:
        reuse_source = catalog.latest_published(
            catalog.list_snapshots(cursor), space=embedding_space()
        )
    if reuse_source and reuse_source.embedding_dim != settings.embedding_dim:
        reuse_source = None
    reusable = _reuse_hashes(conn, reuse_source) if reuse_source else set()

    prepared: list[PreparedChunk] = []
    misses: list[PreparedChunk] = []
    for uri in sorted(plan.reindex):
        _check_cancel()
        source = plan.changes.loaded_sources.get(uri)
        if source is None:
            source = load_indexable_source(uri)
            plan.changes.loaded_sources[uri] = source
        for chunk in chunk_document(source.document):
            item = PreparedChunk(chunk, text_hash(chunk.text), None)
            prepared.append(item)
            if item.text_hash not in reusable:
                misses.append(item)

    # Identical texts share one API call.
    unique_misses: dict[str, str] = {}
    for item in misses:
        unique_misses.setdefault(item.text_hash, item.chunk.text)
    logger.info(
        "Phase A: %d chunks from %d transcripts; %d reuse a stored vector "
        "(source: %s), %d need embedding",
        len(prepared),
        len(plan.reindex),
        len(prepared) - len(misses),
        reuse_source.snapshot_id if reuse_source else "none",
        len(unique_misses),
    )
    vectors = dict(
        zip(
            unique_misses,
            _embed(list(unique_misses.values())) if unique_misses else [],
            strict=True,
        )
    )
    for item in misses:
        item.embedding = vectors[item.text_hash]

    projections: dict[str, TranscriptProjection] = {}
    for uri in sorted(plan.render):
        _check_cancel()
        raw = (CONTENTS_DIR / uri).read_bytes()
        projection = project_transcript(uri, raw, plan.titles[uri])
        expected = plan.changes.current_source_hashes.get(uri)
        if expected and projection.source_hash != expected:
            raise RuntimeError(f"Transcript changed during the build: {uri}")
        projections[uri] = projection

    return Prepared(
        chunks=prepared,
        projections=projections,
        reuse_source=reuse_source,
        embedded=len(unique_misses),
        reused=len(prepared) - len(misses),
    )


# ---------------------------------------------------------------------------
# Phase B: build and publish in one transaction
# ---------------------------------------------------------------------------


def _vector_literal(vector: list[float]) -> str:
    # halfvec keeps ~3 significant decimal digits; 7 already round-trips it
    # exactly and keeps the SQL a third the size of repr().
    return "[" + ",".join(f"{float(v):.7g}" for v in vector) + "]"


def _copy_base(cursor: Cursor, base: str, target: str) -> None:
    for table in SNAPSHOT_TABLES:
        cols = sql.SQL(", ").join(sql.Identifier(c) for c in COPY_COLUMNS[table])
        cursor.execute(
            sql.SQL(
                "INSERT INTO {t}.{tab} ({cols}) SELECT {cols} FROM {b}.{tab}"
            ).format(
                t=sql.Identifier(target),
                b=sql.Identifier(base),
                tab=sql.Identifier(table),
                cols=cols,
            )
        )


def _insert_chunks(cursor: Cursor, target: str, prepared: Prepared) -> None:
    if not prepared.chunks:
        return
    cursor.execute(
        sql.SQL(
            """
            CREATE TEMP TABLE _new_chunks (
                chunk_id TEXT, doc_id TEXT, chunk_index INTEGER, heading TEXT,
                text TEXT, word_count INTEGER, text_hash TEXT,
                embedding halfvec({dim})
            ) ON COMMIT DROP
            """
        ).format(dim=sql.Literal(settings.embedding_dim))
    )
    rows = [
        (
            p.chunk.id,
            p.chunk.doc_id,
            p.chunk.chunk_index,
            p.chunk.heading,
            p.chunk.text,
            p.chunk.word_count,
            p.text_hash,
            _vector_literal(p.embedding) if p.embedding is not None else None,
        )
        for p in prepared.chunks
    ]
    execute_values(
        cursor,
        "INSERT INTO _new_chunks VALUES %s",
        rows,
        template="(%s, %s, %s, %s, %s, %s, %s, %s::halfvec)",
        page_size=200,
    )

    t = sql.Identifier(target)
    if prepared.reuse_source is None:
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {t}.document_chunks
                    (chunk_id, doc_id, chunk_index, heading, text, word_count, embedding)
                SELECT chunk_id, doc_id, chunk_index, heading, text, word_count, embedding
                FROM _new_chunks
                """
            ).format(t=t)
        )
        return
    # Reused vectors are joined server-side from the reuse source by text
    # hash; they never travel through the indexer.
    cursor.execute(
        sql.SQL(
            """
            INSERT INTO {t}.document_chunks
                (chunk_id, doc_id, chunk_index, heading, text, word_count, embedding)
            SELECT n.chunk_id, n.doc_id, n.chunk_index, n.heading, n.text,
                   n.word_count, COALESCE(n.embedding, r.embedding)
            FROM _new_chunks n
            LEFT JOIN (
                SELECT DISTINCT ON (h) h, embedding
                FROM (
                    SELECT encode(sha256(convert_to(text, 'UTF8')), 'hex') AS h,
                           embedding
                    FROM {r}.document_chunks
                    WHERE embedding IS NOT NULL
                ) hashed
                ORDER BY h
            ) r ON r.h = n.text_hash AND n.embedding IS NULL
            """
        ).format(t=t, r=sql.Identifier(prepared.reuse_source.schema_name))
    )


def _write_index_state(cursor: Cursor, target: str, plan: BuildPlan) -> None:
    t = sql.Identifier(target)
    rows = []
    for uri in sorted(plan.reindex):
        src: LoadedIndexableSource = plan.changes.loaded_sources[uri]
        rows.append(
            (uri, src.source_hash, src.body_hash, src.body_normalization_version)
        )
    if rows:
        execute_values(
            cursor,
            sql.SQL(
                "INSERT INTO {t}.index_state (file_path, source_hash, body_hash, "
                "body_normalization_version) VALUES %s"
            )
            .format(t=t)
            .as_string(cursor),
            rows,
        )
    if plan.full:
        return
    for uri in sorted(plan.changes.source_only):
        cursor.execute(
            sql.SQL(
                "UPDATE {t}.index_state SET source_hash = %s, "
                "source_observed_at = CURRENT_TIMESTAMP WHERE file_path = %s"
            ).format(t=t),
            (plan.changes.current_source_hashes[uri], uri),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"index_state row missing for source-only edit: {uri}")


def _write_documents(cursor: Cursor, target: str, plan: BuildPlan) -> None:
    t = sql.Identifier(target)
    cursor.execute(
        sql.SQL("DELETE FROM {t}.documents WHERE NOT (doc_id = ANY(%s))").format(t=t),
        (sorted(plan.current_files),),
    )
    execute_values(
        cursor,
        sql.SQL(
            "INSERT INTO {t}.documents (doc_id, title) VALUES %s "
            "ON CONFLICT (doc_id) DO UPDATE SET title = EXCLUDED.title, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE {t}.documents.title IS DISTINCT FROM EXCLUDED.title"
        )
        .format(t=t)
        .as_string(cursor),
        sorted(plan.titles.items()),
        page_size=500,
    )


def _write_transcripts(
    cursor: Cursor, target: str, plan: BuildPlan, prepared: Prepared
) -> None:
    t = sql.Identifier(target)
    if plan.transcripts_removed:
        cursor.execute(
            sql.SQL("DELETE FROM {t}.transcripts WHERE doc_id = ANY(%s)").format(t=t),
            (sorted(plan.transcripts_removed),),
        )
    rows = [
        (
            p.doc_id,
            p.canonical_title,
            p.source_title,
            p.channel,
            p.publication_date,
            p.body_html,
            p.source_hash,
            p.projection_version,
        )
        for p in prepared.projections.values()
    ]
    if rows:
        execute_values(
            cursor,
            sql.SQL(
                """
                INSERT INTO {t}.transcripts (
                    doc_id, canonical_title, source_title, channel,
                    publication_date, body_html, source_hash, projection_version
                ) VALUES %s
                ON CONFLICT (doc_id) DO UPDATE
                SET canonical_title = EXCLUDED.canonical_title,
                    source_title = EXCLUDED.source_title,
                    channel = EXCLUDED.channel,
                    publication_date = EXCLUDED.publication_date,
                    body_html = EXCLUDED.body_html,
                    source_hash = EXCLUDED.source_hash,
                    projection_version = EXCLUDED.projection_version,
                    updated_at = CURRENT_TIMESTAMP
                """
            )
            .format(t=t)
            .as_string(cursor),
            rows,
            page_size=100,
        )
    # URI映射.md sits outside contents/, so a title-only correction has no
    # source hash change: refresh the lightweight title column separately.
    execute_values(
        cursor,
        sql.SQL(
            "UPDATE {t}.transcripts AS tr SET canonical_title = v.title, "
            "updated_at = CURRENT_TIMESTAMP FROM (VALUES %s) AS v(doc_id, title) "
            "WHERE tr.doc_id = v.doc_id AND tr.canonical_title IS DISTINCT FROM v.title"
        )
        .format(t=t)
        .as_string(cursor),
        sorted(plan.titles.items()),
        page_size=500,
    )


def _write_audit(cursor: Cursor, plan: BuildPlan) -> None:
    c = plan.changes
    actions = (
        [(uri, "ADD") for uri in c.added]
        + [(uri, "MODIFY") for uri in c.body_modified | c.legacy_requires_reindex]
        + [(uri, "SOURCE_ONLY") for uri in c.source_only]
        + [(uri, "DELETE") for uri in c.deleted]
    )
    rows = []
    for uri, action in sorted(actions):
        src = c.loaded_sources.get(uri)
        rows.append(
            (
                uri,
                action,
                c.current_source_hashes.get(uri),
                src.body_hash if src else None,
            )
        )
    if rows:
        execute_values(
            cursor,
            "INSERT INTO rag_state.file_actions (file_path, action_type, "
            "source_hash, body_hash, processed_at) VALUES %s",
            rows,
            template="(%s, %s, %s, %s, CURRENT_TIMESTAMP)",
        )


def _parse_vector(text: str) -> list[float]:
    return [float(v) for v in text.strip("[]").split(",")]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def self_check(cursor: Cursor, target: str, *, check_model: bool) -> dict[str, Any]:
    """Design 6.5. Raises SelfCheckFailed; the caller rolls back."""
    t = sql.Identifier(target)
    dim = settings.embedding_dim

    def ids(query: sql.Composable) -> set[str]:
        cursor.execute(query)
        return {row[0] for row in cursor.fetchall()}

    state_ids = ids(sql.SQL("SELECT file_path FROM {t}.index_state").format(t=t))
    if not state_ids:
        raise SelfCheckFailed("snapshot is empty (no transcripts)")
    doc_ids = ids(sql.SQL("SELECT doc_id FROM {t}.documents").format(t=t))
    reader_ids = ids(sql.SQL("SELECT doc_id FROM {t}.transcripts").format(t=t))
    if not (state_ids == doc_ids == reader_ids):
        raise SelfCheckFailed(
            "doc_id sets differ: "
            f"index_state={len(state_ids)} documents={len(doc_ids)} "
            f"transcripts={len(reader_ids)}; e.g. "
            f"{sorted(state_ids ^ doc_ids ^ reader_ids)[:5]}"
        )
    chunk_docs = ids(
        sql.SQL("SELECT DISTINCT doc_id FROM {t}.document_chunks").format(t=t)
    )
    if missing := state_ids - chunk_docs:
        raise SelfCheckFailed(
            f"{len(missing)} transcripts have no chunks, e.g. {sorted(missing)[:5]}"
        )
    if orphans := chunk_docs - state_ids:
        raise SelfCheckFailed(f"chunks of unknown transcripts: {sorted(orphans)[:5]}")

    cursor.execute(
        sql.SQL(
            "SELECT count(*), count(*) FILTER (WHERE embedding IS NULL "
            "OR vector_dims(embedding) <> %s OR l2_norm(embedding) = 0) "
            "FROM {t}.document_chunks"
        ).format(t=t),
        (dim,),
    )
    total_chunks, bad_vectors = cursor.fetchone()
    if bad_vectors:
        raise SelfCheckFailed(
            f"{bad_vectors} chunks lack a non-zero {dim}-dimensional vector"
        )

    # Queryable: a sampled top-k served by the HNSW index returns k rows.
    k = min(QUERY_CHECK_K, total_chunks)
    cursor.execute(
        sql.SQL(
            "SELECT embedding::text FROM {t}.document_chunks ORDER BY random() LIMIT 1"
        ).format(t=t)
    )
    probe = cursor.fetchone()[0]
    cursor.execute("SET LOCAL enable_seqscan = off")
    cursor.execute(
        sql.SQL(
            "SELECT count(*) FROM (SELECT chunk_id FROM {t}.document_chunks "
            "ORDER BY embedding <=> %s::halfvec LIMIT %s) nearest"
        ).format(t=t),
        (probe, k),
    )
    got = cursor.fetchone()[0]
    cursor.execute("SET LOCAL enable_seqscan = on")
    if got != k:
        raise SelfCheckFailed(f"sampled top-{k} query returned {got} rows")

    checks: dict[str, Any] = {"chunks": total_chunks, "transcripts": len(state_ids)}
    if check_model:
        cursor.execute(
            sql.SQL(
                "SELECT chunk_id, text, embedding::text FROM {t}.document_chunks "
                "ORDER BY random() LIMIT %s"
            ).format(t=t),
            (MODEL_CHECK_SAMPLES,),
        )
        samples = cursor.fetchall()
        fresh = _embed([row[1] for row in samples])
        cosines = [
            _cosine(_parse_vector(row[2]), vector)
            for row, vector in zip(samples, fresh, strict=True)
        ]
        checks["model_check_min_cosine"] = round(min(cosines), 5)
        if min(cosines) < MODEL_CHECK_MIN_COSINE:
            worst = samples[cosines.index(min(cosines))][0]
            raise SelfCheckFailed(
                f"re-embedded sample disagrees with stored vector "
                f"(cosine {min(cosines):.4f} < {MODEL_CHECK_MIN_COSINE}, {worst}); "
                "the embedding service or configured model does not match"
            )
    return checks


def _recheck_base(cursor: Cursor, base: SnapshotInfo) -> None:
    cursor.execute(
        "SELECT status FROM rag_meta.snapshots WHERE snapshot_id = %s FOR UPDATE",
        (base.snapshot_id,),
    )
    row = cursor.fetchone()
    if row is None or row[0] != catalog.STATUS_PUBLISHED:
        raise RuntimeError(
            f"Base snapshot {base.snapshot_id} was retired or removed during the "
            "build; abandoning it so retired data is not republished"
        )
    cursor.execute(
        """
        SELECT snapshot_id FROM rag_meta.snapshots
        WHERE status = 'published' AND pipeline_fingerprint = %s
          AND embedding_space = %s AND published_at > %s
        LIMIT 1
        """,
        (base.pipeline_fingerprint, base.embedding_space, base.published_at),
    )
    newer = cursor.fetchone()
    if newer:
        raise RuntimeError(
            f"Base {base.snapshot_id} is no longer the newest of its lineage "
            f"({newer[0]} was published meanwhile)"
        )


def _unused_snapshot_id(cursor: Cursor, source_commit: str | None) -> str:
    """A fresh id; two builds within one second step to the next second."""
    now = datetime.now(UTC)
    while True:
        snapshot_id = new_snapshot_id(source_commit, now)
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s) "
            "OR EXISTS (SELECT 1 FROM rag_meta.snapshots WHERE snapshot_id = %s)",
            (schema_for(snapshot_id), snapshot_id),
        )
        if not cursor.fetchone()[0]:
            return snapshot_id
        now += timedelta(seconds=1)


def publish(
    conn: Connection, plan: BuildPlan, prepared: Prepared
) -> tuple[str, dict[str, Any]]:
    """Phase B. Returns (snapshot_id, stats); raises on any failure."""
    timings: dict[str, float] = {}
    started = time.perf_counter()

    def step(name: str, since: float) -> float:
        _check_cancel()
        now = time.perf_counter()
        timings[name] = round(now - since, 2)
        return now

    with transaction(conn) as cursor:
        if not try_advisory_xact_lock(cursor):
            raise BuildBusy("another writer holds the snapshot advisory lock")
        snapshot_id = _unused_snapshot_id(cursor, plan.source_commit)
        target = schema_for(snapshot_id)
        logger.info(
            "Phase B: building %s (%s)",
            target,
            f"incremental from {plan.base.snapshot_id}" if plan.base else "full",
        )
        cursor.execute(f"SET LOCAL maintenance_work_mem = '{MAINTENANCE_WORK_MEM}'")
        cursor.execute(
            f"SET LOCAL max_parallel_maintenance_workers = {MAX_PARALLEL_MAINTENANCE_WORKERS}"
        )
        cursor.execute(sql.SQL("CREATE SCHEMA {s}").format(s=sql.Identifier(target)))
        cursor.execute(create_tables_sql(target, settings.embedding_dim))
        t = started

        if plan.base:
            _copy_base(cursor, plan.base.schema_name, target)
            gone = sorted(plan.reindex | plan.changes.deleted)
            ti = sql.Identifier(target)
            cursor.execute(
                sql.SQL(
                    "DELETE FROM {t}.document_chunks WHERE doc_id = ANY(%s)"
                ).format(t=ti),
                (gone,),
            )
            cursor.execute(
                sql.SQL("DELETE FROM {t}.index_state WHERE file_path = ANY(%s)").format(
                    t=ti
                ),
                (gone,),
            )
            t = step("copy_base", t)

        _insert_chunks(cursor, target, prepared)
        _write_index_state(cursor, target, plan)
        _write_documents(cursor, target, plan)
        _write_transcripts(cursor, target, plan, prepared)
        _write_audit(cursor, plan)
        t = step("write", t)

        for table in SNAPSHOT_TABLES:
            cursor.execute(
                sql.SQL("ANALYZE {s}.{t}").format(
                    s=sql.Identifier(target), t=sql.Identifier(table)
                )
            )
        t = step("analyze", t)
        cursor.execute(create_hnsw_sql(target))
        t = step("hnsw", t)

        checks = self_check(
            cursor, target, check_model=plan.full or prepared.embedded > 0
        )
        t = step("self_check", t)

        if plan.base:
            _recheck_base(cursor, plan.base)

        stats = {
            **checks,
            "build": "full" if plan.full else "incremental",
            "reindexed_transcripts": len(plan.reindex),
            "rendered_transcripts": len(prepared.projections),
            "new_chunks": len(prepared.chunks),
            "embedded_new": prepared.embedded,
            "embedding_reused": prepared.reused,
            "reuse_source": (
                prepared.reuse_source.snapshot_id if prepared.reuse_source else None
            ),
            "changes": {
                "added": len(plan.changes.added),
                "body_modified": len(plan.changes.body_modified),
                "source_only": len(plan.changes.source_only),
                "deleted": len(plan.changes.deleted),
            },
            "timings_s": timings,
        }
        cursor.execute(
            """
            INSERT INTO rag_meta.snapshots (
                snapshot_id, schema_name, format_version, pipeline_fingerprint,
                embedding_space, embedding_model, embedding_dim,
                normalization_version, chunker_version, source_commit,
                base_snapshot_id, builder_version, status, published_at,
                index_params, stats
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      'published', clock_timestamp(), %s, %s)
            """,
            (
                snapshot_id,
                target,
                FORMAT_VERSION,
                pipeline_fingerprint(),
                embedding_space(),
                settings.embedding.model,
                settings.embedding_dim,
                document_loader.BODY_NORMALIZATION_VERSION,
                chunker.CHUNKER_VERSION,
                plan.source_commit,
                plan.base.snapshot_id if plan.base else None,
                catalog.builder_version(),
                Json(index_params()),
                Json(stats),
            ),
        )
        catalog.grant_read(cursor, target)
    timings["total"] = round(time.perf_counter() - started, 2)
    return snapshot_id, stats


__all__ = [
    "BuildBusy",
    "BuildCancelled",
    "BuildPlan",
    "SelfCheckFailed",
    "disk_precheck",
    "plan_build",
    "prepare",
    "publish",
    "self_check",
]
