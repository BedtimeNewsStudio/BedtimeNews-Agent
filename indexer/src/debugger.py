"""Database Utilities for RAG Vector Database

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
    docker compose exec indexer python -m src.debugger clear
"""

import argparse
import logging
import shutil
import sys

from .paths import BEDTIMENEWS_TRANSCRIPTS_DIR
from .scheduler import LOG_FILE
from .vector_db import (
    clear_all_chunks,
    clear_document_titles,
    clear_file_actions,
    clear_indexing_history,
    clear_transcript_projections,
    get_file_chunks,
    get_indexed_files,
    get_indexing_history,
    get_recent_file_actions,
    get_table_stats,
    test_connection,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _cmd_test():
    """Test database connection."""
    if test_connection():
        logger.info("Database ready")
        return True
    else:
        logger.error("Database test failed")
        return False


def _cmd_stats():
    """Display database statistics."""
    logger.info("=" * 60)
    logger.info("Database Statistics")
    logger.info("=" * 60)

    stats = get_table_stats()
    logger.info(f"Total documents:        {stats['total_documents']}")
    logger.info(f"Total chunks:           {stats['total_chunks']}")
    logger.info(f"Reader documents:       {stats['reader_documents']}")
    logger.info("=" * 60)


def _cmd_history(file_path: str | None = None):
    """Display indexing history."""
    if file_path:
        logger.info(f"Indexing history for: {file_path}")
        history = get_indexing_history(file_path)
        if history:
            logger.info(f"  Source hash:   {history['source_hash']}")
            logger.info(f"  Body hash:     {history['body_hash']}")
            logger.info(f"  Body version:  {history['body_normalization_version']}")
            logger.info(f"  Indexed at:    {history['indexed_at']}")
            logger.info(f"  Source seen:   {history['source_observed_at']}")
        else:
            logger.info("  No history found")
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

    history = get_indexing_history(file_path)
    if history:
        logger.info(f"  Source hash:   {history['source_hash']}")
        logger.info(f"  Body hash:     {history['body_hash']}")
        logger.info(f"  Body version:  {history['body_normalization_version']}")
        logger.info(f"  Indexed at:    {history['indexed_at']}")
        logger.info(f"  Source seen:   {history['source_observed_at']}")

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

    log_file = LOG_FILE
    if not log_file.exists():
        logger.info(
            "No scheduler logs yet. The file is created when the indexer starts."
        )
        logger.info("Schedule: see indexer_cron_schedule in config.yml")
        return

    logger.info(f"Scheduler logs from: {log_file}")
    logger.info("=" * 60)

    try:
        with open(log_file) as f:
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


def _cmd_clear(force: bool = False):
    """Clear all data from database and delete cloned git repository."""
    if not force:
        logger.warning("=" * 60)
        logger.warning("WARNING: This will delete ALL data from the database:")
        logger.warning("         - All chunks")
        logger.warning("         - All indexing history")
        logger.warning("         - All document titles")
        logger.warning("         - All reader projections")
        logger.warning("         - All file action logs")
        logger.warning("         and remove the cloned git repository!")
        logger.warning("=" * 60)
        confirm = input("Type 'DELETE ALL' to confirm: ")
        if confirm != "DELETE ALL":
            logger.info("Cancelled")
            return

    clear_all_chunks()
    clear_document_titles()
    clear_transcript_projections()
    clear_indexing_history()
    clear_file_actions()

    if BEDTIMENEWS_TRANSCRIPTS_DIR.exists():
        shutil.rmtree(BEDTIMENEWS_TRANSCRIPTS_DIR)
        logger.info(f"Deleted repository: {BEDTIMENEWS_TRANSCRIPTS_DIR}")
    else:
        logger.info(f"Repository not found: {BEDTIMENEWS_TRANSCRIPTS_DIR}")


def main():
    """Main entry point for database utilities."""
    parser = argparse.ArgumentParser(
        description="RAG Vector Database Utilities",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    subparsers.add_parser("test", help="Test database connection")
    subparsers.add_parser("stats", help="Display database statistics")

    history_parser = subparsers.add_parser("history", help="Display indexing history")
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

    clear_parser = subparsers.add_parser(
        "clear",
        help="Wipe all RAG tables and the cloned transcript repo (DANGEROUS)",
    )
    clear_parser.add_argument(
        "--force", action="store_true", help="Skip confirmation prompt"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        if args.command == "test":
            success = _cmd_test()
            sys.exit(0 if success else 1)
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
        elif args.command == "clear":
            _cmd_clear(args.force)

    except Exception:
        logger.exception("Command failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
