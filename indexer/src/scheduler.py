"""In-process cron-expression scheduler for periodic pipeline execution."""

import logging
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import FrameType

from croniter import croniter

from .db import request_shutdown
from .settings import settings

logger = logging.getLogger(__name__)

LOG_FILE = Path("/var/log/indexer/cron.log")
POLL_INTERVAL_SECONDS = 10

# Global flag set by Unix signal handlers for graceful shutdown.
shutdown_requested = False


def run_scheduler(run_pipeline: Callable[[], None]) -> None:
    """Run the pipeline according to the configured cron expression."""
    install_signal_handlers()
    _configure_file_logging()

    cron_schedule = settings.indexer_cron_schedule
    logger.info("=" * 70)
    logger.info(" INDEXER SERVICE STARTING")
    logger.info("=" * 70)
    logger.info("Schedule: %s", cron_schedule)

    try:
        croniter(cron_schedule, datetime.now().astimezone())
    except KeyError, TypeError, ValueError:
        logger.exception("Invalid cron schedule: %s", cron_schedule)
        sys.exit(1)

    while not shutdown_requested:
        # Compute the next slot from *now*: a run that overran the interval
        # skips the missed slots instead of firing them back to back.
        next_run = croniter(cron_schedule, datetime.now().astimezone()).get_next(
            datetime
        )
        logger.info("Next indexing run: %s", next_run.isoformat())
        if not _wait_until(next_run):
            break
        try:
            run_pipeline()
        except Exception:
            logger.exception("Scheduled pipeline execution failed")

    logger.info("Shutting down...")


def install_signal_handlers() -> None:
    """Route SIGTERM/SIGINT to a graceful stop that also aborts a build."""
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)


def _configure_file_logging() -> None:
    """Mirror scheduled-run logs to the persistent log volume."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    resolved_log_file = LOG_FILE.resolve()
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if (
            isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename) == resolved_log_file
        ):
            return

    handler = logging.FileHandler(resolved_log_file)
    handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root_logger.addHandler(handler)


def _wait_until(next_run: datetime) -> bool:
    """Wait interruptibly until ``next_run``; return false on shutdown."""
    while not shutdown_requested:
        remaining = (next_run - datetime.now(next_run.tzinfo)).total_seconds()
        if remaining <= 0:
            return True
        time.sleep(min(remaining, POLL_INTERVAL_SECONDS))
    return False


def _signal_handler(signum: int, _frame: FrameType | None) -> None:
    """Stop scheduling and abort a running build.

    The running statement is cancelled, so Postgres rolls the build
    transaction back and nothing half-built remains (compose gives the
    indexer a 30 s stop grace period).
    """
    global shutdown_requested
    logger.info("Received signal %s", signum)
    shutdown_requested = True
    request_shutdown()
