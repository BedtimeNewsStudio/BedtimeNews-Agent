"""File scanning and configuration-based filtering."""

import logging
import os
from pathlib import Path

import yaml

from .paths import CONTENTS_DIR, INDEX_CONFIG_FILE

logger = logging.getLogger(__name__)


def scan_files() -> set[str]:
    """Scan the transcript tree and filter files based on config.

    Returns the URIs of the files to index: each path relative to CONTENTS_DIR,
    including the .md suffix, which is exactly the identifier the upstream
    URI映射.md is keyed by.
    """
    md_files = set()
    for md_file in CONTENTS_DIR.rglob("*.md"):
        uri = md_file.relative_to(CONTENTS_DIR).as_posix()
        md_files.add(uri)

    config = _load_config()

    filtered_files = set()
    for md_file in md_files:
        if _should_include_file(md_file, config):
            filtered_files.add(md_file)

    logger.info(f"Scanned: {len(md_files)} files, filtered: {len(filtered_files)}")
    return filtered_files


def _load_config() -> dict:
    """Load index configuration from YAML file."""
    try:
        with open(INDEX_CONFIG_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError as e:
        raise RuntimeError(f"Config file not found: {INDEX_CONFIG_FILE}") from e
    except yaml.YAMLError as e:
        raise RuntimeError(f"Error parsing config file: {INDEX_CONFIG_FILE}") from e


def _should_include_file(md_file: str, config: dict) -> bool:
    """Check if a file should be included based on config rules.

    Args:
        md_file: Document URI (path relative to CONTENTS_DIR, with .md)
        config: Config rules for indexing

    Returns:
        True if file should be included, False otherwise
    """
    md_file_path = Path(md_file)

    # Check include patterns
    include_patterns = config.get("include", [])
    if include_patterns and not any(
        md_file_path.match(pattern) for pattern in include_patterns
    ):
        return False

    # Check exclude patterns
    exclude_patterns = config.get("exclude", [])
    if exclude_patterns and any(
        md_file_path.match(pattern) for pattern in exclude_patterns
    ):
        return False

    # Validate file size
    min_size = config.get("validation", {}).get("min_file_size", 0)
    max_size = config.get("validation", {}).get("max_file_size", 10485760)

    file_size = os.path.getsize(CONTENTS_DIR / md_file)
    return min_size <= file_size <= max_size
