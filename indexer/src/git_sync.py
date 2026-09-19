"""Git repository synchronization."""

import logging
import subprocess
from pathlib import Path

from .paths import BEDTIMENEWS_TRANSCRIPTS_DIR

logger = logging.getLogger(__name__)

BEDTIMENEWS_TRANSCRIPTS_REPO_URL = (
    "https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts.git"
)


def sync_repository() -> None:
    """Get a local copy of the latest git repo content of BedtimeNews-Transcripts.

    Uses fetch + hard reset rather than pull so a rewritten remote history
    (force-push / squashed initial commit) still converges without failing
    on non-fast-forward.
    """
    if not (BEDTIMENEWS_TRANSCRIPTS_DIR / ".git").exists():
        logger.info(f"Cloning repository to {BEDTIMENEWS_TRANSCRIPTS_DIR}")
        success, output = _run_command(
            [
                "git",
                "clone",
                BEDTIMENEWS_TRANSCRIPTS_REPO_URL,
                str(BEDTIMENEWS_TRANSCRIPTS_DIR),
            ],
            BEDTIMENEWS_TRANSCRIPTS_DIR.parent,
        )
        if not success:
            logger.error(f"Failed to clone: {output}")
            raise RuntimeError(f"Failed to clone repository: {output}")
        return

    git = [
        "git",
        "-c",
        f"safe.directory={BEDTIMENEWS_TRANSCRIPTS_DIR}",
    ]
    success, output = _run_command(
        [*git, "fetch", "--prune", "origin", "main"],
        BEDTIMENEWS_TRANSCRIPTS_DIR,
    )
    if not success:
        logger.error(f"Failed to fetch: {output}")
        raise RuntimeError(f"Failed to fetch changes: {output}")

    success, output = _run_command(
        [*git, "reset", "--hard", "origin/main"],
        BEDTIMENEWS_TRANSCRIPTS_DIR,
    )
    if not success:
        logger.error(f"Failed to reset to origin/main: {output}")
        raise RuntimeError(f"Failed to reset repository: {output}")

    # Drop leftover untracked files so the working tree matches remote exactly.
    success, output = _run_command(
        [*git, "clean", "-fd"],
        BEDTIMENEWS_TRANSCRIPTS_DIR,
    )
    if not success:
        logger.error(f"Failed to clean working tree: {output}")
        raise RuntimeError(f"Failed to clean repository: {output}")


def _run_command(cmd: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """Run a shell command and return success status and combined output."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=False
        )
        # Include stderr: git writes its error messages there, and losing them
        # makes clone/pull failures undiagnosable from the logs.
        output = "\n".join(
            part for part in (result.stdout.strip(), result.stderr.strip()) if part
        )
        return result.returncode == 0, output
    except Exception as e:
        return False, str(e)
