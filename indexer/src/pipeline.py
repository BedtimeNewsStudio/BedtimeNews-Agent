"""One indexer run: build and publish a RAG snapshot.

    file lock -> bootstrap/adopt -> git sync and pin commit -> plan (diff
    against the base snapshot) -> GC + disk precheck -> Phase A -> Phase B
    -> GC -> indexer_status

Every step that can fail leaves published snapshots untouched; the next run
retries against the same base. ``python -m src.pipeline`` runs one
incremental build (same as ``python -m src.snapshots build``).
"""

import logging
from dataclasses import dataclass

from . import catalog
from .builder import (
    BuildBusy,
    BuildCancelled,
    disk_precheck,
    plan_build,
    prepare,
    publish,
)
from .db import SHUTDOWN, close, connect
from .git_sync import head_commit, sync_repository
from .run_lock import RunLockBusy, run_lock
from .settings import settings

logging.basicConfig(
    level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    result: str  # published / no_change / skipped_busy / failed
    snapshot_id: str | None = None
    error: str | None = None


def _validate_scope_safety() -> None:
    """Fail closed before a partial scan can touch a production database."""
    if settings.indexer_scope not in {"full", "sample"}:
        raise ValueError("INDEXER_SCOPE must be 'full' or 'sample'")
    if settings.indexer_scope == "sample" and not settings.postgres_db.endswith(
        "_local"
    ):
        raise RuntimeError(
            "Sample indexing is local-only: POSTGRES_DB must end with '_local'"
        )


def _record(result: RunResult, *, published: bool = False) -> None:
    try:
        conn = connect()
    except Exception:
        logger.exception("Could not record indexer status")
        return
    try:
        catalog.record_run(conn, result.result, result.error, published=published)
    except Exception:
        logger.exception("Could not record indexer status")
    finally:
        close(conn)


def run_once(*, full: bool = False, sync: bool = True) -> RunResult:
    """Run one build cycle; never raises for build failures (see RunResult)."""
    _validate_scope_safety()
    try:
        with run_lock():
            result = _locked_run(full=full, sync=sync)
    except RunLockBusy as exc:
        logger.info("Skipped: %s", exc)
        result = RunResult("skipped_busy", error=str(exc))
        _record(result)
        return result
    return result


def _locked_run(*, full: bool, sync: bool) -> RunResult:
    logger.info("=" * 70)
    logger.info(" SNAPSHOT BUILD (%s)", "full requested" if full else "incremental")
    logger.info("=" * 70)
    conn = None
    published = False
    try:
        conn = connect()
        catalog.bootstrap(conn)
        if sync:
            sync_repository()
        commit = head_commit()
        logger.info("Transcripts commit: %s", commit or "unknown")

        plan = plan_build(conn, full=full, source_commit=commit)
        c = plan.changes
        logger.info(
            "Plan: %s build, base=%s; +%d body~%d source~%d legacy~%d -%d, "
            "render=%d removed=%d titles_changed=%s",
            "full" if plan.full else "incremental",
            plan.base.snapshot_id if plan.base else None,
            len(c.added),
            len(c.body_modified),
            len(c.source_only),
            len(c.legacy_requires_reindex),
            len(c.deleted),
            len(plan.render),
            len(plan.transcripts_removed),
            plan.titles_changed,
        )
        if not plan.has_changes:
            logger.info("No changes against %s", plan.base.snapshot_id)
            result = RunResult("no_change")
        else:
            catalog.collect_garbage(conn)
            disk_precheck(conn, plan)
            prepared = prepare(conn, plan)
            snapshot_id, stats = publish(conn, plan, prepared)
            published = True
            logger.info("Published snapshot %s: %s", snapshot_id, stats)
            result = RunResult("published", snapshot_id)
        try:
            catalog.collect_garbage(conn)
        except Exception:
            # The run's outcome is already decided; the next run retries GC.
            logger.exception("GC after the run failed")
    except BuildBusy as exc:
        logger.info("Skipped: %s", exc)
        result = RunResult("skipped_busy", error=str(exc))
    except BuildCancelled as exc:
        logger.warning("Build cancelled: %s", exc)
        result = RunResult("failed", error=f"cancelled: {exc}")
    except Exception as exc:
        if SHUTDOWN.is_set():
            logger.warning("Build interrupted by shutdown: %s", exc)
        else:
            logger.exception("Build failed")
        result = RunResult("failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        if conn is not None:
            close(conn)
    _record(result, published=published)
    logger.info("Run result: %s", result.result)
    return result


def main() -> None:
    """Scheduler/entrypoint hook: one incremental run."""
    result = run_once()
    if result.result == "failed":
        raise RuntimeError(result.error)


if __name__ == "__main__":
    main()
