"""Change detection for content files."""

import hashlib

from .paths import CONTENTS_DIR
from .vector_db import get_indexed_files, get_indexing_history


def detect_changes(current_files: set[str]) -> tuple[set[str], set[str], set[str]]:
    """Detect added, modified, and deleted files."""
    indexed_files = set(get_indexed_files())

    added = set()
    modified = set()

    for md_file in current_files:
        if md_file not in indexed_files:
            added.add(md_file)
        else:
            history = get_indexing_history(md_file)
            if not history:
                added.add(md_file)
            else:
                current_hash = calculate_file_hash(md_file)
                if history["content_hash"] != current_hash:
                    modified.add(md_file)

            indexed_files.discard(md_file)

    deleted = indexed_files

    return added, modified, deleted


def calculate_file_hash(md_file: str) -> str:
    """Calculate SHA256 hash of file content."""
    with open(CONTENTS_DIR / md_file, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def get_doc_id(md_file: str) -> str:
    """Document ID for a scanned file.

    The scanner already yields URIs — paths relative to CONTENTS_DIR with the
    .md suffix — and the URI *is* the doc_id, kept byte-for-byte identical to
    the keys of the upstream URI映射.md so the two can be joined directly.
    """
    return md_file
