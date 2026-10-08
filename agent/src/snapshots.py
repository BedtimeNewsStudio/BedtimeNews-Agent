"""Which RAG snapshot this agent reads (design 7.1-7.4).

The indexer publishes immutable snapshots (``rag_s<id>`` schemas) registered
in ``rag_meta.snapshots``. The agent keeps the snapshot it serves in process
memory; request handling only reads that value (``require()``) and never
queries ``rag_meta``. A background thread re-applies the selection rule every
``POLL_INTERVAL_S`` seconds and swaps the value atomically, so new requests
see a newly published (or rolled-back) snapshot without a restart, while a
request in flight keeps the snapshot it started with.

Selection rule: among published snapshots whose ``format_version`` is in
``SUPPORTED_FORMATS`` and whose ``embedding_space`` equals this agent's,
take the highest format, then the most recently published. ``RAG_SNAPSHOT``
pins one snapshot id instead. Before the indexer has created ``rag_meta``
(an older indexer), the original ``rag`` schema is read as the implicit
format-1 snapshot ``legacy``, if its embedding dimension matches.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .settings import settings

logger = logging.getLogger(__name__)

# Snapshot formats this agent can read. Format 1 is the original `rag` layout.
SUPPORTED_FORMATS: frozenset[int] = frozenset({1})
POLL_INTERVAL_S = 15.0
LEGACY_SNAPSHOT_ID = "legacy"
LEGACY_SCHEMA = "rag"


class SnapshotUnavailable(RuntimeError):
    """No readable snapshot is selected; the agent is not ready."""


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    schema_name: str
    format_version: int
    embedding_space: str
    published_at: datetime | None = None
    source_commit: str | None = None

    def describe(self) -> dict[str, Any]:
        age = (
            (datetime.now(UTC) - self.published_at).total_seconds()
            if self.published_at
            else None
        )
        return {
            "id": self.snapshot_id,
            "schema": self.schema_name,
            "format_version": self.format_version,
            "embedding_space": self.embedding_space,
            "published_at": self.published_at,
            "data_age_seconds": round(age) if age is not None else None,
            "source_commit": self.source_commit,
        }


def agent_embedding_space() -> str:
    """This agent's vector space: ``embedding.space_id`` or ``model@dim``."""
    if settings.embedding.space_id:
        return settings.embedding.space_id
    return f"{settings.embedding.model}@{settings.embedding_dim}"


def select_snapshot(cursor) -> tuple[Snapshot | None, str | None, dict | None]:
    """Apply the selection rule. Returns (snapshot, not-ready reason, indexer status)."""
    space = agent_embedding_space()
    cursor.execute("SELECT to_regclass('rag_meta.snapshots') IS NOT NULL AS ok")
    if not cursor.fetchone()["ok"]:
        return (*_implicit_legacy(cursor, space), None)

    indexer_status = None
    try:
        cursor.execute(
            "SELECT last_run_at, last_result, last_error, last_published_at, "
            "consecutive_failures FROM rag_meta.indexer_status"
        )
        row = cursor.fetchone()
        indexer_status = dict(row) if row else None
    except Exception:  # noqa: BLE001 - status is informational only
        cursor.connection.rollback()

    columns = (
        "snapshot_id, schema_name, format_version, embedding_space, "
        "published_at, source_commit, status"
    )
    if settings.rag_snapshot:
        cursor.execute(
            f"SELECT {columns} FROM rag_meta.snapshots WHERE snapshot_id = %s",
            (settings.rag_snapshot,),
        )
        row = cursor.fetchone()
        if row is None:
            return (
                None,
                f"pinned snapshot {settings.rag_snapshot} does not exist",
                indexer_status,
            )
        if row["status"] != "published":
            return (
                None,
                f"pinned snapshot {settings.rag_snapshot} is {row['status']}",
                indexer_status,
            )
        if row["format_version"] not in SUPPORTED_FORMATS:
            return (
                None,
                (
                    f"pinned snapshot {settings.rag_snapshot} has unsupported format "
                    f"{row['format_version']}"
                ),
                indexer_status,
            )
        if row["embedding_space"] != space:
            return (
                None,
                (
                    f"pinned snapshot {settings.rag_snapshot} is in vector space "
                    f"{row['embedding_space']}, this agent embeds in {space}"
                ),
                indexer_status,
            )
        return _from_row(row), None, indexer_status

    cursor.execute(
        f"""
        SELECT {columns} FROM rag_meta.snapshots
        WHERE status = 'published'
          AND format_version = ANY(%s)
          AND embedding_space = %s
        ORDER BY format_version DESC, published_at DESC, snapshot_id DESC
        LIMIT 1
        """,
        (sorted(SUPPORTED_FORMATS), space),
    )
    row = cursor.fetchone()
    if row is None:
        return (
            None,
            (
                f"no published snapshot in vector space {space} with format "
                f"{sorted(SUPPORTED_FORMATS)}"
            ),
            indexer_status,
        )
    return _from_row(row), None, indexer_status


def _from_row(row) -> Snapshot:
    return Snapshot(
        snapshot_id=row["snapshot_id"],
        schema_name=row["schema_name"],
        format_version=row["format_version"],
        embedding_space=row["embedding_space"],
        published_at=row["published_at"],
        source_commit=row["source_commit"],
    )


def _implicit_legacy(cursor, space: str) -> tuple[Snapshot | None, str | None]:
    """The pre-adoption `rag` schema, read as format-1 snapshot `legacy`."""
    if settings.rag_snapshot and settings.rag_snapshot != LEGACY_SNAPSHOT_ID:
        return None, f"pinned snapshot {settings.rag_snapshot} does not exist"
    cursor.execute(
        """
        SELECT a.atttypmod AS dim FROM pg_attribute a
        WHERE a.attrelid = to_regclass('rag.document_chunks')
          AND a.attname = 'embedding' AND NOT a.attisdropped
        """
    )
    row = cursor.fetchone()
    if row is None:
        return None, "no snapshot registry and no legacy rag schema"
    if row["dim"] != settings.embedding_dim:
        return None, (
            f"legacy rag schema stores {row['dim']}-dimensional vectors, "
            f"this agent embeds {settings.embedding_dim}"
        )
    cursor.execute("SELECT EXISTS (SELECT 1 FROM rag.document_chunks) AS ok")
    if not cursor.fetchone()["ok"]:
        return None, "legacy rag schema is empty"
    return Snapshot(LEGACY_SNAPSHOT_ID, LEGACY_SCHEMA, 1, space), None


class SnapshotManager:
    """Holds the current snapshot and refreshes it from a background thread."""

    def __init__(self) -> None:
        self._snapshot: Snapshot | None = None
        self._reason: str | None = "not yet polled"
        self._db_ok = False
        self._indexer_status: dict | None = None
        self._checked_at: datetime | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- request path: memory only ------------------------------------------
    def require(self) -> Snapshot:
        snapshot = self._snapshot  # single reference read: atomic
        if snapshot is None:
            raise SnapshotUnavailable(self._reason or "no readable snapshot")
        return snapshot

    def health(self) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            snapshot, reason, db_ok = self._snapshot, self._reason, self._db_ok
            body: dict[str, Any] = {
                "snapshot": snapshot.describe() if snapshot else None,
                "embedding_space": agent_embedding_space(),
                "supported_formats": sorted(SUPPORTED_FORMATS),
                "pinned_by_config": settings.rag_snapshot or None,
                "checked_at": self._checked_at,
                "indexer_status": self._indexer_status,
            }
        ready = db_ok and snapshot is not None and reason is None
        body["ready"] = ready
        if not ready:
            body["reason"] = reason or "database unreachable"
        return ready, body

    # -- polling ---------------------------------------------------------------
    def refresh(self) -> None:
        """Re-apply the selection rule once (called by the poller)."""
        from .vector_db import read_cursor

        try:
            with read_cursor() as cursor:
                snapshot, reason, indexer_status = select_snapshot(cursor)
        except Exception as exc:  # noqa: BLE001 - stay up, report not ready
            logger.warning("Snapshot poll failed: %s", exc)
            with self._lock:
                self._db_ok = False
                self._reason = f"database unreachable: {type(exc).__name__}"
                self._checked_at = datetime.now(UTC)
            return

        with self._lock:
            previous = self._snapshot
            # A pinned snapshot that disappears or is retired makes the agent
            # not ready; otherwise "no snapshot" also clears the current one.
            self._snapshot = snapshot
            self._reason = reason
            self._db_ok = True
            self._indexer_status = indexer_status
            self._checked_at = datetime.now(UTC)
        if previous != snapshot:
            if snapshot:
                logger.info(
                    "Serving snapshot %s (schema %s)",
                    snapshot.snapshot_id,
                    snapshot.schema_name,
                )
            else:
                logger.warning("No readable snapshot: %s", reason)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self.refresh()
        self._thread = threading.Thread(
            target=self._run, name="snapshot-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(POLL_INTERVAL_S):
            self.refresh()

    # -- tests -------------------------------------------------------------------
    def set_for_tests(
        self, snapshot: Snapshot | None, reason: str | None = None
    ) -> None:
        with self._lock:
            self._snapshot = snapshot
            self._reason = reason if snapshot is None else None
            self._db_ok = True


snapshot_manager = SnapshotManager()
