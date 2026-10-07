"""Snapshot ops commands.

Usage (inside the indexer container):
    docker compose exec indexer python -m src.snapshots list
    docker compose exec indexer python -m src.snapshots status
    docker compose exec indexer python -m src.snapshots retire <id>
    docker compose exec indexer python -m src.snapshots unretire <id>
    docker compose exec indexer python -m src.snapshots pin <id>
    docker compose exec indexer python -m src.snapshots unpin <id>
    docker compose exec indexer python -m src.snapshots build [--full]

``retire`` is the data rollback: agents stop selecting the snapshot within
their 15 s poll and fall back to the previous readable one. ``unretire``
undoes it within the 24 h retention. Pinned snapshots are never collected.
``build`` takes the same run lock and advisory lock as the scheduled run.
"""

import argparse
import json
import sys

from . import catalog
from .db import connection, transaction


def _fmt_size(size: int | None) -> str:
    if size is None:
        return "-"
    return f"{size / 1024**2:.0f} MB"


def cmd_list() -> int:
    with connection() as conn, transaction(conn) as cursor:
        cursor.execute("SELECT to_regclass('rag_meta.snapshots') IS NOT NULL")
        if not cursor.fetchone()[0]:
            print("No snapshot registry yet (the indexer has not started).")
            return 0
        snapshots = catalog.list_snapshots(cursor)
        sizes = catalog.snapshot_sizes(cursor)
    lineage = catalog.current_lineage()
    if not snapshots:
        print("No snapshots.")
        return 0
    header = f"{'snapshot':<28} {'status':<9} {'pin':<3} {'lineage':<8} {'published_at':<25} {'size':>8}  space"
    print(header)
    print("-" * len(header))
    for s in snapshots:
        print(
            f"{s.snapshot_id:<28} {s.status:<9} {'yes' if s.pinned else '':<3} "
            f"{'current' if s.lineage == lineage else 'other':<8} "
            f"{s.published_at.isoformat(timespec='seconds'):<25} "
            f"{_fmt_size(sizes.get(s.schema_name)):>8}  {s.embedding_space}"
        )
    return 0


def cmd_status() -> int:
    with connection() as conn, transaction(conn) as cursor:
        status = catalog.get_status(cursor)
    print(json.dumps(status, default=str, indent=2, ensure_ascii=False))
    return 0


def _set(status: str, snapshot_id: str) -> int:
    with connection() as conn:
        snap = catalog.set_status(conn, snapshot_id, status)
    print(f"{snap.snapshot_id}: {snap.status}")
    if status == catalog.STATUS_RETIRED:
        print("Agents fall back to the previous readable snapshot within 15 s.")
    return 0


def _pin(snapshot_id: str, pinned: bool) -> int:
    with connection() as conn:
        snap = catalog.set_pinned(conn, snapshot_id, pinned)
    print(f"{snap.snapshot_id}: pinned={snap.pinned}")
    return 0


def cmd_build(full: bool) -> int:
    from .pipeline import run_once
    from .scheduler import install_signal_handlers

    install_signal_handlers()
    result = run_once(full=full)
    print(
        f"result: {result.result}"
        + (f" ({result.snapshot_id})" if result.snapshot_id else "")
    )
    if result.error:
        print(f"error: {result.error}")
    return 0 if result.result in ("published", "no_change") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.snapshots")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List snapshots")
    sub.add_parser("status", help="Show indexer_status")
    for name, help_text in (
        ("retire", "Retire a snapshot (data rollback)"),
        ("unretire", "Undo a retirement"),
        ("pin", "Never garbage-collect this snapshot"),
        ("unpin", "Allow garbage collection again"),
    ):
        sub.add_parser(name, help=help_text).add_argument("snapshot_id")
    build = sub.add_parser("build", help="Run a build now")
    build.add_argument("--full", action="store_true", help="Force a full build")
    args = parser.parse_args(argv)

    try:
        match args.command:
            case "list":
                return cmd_list()
            case "status":
                return cmd_status()
            case "retire":
                return _set(catalog.STATUS_RETIRED, args.snapshot_id)
            case "unretire":
                return _set(catalog.STATUS_PUBLISHED, args.snapshot_id)
            case "pin":
                return _pin(args.snapshot_id, True)
            case "unpin":
                return _pin(args.snapshot_id, False)
            case "build":
                return cmd_build(args.full)
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
