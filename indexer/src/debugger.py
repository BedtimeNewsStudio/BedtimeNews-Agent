"""Read-only debugging commands for the RAG snapshots.

stats / history / inspect read the current snapshot: the newest published
snapshot of the indexer's current lineage, falling back to the newest
published snapshot of any lineage (history reads its index_state). recent
reads rag_state.file_actions. Nothing here writes; snapshot operations (list,
retire, pin, build, ...) live in ``python -m src.snapshots``.

Usage (inside Docker container):
    docker compose exec indexer python -m src.debugger test
    docker compose exec indexer python -m src.debugger stats
    docker compose exec indexer python -m src.debugger history
    docker compose exec indexer python -m src.debugger history ShuiQianXiaoXi/0901-1000/0960.md
    docker compose exec indexer python -m src.debugger recent --limit 20
    docker compose exec indexer python -m src.debugger inspect ShuiQianXiaoXi/0901-1000/0960.md
    docker compose exec indexer python -m src.debugger logs
    docker compose exec indexer python -m src.debugger logs --lines 100
    docker compose exec indexer python -m src.debugger logs --all
"""

import argparse
import logging
import sys
from typing import Any

from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from . import catalog
from .db import connection
from .scheduler import LOG_FILE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _current_schema(cursor) -> str:
    with cursor.connection.cursor() as plain:
        plain.execute("SELECT to_regclass('rag_meta.snapshots') IS NOT NULL")
        if not plain.fetchone()[0]:
            raise RuntimeError("No snapshot registry yet (the indexer has not run)")
        snapshots = catalog.list_snapshots(plain)
    snap = catalog.latest_published(
        snapshots, lineage=catalog.current_lineage()
    ) or catalog.latest_published(snapshots)
    if snap is None:
        raise RuntimeError("No published snapshot")
    return snap.schema_name


def _query(query: str, params: tuple = (), *, snapshot: bool = True) -> list[dict]:
    """Run a read-only query; ``{s}`` names the current snapshot's schema."""
    with connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            statement = sql.SQL(query)
            if snapshot:
                schema = sql.Identifier(_current_schema(cursor))
                statement = statement.format(s=schema)
            cursor.execute(statement, params)
            rows = [dict(row) for row in cursor.fetchall()]
        conn.rollback()
    return rows


def check_connection() -> bool:
    """Print the server and pgvector versions; True when pgvector is installed."""
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version()")
        print(cursor.fetchone()[0])
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cursor.fetchone()
        print(f"pgvector: {row[0] if row else 'NOT INSTALLED'}")
        return row is not None


def get_table_stats() -> dict[str, Any]:
    return _query(
        """
        SELECT COUNT(*) AS total_chunks,
               COUNT(DISTINCT doc_id) AS total_documents,
               (SELECT COUNT(*) FROM {s}.transcripts) AS reader_documents
        FROM {s}.document_chunks
        """
    )[0]


def get_index_state(file_path: str) -> dict[str, Any] | None:
    rows = _query(
        """
        SELECT file_path, source_hash, body_hash, body_normalization_version,
               indexed_at, source_observed_at
        FROM {s}.index_state WHERE file_path = %s
        """,
        (file_path,),
    )
    return rows[0] if rows else None


def get_indexed_files() -> list[str]:
    return [row["file_path"] for row in _query("SELECT file_path FROM {s}.index_state")]


def get_file_chunks(doc_id: str) -> list[dict[str, Any]]:
    return _query(
        """
        SELECT chunk_id, chunk_index, heading, word_count,
               embedding IS NOT NULL AS has_embedding
        FROM {s}.document_chunks WHERE doc_id = %s ORDER BY chunk_index
        """,
        (doc_id,),
    )


def get_recent_file_actions(limit: int = 10) -> list[dict[str, Any]]:
    return _query(
        """
        SELECT file_path, action_type, source_hash, body_hash, processed_at
        FROM rag_state.file_actions
        ORDER BY processed_at DESC NULLS LAST, id DESC
        LIMIT %s
        """,
        (limit,),
        snapshot=False,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _log_index_state(state: dict[str, Any]) -> None:
    logger.info(f"  Source hash:   {state['source_hash']}")
    logger.info(f"  Body hash:     {state['body_hash']}")
    logger.info(f"  Body version:  {state['body_normalization_version']}")
    logger.info(f"  Indexed at:    {state['indexed_at']}")
    logger.info(f"  Source seen:   {state['source_observed_at']}")


def _cmd_test() -> bool:
    """Check the database connection and the pgvector extension."""
    if check_connection():
        logger.info("Database ready")
        return True
    logger.error("Database test failed")
    return False


def _cmd_stats():
    """Display the current snapshot's statistics."""
    logger.info("=" * 60)
    logger.info("Current snapshot statistics")
    logger.info("=" * 60)

    stats = get_table_stats()
    logger.info(f"Total documents:        {stats['total_documents']}")
    logger.info(f"Total chunks:           {stats['total_chunks']}")
    logger.info(f"Reader documents:       {stats['reader_documents']}")
    logger.info("=" * 60)


def _cmd_history(file_path: str | None = None):
    """Display the current snapshot's index state for one file, or list all."""
    if file_path:
        logger.info(f"Index state for: {file_path}")
        state = get_index_state(file_path)
        if state:
            _log_index_state(state)
        else:
            logger.info("  Not in the current snapshot")
    else:
        logger.info("All indexed files:")
        indexed_files = get_indexed_files()
        logger.info(f"Total: {len(indexed_files)} files")
        for i, file in enumerate(sorted(indexed_files)[:20], 1):
            logger.info(f"  {i}. {file}")
        if len(indexed_files) > 20:
            logger.info(f"  ... and {len(indexed_files) - 20} more")


def _cmd_recent(limit: int = 10):
    """Display recently processed files."""
    logger.info(f"Recent file actions (last {limit}):")
    rows = get_recent_file_actions(limit)
    if rows:
        for row in rows:
            logger.info(
                f"  [{row['action_type']:8}] {row['file_path']} ({row['processed_at']})"
            )
    else:
        logger.info("  No recent actions")


def _cmd_inspect(file_path: str):
    """Inspect a specific file's chunks."""
    logger.info(f"Inspecting: {file_path}")

    state = get_index_state(file_path)
    if state:
        _log_index_state(state)

    chunks = get_file_chunks(file_path)
    if chunks:
        logger.info(f"  Chunks: {len(chunks)}")
        for chunk in chunks[:10]:
            emb = "EMB" if chunk["has_embedding"] else "---"
            heading = chunk["heading"] or "(no heading)"
            logger.info(
                f"    [{chunk['chunk_index']:3}] {emb} {heading[:50]} "
                f"({chunk['word_count']} words)"
            )
        if len(chunks) > 10:
            logger.info(f"    ... and {len(chunks) - 10} more chunks")
    else:
        logger.info("  No chunks found")


def _cmd_logs(lines: int | None = None, show_all: bool = False):
    """Display scheduled pipeline run logs."""
    if not LOG_FILE.exists():
        logger.info(
            "No scheduler logs yet. The file is created when the indexer starts."
        )
        logger.info("Schedule: see indexer_cron_schedule in config.yml")
        return

    logger.info(f"Scheduler logs from: {LOG_FILE}")
    logger.info("=" * 60)

    try:
        with open(LOG_FILE) as f:
            all_lines = f.readlines()

        if not all_lines:
            logger.info("Log file is empty")
            return

        if show_all:
            logger.info(f"Showing all {len(all_lines)} lines:")
            for line in all_lines:
                logger.info(line.rstrip())
        else:
            display_lines = lines if lines else 50
            start_idx = max(0, len(all_lines) - display_lines)
            actual_lines = len(all_lines) - start_idx

            logger.info(f"Showing last {actual_lines} lines (total: {len(all_lines)}):")
            for line in all_lines[start_idx:]:
                logger.info(line.rstrip())

    except Exception:
        logger.exception("Failed to read log file")


def main():
    parser = argparse.ArgumentParser(
        description="Read-only debugging commands for the RAG snapshots",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    subparsers.add_parser("test", help="Check the database connection and pgvector")
    subparsers.add_parser("stats", help="Display the current snapshot's statistics")

    history_parser = subparsers.add_parser(
        "history", help="Display the current snapshot's index state"
    )
    history_parser.add_argument("file", nargs="?", help="Specific file to inspect")

    recent_parser = subparsers.add_parser(
        "recent", help="Display recently processed files"
    )
    recent_parser.add_argument(
        "--limit", type=int, default=10, help="Number of recent files to show"
    )

    inspect_parser = subparsers.add_parser("inspect", help="Inspect a specific file")
    inspect_parser.add_argument("file", help="File path to inspect")

    logs_parser = subparsers.add_parser(
        "logs", help="Display scheduled pipeline run logs"
    )
    logs_parser.add_argument(
        "--lines", type=int, help="Number of lines to show (default: 50)"
    )
    logs_parser.add_argument("--all", action="store_true", help="Show all log lines")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        if args.command == "test":
            sys.exit(0 if _cmd_test() else 1)
        elif args.command == "stats":
            _cmd_stats()
        elif args.command == "history":
            _cmd_history(args.file)
        elif args.command == "recent":
            _cmd_recent(args.limit)
        elif args.command == "inspect":
            _cmd_inspect(args.file)
        elif args.command == "logs":
            _cmd_logs(lines=args.lines, show_all=args.all)

    except Exception:
        logger.exception("Command failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
