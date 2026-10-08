"""Container entry point for the indexer service.

The image runs ``python -m src.entrypoint --run-immediately``: one build at
startup, then a build at every ``indexer_cron_schedule`` slot. Without the flag
the first build waits for the first slot.

For a one-off build in the running container use the ops command instead (a
second entrypoint would start a second scheduler):
    docker compose exec indexer python -m src.snapshots build
"""

import argparse
import logging

from .pipeline import main as run_pipeline
from .scheduler import install_signal_handlers, run_scheduler

logging.basicConfig(
    level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)

parser = argparse.ArgumentParser(
    description="Indexer service entrypoint",
    formatter_class=argparse.RawDescriptionHelpFormatter,
)
parser.add_argument(
    "--run-immediately",
    action="store_true",
    help="Run a build immediately on startup before scheduling (default: False)",
)


def main(run_immediately: bool):
    """Main entrypoint for the indexer service.

    Args:
        run_immediately: If True, run a build immediately before setting up
                         scheduled execution. If False, wait for the next run.
    """
    # Before the first build, so a SIGTERM during it aborts it cleanly.
    install_signal_handlers()
    if run_immediately:
        logger.info("Running build immediately...")
        try:
            run_pipeline()
        except Exception:
            logger.exception("Build failed")
            # Continue anyway to set up scheduled runs

    # Keep this process alive and run builds on the configured schedule.
    run_scheduler(run_pipeline)


if __name__ == "__main__":
    args = parser.parse_args()
    main(run_immediately=args.run_immediately)
