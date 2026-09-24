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

from .settings import settings

logger = logging.getLogger(__name__)

LOG_FILE = Path("/var/log/indexer/cron.log")
POLL_INTERVAL_SECONDS = 10

# Global flag set by Unix signal handlers for graceful shutdown.
shutdown_requested = False


def run_scheduler(run_pipeline: Callable[[], None]) -> None:
    """Run the pipeline according to the configured cron expression."""
    global shutdown_requested
    shutdown_requested = False
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    _configure_file_logging()

    cron_schedule = settings.indexer_cron_schedule
    logger.info("=" * 70)
    logger.info(" INDEXER SERVICE STARTING")
    logger.info("=" * 70)
    logger.info("Schedule: %s", cron_schedule)

    try:
        schedule = croniter(cron_schedule, datetime.now().astimezone())
    except KeyError, TypeError, ValueError:
        logger.exception("Invalid cron schedule: %s", cron_schedule)
        sys.exit(1)

    while not shutdown_requested:
        next_run = schedule.get_next(datetime)
        logger.info("Next indexing run: %s", next_run.isoformat())
        if not _wait_until(next_run):
            break
        try:
            run_pipeline()
        except Exception:
            logger.exception("Scheduled pipeline execution failed")

    logger.info("Shutting down...")


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
    """Request shutdown after the active pipeline or scheduler wait finishes."""
    global shutdown_requested
    logger.info("Received signal %s", signum)
    shutdown_requested = True
