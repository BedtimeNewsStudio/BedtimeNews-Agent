"""PostgreSQL connections for the indexer (the snapshot builder).

The indexer is the sole writer and connects as the database superuser. Every
connection it opens is tracked so a SIGTERM can cancel the statement in flight
(``cancel_active``): Postgres then rolls the build transaction back and nothing
half-built remains.
"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as Connection
from psycopg2.extensions import cursor as Cursor

from .settings import settings

logger = logging.getLogger(__name__)

# Fixed key of the database advisory lock held by every write transaction
# (build publish, GC, adoption). Guards against two indexers with different
# data directories (and therefore different file locks) on one database.
ADVISORY_LOCK_KEY = 0x5241_4753_4E41_50  # "RAGSNAP"

# Set by the SIGTERM/SIGINT handler. Long-running build steps poll it between
# units of work; statements already running are cancelled via cancel_active.
SHUTDOWN = threading.Event()

_active: set[Connection] = set()
_active_lock = threading.Lock()

# Run queries through a Python-level select() loop instead of blocking inside
# libpq. Python only runs signal handlers between bytecodes, so without this a
# SIGTERM would wait for the running statement (an HNSW build: tens of
# seconds) before the handler could cancel it.
psycopg2.extensions.set_wait_callback(psycopg2.extras.wait_select)


def connect() -> Connection:
    """Open a dedicated connection (not pooled: a build is one long transaction)."""
    conn = psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
        application_name="bedtimenews-indexer",
        # If the indexer dies (SIGKILL, OOM), make the server notice the dead
        # client during a long statement and roll back promptly instead of
        # finishing the statement first.
        options="-c client_connection_check_interval=5000",
    )
    with _active_lock:
        _active.add(conn)
    return conn


def close(conn: Connection) -> None:
    with _active_lock:
        _active.discard(conn)
    try:
        conn.close()
    except Exception:
        logger.exception("Error closing connection")


@contextmanager
def connection() -> Iterator[Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        close(conn)


@contextmanager
def transaction(conn: Connection) -> Iterator[Cursor]:
    """One transaction: commit on success, roll back on any exception."""
    try:
        with conn.cursor() as cursor:
            yield cursor
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback failed")
        raise


def try_advisory_xact_lock(cursor: Cursor) -> bool:
    cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
    return bool(cursor.fetchone()[0])


def cancel_active() -> None:
    """Cancel the statement running on every open indexer connection."""
    with _active_lock:
        connections = list(_active)
    for conn in connections:
        try:
            conn.cancel()
        except Exception:
            logger.exception("Error cancelling statement")


def request_shutdown() -> None:
    """Ask a running build to stop: flag it and cancel in-flight statements."""
    SHUTDOWN.set()
    cancel_active()
